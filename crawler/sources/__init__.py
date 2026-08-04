"""Source registry.

``type:`` in ``config.yaml`` is looked up here. Registering a new source type
is a one-line call to :func:`register_source`, which keeps the pipeline free of
any knowledge of individual sites.
"""

from __future__ import annotations

from collections.abc import Callable

from core.config import AppConfig, SourceConfig, get_config
from core.errors import SourceConfigError
from crawler.base import BaseSource
from crawler.fetcher import BaseFetcher
from crawler.sources.generic_html import GenericHtmlSource
from crawler.sources.sample import SampleSource, sample_source_config

SourceFactory = Callable[[SourceConfig, BaseFetcher | None, AppConfig | None], BaseSource]

_REGISTRY: dict[str, SourceFactory] = {}


def register_source(type_name: str, factory: SourceFactory) -> None:
    """Register a source type. Re-registering a name replaces it."""
    _REGISTRY[type_name] = factory


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def build_source(
    source_config: SourceConfig,
    fetcher: BaseFetcher | None = None,
    config: AppConfig | None = None,
) -> BaseSource:
    """Instantiate the source described by ``source_config``."""
    factory = _REGISTRY.get(source_config.type)
    if factory is None:
        known = ", ".join(registered_types()) or "(none registered)"
        raise SourceConfigError(
            f"unknown source type {source_config.type!r} for source "
            f"{source_config.name!r}; known types: {known}"
        )
    return factory(source_config, fetcher, config or get_config())


register_source("sample", lambda sc, f, c: SampleSource(sc, f, c))
register_source("generic_html", lambda sc, f, c: GenericHtmlSource(sc, f, c))

__all__ = [
    "GenericHtmlSource",
    "SampleSource",
    "build_source",
    "register_source",
    "registered_types",
    "sample_source_config",
]
