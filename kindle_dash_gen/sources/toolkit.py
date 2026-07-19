"""Public surface a source plugin builds on.

Kept separate from the registry (mirroring :mod:`kindle_dash_gen.render.toolkit`) so a plugin
depends only on the stable error type, not on registry internals.
"""

from __future__ import annotations

# Re-exported so a source's Config can type a credential field as ``Secret`` without reaching into
# app config internals (see docs/sources.md).
from ..config import Secret

__all__ = ["Secret", "SourceError"]


class SourceError(RuntimeError):
    """A source could not fetch its data.

    Every source-specific error (e.g. the NWS or MTA fetcher's) subclasses this, so the pipeline
    can isolate any one source generically: a ``SourceError`` drops that source's data and the
    render proceeds with whatever else was gathered.
    """
