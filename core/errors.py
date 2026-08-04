"""Exception hierarchy.

Every failure this application raises on purpose derives from :class:`CRMError`,
so callers (CLI, GUI) can distinguish "we knew this could happen" from a bug.
"""

from __future__ import annotations


class CRMError(Exception):
    """Base class for all deliberate application errors."""


class ConfigError(CRMError):
    """config.yaml is missing, malformed, or fails validation."""


class DatabaseError(CRMError):
    """Persistence layer failure."""


class CrawlError(CRMError):
    """Crawl could not complete."""


class RobotsDisallowedError(CrawlError):
    """robots.txt forbids the URL. Never caught-and-ignored by design."""

    def __init__(self, url: str, user_agent: str) -> None:
        super().__init__(f"robots.txt disallows {url} for user-agent {user_agent!r}")
        self.url = url
        self.user_agent = user_agent


class SourceConfigError(CrawlError):
    """A crawl source is misconfigured (bad selectors, missing URL, ...)."""


class ExportError(CRMError):
    """Export could not be written."""


class VerificationError(CRMError):
    """Contact verification failed for an unexpected reason."""


class GmailError(CRMError):
    """Gmail/IMAP connection or parsing failure."""


class BackupError(CRMError):
    """Backup or restore failure."""
