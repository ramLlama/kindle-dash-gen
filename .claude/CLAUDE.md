# kindle-dash-gen

## What This Project Does

A Python CLI that periodically generates a Kindle e-ink dashboard image for NYC. It pulls the
local NWS weather forecast plus real-time MTA subway arrivals, renders the whole dashboard via one
of two backends (a deterministic local **pillow** layout, the default; or an **llm** OpenRouter
image model), post-processes the resulting PNG for a Kindle Voyage (grayscale, exact pixel
dimensions, 16 hardware gray levels), and writes it to a configured path for syncing to the
device. Intended to run unattended on an interval (e.g. every 5 minutes).

## Tech Stack

- **Python 3.14+** (uses `tomllib`, `StrEnum`, `X | None` unions everywhere)
- **uv** for env/deps. The project is `package = false` — run in place, never installed.
- **typer** `0.26.*` — CLI framework
- **pydantic** `2.*` — config validation (`extra="forbid"` on every model)
- **niquests** `3.*` — HTTP client (NWS + OpenRouter); `niquests-mock` in tests
- **nyct-gtfs** `2.*` — MTA GTFS-realtime feed parsing
- **jinja2** `3.*` — prompt templating (llm backend)
- **pillow** `12.*` — image post-processing and the pillow rendering backend
- **fontconfig** (`fc-match`, system tool) — the pillow backend resolves a font family name to a
  file; required at runtime when `backend = "pillow"`
- **pytest** `9.*`, **ruff** `0.15.*` — test + lint gates

## Repository Structure

```
kindle_dash_gen/
  __main__.py          # `python -m kindle_dash_gen` entry -> cli.run()
  cli.py               # typer app: version, run, weather, mta group, dashboard group
  config.py            # TOML -> pydantic Config; Secret (value | value_from_cmd)
  pipeline.py          # gather -> build_prompt -> render -> post_process -> atomic write
  format.py            # display formatters (temp/reading/apparent/wind/eta); SI -> display
  models/              # frozen dataclasses (domain models, no presentation)
    weather.py         # Temperature, HourlyForecast, WeatherReport
    mta.py             # Direction (StrEnum), TrainArrival, StationBoard, MtaBoards
    dashboard_data.py  # DashboardData (source_data keyed by produced type)
  sources/             # data-source plugins (source-side mirror of render/)
    toolkit.py         # public plugin API: SourceError (base all source errors subclass)
    registry.py        # Source protocol, register_source, build_sources() dispatch
    builtins/          # bundled source plugins (discovered, not special-cased)
      nws/             # "nws" source: NwsClient + NwsConfig -> WeatherReport
      mta/             # "mta" source: MtaClient + MtaConfig (owns Platform/Station) -> MtaBoards
  render/              # turn data into a Kindle-ready PNG (two backends)
    prompt.py          # llm backend: render_prompt() Jinja2, public template context contract
    openrouter.py      # llm backend: OpenRouterClient, Unified Image API, capability discovery
    layout.py          # pillow backend: Layout protocol, register_layout, render() dispatch
    toolkit.py         # pillow backend: public plugin API (Fonts, INK/PAPER, fit_font, assets)
    builtins/          # bundled layout plugins (discovered, not special-cased)
      glanceable/      # the default layout as a self-contained plugin (owns its assets/icons/)
    postprocess.py     # post_process(): grayscale, fit, quantize (Pillow) — shared by both
  plugins.py           # plugin discovery: bundled layout + source roots + optional local plugins_path
  assets/
    dashboard_prompts/*.j2       # bundled prompt templates ("dense", "glanceable") — llm backend
    mta/stations.csv             # bundled station lookup (for `mta list-stations`)
tests/                 # pytest, one file per module; HTTP mocked with niquests-mock
config.example.toml    # copy to config.toml (gitignored) and edit
docs/plugins.md        # how to write a render layout plugin (the public contract)
docs/sources.md        # how to write a data-source plugin (the public contract)
```

## Key Concepts & Domain Model

- **DashboardData** (`models/dashboard_data.py`) is the aggregate handed to the renderer:
  `generated_at` (also used as "now" for ETAs) plus `source_data: dict[type, Any]`, keyed by each
  source's produced data class (e.g. `WeatherReport`, `MtaBoards`). Consumers look up defensively:
  `data.source_data.get(WeatherReport)`; a failed or empty source is simply absent from the dict.
- **MtaBoards** (`models/mta.py`) wraps `list[StationBoard]` so the subway source contributes a
  single typed value (a bare list can't be a `source_data` key).
- **Station vs Platform** (`sources/builtins/mta/`): the mta source owns these config models. A
  **Station** is a display board keyed by name; it merges one or more **Platform** entries (each a
  GTFS base stop id + the lines serving it) into per-direction arrival lists. Example: "Union Sq"
  merges the N/Q/R/W, 4/5/6, and L platforms into one board. Boards are **uncapped** — the layout
  decides how many arrivals to show at render.
- **Direction** is a `StrEnum` with values `"N"`/`"S"` (GTFS uptown/downtown, nominal for the L).
- **WeatherReport** carries current conditions, today/tomorrow high-low, and upcoming hours.
  `Temperature` bundles a `real` value with an optional `feels_like` (apparent).

## Architecture Overview

Linear pipeline, wired in `pipeline.py`:
`gather()` (iterate the discovered source plugins **once**, isolating each) → for each configured
`[dashboards.<name>]`: `render_raw()` (dispatch on that dashboard's `backend`) → `post_process()`
(grayscale, fit, quantize) → atomic write to the dashboard's `path`. `run_once()` returns a
`RunResult(written, failed)`; one dashboard's render failure is isolated (logged, others proceed).
`render_raw()` branches: the **pillow** backend calls `layout.render()` (draws
`DashboardData` at native size); the **llm** backend does `build_prompt()` →
`OpenRouterClient.generate()`. Both return raw PNG bytes, and `post_process()` is shared (for
pillow the fit step is a no-op since it's already exact-sized, so only quantization applies). The
`dashboard` CLI subcommands expose each step in isolation for debugging.

See [architecture.md](architecture.md) for data flow, the NWS multi-step fetch, MTA feed
deduplication, and the OpenRouter capability-discovery details.

## Development Workflow

```sh
uv sync
cp config.example.toml config.toml     # edit; config.toml is gitignored

# Run in place (NOT installed — always via -m):
uv run python -m kindle_dash_gen --help
uv run python -m kindle_dash_gen --config config.toml dashboard preview-prompt  # no API spend
uv run python -m kindle_dash_gen --config config.toml run --one-shot            # one iteration
uv run python -m kindle_dash_gen --config config.toml run                       # loop

# Verification gates (both must pass):
uv run pytest
uv run ruff check .
```

Global `--config` / `-c` defaults to `config.toml`; it is stored on the typer context and each
subcommand loads it on demand via `_config(ctx)`.

## Critical Idiosyncrasies & Gotchas

- **SI internally, round at display.** All weather data is kept in SI (°C, km/h) at full
  precision through the models and sources. Conversion and rounding happen only in `format.py`
  at output time. Do not round or convert units inside sources or models.
- **Secrets never come from environment variables.** The OpenRouter API key is a `Secret`:
  either an inline `{ value = "..." }` or `{ value_from_cmd = "..." }` whose stdout is the key.
  This is a deliberate design choice, not an oversight — do not add env-var fallbacks.
- **Multiple dashboards, one fetch.** Config has `dashboards: dict[str, Dashboard]` (named
  `[dashboards.<name>]` tables). `gather()` runs once and every dashboard renders from that shared
  data to its own `path`. `[openrouter]` is required only if *some* dashboard uses the llm backend.
- **Two render backends.** Each dashboard's `backend` selects `"pillow"` (default: deterministic
  local layout; free, offline, exact — never garbles data) or `"llm"` (OpenRouter image model).
  `[openrouter]` is optional and only required for the llm backend (a `Config` validator enforces
  this). The pillow backend resolves its `font` family via fontconfig (`fc-match`); a missing
  font/asset raises `LayoutError`.
- **Both layouts and sources are plugins (no special builtins).** `plugins.load_plugins()`
  discovers two kinds by identical logic, each from a bundled root plus the optional shared local
  `plugins_path` dir (which hosts both kinds). Registries start empty:
  - **Layouts** register via `register_layout` at import, bundled root
    `kindle_dash_gen.render.builtins` (the `glanceable` layout lives at `render/builtins/glanceable/`).
    Build on `render/toolkit.py` (`Fonts`, `INK`/`PAPER`, `fit_font`, `load_asset_image`,
    `LayoutError`). See `docs/plugins.md`.
  - **Sources** register via `register_source` at import, bundled root
    `kindle_dash_gen.sources.builtins` (the `nws` and `mta` sources). A source is a `Source`
    protocol class with a `Config: ClassVar[type[BaseModel]]` and a `fetch(now)`; build on
    `sources/toolkit.py` (`SourceError`). See `docs/sources.md`.

  Do **not** re-add a hardcoded builtin dict for either kind.
- **Per-source isolation.** In `gather()`, each source's `SourceError` (subclasses: `WeatherError`,
  `MtaError`) drops just that source's data (logged) and the render proceeds with whatever remains.
  Only `SourceError` is swallowed; two sources producing the same data type is a misconfiguration
  and fails loud. If *every* source is empty (`len(source_data) == 0`), `run_once()` skips the
  render entirely so it never spends a paid generation or clobbers the last good image.
- **Atomic writes.** Output is written to a `.tmp` sibling then `Path.replace`d, so a crash
  mid-write leaves the previous PNG intact. Keep this when touching the write path.
- **`package = false` / run via `-m`.** There is no install step and no console script on PATH.
  Always invoke `uv run python -m kindle_dash_gen`.
- **protobuf override.** `nyct-gtfs` hard-pins `protobuf==4.25.3`, which crashes on Python 3.14.
  `pyproject.toml` forces `protobuf>=6` via `[tool.uv] override-dependencies`. See the comment
  there and the upstream issue link before touching MTA deps.
- **OpenRouter capabilities are discovered at runtime**, not hardcoded — aspect ratios and
  resolutions are queried per model from its `/endpoints` listing, unioned across endpoints. An
  unsupported `aspect_ratio`/`resolution` override fails fast with the valid values listed.
- **Config is strict, and source config is validated per-plugin.** Every pydantic model sets
  `extra="forbid"`; an unknown TOML key is a validation error. Top-level `Config` no longer defines
  the source sections — it holds `sources: dict[str, dict[str, Any]]` (raw `[sources.<name>]`
  tables). After plugin discovery, `build_sources()` validates each slice against its plugin's own
  `Config` model (each still `extra="forbid"`), so unknown/malformed source keys fail fast there,
  not statically in `Config`. An unknown source *name* also fails fast. The CLI `_config()` runs
  `build_sources()` eagerly so a bad source is caught before any fetch. Zero sources is valid
  (every render then legitimately skips). Display temperature units are now `weather_temp_units`
  on each `[dashboards.<name>]` (both backends use it), not a weather-source setting.
- **Milestone-per-commit.** History is built as discrete milestones (M6 = the pillow rendering
  backend; the latest reworks data sources into discovered plugins, mirroring the layout system),
  one feature/refactor per commit, Conventional Commits style.

## Context Files

- [Architecture & Data Flow](architecture.md)
- [Style Guide](style-guide.md)
- [Testing](context/testing.md)
