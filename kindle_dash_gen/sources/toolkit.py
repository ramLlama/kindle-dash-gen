"""Public surface a source plugin builds on.

Kept separate from the registry (mirroring :mod:`kindle_dash_gen.render.toolkit`) so a plugin
depends only on the stable error type, not on registry internals.
"""

from __future__ import annotations

import typer
from pydantic import BaseModel

# Re-exported so a source's Config can type a credential field as ``Secret`` without reaching into
# app config internals (see docs/sources.md).
from ..config import Secret, load_config

__all__ = ["Secret", "SourceError", "source_config"]


def source_config[ConfigT: BaseModel](
    ctx: typer.Context, name: str, config_cls: type[ConfigT]
) -> ConfigT:
    """This source's own validated ``[sources.<name>]`` table, for use inside its ``cli()`` verbs.

    A source's CLI verbs reach the same config the rest of the app uses, so a verb needing a
    credential or a station list reads it from the operator's config file rather than re-taking it
    as a flag. Takes the ``typer.Context`` that the global ``--config`` is recorded on, so a verb
    just declares ``ctx: typer.Context`` as its first parameter.

    Only this source's slice is validated: inspecting one source shouldn't fail because an
    unrelated source or dashboard is misconfigured.
    """
    raw = load_config(ctx.obj).sources.get(name)
    if raw is None:
        raise typer.BadParameter(f"source {name!r} is not configured in {ctx.obj}")
    return config_cls.model_validate(raw)


class SourceError(RuntimeError):
    """A source could not fetch its data.

    Every source-specific error (e.g. the NWS or MTA fetcher's) subclasses this, so the pipeline
    can isolate any one source generically: a ``SourceError`` drops that source's data and the
    render proceeds with whatever else was gathered.
    """
