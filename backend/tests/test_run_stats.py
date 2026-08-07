"""Tests for `app.features.crawl.internals.run_stats` — the pure, no-I/O module that builds
the exact `dict` `runs.stats` stores.

No database, no network: `build_run_stats` takes a `Mapping` and returns a `dict`, so
everything here is plain in-memory assertions, the same category `tests/test_crawl_payload.py`
beside it is in.
"""

from app.features.crawl.internals.run_stats import RUN_STATS_VERSION, build_run_stats


def test_run_stats_version_is_5() -> None:
    """PER-180 bumped this from 4 to 5 when `pages_enriched`, `enrich_failures`,
    `enrich_input_tokens` and `enrich_output_tokens` joined the persisted shape — the four
    numbers `internals/enrich.py`'s flag-gated, model-assisted summarization pass reports
    about itself. See `RUN_STATS_VERSION`'s own docstring for the full history, including why
    all four are `0` (a real, recorded value) on a flag-off row rather than an absent key.

    Pinned here, directly, so a future change to the persisted shape has to bump this constant
    deliberately rather than by accident: `tests/test_run_persistence.py` only checks the
    version NUMBER a live row lands with, which would pass just as happily against a
    `RUN_STATS_VERSION` that was bumped again without anyone noticing this test existed."""
    assert RUN_STATS_VERSION == 5


def test_build_run_stats_passes_crawl_stats_through_unchanged_and_adds_ten_keys() -> None:
    """`crawl_stats` — including `pages_empty_content` — is spread into the result verbatim;
    `links_emitted`, `full_txt_truncated`, `discovery_source`, `urls_discovered`,
    `urls_selected`, `pages_enriched`, `enrich_failures`, `enrich_input_tokens`,
    `enrich_output_tokens`, and `version` are the only ten keys `build_run_stats` itself
    contributes."""
    crawl_stats = {
        "pages_crawled": 3,
        "pages_failed": 1,
        "bytes_fetched": 4_096,
        "duration_ms": 250,
        "cap_hit": None,
        "pages_empty_content": 2,
    }

    stats = build_run_stats(
        crawl_stats,
        links_emitted=1,
        full_txt_truncated=0,
        discovery_source="sitemap",
        urls_discovered=5,
        urls_selected=3,
        pages_enriched=1,
        enrich_failures=0,
        enrich_input_tokens=1200,
        enrich_output_tokens=40,
    )

    assert stats == {
        **crawl_stats,
        "links_emitted": 1,
        "full_txt_truncated": 0,
        "discovery_source": "sitemap",
        "urls_discovered": 5,
        "urls_selected": 3,
        "pages_enriched": 1,
        "enrich_failures": 0,
        "enrich_input_tokens": 1200,
        "enrich_output_tokens": 40,
        "version": RUN_STATS_VERSION,
    }


def test_links_emitted_is_recorded_as_passed_even_when_it_differs_from_pages_crawled() -> None:
    """The version-3 divergence, pinned at this layer: `build_run_stats` records what the
    artifact reported and never reconciles it against `pages_crawled`. A run that fetched three
    pages and found content on one stores exactly that, rather than a number this module
    derived for itself as `pages_crawled` minus `pages_empty_content` — see `build_run_stats`'
    own docstring for why that subtraction is not a valid substitute for asking the artifact."""
    crawl_stats = {"pages_crawled": 3, "pages_empty_content": 2}

    stats = build_run_stats(
        crawl_stats,
        links_emitted=1,
        full_txt_truncated=0,
        discovery_source="sitemap",
        urls_discovered=9,
        urls_selected=2,
        pages_enriched=0,
        enrich_failures=0,
        enrich_input_tokens=0,
        enrich_output_tokens=0,
    )

    assert stats["pages_crawled"] == 3
    assert stats["links_emitted"] == 1


def test_build_run_stats_leaves_the_crawl_loops_own_keys_intact() -> None:
    """Every key `crawl_stats` arrived with survives into the result with its original value,
    alongside the ten this module contributes.

    Deliberately NOT a collision test. `build_run_stats` spreads `{**crawl_stats, ...}`, so a
    `crawl_stats` that already carried one of the ten contributed keys would have that value
    OVERWRITTEN, not preserved — asserting otherwise here would be asserting the opposite of
    what the code does. The real guarantee, as `build_run_stats`' own docstring states, is
    that none of the ten is a key `CrawlResult.stats` has ever produced, which is a property
    of `internals/crawler.py` rather than of this function; `tests/test_crawler_caps.py` is
    where that side of it is pinned down."""
    crawl_stats = {"pages_crawled": 1, "pages_empty_content": 0}

    stats = build_run_stats(
        crawl_stats,
        links_emitted=1,
        full_txt_truncated=2,
        discovery_source="none",
        urls_discovered=0,
        urls_selected=0,
        pages_enriched=1,
        enrich_failures=1,
        enrich_input_tokens=100,
        enrich_output_tokens=10,
    )

    assert stats["pages_crawled"] == 1
    assert stats["pages_empty_content"] == 0
    assert stats["links_emitted"] == 1
    assert stats["full_txt_truncated"] == 2
    assert stats["discovery_source"] == "none"
    assert stats["urls_discovered"] == 0
    assert stats["urls_selected"] == 0
    assert stats["pages_enriched"] == 1
    assert stats["enrich_failures"] == 1
    assert stats["enrich_input_tokens"] == 100
    assert stats["enrich_output_tokens"] == 10
    assert stats["version"] == RUN_STATS_VERSION


def test_build_run_stats_carries_the_discovery_counters() -> None:
    """The three PER-176 keys land with exactly the values passed in — a narrower,
    single-purpose companion to the "adds ten keys" test above, named for the acceptance
    criterion it pins rather than for the mechanics of the dict spread.

    `urls_discovered` (7) and `urls_selected` (3) are deliberately unequal to each other and
    to `pages_crawled` (4): all three are separate measurements of a frontier at different
    stages — found, kept after ranking, and actually fetched — and a test that gave them the
    same value would pass just as happily against an implementation that confused two of
    them."""
    crawl_stats = {"pages_crawled": 4, "pages_empty_content": 0}

    stats = build_run_stats(
        crawl_stats,
        links_emitted=4,
        full_txt_truncated=0,
        discovery_source="robots",
        urls_discovered=7,
        urls_selected=3,
        pages_enriched=0,
        enrich_failures=0,
        enrich_input_tokens=0,
        enrich_output_tokens=0,
    )

    assert stats["discovery_source"] == "robots"
    assert stats["urls_discovered"] == 7
    assert stats["urls_selected"] == 3


def test_build_run_stats_carries_the_enrichment_counters() -> None:
    """The four PER-180 keys land with exactly the values passed in — the enrichment-layer
    counterpart to `test_build_run_stats_carries_the_discovery_counters` above. `pages_enriched`
    (7) and `enrich_failures` (2) deliberately do not sum to `pages_crawled` (10): the gap is
    exactly the pages enrichment skipped for having no text to send (`RUN_STATS_VERSION`'s own
    version-5 docstring), not a bug in this test's numbers."""
    crawl_stats = {"pages_crawled": 10, "pages_empty_content": 1}

    stats = build_run_stats(
        crawl_stats,
        links_emitted=9,
        full_txt_truncated=0,
        discovery_source="none",
        urls_discovered=0,
        urls_selected=0,
        pages_enriched=7,
        enrich_failures=2,
        enrich_input_tokens=8_400,
        enrich_output_tokens=320,
    )

    assert stats["pages_enriched"] == 7
    assert stats["enrich_failures"] == 2
    assert stats["enrich_input_tokens"] == 8_400
    assert stats["enrich_output_tokens"] == 320
