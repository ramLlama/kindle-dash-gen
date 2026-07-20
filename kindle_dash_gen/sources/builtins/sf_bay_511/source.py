"""The ``sf-bay-511`` source client and config: Bay Area transit arrivals from 511.org.

511 is a regional aggregator: one keyed API covers every Bay Area operator, selected per request by
an ``agency`` code. This uses its SIRI ``StopMonitoring`` endpoint, which returns JSON with the
stop name, line, direction, headsign, and both predicted and scheduled times already resolved — so
unlike a GTFS-realtime feed it needs no static-schedule join to be useful. The produced data type
lives in :mod:`.model`.

Two things about this API shape the client:

* ``stopcode`` is always sent. Omitting it returns the operator's *entire* network (Muni alone is
  ~35k arrivals, AC Transit ~12MB), which is neither needed nor polite.
* The default rate limit is 60 requests/hour/key. At the default 5-minute interval that is about
  five distinct stops per run, so identical ``(agency, stopcode)`` pairs are fetched once and
  shared between boards. 511 grants higher limits on request.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime

import niquests
import typer
from pydantic import BaseModel, ConfigDict

from kindle_dash_gen.sources.registry import Source
from kindle_dash_gen.sources.toolkit import Secret, SourceError, source_config

from .model import Agency, Direction, SfBay511Data, StopBoard, TransitArrival, direction_enum

# HTTPS, not the http:// URLs 511's own docs print: the API key travels as a query parameter,
# so an unencrypted request would put the credential on the wire every polling interval.
STOP_MONITORING_API = "https://api.511.org/transit/StopMonitoring"
_STOPS_API = "https://api.511.org/transit/stops"

# Arrival times in preference order: what the vehicle is *predicted* to do beats the timetable,
# and an arrival beats a departure. A visit with none of these is not renderable and is skipped.
_TIME_FIELDS = (
    "ExpectedArrivalTime",
    "ExpectedDepartureTime",
    "AimedArrivalTime",
    "AimedDepartureTime",
)


class SfBay511Error(SourceError):
    """Raised when 511 transit data cannot be fetched or parsed."""


class StopRequest(BaseModel):
    """One stop to poll: an operator plus that operator's stopcode."""

    model_config = ConfigDict(extra="forbid")

    agency: Agency
    stopcode: str
    # Optional allowlist of LineRefs. A busy stop can serve many lines; a dashboard usually wants a
    # few. Unset means every line calling at the stop.
    lines: list[str] | None = None


class Board(BaseModel):
    """A display board: one or more stops merged under a single name.

    Several stops merge when one place is served by more than one stopcode, or by more than one
    operator (a BART station above a Muni stop). Boards carry every upcoming arrival; how many to
    show is a render-time decision for the layout, not a fetch-time cap.
    """

    model_config = ConfigDict(extra="forbid")

    stops: list[StopRequest]
    # Label a layout shows instead of the board's name (the config key). The key stays the
    # canonical name plugins match on, so renaming the display never breaks that match.
    display_name: str | None = None


class SfBay511Config(BaseModel):
    """Config for the ``[sources.sf-bay-511]`` table."""

    model_config = ConfigDict(extra="forbid")

    api_key: Secret  # 511 requires a key; see docs/sources.md for the three ways to supply it
    boards: dict[str, Board]  # board name -> the stops it merges
    timeout: int = 30  # per-request timeout, seconds


class SfBay511Client:
    """Polls each configured stop once and builds a merged board per name."""

    def __init__(self, api_key: str, boards: dict[str, Board], timeout: int) -> None:
        self._api_key = api_key
        self._boards = boards
        self._timeout = timeout

    async def fetch(self, now: datetime) -> list[StopBoard]:
        """Poll every distinct stop concurrently and build one board per configured name.

        All-or-nothing: any request failing fails the whole source. A partially-populated board is
        worse than none, because a board missing half its arrivals looks like "nothing else is
        coming" rather than "we don't know" — and the pipeline already degrades a failed source
        gracefully by rendering the rest of the dashboard without it.
        """
        keys = self._distinct_stops()
        async with niquests.AsyncSession() as session:
            results = await asyncio.gather(
                *(self._visits(session, agency, stopcode) for agency, stopcode in keys),
                return_exceptions=True,
            )
        visits: dict[tuple[Agency, str], list[dict]] = {}
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            visits[key] = result
        return [self._board(name, board, visits, now) for name, board in self._boards.items()]

    def _distinct_stops(self) -> list[tuple[Agency, str]]:
        """Every distinct ``(agency, stopcode)`` across all boards, in first-seen order.

        Deduplicated because two boards may watch the same stop, and each request spends from a
        60/hour budget.
        """
        seen: dict[tuple[Agency, str], None] = {}
        for board in self._boards.values():
            for stop in board.stops:
                seen[(stop.agency, stop.stopcode)] = None
        return list(seen)

    async def _visits(
        self, session: niquests.AsyncSession, agency: Agency, stopcode: str
    ) -> list[dict]:
        """The raw ``MonitoredStopVisit`` list for one stop."""
        params = {
            "api_key": self._api_key,
            "agency": agency.value,
            "stopcode": stopcode,
            "format": "json",
        }
        try:
            resp = await session.get(STOP_MONITORING_API, params=params, timeout=self._timeout)
            resp.raise_for_status()
        except niquests.exceptions.RequestException as exc:
            raise SfBay511Error(f"511 request failed for {agency.value} stop {stopcode}") from exc
        return _monitored_visits(_decode(resp.content))

    def _board(
        self,
        name: str,
        board: Board,
        visits: dict[tuple[Agency, str], list[dict]],
        now: datetime,
    ) -> StopBoard:
        """Build one board from already-fetched visits, nested agency → direction and sorted.

        Pure CPU work on in-memory dicts: ``visits`` is the result of the one concurrent fan-out in
        :meth:`fetch`, keyed by ``(agency, stopcode)``, so a board reads its stops' arrivals from
        the map rather than issuing requests of its own. That is also what lets two boards share a
        stop without spending the rate budget twice.
        """
        nested: dict[Agency, dict[Direction, list[TransitArrival]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for stop in board.stops:
            for visit in visits[(stop.agency, stop.stopcode)]:
                arrival = _parse_visit(visit, stop.agency, stop.lines, now)
                if arrival is not None:
                    nested[arrival.agency][arrival.direction].append(arrival)
        arrivals = {
            agency: {d: sorted(a, key=lambda x: x.arrival) for d, a in by_direction.items()}
            for agency, by_direction in nested.items()
        }
        return StopBoard(name=name, arrivals=arrivals, display_name=board.display_name)


def _decode(content: bytes | None) -> dict:
    """Parse a 511 JSON body, tolerating the UTF-8 BOM it serves.

    ``utf-8-sig`` rather than ``resp.json()``: 511 prefixes its responses with a byte-order mark,
    which a plain UTF-8 decode carries into the first key and makes ``json.loads`` reject.
    """
    if content is None:  # a 2xx with no body still isn't data
        raise SfBay511Error("511 returned an empty body")
    try:
        return json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SfBay511Error("511 returned a body that is not JSON") from exc


def _as_list(value: object) -> list:
    """Normalize a SIRI-JSON field that may collapse a single-element array to a lone object."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _monitored_visits(payload: dict) -> list[dict]:
    """The ``MonitoredStopVisit`` list, or empty when the stop currently has no service.

    Both the delivery and the visit list get the object-or-array treatment: SIRI-JSON lets either
    collapse to a lone object, and a stop with exactly one train due (late evening, precisely when
    a board matters most) is the case that would otherwise iterate a dict's string keys and drop
    the whole source.
    """
    try:
        deliveries = _as_list(payload["ServiceDelivery"]["StopMonitoringDelivery"])
        # 511 sends a single delivery; later ones are ignored deliberately rather than merged.
        delivery = deliveries[0] if len(deliveries) > 0 else {}
        return _as_list(delivery.get("MonitoredStopVisit"))
    except (KeyError, TypeError, AttributeError) as exc:
        raise SfBay511Error("unexpected 511 StopMonitoring response") from exc


def _parse_visit(
    visit: dict, agency: Agency, lines: list[str] | None, now: datetime
) -> TransitArrival | None:
    """One visit as a :class:`TransitArrival`, or ``None`` if it should not be shown.

    Returns ``None`` for a visit that is unassigned, filtered out by ``lines``, carries no usable
    time, or has already departed. A *malformed* visit raises instead, so a shape change surfaces
    rather than quietly emptying the board.
    """
    try:
        journey = visit["MonitoredVehicleJourney"]
        line, direction = journey["LineRef"], journey["DirectionRef"]
        # A scheduled trip with no vehicle assigned yet reports null line *and* direction (over half
        # of a live BART station's visits look like this). There is nothing to draw for one — no
        # route badge, no direction to file it under — so it is skipped rather than treated as a
        # feed error.
        if line is None and direction is None:
            return None
        # Exactly one of the pair missing is a shape change, not an unassigned trip. Raised
        # explicitly because a frozen dataclass does no runtime type checking: a null LineRef would
        # otherwise sail through as `line=None` and reach a layout as a missing route badge.
        if line is None or direction is None:
            raise SfBay511Error(
                f"511 visit has LineRef={line!r} but DirectionRef={direction!r}; expected both"
            )
        if lines is not None and line not in lines:
            return None
        call = journey.get("MonitoredCall") or {}
        raw_time = next((call[f] for f in _TIME_FIELDS if call.get(f) is not None), None)
        if raw_time is None:
            return None
        arrival = _to_utc(raw_time)
        if arrival < now:  # already departed
            return None
        scheduled = call.get("AimedArrivalTime") or call.get("AimedDepartureTime")
        destination = call.get("DestinationDisplay") or journey.get("DestinationName") or ""
        scheduled_utc = _to_utc(scheduled) if scheduled is not None else None
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise SfBay511Error("unexpected 511 StopMonitoring visit") from exc

    # Built outside the try so a TypeError from changing TransitArrival surfaces as the
    # programming error it is, instead of being reported (and silently isolated by the
    # pipeline) as a feed outage.
    return TransitArrival(
        agency=agency,
        line=line,
        direction=_direction(agency, direction),
        destination=destination,
        arrival=arrival,
        scheduled=scheduled_utc,
    )


def _direction(agency: Agency, value: str) -> Direction:
    """``value`` as ``agency``'s own direction enum.

    Case and surrounding space are normalized first: under the all-or-nothing fetch a cosmetic
    feed change like "n" for "N" would otherwise blank the whole board.
    """
    try:
        return direction_enum(agency)(value.strip().upper())
    except ValueError as exc:
        raise SfBay511Error(
            f"511 sent direction {value!r}, which is not valid for {agency.label}"
        ) from exc


def _to_utc(value: str) -> datetime:
    """Parse a 511 timestamp as aware UTC.

    511 stamps its times in UTC with a ``Z`` suffix, which ``fromisoformat`` handles natively; the
    conversion mainly normalizes the tzinfo so every datetime this source produces is aware UTC.
    """
    return datetime.fromisoformat(value).astimezone(UTC)


class SfBay511Source(Source[SfBay511Config]):
    """The ``sf-bay-511`` source: fetches :class:`SfBay511Data` (a board per configured name)."""

    Config = SfBay511Config

    def __init__(self, config: SfBay511Config) -> None:
        self._config = config

    async def fetch(self, now: datetime) -> SfBay511Data:
        # The key is read here, not in __init__, so an unreadable one is a *source* failure the
        # pipeline can isolate rather than an exception escaping mid-construction. `Secret.value`
        # caches, so the read only costs anything on the first fetch.
        try:
            api_key = self._config.api_key.value
        except RuntimeError as exc:
            raise SfBay511Error("511 api_key could not be read") from exc
        client = SfBay511Client(api_key, self._config.boards, self._config.timeout)
        return SfBay511Data(boards=await client.fetch(now))

    @classmethod
    def cli(cls) -> typer.Typer:
        """Source-specific CLI verbs, mounted by the CLI under ``source sf-bay-511``."""
        app = typer.Typer()

        @app.command("list-stops")
        def list_stops(
            ctx: typer.Context,
            agency: str = typer.Option(..., help="511 operator code, e.g. BA, SF, CT, AC."),
        ) -> None:
            """Dump an operator's stops (code, platform, parent, name) — grep it to fill in config.

            Queried live rather than bundled: 511 covers 40+ operators and their stop lists change,
            so a checked-in copy would rot. Unlike the mta source's equivalent this needs a key,
            which it reads from the configured ``[sources.sf-bay-511]`` table — no second place to
            put a credential, and whatever ``Secret`` form the operator chose keeps working.
            """
            api_key = source_config(ctx, "sf-bay-511", SfBay511Config).api_key.value
            params = {"api_key": api_key, "operator_id": agency, "format": "json"}
            try:
                resp = niquests.get(_STOPS_API, params=params, timeout=30)
                resp.raise_for_status()
            except niquests.exceptions.RequestException as exc:
                raise typer.BadParameter(f"511 stop lookup failed for {agency!r}: {exc}") from exc
            try:
                rows = _stop_rows(_decode(resp.content))
            except (
                SfBay511Error
            ) as exc:  # typer renders BadParameter; a SourceError would traceback
                raise typer.BadParameter(str(exc)) from exc
            if len(rows) == 0:
                raise typer.BadParameter(f"511 returned no stops for operator {agency!r}")
            widths = [max(len(row[i]) for row in rows) for i in range(3)]
            for code, platform, parent, name in rows:
                typer.echo(
                    f"{code:<{widths[0]}}  {platform:<{widths[1]}}  {parent:<{widths[2]}}  {name}"
                )

        @app.command("agencies")
        def agencies() -> None:
            """List the operator codes this source understands."""
            for member in Agency:
                typer.echo(f"{member.value}  {member.label}")

        return app


def _stop_rows(payload: dict) -> list[tuple[str, str, str, str]]:
    """(stopcode, platform, parent station, name) for each stop in a NeTEx stops payload."""
    try:
        stops = _as_list(payload["Contents"]["dataObjects"]["ScheduledStopPoint"])
        rows = []
        for stop in stops:
            extensions = stop.get("Extensions") or {}
            rows.append(
                (
                    str(stop.get("id") or ""),
                    str(extensions.get("PlatformCode") or "-"),
                    str(extensions.get("ParentStation") or "-"),
                    str(stop.get("Name") or ""),
                )
            )
    except (KeyError, TypeError, AttributeError) as exc:
        raise SfBay511Error("unexpected 511 stops response") from exc
    return rows
