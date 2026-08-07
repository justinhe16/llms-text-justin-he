"""Tests for `app.features.crawl.internals.run_stats` — the pure, no-I/O module that builds
the exact `dict` `runs.stats` stores.

No database, no network: `build_run_stats` takes a `Mapping` and returns a `dict`, so
everything here is plain in-memory assertions, the same category `tests/test_crawl_payload.py`
beside it is in.
"""

from app.features.crawl.internals.run_stats import RUN_STATS_VERSION, build_run_stats


def test_run_stats_version_is_2() -> None:
    """PER-177 bumped this from 1 to 2 the moment `pages_empty_content` became a key every
    freshly-built `runs.stats` row carries — see `RUN_STATS_VERSION`'s own docstring for what
    distinguishes a version-1 row from a version-2 one. Pinned here, directly, so a future
    change to the persisted shape has to bump this constant deliberately rather than by
    accident: `tests/test_run_persistence.py` only checks the version NUMBER a live row
    lands with, which would pass just as happily against a `RUN_STATS_VERSION` that was
    bumped again without anyone noticing this test existed."""
    assert RUN_STATS_VERSION == 2


def test_build_run_stats_passes_crawl_stats_through_unchanged_and_adds_two_keys() -> None:
    """`crawl_stats` — including `pages_empty_content`, `CrawlResult.stats`'s newest key — is
    spread into the result verbatim; `links_emitted` and `version` are the only two keys
    `build_run_stats` itself contributes."""
    crawl_stats = {
        "pages_crawled": 3,
        "pages_failed": 1,
        "bytes_fetched": 4_096,
        "duration_ms": 250,
        "cap_hit": None,
        "pages_empty_content": 2,
    }

    stats = build_run_stats(crawl_stats, links_emitted=3)

    assert stats == {
        **crawl_stats,
        "links_emitted": 3,
        "version": RUN_STATS_VERSION,
    }


def test_build_run_stats_leaves_the_crawl_loops_own_keys_intact() -> None:
    """Every key `crawl_stats` arrived with survives into the result with its original value,
    alongside the two this module contributes.

    Deliberately NOT a collision test. `build_run_stats` spreads `{**crawl_stats, ...}`, so a
    `crawl_stats` that already carried `links_emitted` or `version` would have that value
    OVERWRITTEN, not preserved — asserting otherwise here would be asserting the opposite of
    what the code does. The real guarantee, as `build_run_stats`' own docstring states, is
    that neither name is one `CrawlResult.stats` has ever produced, which is a property of
    `internals/crawler.py` rather than of this function; `tests/test_crawler_caps.py` is where
    that side of it is pinned down."""
    crawl_stats = {"pages_crawled": 1, "pages_empty_content": 0}

    stats = build_run_stats(crawl_stats, links_emitted=1)

    assert stats["pages_crawled"] == 1
    assert stats["pages_empty_content"] == 0
    assert stats["links_emitted"] == 1
    assert stats["version"] == RUN_STATS_VERSION
