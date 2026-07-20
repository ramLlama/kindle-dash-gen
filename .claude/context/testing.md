# Testing

Run with `uv run pytest` (config in `pyproject.toml`: `testpaths = ["tests"]`,
`pythonpath = ["."]`). One test file per source module (`tests/test_<module>.py`).

## Conventions

- **Tests are offline.** No test hits a real network. HTTP-backed clients are tested with
  `niquests-mock` (imported as `import niquests_mock as nm`); collaborators that aren't HTTP
  (the MTA feed, the layout in pipeline tests) are stubbed via injected fakes or `monkeypatch`.
- **Async fetches.** `fetch` (and `gather`/`run_once`/`run`) are coroutines; tests drive them with
  `asyncio.run(...)` (e.g. `asyncio.run(_client().fetch(LAT, LON))`).
- **Dependency injection over patching where the seam exists.** `NwsClient` opens its own
  `niquests.AsyncSession`, so NWS tests mock at the HTTP layer with `niquests-mock` (no session is
  injected). `MtaClient` takes an optional async `feed_loader` (`Callable[[str], Awaitable[NYCTFeed]]`);
  prefer passing a fake through the constructor, and use `monkeypatch.setattr(pipeline, "MtaClient", Fake)`
  only for wiring at the pipeline level.
- **Host-timezone independence** is tested by actually moving the process zone. `tests/conftest.py`
  provides a `host_timezone` fixture that yields a *setter* (so one test can render under several
  zones and compare) and restores `TZ` + `time.tzset()` in teardown — that state is process-global,
  so a leak would surface as an unrelated test failing depending on ordering. Used by
  `test_layout.py` and `test_mta.py`.
- **Hand-written fixtures under-represent a live feed.** Tests are offline, so a mock payload only
  contains the shapes its author thought of. Two `sf-bay-511` failures shipped this way: over half a
  real BART station's visits carry a null `LineRef`/`DirectionRef` pair (no fixture had one), and a
  rail stopcode is a single *platform*, so a fixture with one stop per board hid that a
  two-direction board must merge two stopcodes. When adding a source, hit the real endpoint once
  by hand, then encode what you actually saw — especially the *proportions* — as fixtures.
- **Config in tests** is built with `Config.model_validate(CONFIG_DICT)` from an inline dict
  (see `tests/test_pipeline.py`), not by loading a TOML file.
- **Real image assertions.** Pillow-touching tests assert against actual decoded output
  (`Image.open(...).size`, `.mode == "L"`), not mocks — verify the bytes are a real
  Kindle-ready PNG.
- Follow the global preference: `with pytest.raises(ExnType):` without matching on the message.

## What each test file covers

- `test_pipeline.py` — source isolation (a `SourceError` from one source still renders the rest),
  the skip-render-when-all-sources-empty short-circuit (must not overwrite the last image or spend a
  generation), one-shot writes a grayscale image at the configured size, and the run loop
  (loops until `KeyboardInterrupt`, survives an iteration failure and retries).
- `test_plugins.py` — layout + source registry APIs, discovery of the bundled plugins, and loading
  a local `plugins_path` directory (the mechanism serving both layouts and sources).
- `test_sources.py` — `build_sources`: per-plugin `Config` validation of each `[sources.<name>]`
  slice, and fail-fast on an unknown source name or an invalid slice.
- `test_layout.py` — the layout registry: `validate_layout`/`build_layout` config validation,
  dispatch, `Fonts` resolution, `LayoutError`, that `glanceable`'s `timezone` is required and
  rejects an unknown zone at validation, and that a render is pixel-identical across host timezones
  (the configured `timezone` is the only thing that may change the clock it draws).
- `test_weather.py` — NWS (`nws` source) multi-step fetch parsing, `_day_high_low` (exact-day match,
  including the evening case where an expired daytime period must report `None` rather than borrow
  tomorrow's high), the `as_of`-anchored "today", apparent-temp series, observation/raining
  derivation.
- `test_mta.py` — MTA (`mta` source) feed dedup/reuse, platform merging, per-direction sort, error
  paths, and that arrivals come out as the same aware-UTC instant regardless of the host zone.
- `test_sf_bay_511.py` — 511 (`sf-bay-511` source) visit parsing, one-request-per-distinct-stopcode
  dedup, multi-stop/multi-agency board merging, the `lines` filter, all-or-nothing failure, BOM and
  object-or-array response shapes, and `Secret` resolution (including that an unreadable key is a
  `SfBay511Error`, not a crash). Two model invariants are pinned deliberately: that **BART and
  Caltrain directions stay distinct types** (they are value-equal, so only the type check catches a
  mis-filed arrival) and that a direction from another agency is rejected. There is no runtime
  "every `Agency` has a label" test — `match`-based exhaustiveness makes that a mypy error instead.
- `test_postprocess.py` — grayscale/fit/quantize, each `method`, gray-level LUT.
- `test_config.py` — pydantic validation, `extra="forbid"`, the reshaped `Dashboard` (output spec
  + raw `layout_config`).
- `test_format.py` — unit conversion and display formatting across `us`/`si`/`both`.
- `test_cli.py` — typer command wiring, plus a regression test driving the **real** `MtaClient`
  through `source mta` with only the feed loader stubbed. Most CLI source tests substitute the whole
  client, which is exactly why a naive `datetime.now()` in the `source <name>` fetch path shipped
  and crashed with "can't compare offset-naive and offset-aware datetimes". When a CLI path builds
  a real object, test it with a real object.

## Adding tests

- New source or render module → add `tests/test_<module>.py` mirroring the existing style.
- When adding a network call, mock it with `niquests-mock`; do not introduce a live dependency.
- A new layout is a plugin: add a test exercising its `Config` validation and that `render` returns
  an `Image` at the panel size (see `test_layout.py`). When changing the `Layout` protocol or the
  `layout_config` contract, update `docs/plugins.md`, `config.example.toml`, and
  [architecture.md](../architecture.md) together — it is the public plugin API.
