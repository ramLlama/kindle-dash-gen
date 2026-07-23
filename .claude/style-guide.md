# Style Guide

Match the existing code first. This documents the conventions already in play; enforced rules
live in `pyproject.toml` (`[tool.ruff]`).

## Tooling & Enforcement

- **Line length:** 100 (`tool.ruff.line-length`).
- **Ruff lint select:** `E`, `F`, `I` (isort), `UP` (pyupgrade), `B` (bugbear). Run
  `uv run ruff check .` — it is a required gate alongside `uv run pytest`.
- Target Python **3.14+**. Use modern syntax freely: `X | None` unions, `StrEnum`, `tomllib`,
  built-in generics (`list[str]`, `dict[str, Station]`).
- Every module starts with `from __future__ import annotations`.

## Naming

- Modules and functions: `snake_case`. Classes: `PascalCase`. Constants: `UPPER_SNAKE`.
- **Module-private helpers are prefixed with `_`** (e.g. `_atomic_write`, `_day_high_low`,
  `_quantize_lut`, `_fit`). Public API is the un-prefixed surface.
- Domain error classes are named `<Thing>Error`. Source-fetch errors subclass the shared
  `SourceError` (in `sources/toolkit.py`) so the pipeline can isolate any source generically —
  `WeatherError`, `OpenMeteoError`, `MtaError`, `SfBay511Error`. Other error types subclass
  `RuntimeError` directly (`LayoutError`, `SourceError` itself).
- Module-level constants for external vocab / literals: `NWS_API`, `_LINE_TO_URL`,
  `_RAIN_KEYWORDS`, `_DIRECTION_SUFFIXES`. This extends to an exception tuple a module swallows
  repeatedly: name it (`_ALERT_TIME_SWALLOW = (ValueError, TypeError)`) rather than writing a bare
  inline `except (ValueError, TypeError):`.

## Types & Models

- **Domain models are frozen dataclasses**, keyword-only where they have many fields
  (`@dataclass(frozen=True, kw_only=True)`). They carry data only — no methods, no formatting.
- **Config models are pydantic** `BaseModel` with `model_config = ConfigDict(extra="forbid")`
  on every class. Provide sensible defaults inline; document non-obvious fields with a trailing
  `#` comment (see `config.py`).
- **Datetimes are timezone-aware UTC.** Never construct a naive datetime: `datetime.now(UTC)`, not
  `datetime.now()`. A source normalizes provider timestamps with `.astimezone(UTC)` (or
  `.replace(tzinfo=<named zone>).astimezone(UTC)` when the provider sends naive local values), and
  only a layout converts back with `.astimezone(self.tz)` for display. A display zone is typed
  `ZoneInfo` on the config model so pydantic validates the IANA name at load time.
- Prefer precise unions and `Literal[...]` for closed sets (`Literal["us", "si", "both"]`,
  `PostProcessMethod = Literal["resize", "crop", "pad"]`).
- **Map an enum with `match`, not a dict.** When every member of an enum needs an entry (a label, a
  companion type, a URL), write a `match` over the members with a `return` per arm. mypy
  (`[tool.mypy]` in `pyproject.toml`, `files = ["kindle_dash_gen"]`) then enforces exhaustiveness
  statically: adding a member without an arm is a "Missing return statement" at check time, where a
  dict lookup only blows up with a `KeyError` at runtime, and only once that member is first
  exercised. `Agency.label` and `direction_enum()` in `sources/builtins/sf_bay_511/model.py` are
  the worked examples; this rule let a runtime "every member has an entry" test be deleted.
- **`StrEnum` members from different enums can compare and hash equal.** Two `StrEnum`s sharing a
  value (`BartDirection.NORTH` and `MuniDirection.NORTH` are both `"N"`) are interchangeable to
  `==` and as dict keys. When the distinction matters, check the **type** (`isinstance`, or
  `type(a) is type(b)`) and don't key a single flat dict by values drawn from more than one of
  them. See the `sf-bay-511` direction model.

## Functions & Docstrings

- Non-trivial functions and all public functions have a one-line (or short) docstring stating
  intent, not mechanics. Module docstrings explain the module's role and any non-obvious
  invariant (e.g. "all data is SI; callers round for display").
- Keyword-only params (`*,`) for multi-arg render/pipeline functions to keep call sites
  self-documenting (`post_process(image, *, width, height, gray_levels, method, rotate)`).
- Inject collaborators for testability with a defaulted optional param (e.g. `MtaClient`'s async
  `feed_loader: FeedLoader | None = None`). HTTP clients that open their own `niquests.AsyncSession`
  (e.g. `NwsClient`) are instead mocked at the HTTP layer with `niquests-mock`.

## Comments

- Comment the *why* and non-obvious *what*, not the line-by-line obvious. Good examples in the
  codebase: the protobuf-override note in `pyproject.toml`, the "NWS rejects >4 decimal places"
  note, the "anchor today on `as_of`, not the first daily period" rationale in `nws/source.py`, the
  atomic-write explanation.
- Do not leave commented-out code or `TODO` placeholders.
- **Verify a timezone assumption empirically, then comment the finding.** Reasoning about offsets,
  DST folds, and what a provider's `timezone` parameter actually controls is unreliable — run it
  under a moved `TZ` or against a real response and record the concrete numbers in the comment. The
  Open-Meteo `timezone=auto` note (a real 18.1-vs-20.8 high under a UTC aggregation window) and the
  nyct-gtfs host-local round-trip note are both written this way, and both are load-bearing: each
  documents a change that *looks* like a simplification but is wrong.
- **Inspect a real render for pixel-affecting layout work — tests can't see misplaced text.** A
  test asserts text *was drawn*, not *where*; a badge centered into the neighbouring column passes
  every assertion and only a rendered PNG shows the collision. When you touch geometry, look at the
  actual image, and pin the intended output with a checked-in digest
  (`test_mta_rendering_is_unchanged_by_the_transit_adapter`) so a later 1px regression fails loudly.
- **A "no-op for the common case" claim must be checked across the whole parameter range**, not
  just the fixture. A refactor that "preserves existing subway boards" was digest-tested only at two
  columns; the same code silently shrank badges from size 50 to 18 at four columns. When you assert
  behavior is unchanged for a class of inputs, verify at the extremes of every parameter (here,
  column count) — or derive a constant so the property holds by construction, as `_BADGE_MIN_WIDTH`
  does.

## Python-specific conventions (from global user prefs, honored here)

- **Explicit checks over truthiness** for containers/values: `if len(x) == 0`, `if x is None`,
  `if x is not None` — not `if not x`. This is used consistently across the codebase.
- **Minimal visibility**: don't export/widen something until it's used outside its module.
- **Centralize cross-cutting logic**: display formatting lives only in `format.py`; feed-URL
  mapping and direction suffixes are single named constants.
- **Fail fast** on things that should succeed (bad explicit override, missing capability);
  **degrade gracefully** only for expected external outages (source fetch failures).

## Imports & Structure

- isort-ordered (ruff `I`): stdlib, third-party, first-party (`from . / from ..`).
- `models/__init__.py` re-exports the domain models with an explicit `__all__`; import domain
  types from `kindle_dash_gen.models`, not the submodules.
- Bundled assets are loaded via `importlib.resources.files("kindle_dash_gen")`, never with
  hardcoded filesystem paths — keeps them resolvable regardless of CWD.

## Commits

Conventional Commits (`feat`, `fix`, `refactor`, `docs`, `test`, `build`, `ci`, `perf`) — no
`chore`. One milestone/feature per commit. Include a `Co-Authored-By: Claude` trailer.
