"""Tests for the SF Bay 511 transit source."""

import asyncio
from datetime import UTC, datetime, timedelta

import niquests_mock as nm
import pytest
from pydantic import ValidationError

from kindle_dash_gen.sources.builtins.sf_bay_511.model import (
    AcTransitDirection,
    Agency,
    BartDirection,
    CaltrainDirection,
    MuniDirection,
    SfBay511Data,
    StopBoard,
    TransitArrival,
    direction_enum,
)
from kindle_dash_gen.sources.builtins.sf_bay_511.source import (
    STOP_MONITORING_API,
    Board,
    SfBay511Client,
    SfBay511Config,
    SfBay511Error,
    SfBay511Source,
    StopRequest,
    _to_utc,
)
from kindle_dash_gen.sources.toolkit import Secret

KEY = "test-key"
NOW = datetime(2026, 7, 17, 19, 0, tzinfo=UTC)


def _visit(
    line: str = "Green-N",
    direction: str = "N",
    stop: str = "901162",
    expected: str | None = "2026-07-17T19:09:19Z",
    aimed: str | None = "2026-07-17T19:08:00Z",
    destination: str | None = "OAK Airport / Berryessa",
) -> dict:
    """One MonitoredStopVisit, shaped like a real 511 response."""
    call = {
        "StopPointRef": stop,
        "StopPointName": "Embarcadero",
        "DestinationDisplay": destination,
        "AimedArrivalTime": aimed,
        "ExpectedArrivalTime": expected,
        "AimedDepartureTime": None,
        "ExpectedDepartureTime": None,
    }
    return {
        "RecordedAtTime": "1970-01-01T00:00:00Z",
        "MonitoringRef": stop,
        "MonitoredVehicleJourney": {
            "LineRef": line,
            "DirectionRef": direction,
            "PublishedLineName": "Daly City to Berryessa",
            "OperatorRef": "BA",
            "DestinationName": "Berryessa / North San Jose",
            "MonitoredCall": call,
        },
    }


def _payload(*visits: dict) -> dict:
    return {
        "ServiceDelivery": {
            "ResponseTimestamp": "2026-07-17T19:00:00Z",
            "StopMonitoringDelivery": {"MonitoredStopVisit": list(visits)},
        }
    }


def _client(boards: dict[str, Board] | None = None) -> SfBay511Client:
    if boards is None:
        boards = {"Embarcadero": Board(stops=[StopRequest(agency="BA", stopcode="901162")])}
    return SfBay511Client(api_key=KEY, boards=boards, timeout=30)


def _route(router, payload: dict | None = None, **by_stopcode: dict) -> None:
    """Route each stopcode to its payload (511 matches on the `stopcode` query param)."""
    if payload is not None:
        by_stopcode = {"901162": payload}
    for stopcode, body in by_stopcode.items():
        router.get(STOP_MONITORING_API, params={"stopcode": stopcode}).respond(json=body)


def _fetch(client: SfBay511Client):
    return asyncio.run(client.fetch(NOW))


# ── Parsing ────────────────────────────────────────────────────────────────────────────────────


def test_fetch_parses_core_fields() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit()))
        boards = _fetch(_client())

    assert len(boards) == 1
    board = boards[0]
    assert board.name == "Embarcadero"
    arrival = board.arrivals[Agency.BART][BartDirection.NORTH][0]
    assert arrival.agency is Agency.BART
    assert arrival.line == "Green-N"
    assert arrival.direction is BartDirection.NORTH
    assert arrival.destination == "OAK Airport / Berryessa"
    assert arrival.arrival == datetime(2026, 7, 17, 19, 9, 19, tzinfo=UTC)
    assert arrival.scheduled == datetime(2026, 7, 17, 19, 8, tzinfo=UTC)


def test_arrivals_are_aware_utc() -> None:
    # The source contract: every datetime handed to the pipeline is aware UTC.
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit()))
        boards = _fetch(_client())
    arrival = boards[0].arrivals[Agency.BART][BartDirection.NORTH][0]
    assert arrival.arrival.utcoffset() == timedelta(0)
    assert arrival.scheduled.utcoffset() == timedelta(0)


def test_to_utc_normalizes_a_z_suffix() -> None:
    assert _to_utc("2026-07-17T19:09:19Z") == datetime(2026, 7, 17, 19, 9, 19, tzinfo=UTC)


def test_bom_prefixed_response_parses() -> None:
    # 511 serves its JSON with a UTF-8 BOM, which a naive decode chokes on.
    with nm.mock(assert_all_called=False) as router:
        import json as _json

        body = ("﻿" + _json.dumps(_payload(_visit()))).encode("utf-8")
        router.get(STOP_MONITORING_API, params={"stopcode": "901162"}).respond(content=body)
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


def test_arrivals_nest_by_agency_then_direction_and_sort() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(
            router,
            _payload(
                _visit(line="Green-N", direction="N", expected="2026-07-17T19:20:00Z"),
                _visit(line="Blue-N", direction="N", expected="2026-07-17T19:05:00Z"),
                _visit(line="Green-S", direction="S", expected="2026-07-17T19:10:00Z"),
            ),
        )
        boards = _fetch(_client())

    by_direction = boards[0].arrivals[Agency.BART]
    assert set(by_direction) == {BartDirection.NORTH, BartDirection.SOUTH}
    assert [a.line for a in by_direction[BartDirection.NORTH]] == ["Blue-N", "Green-N"]  # sorted
    assert [a.line for a in by_direction[BartDirection.SOUTH]] == ["Green-S"]


def test_destination_falls_back_to_destination_name() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(destination=None)))
        boards = _fetch(_client())
    arrival = boards[0].arrivals[Agency.BART][BartDirection.NORTH][0]
    assert arrival.destination == "Berryessa / North San Jose"


def test_expected_time_preferred_over_aimed() -> None:
    # Aimed is the schedule; expected is the prediction. A rider wants the prediction.
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(expected=None, aimed="2026-07-17T19:30:00Z")))
        boards = _fetch(_client())
    arrival = boards[0].arrivals[Agency.BART][BartDirection.NORTH][0]
    assert arrival.arrival == datetime(2026, 7, 17, 19, 30, tzinfo=UTC)


def test_visit_without_any_time_is_skipped() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(expected=None, aimed=None), _visit()))
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


def test_unassigned_visits_are_skipped() -> None:
    """A scheduled trip with no vehicle assigned yet reports null line *and* direction.

    Over half the visits at a live BART station look like this. They carry no route badge and no
    direction to file under, so they are skipped — not treated as a malformed feed.
    """
    unassigned = _visit()
    unassigned["MonitoredVehicleJourney"].update(
        {"LineRef": None, "DirectionRef": None, "PublishedLineName": None, "DestinationName": None}
    )
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(unassigned, _visit()))
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


def test_half_null_line_direction_still_raises() -> None:
    # Only a *fully* unassigned visit is benign; one of the two missing is a shape change.
    half = _visit()
    half["MonitoredVehicleJourney"]["DirectionRef"] = None
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(half))
        with pytest.raises(SfBay511Error):
            _fetch(_client())


def test_past_arrivals_are_excluded() -> None:
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(expected="2026-07-17T18:00:00Z"), _visit()))
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


def test_empty_response_yields_an_empty_board() -> None:
    # A stop with no service right now is not an error.
    with nm.mock(assert_all_called=False) as router:
        _route(router, {"ServiceDelivery": {"StopMonitoringDelivery": {}}})
        boards = _fetch(_client())
    assert boards[0].arrivals == {}


def test_delivery_as_a_list_is_accepted() -> None:
    # SIRI-JSON permits the delivery to be an array; 511 sends an object.
    payload = {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": [_visit()]}]}}
    with nm.mock(assert_all_called=False) as router:
        _route(router, payload)
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


# ── Per-agency directions ──────────────────────────────────────────────────────────────────────


def test_muni_directions_use_the_muni_enum() -> None:
    boards = {"Ferry Plaza": Board(stops=[StopRequest(agency="SF", stopcode="15551")])}
    with nm.mock(assert_all_called=False) as router:
        _route(router, **{"15551": _payload(_visit(line="22", direction="IB", stop="15551"))})
        result = _fetch(_client(boards))
    arrival = result[0].arrivals[Agency.MUNI][MuniDirection.INBOUND][0]
    assert isinstance(arrival.direction, MuniDirection)
    assert arrival.direction is MuniDirection.INBOUND


def test_ac_transit_supports_east_west() -> None:
    boards = {"Downtown": Board(stops=[StopRequest(agency="AC", stopcode="55555")])}
    with nm.mock(assert_all_called=False) as router:
        _route(router, **{"55555": _payload(_visit(line="12", direction="W", stop="55555"))})
        result = _fetch(_client(boards))
    assert result[0].arrivals[Agency.AC_TRANSIT][AcTransitDirection.WEST][0].line == "12"


def test_direction_outside_the_agencys_vocabulary_raises() -> None:
    # BART has no east/west; a value outside its enum means the feed or the config is wrong.
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(direction="E")))
        with pytest.raises(SfBay511Error):
            _fetch(_client())


@pytest.mark.parametrize(
    ("agency", "expected"),
    [
        (Agency.BART, BartDirection),
        (Agency.MUNI, MuniDirection),
        (Agency.AC_TRANSIT, AcTransitDirection),
    ],
)
def test_agency_direction_mapping(agency, expected) -> None:
    # Exhaustiveness is enforced statically (both mappings `match` on the enum, so a new Agency
    # without a label or direction vocabulary is a mypy "Missing return statement"). This only
    # pins that each agency maps to the *right* one.
    assert direction_enum(agency) is expected


def test_bart_and_caltrain_directions_stay_distinct_types() -> None:
    # Both are north/south, so they look like duplicates worth collapsing into an alias. They are
    # not: _check_direction is isinstance-based, so sharing one enum would let a Caltrain direction
    # pass as BART's (and vice versa) despite the StrEnum values comparing equal anyway.
    assert direction_enum(Agency.BART) is not direction_enum(Agency.CALTRAIN)
    assert BartDirection.NORTH == CaltrainDirection.NORTH  # equal by value...
    assert not isinstance(CaltrainDirection.NORTH, BartDirection)  # ...but not interchangeable


def test_every_agency_has_a_readable_label() -> None:
    # Title-casing the member name would give "Bart" and "Ac Transit".
    assert {a.label for a in Agency} == {"BART", "Muni", "Caltrain", "AC Transit"}


# ── Model validators ───────────────────────────────────────────────────────────────────────────


def _arrival(**kw) -> TransitArrival:
    defaults = dict(
        agency=Agency.BART,
        line="Green-N",
        direction=BartDirection.NORTH,
        destination="Berryessa",
        arrival=NOW,
    )
    return TransitArrival(**{**defaults, **kw})


def test_arrival_rejects_a_direction_from_another_agency() -> None:
    # MuniDirection.NORTH and BartDirection.NORTH are both "N", so equality alone cannot catch
    # this; the validator checks the enum *type* against the arrival's agency.
    with pytest.raises(ValueError):
        _arrival(direction=MuniDirection.INBOUND)
    with pytest.raises(ValueError):
        _arrival(agency=Agency.MUNI, direction=BartDirection.NORTH)


def test_board_rejects_a_direction_key_from_another_agency() -> None:
    with pytest.raises(ValueError):
        StopBoard(
            name="Embarcadero",
            arrivals={Agency.BART: {MuniDirection.INBOUND: []}},
        )


def test_board_rejects_an_arrival_filed_under_the_wrong_agency() -> None:
    with pytest.raises(ValueError):
        StopBoard(
            name="Embarcadero",
            arrivals={Agency.MUNI: {MuniDirection.INBOUND: [_arrival()]}},
        )


def test_board_label_prefers_display_name() -> None:
    assert StopBoard(name="Embarcadero", arrivals={}).label == "Embarcadero"
    assert StopBoard(name="Embarcadero", arrivals={}, display_name="Embc").label == "Embc"


# ── Requests: dedup, merging, filtering, failure ───────────────────────────────────────────────


def test_one_request_per_distinct_stopcode() -> None:
    # Two boards watching the same stop must not spend the rate budget twice (60 req/hour).
    boards = {
        "A": Board(stops=[StopRequest(agency="BA", stopcode="901162")]),
        "B": Board(stops=[StopRequest(agency="BA", stopcode="901162")]),
    }
    with nm.mock(assert_all_called=False) as router:
        route = router.get(STOP_MONITORING_API, params={"stopcode": "901162"})
        route.respond(json=_payload(_visit()))
        result = _fetch(_client(boards))
    assert len(route.calls) == 1
    assert len(result) == 2  # both boards still built


def test_a_board_merges_several_stops_and_agencies() -> None:
    boards = {
        "Embarcadero": Board(
            stops=[
                StopRequest(agency="BA", stopcode="901162"),
                StopRequest(agency="SF", stopcode="15551"),
            ]
        )
    }
    with nm.mock(assert_all_called=False) as router:
        _route(
            router,
            **{
                "901162": _payload(_visit()),
                "15551": _payload(_visit(line="22", direction="IB", stop="15551")),
            },
        )
        result = _fetch(_client(boards))
    assert set(result[0].arrivals) == {Agency.BART, Agency.MUNI}


def test_lines_filter_drops_other_lines() -> None:
    boards = {
        "Embarcadero": Board(stops=[StopRequest(agency="BA", stopcode="901162", lines=["Blue-N"])])
    }
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(line="Green-N"), _visit(line="Blue-N")))
        result = _fetch(_client(boards))
    assert [a.line for a in result[0].arrivals[Agency.BART][BartDirection.NORTH]] == ["Blue-N"]


def test_any_request_failure_fails_the_whole_source() -> None:
    # All-or-nothing: a partial board reads as "no more trains" when really we just don't know.
    boards = {
        "A": Board(stops=[StopRequest(agency="BA", stopcode="901162")]),
        "B": Board(stops=[StopRequest(agency="BA", stopcode="901163")]),
    }
    with nm.mock(assert_all_called=False) as router:
        router.get(STOP_MONITORING_API, params={"stopcode": "901162"}).respond(
            json=_payload(_visit())
        )
        router.get(STOP_MONITORING_API, params={"stopcode": "901163"}).respond(status_code=500)
        with pytest.raises(SfBay511Error):
            _fetch(_client(boards))


def test_null_visit_list_is_treated_as_no_service() -> None:
    # A null where the visit array goes means "nothing calling here right now", not a broken feed.
    with nm.mock(assert_all_called=False) as router:
        empty = {"ServiceDelivery": {"StopMonitoringDelivery": {"MonitoredStopVisit": None}}}
        _route(router, empty)
        boards = _fetch(_client())
    assert boards[0].arrivals == {}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"ServiceDelivery": None}, id="null-service-delivery"),
        pytest.param({"nothing": "useful"}, id="missing-service-delivery"),
        pytest.param(
            {"ServiceDelivery": {"StopMonitoringDelivery": {"MonitoredStopVisit": [{}]}}},
            id="empty-visit",
        ),
        pytest.param(
            {
                "ServiceDelivery": {
                    "StopMonitoringDelivery": {
                        "MonitoredStopVisit": [{"MonitoredVehicleJourney": None}]
                    }
                }
            },
            id="null-journey",
        ),
    ],
)
def test_malformed_payload_raises_source_error(payload) -> None:
    # Must surface as SfBay511Error so the pipeline isolates it, not a raw TypeError/KeyError that
    # would escape isolation and sink the whole render.
    with nm.mock(assert_all_called=False) as router:
        _route(router, payload)
        with pytest.raises(SfBay511Error):
            _fetch(_client())


def test_non_json_body_raises_source_error() -> None:
    with nm.mock(assert_all_called=False) as router:
        router.get(STOP_MONITORING_API, params={"stopcode": "901162"}).respond(
            content=b"<html>rate limited</html>"
        )
        with pytest.raises(SfBay511Error):
            _fetch(_client())


def test_api_key_is_sent_on_the_request() -> None:
    with nm.mock(assert_all_called=False) as router:
        route = router.get(STOP_MONITORING_API, params={"stopcode": "901162"})
        route.respond(json=_payload(_visit()))
        _fetch(_client())
    assert route.calls[-1].request.url.count(f"api_key={KEY}") == 1


# ── Config ─────────────────────────────────────────────────────────────────────────────────────


def test_config_resolves_a_secret_api_key(monkeypatch) -> None:
    monkeypatch.setenv("KDG_TEST_511", "from-env")
    cfg = SfBay511Config(
        api_key=Secret(value_from_env="KDG_TEST_511"),
        boards={"A": Board(stops=[StopRequest(agency="BA", stopcode="901162")])},
    )
    assert cfg.api_key.value == "from-env"


def test_config_rejects_an_unknown_agency() -> None:
    with pytest.raises(ValidationError):
        StopRequest(agency="ZZ", stopcode="1")


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        StopRequest(agency="BA", stopcode="1", bogus=True)


def test_null_line_with_a_direction_raises() -> None:
    # Mirror of test_half_null_line_direction_still_raises. A frozen dataclass does no runtime type
    # checking, so without an explicit guard this would sail through as line=None and reach a
    # layout as a missing route badge.
    half = _visit()
    half["MonitoredVehicleJourney"]["LineRef"] = None
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(half))
        with pytest.raises(SfBay511Error):
            _fetch(_client())


def test_single_visit_sent_as_a_lone_object() -> None:
    """SIRI-JSON lets a single-element array collapse to a lone object.

    Iterating a dict yields its string keys, so without normalizing this the source would drop
    entirely at exactly the moment a board has one train left — late evening.
    """
    payload = {"ServiceDelivery": {"StopMonitoringDelivery": {"MonitoredStopVisit": _visit()}}}
    with nm.mock(assert_all_called=False) as router:
        _route(router, payload)
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


def test_single_delivery_sent_as_a_lone_object() -> None:
    payload = {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": [_visit()]}]}}
    with nm.mock(assert_all_called=False) as router:
        _route(router, payload)
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


def test_visit_without_a_monitored_call_is_skipped() -> None:
    # No MonitoredCall means no times, so nothing to show — but not a broken feed.
    bare = _visit()
    del bare["MonitoredVehicleJourney"]["MonitoredCall"]
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(bare, _visit()))
        boards = _fetch(_client())
    assert len(boards[0].arrivals[Agency.BART][BartDirection.NORTH]) == 1


@pytest.mark.parametrize("raw", ["n", " N ", "N"])
def test_direction_casing_and_padding_are_tolerated(raw) -> None:
    # Under all-or-nothing a cosmetic feed change would otherwise blank the whole board.
    with nm.mock(assert_all_called=False) as router:
        _route(router, _payload(_visit(direction=raw)))
        boards = _fetch(_client())
    assert boards[0].arrivals[Agency.BART][BartDirection.NORTH][0].direction is BartDirection.NORTH


def test_board_rejects_an_arrival_under_the_wrong_direction_key() -> None:
    # The remaining way to build an inconsistent board: right agency, wrong direction bucket.
    with pytest.raises(ValueError):
        StopBoard(
            name="Embarcadero",
            arrivals={Agency.BART: {BartDirection.SOUTH: [_arrival()]}},
        )


# ── The Source class and plugin wiring ─────────────────────────────────────────────────────────


def test_source_is_registered_under_its_config_name() -> None:
    from kindle_dash_gen import plugins
    from kindle_dash_gen.sources.registry import build_sources

    plugins.load_plugins()
    cls, config = build_sources(
        {
            "sf-bay-511": {
                "api_key": {"value": "k"},
                "boards": {"A": {"stops": [{"agency": "BA", "stopcode": "901162"}]}},
            }
        }
    )["sf-bay-511"]
    assert cls is SfBay511Source
    assert isinstance(config, SfBay511Config)


def test_source_resolves_its_secret_and_wraps_boards(monkeypatch) -> None:
    monkeypatch.setenv("KDG_TEST_511", KEY)
    config = SfBay511Config(
        api_key=Secret(value_from_env="KDG_TEST_511"),
        boards={"Embarcadero": Board(stops=[StopRequest(agency="BA", stopcode="901162")])},
    )
    with nm.mock(assert_all_called=False) as router:
        route = router.get(STOP_MONITORING_API, params={"stopcode": "901162"})
        route.respond(json=_payload(_visit()))
        data = asyncio.run(SfBay511Source(config).fetch(NOW))
    assert isinstance(data, SfBay511Data)
    assert data.boards[0].label == "Embarcadero"
    assert route.calls[-1].request.url.count(f"api_key={KEY}") == 1


def test_unreadable_secret_is_a_source_error_not_a_crash(monkeypatch) -> None:
    """An unreadable key must be isolatable by the pipeline, like any other source outage.

    Resolving it in __init__ instead would raise while gather()'s arguments are still being built,
    outside the isolation return_exceptions provides, taking down the whole run.
    """
    monkeypatch.delenv("KDG_TEST_511_MISSING", raising=False)
    config = SfBay511Config(
        api_key=Secret(value_from_env="KDG_TEST_511_MISSING"),
        boards={"A": Board(stops=[StopRequest(agency="BA", stopcode="901162")])},
    )
    source = SfBay511Source(config)  # construction alone must not read the key
    with pytest.raises(SfBay511Error):
        asyncio.run(source.fetch(NOW))
