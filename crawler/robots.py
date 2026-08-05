"""robots.txt policy.

Fetched once per host and cached for the run. Failure handling follows the
conservative reading of RFC 9309:

* ``404``/``410`` -- no rules published, crawling is allowed.
* ``2xx``        -- parse and obey the rules.
* ``401``/``403`` -- access to the whole site is restricted; disallow.
* ``5xx`` or network failure -- unknown, so disallow rather than guess.

A ``Crawl-delay`` in robots.txt always wins over the configured delay when it
is longer. Politeness is a floor, not a target.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from core.constants import LogCategory
from core.logging_setup import get_logger

log = get_logger(LogCategory.CRAWL)


@dataclass(slots=True)
class HostRules:
    """Parsed rules for one scheme+host."""

    parser: RobotFileParser | None
    allow_all: bool
    deny_all: bool
    crawl_delay: float | None = None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all or self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)


class RobotsPolicy:
    """Per-host robots.txt cache.

    Set ``enabled=False`` only for sites you own; the crawler's default is on.
    """

    def __init__(
        self,
        user_agent: str,
        timeout: float = 10.0,
        enabled: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.enabled = enabled
        self._client = client
        self._owns_client = client is None
        self._cache: dict[str, HostRules] = {}

    # ------------------------------------------------------------------ api

    def can_fetch(self, url: str) -> bool:
        if not self.enabled:
            return True
        rules = self._rules_for(url)
        allowed = rules.can_fetch(self.user_agent, url)
        if not allowed:
            log.warning("robots.txt disallows {}", url)
        return allowed

    def crawl_delay(self, url: str) -> float | None:
        """Host's requested delay in seconds, if it publishes one."""
        if not self.enabled:
            return None
        return self._rules_for(url).crawl_delay

    def sitemaps(self, url: str) -> list[str]:
        """網站在 robots.txt 裡公告的 sitemap 網址。

        探索整個網站時這是最該先看的東西：網站自己列出「我有哪些頁面」，
        照著看比一頁一頁點連結去猜少了非常多次請求——對對方的伺服器也客氣得多。
        """
        rules = self._rules_for(url)
        if rules.parser is None:
            return []
        try:
            return list(rules.parser.site_maps() or [])
        except Exception:  # pragma: no cover - 壞掉的 robots.txt 不該讓探索中止
            return []

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "RobotsPolicy":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- internal

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    def _rules_for(self, url: str) -> HostRules:
        origin = self._origin(url)
        cached = self._cache.get(origin)
        if cached is not None:
            return cached

        rules = self._fetch(origin)
        self._cache[origin] = rules
        return rules

    def _fetch(self, origin: str) -> HostRules:
        robots_url = f"{origin}/robots.txt"
        try:
            response = self._http().get(robots_url)
        except httpx.HTTPError as exc:
            log.warning("could not fetch {}: {} -- treating host as disallowed", robots_url, exc)
            return HostRules(parser=None, allow_all=False, deny_all=True)

        if response.status_code in (401, 403):
            log.warning("{} returned {} -- treating host as disallowed",
                        robots_url, response.status_code)
            return HostRules(parser=None, allow_all=False, deny_all=True)

        if response.status_code >= 500:
            log.warning("{} returned {} -- treating host as disallowed",
                        robots_url, response.status_code)
            return HostRules(parser=None, allow_all=False, deny_all=True)

        if response.status_code >= 400:
            log.debug("{} returned {} -- no rules published, crawling allowed",
                      robots_url, response.status_code)
            return HostRules(parser=None, allow_all=True, deny_all=False)

        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.parse(response.text.splitlines())
        except Exception as exc:  # malformed robots.txt should not crash a run
            log.warning("could not parse {}: {} -- crawling allowed", robots_url, exc)
            return HostRules(parser=None, allow_all=True, deny_all=False)

        delay = None
        try:
            raw_delay = parser.crawl_delay(self.user_agent)
            delay = float(raw_delay) if raw_delay is not None else None
        except (TypeError, ValueError):  # pragma: no cover - defensive
            delay = None

        log.debug("robots.txt loaded for {} (crawl-delay={})", origin, delay)
        return HostRules(parser=parser, allow_all=False, deny_all=False, crawl_delay=delay)
