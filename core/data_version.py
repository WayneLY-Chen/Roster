"""A process-wide "something in the database changed" counter.

Switching to a page always calls its :meth:`~gui.pages.base.BasePage.on_show`,
even when nothing has moved underneath it since the last visit -- clicking a
sidebar button back and forth re-runs the page's query and refills its table
every single time. On the heavier pages (公司's 215-row table, 儀表板's
duplicate-group scan) that query-and-refill is most of the per-click cost.

Every place that actually writes to the database bumps this counter with
:func:`bump`. A page remembers the version it last rendered (see
``gui.pages.companies`` and ``gui.pages.dashboard``) and skips the requery
when the counter has not moved -- which is safe precisely because nothing
*could* have changed underneath it.
"""

from __future__ import annotations

_version = 0


def bump() -> int:
    """Call this right after a write a page's ``on_show`` might otherwise redo."""
    global _version
    _version += 1
    return _version


def current() -> int:
    """The version a page should compare its own last-rendered version against."""
    return _version
