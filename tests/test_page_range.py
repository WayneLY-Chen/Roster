"""Tests for crawling a specific range of pages.

Two conventions have to hold together: ``page_end`` bounds the *last* page
collected, and ``page_start`` skips earlier ones. For link-based pagination
the earlier pages still have to be fetched (there is no addressable URL for
page 5), they simply are not collected from.
"""

from __future__ import annotations

import pytest

from core.config import SourceConfig
from crawler.sources.sample import SampleSource, sample_source_config


def _source(tmp_config, **overrides) -> SampleSource:
    config = sample_source_config().model_copy(update=overrides)
    return SampleSource(config, None, tmp_config)


def _pages(source: SampleSource) -> list[int]:
    return [batch.page_number for batch in source.iter_pages()]


def test_default_collects_every_page(tmp_config) -> None:
    assert _pages(_source(tmp_config)) == [1, 2]


def test_page_end_stops_early(tmp_config) -> None:
    assert _pages(_source(tmp_config, page_end=1)) == [1]


def test_page_start_skips_earlier_pages(tmp_config) -> None:
    assert _pages(_source(tmp_config, page_start=2)) == [2]


def test_explicit_single_page_range(tmp_config) -> None:
    assert _pages(_source(tmp_config, page_start=2, page_end=2)) == [2]


def test_range_beyond_available_pages_yields_nothing(tmp_config) -> None:
    assert _pages(_source(tmp_config, page_start=5)) == []


def test_max_pages_still_caps_a_wide_range(tmp_config) -> None:
    source = _source(tmp_config, page_start=1, page_end=99, max_pages=1)
    assert _pages(source) == [1]


# ------------------------------------------------------------------ validation


def test_page_end_below_page_start_is_rejected() -> None:
    with pytest.raises(Exception):
        SourceConfig.model_validate(
            {"name": "x", "type": "sample", "page_start": 5, "page_end": 2}
        )


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(1, None, None), (1, 1, 1), (2, 4, 3), (3, 3, 1)],
)
def test_page_count(start: int, end: int | None, expected: int | None) -> None:
    config = SourceConfig.model_validate(
        {"name": "x", "type": "sample", "page_start": start, "page_end": end}
    )
    assert config.page_count == expected


def test_page_limit_takes_the_smallest_of_every_cap(tmp_config) -> None:
    """The range, the source cap and the global cap all bound the run."""
    source = _source(tmp_config, page_start=1, page_end=8, max_pages=3)
    # tmp_config's global crawler.max_pages defaults to 10, so the source's
    # own cap of 3 is the binding one here.
    assert source.page_limit == 3


# -------------------------------------------------------------------- pipeline


def test_pipeline_range_override_does_not_mutate_the_source(tmp_config) -> None:
    from crawler.pipeline import _with_page_range

    original = sample_source_config()
    adjusted = _with_page_range(original, 2, 3)

    assert (adjusted.page_start, adjusted.page_end) == (2, 3)
    assert (original.page_start, original.page_end) == (1, None)


def test_pipeline_range_override_is_a_no_op_when_unset() -> None:
    from crawler.pipeline import _with_page_range

    original = sample_source_config()
    assert _with_page_range(original, None, None) is original
