"""Configuration model loaded from a TOML file."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# How the rendered image is fitted to the Kindle's exact pixel dimensions:
#   resize -- stretch to fill, ignoring aspect (minor distortion)
#   crop   -- scale to cover, center-crop the excess (no distortion, trims a sliver)
#   pad    -- scale to fit, add white e-ink bars (nothing cropped or distorted)
PostProcessMethod = Literal["resize", "crop", "pad"]


class Dashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    # Display units for weather temperatures (data is always SI internally; rounded/converted at
    # render). A whole-dashboard presentation choice, so it lives here rather than on the source.
    weather_temp_units: Literal["us", "si", "both"] = "us"
    layout: str = "glanceable"  # registered layout plugin (see docs/plugins.md)
    # System font family (resolved via fontconfig). None = unspecified, letting a layout choose its
    # own default (glanceable falls back to toolkit.DEFAULT_FONT). A set value overrides it.
    font: str | None = None
    width: int = 1072  # Kindle Voyage, portrait (native orientation)
    height: int = 1448
    gray_levels: int = 16
    post_process_method: PostProcessMethod = "resize"
    # rotate the final image 90° before writing (for a physically rotated device)
    rotate: bool = False


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_minutes: int = 5


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Raw [sources.<name>] tables, validated per-plugin (not here) after plugin discovery so each
    # source owns its own schema. See kindle_dash_gen.sources.registry.build_sources. Zero sources
    # is valid: every render then legitimately skips (keeps the last image).
    sources: dict[str, dict[str, Any]] = {}
    dashboards: dict[str, Dashboard]  # name -> output; one shared data fetch renders each
    plugins_path: Path | None = None  # absolute dir of private render plugins (see docs/plugins.md)
    schedule: Schedule = Schedule()

    @model_validator(mode="after")
    def _validate_dashboards(self) -> Config:
        if len(self.dashboards) == 0:
            raise ValueError("at least one [dashboards.<name>] section is required")
        # Absolute so plugin discovery is unambiguous regardless of the process's working directory.
        if self.plugins_path is not None and not self.plugins_path.is_absolute():
            raise ValueError(f"plugins_path must be an absolute path, got {self.plugins_path}")
        return self


def load_config(path: Path) -> Config:
    """Load and validate the TOML config at ``path``."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
