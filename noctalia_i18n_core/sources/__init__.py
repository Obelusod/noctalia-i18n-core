"""Source contract and built-in implementations."""

from __future__ import annotations

from typing import Protocol

from ..models import JsonValue, PollResult
from .noctalia import NoctaliaSource

__all__ = ["NoctaliaSource", "Source"]


class Source(Protocol):
    """Acquisition boundary for normalized translation data."""

    def poll(self, cursor: JsonValue | None, /) -> PollResult:
        """Collect normalized changes, a cursor, and any required source texts.

        ``None`` establishes an initial position without replaying history.
        Returned changes are unique and ordered from oldest to newest.
        Initial polls include the complete current source-language mapping
        (``en`` for Noctalia). Subsequent polls may include a replacement
        mapping; otherwise monitoring applies returned source-language changes
        to the stored mapping.
        Cursors from another source or project fail instead of silently
        rebaselining.
        Locale identifiers belong to the selected localization project.
        Change and editor URLs are optional metadata.
        """

        ...
