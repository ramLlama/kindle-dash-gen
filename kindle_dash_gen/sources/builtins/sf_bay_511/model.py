"""Data the ``sf-bay-511`` source produces (owned by the source that produces it).

Directions are typed per agency rather than as free strings, because each Bay Area operator uses a
different vocabulary and the differences are real (BART runs north/south, Muni runs inbound/
outbound, AC Transit's bus network runs all four compass points). Every enum below covers exactly
the values that agency was observed to emit, so a value outside it means the feed or the config is
wrong and should fail loudly rather than render a board with a silently-dropped direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Agency(StrEnum):
    """A 511 operator code, as passed to the API's ``agency`` parameter."""

    BART = "BA"
    MUNI = "SF"
    CALTRAIN = "CT"
    AC_TRANSIT = "AC"

    @property
    def label(self) -> str:
        """The operator's name as riders write it ("BART", not "Bart" or "BA").

        Spelled out rather than derived from the member name, which title-cases to "Bart" and
        "Ac Transit". A layout drawing a board that several operators serve needs these to read
        right.

        Matched rather than looked up in a dict so the type checker enforces exhaustiveness: adding
        an ``Agency`` member without a label here is a "Missing return statement" error at check
        time, where a dict would only fail with a ``KeyError`` once that agency was configured.
        """
        match self:
            case Agency.BART:
                return "BART"
            case Agency.MUNI:
                return "Muni"
            case Agency.CALTRAIN:
                return "Caltrain"
            case Agency.AC_TRANSIT:
                return "AC Transit"


class BartDirection(StrEnum):
    """BART's regional rail runs north/south."""

    NORTH = "N"
    SOUTH = "S"


class CaltrainDirection(StrEnum):
    """Caltrain runs north/south down the Peninsula."""

    NORTH = "N"
    SOUTH = "S"


class MuniDirection(StrEnum):
    """Muni is mostly inbound/outbound, but its feed also emits north/south on some lines."""

    INBOUND = "IB"
    OUTBOUND = "OB"
    NORTH = "N"
    SOUTH = "S"


class AcTransitDirection(StrEnum):
    """AC Transit's bus network runs all four compass directions."""

    NORTH = "N"
    SOUTH = "S"
    EAST = "E"
    WEST = "W"


# The direction union, disambiguated by an arrival's ``agency``. Note the members are not disjoint
# by *value* — ``BartDirection.NORTH`` and ``MuniDirection.NORTH`` are both the string "N", and
# being StrEnums they compare equal. Only the enum *type* distinguishes them, which is what the
# validators below check and why boards nest by agency before direction.
Direction = BartDirection | MuniDirection | CaltrainDirection | AcTransitDirection


def direction_enum(agency: Agency) -> type[Direction]:
    """The direction enum ``agency`` speaks.

    Matched rather than looked up in a dict for the same reason as :attr:`Agency.label`: a new
    ``Agency`` member without a direction vocabulary fails at check time instead of raising a
    ``KeyError`` mid-fetch. Note BART and Caltrain map to *distinct* enums despite both being
    north/south — collapsing them into one alias would defeat the per-agency type check below.
    """
    match agency:
        case Agency.BART:
            return BartDirection
        case Agency.MUNI:
            return MuniDirection
        case Agency.CALTRAIN:
            return CaltrainDirection
        case Agency.AC_TRANSIT:
            return AcTransitDirection


def _check_direction(agency: Agency, direction: Direction) -> None:
    """Raise unless ``direction`` is from ``agency``'s own enum."""
    expected = direction_enum(agency)
    if not isinstance(direction, expected):
        raise ValueError(
            f"{agency.name} direction must be a {expected.__name__}, "
            f"got {type(direction).__name__}.{direction.name}"
        )


@dataclass(frozen=True, kw_only=True)
class TransitArrival:
    """A predicted arrival at a stop.

    ``agency`` is what makes ``direction`` unambiguous, so the two are validated together.
    """

    agency: Agency
    line: str  # raw LineRef, e.g. "Green-N" (BART) or "22" (Muni)
    direction: Direction
    destination: str  # headsign, e.g. "OAK Airport / Berryessa" ("" if the feed omits it)
    arrival: datetime  # predicted arrival (aware UTC; a layout converts for display)
    scheduled: datetime | None = None  # timetabled arrival, when the feed supplies one

    def __post_init__(self) -> None:
        _check_direction(self.agency, self.direction)


@dataclass(frozen=True, kw_only=True)
class StopBoard:
    """Upcoming arrivals for one named board, nested agency → direction → arrivals.

    Several operators can serve one location (a BART station above a Muni stop), so the agency is
    the outer key: it keeps each operator's arrivals separate and, because direction values overlap
    across agencies, stops ``BartDirection.NORTH`` and ``CaltrainDirection.NORTH`` from colliding as
    keys in a single flat dict. ``name`` is the canonical config key plugins match on;
    ``display_name`` optionally overrides what a layout shows.
    """

    name: str
    arrivals: dict[Agency, dict[Direction, list[TransitArrival]]] = field(default_factory=dict)
    display_name: str | None = None

    def __post_init__(self) -> None:
        for agency, by_direction in self.arrivals.items():
            for direction, arrivals in by_direction.items():
                _check_direction(agency, direction)
                for arrival in arrivals:
                    if arrival.agency is not agency:
                        raise ValueError(
                            f"arrival for {arrival.agency.name} filed under {agency.name}"
                        )
                    # Not just equality: `BartDirection.NORTH == MuniDirection.NORTH` is True, so
                    # an arrival can only be considered correctly filed if the enum types match too.
                    if type(arrival.direction) is not type(direction) or (
                        arrival.direction is not direction
                    ):
                        raise ValueError(f"{arrival.direction!r} arrival filed under {direction!r}")

    @property
    def label(self) -> str:
        """Name a layout should show: ``display_name`` if set, else the canonical ``name``."""
        return self.display_name if self.display_name is not None else self.name


@dataclass(frozen=True, kw_only=True)
class SfBay511Data:
    """All boards from one 511 fetch, wrapped as a single value.

    The source contributes this to ``DashboardData.source_data`` under its own type key (a bare
    list can't key that dict); consumers read ``.boards``.
    """

    boards: list[StopBoard]
