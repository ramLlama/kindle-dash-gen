# kindle-dash-gen-nyc

Generate a Kindle e-ink dashboard image with local weather (NWS) and real-time NYC subway
arrivals (MTA), rendered by an OpenRouter image model and post-processed for a Kindle
display.

The generator, every few minutes:

1. Pulls the local forecast from the [NWS API](https://www.weather.gov/documentation/services-web-api).
2. Pulls real-time subway arrivals via [`nyct-gtfs`](https://github.com/Andrew-Dickinson/nyct-gtfs).
3. Asks an OpenRouter image model to render the whole dashboard from that data.
4. Post-processes the image (grayscale, exact resolution, reduced bit depth) for the Kindle.
5. Writes the PNG to a configured path for syncing to the device.

## Requirements

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/)

## Setup

```sh
uv sync
cp config.example.toml config.toml   # then edit config.toml
```

## Usage

Run in place from the clone (no install step):

```sh
uv run python -m kindle_dash_gen_nyc --help
uv run python -m kindle_dash_gen_nyc version
uv run python -m kindle_dash_gen_nyc generate --config config.toml   # one-shot (M5)
uv run python -m kindle_dash_gen_nyc run --config config.toml        # loop every interval (M5)
```

## Configuration

See [`config.example.toml`](config.example.toml). The OpenRouter API key is never read from
an environment variable — supply it inline (`{ value = "..." }`) or as a command whose
stdout is the key (`{ value_from_cmd = "pass show openrouter/key" }`).

## Development

```sh
uv run pytest
uv run ruff check
```
