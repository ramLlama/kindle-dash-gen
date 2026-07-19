"""Configuration model loaded from a TOML file."""

from __future__ import annotations

import os
import subprocess
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

# Wall-clock ceiling on a `value_from_cmd` helper. Password-manager CLIs (`pass`, `op`, `bw`) block
# on a passphrase/biometric prompt once their agent lock expires; unattended (`run` on an interval,
# inside `asyncio.gather`) that would hang the event loop indefinitely instead of failing a fetch.
_CMD_TIMEOUT_SECONDS = 10


class Secret(BaseModel):
    """A secret read from a literal value, a command's stdout, or an environment variable.

    Exactly one of the three ``value_from_*`` sources must be set. Part of the public plugin surface
    (re-exported from both toolkits), so any plugin config can type a credential field as ``Secret``
    and let the operator keep it out of the config file::

        api_key = { value = "sk-live-..." }
        api_key = { value_from_cmd = "pass show 511" }
        api_key = { value_from_env = "KDG_511_API_KEY" }

    Consumers read the secret as :attr:`value` (``config.api_key.value``); the literal *input* is
    spelled ``value`` in TOML but stored as ``value_from_value``, keeping the three sources
    symmetrically named and leaving ``value`` free for the resolved accessor.

    Reading happens at use time, not at validation, so merely loading a config never shells out or
    requires the environment to be populated. The literal is a ``SecretStr``, so it is masked in
    ``repr`` and in pydantic validation errors — a config object can be logged without leaking the
    credential.
    """

    # populate_by_name so the model still round-trips through `model_validate(model_dump())`, which
    # dumps by field name rather than by the `value` alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    value_from_value: SecretStr | None = Field(default=None, alias="value")
    value_from_cmd: str | None = None
    value_from_env: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Secret:
        # `is not None` rather than truthiness, so an intentionally empty `value = ""` counts as
        # provided instead of silently falling through to another source.
        provided = [
            field
            for field in (self.value_from_value, self.value_from_cmd, self.value_from_env)
            if field is not None
        ]
        if len(provided) != 1:
            raise ValueError("set exactly one of 'value', 'value_from_cmd', or 'value_from_env'")
        return self

    @cached_property
    def value(self) -> str:
        """The secret itself, whitespace-stripped, from whichever source is configured.

        Read once per instance and cached, so a consumer may touch this freely (e.g. per fetch)
        without putting a subprocess on the hot path every interval. A *failed* read is not cached,
        so a transient failure is retried on next access. The tradeoff of caching is that a secret
        rotated underneath a running process is not picked up until it restarts. The cached value
        stays out of ``repr`` and ``model_dump()`` (pydantic renders declared fields only).

        Raises ``RuntimeError`` if the command fails or times out, or the variable is unset.
        Stripping is uniform across all three sources: a credential is never meant to carry
        surrounding whitespace, and a stray newline (``export K=$(cat file)``) silently breaks an
        auth header.
        """
        if self.value_from_value is not None:
            return self.value_from_value.get_secret_value().strip()
        if self.value_from_cmd is not None:
            return _run_secret_cmd(self.value_from_cmd)
        if self.value_from_env is not None:
            try:
                return os.environ[self.value_from_env].strip()
            except KeyError as exc:
                raise RuntimeError(
                    f"value_from_env: environment variable {self.value_from_env!r} is not set"
                ) from exc
        # Unreachable: the validator guarantees exactly one source. Explicit rather than `assert`,
        # which `python -O` strips (leaving a confusing TypeError further down instead).
        raise RuntimeError("Secret has no source configured")


def _run_secret_cmd(command: str) -> str:
    """Run ``command`` in a shell and return its stripped stdout."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"value_from_cmd timed out after {_CMD_TIMEOUT_SECONDS}s: {command!r} "
            "(an interactive prompt cannot be answered here)"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"value_from_cmd failed ({result.returncode}): {command!r}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


# How the rendered image is fitted to the Kindle's exact pixel dimensions:
#   resize -- stretch to fill, ignoring aspect (minor distortion)
#   crop   -- scale to cover, center-crop the excess (no distortion, trims a sliver)
#   pad    -- scale to fit, add white e-ink bars (nothing cropped or distorted)
PostProcessMethod = Literal["resize", "crop", "pad"]


class Dashboard(BaseModel):
    """One output: which layout draws it, where it's written, and the Kindle output spec.

    The dashboard owns the *output* (path, resolution, post-processing); the layout owns *how it
    draws* — its own config, validated per-plugin from ``layout_config`` (see docs/plugins.md), so
    render knobs like the font or display units live there, not here.
    """

    model_config = ConfigDict(extra="forbid")

    layout: str = "glanceable"  # registered layout plugin (see docs/plugins.md)
    output_path: Path  # where the finished PNG is written
    width: int = 1072  # Kindle Voyage, portrait (native orientation)
    height: int = 1448
    gray_levels: int = 16
    post_process_method: PostProcessMethod = "resize"
    # rotate the final image 90° before writing (for a physically rotated device)
    rotate: bool = False
    # Raw table validated by the selected layout's own Config model (extra keys rejected there).
    layout_config: dict[str, Any] = {}


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
