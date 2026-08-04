"""MX record lookups.

Answers one question: does this domain accept mail at all? That is as far as
verification goes here -- probing individual mailboxes over SMTP gets servers
blocklisted and is not something this tool does.

Results are cached in memory for the run and in the ``mx_cache`` table across
runs, so re-verifying a 10k-row database costs one lookup per *domain*.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.config import AppConfig, get_config
from core.constants import LogCategory
from core.logging_setup import get_logger
from database.repository import MXCacheRepository

log = get_logger(LogCategory.DATABASE)

try:  # pragma: no cover - exercised by the import-failure path only
    import dns.exception
    import dns.resolver

    DNS_AVAILABLE = True
except ImportError:  # pragma: no cover
    DNS_AVAILABLE = False


class MXChecker:
    """Cached MX resolver.

    ``session`` is optional: without it the persistent cache is skipped and
    only the per-instance memo applies (handy in tests).
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        session: Session | None = None,
    ) -> None:
        self.config = config or get_config()
        self.session = session
        self._memo: dict[str, bool] = {}
        self._db_cache = MXCacheRepository(session) if session is not None else None
        self._resolver = None
        if DNS_AVAILABLE:
            self._resolver = dns.resolver.Resolver()
            self._resolver.timeout = self.config.verifier.mx_timeout
            self._resolver.lifetime = self.config.verifier.mx_timeout

    def has_mx(self, domain: str | None) -> bool:
        """True when ``domain`` publishes MX (or fallback A) records."""
        if not domain:
            return False
        key = domain.strip().lower().rstrip(".")
        if not key:
            return False

        if key in self._memo:
            return self._memo[key]

        if self._db_cache is not None:
            cached = self._db_cache.lookup(key, self.config.verifier.mx_cache_hours)
            if cached is not None:
                self._memo[key] = cached
                return cached

        result = self._resolve(key)
        self._memo[key] = result
        if self._db_cache is not None:
            self._db_cache.store(key, result)
        return result

    def _resolve(self, domain: str) -> bool:
        if not DNS_AVAILABLE or self._resolver is None:
            log.warning("dnspython is not installed; skipping MX check for {}", domain)
            return False
        try:
            answers = self._resolver.resolve(domain, "MX")
            if len(answers) > 0:
                return True
        except dns.resolver.NoAnswer:
            pass  # no MX -> fall through to the implicit-A rule below
        except dns.resolver.NXDOMAIN:
            log.debug("domain does not exist: {}", domain)
            return False
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            # A transient DNS failure is not evidence the domain is bad, but we
            # have no better answer to give; do not cache a maybe as a no.
            log.warning("MX lookup for {} failed: {}", domain, exc)
            raise MXLookupUnavailable(domain) from exc
        except dns.exception.DNSException as exc:  # pragma: no cover
            log.warning("MX lookup error for {}: {}", domain, exc)
            return False

        # RFC 5321 §5.1: a domain with an A/AAAA record but no MX still accepts
        # mail at that address.
        for record_type in ("A", "AAAA"):
            try:
                if len(self._resolver.resolve(domain, record_type)) > 0:
                    return True
            except dns.exception.DNSException:
                continue
        return False

    def close(self) -> None:
        self._memo.clear()


class MXLookupUnavailable(Exception):
    """DNS was unreachable, so no verdict could be reached for the domain."""

    def __init__(self, domain: str) -> None:
        super().__init__(f"MX lookup unavailable for {domain}")
        self.domain = domain
