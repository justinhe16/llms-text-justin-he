"""Tests for `app.features.crawl.internals.url_ranking` — ranking and selecting a crawl's
frontier from raw discovered URLs.

No database, no network, no clock: `select_urls` is pure, and every test here calls it
directly with hand-built or fixture-loaded `DiscoveredUrl` lists. `tests/test_url_normalize.py`
and `tests/test_llms_txt_stub.py` are the two closest siblings in shape — this suite follows
the same pattern: one test (or a parametrized group) per named behaviour, named for what it
asserts rather than how.
"""

import inspect
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.features.crawl.internals import url_ranking
from app.features.crawl.internals.url_ranking import (
    DiscoveredUrl,
    DiscoveredUrlSource,
    SelectionResult,
    select_urls,
)


SEED = "https://example.test"


def _url(
    url: str,
    *,
    lastmod: datetime | None = None,
    priority: float | None = None,
    source: DiscoveredUrlSource = "sitemap",
) -> DiscoveredUrl:
    return DiscoveredUrl(url=url, lastmod=lastmod, priority=priority, source=source)


def _dt(day: int, *, month: int = 1, year: int = 2025, tz: bool = True) -> datetime:
    value = datetime(year, month, day, tzinfo=UTC if tz else None)
    return value


def _assert_reconciles(result: SelectionResult) -> None:
    """The invariant the module docstring names: every candidate is selected or dropped
    under exactly one rule, never both, never neither."""
    assert result.discovered_count == len(result.selected) + sum(result.dropped.values())


# --- Purity ---------------------------------------------------------------------------------


def test_module_is_pure_no_async_no_io_no_clock() -> None:
    """Source-level assertion, deliberately: this directly encodes the acceptance criterion
    that this module never awaits, never opens a socket or a file, and never reads the
    clock. A behavioural test could not distinguish "doesn't need the clock for this input"
    from "cannot read the clock at all" — reading the source can.
    """
    source = inspect.getsource(url_ranking)

    assert "async " not in source
    assert "await " not in source
    assert "httpx" not in source
    assert "requests" not in source
    assert "socket" not in source
    assert "open(" not in source
    assert "Path(" not in source
    assert "datetime.now(" not in source
    assert ".utcnow(" not in source
    assert "time.time(" not in source
    assert "time.monotonic(" not in source
    assert "import time" not in source
    assert "app.core.settings" not in source


# --- Happy path -------------------------------------------------------------------------------


def test_documentation_paths_outrank_deep_and_noisy_pages() -> None:
    candidates = [
        _url(f"{SEED}/docs/getting-started"),
        _url(f"{SEED}/guide/deploy"),
        _url(f"{SEED}/reference/api/v2/objects/widget/fields/name"),  # doc-shaped but very deep
        _url(f"{SEED}/blog/2024/some-announcement-post-title"),  # noise: deep and not doc-shaped
        _url(f"{SEED}/about/team/leadership/bios/jane-doe"),  # noise: deep, not doc-shaped
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected[0] == f"{SEED}/docs/getting-started"
    assert result.selected[1] == f"{SEED}/guide/deploy"
    # Both noise pages, being deep and undated in a way that would slip past dated_archive
    # (no 4-digit year token) rank below every doc-shaped page.
    doc_pages = {f"{SEED}/docs/getting-started", f"{SEED}/guide/deploy"}
    noise_index = result.selected.index(f"{SEED}/about/team/leadership/bios/jane-doe")
    assert all(result.selected.index(page) < noise_index for page in doc_pages)
    _assert_reconciles(result)


def test_site_root_outranks_every_other_page() -> None:
    candidates = [
        _url(f"{SEED}/docs/intro"),
        _url(f"{SEED}/"),  # the root, discovered separately from the seed itself
    ]

    result = select_urls(candidates, seed_url=f"{SEED}/some/other/seed/path", limit=10)

    assert result.selected[0] == f"{SEED}/"
    _assert_reconciles(result)


def test_priority_breaks_ties_between_equally_shaped_pages() -> None:
    candidates = [
        _url(f"{SEED}/docs/a", priority=0.1),
        _url(f"{SEED}/docs/b", priority=0.9),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected == [f"{SEED}/docs/b", f"{SEED}/docs/a"]


def test_lastmod_recency_is_a_mild_tiebreaker_never_a_dominant_signal() -> None:
    """The ticket's own acceptance criterion, stated as a test: a stable, shallow reference
    page must not rank below a deep, freshly-edited one. `LASTMOD_WEIGHT` is small enough
    that no amount of recency spread overcomes even one extra level of path depth.
    """
    candidates = [
        _url(f"{SEED}/docs/stable-reference", lastmod=None),
        _url(f"{SEED}/misc/very/deeply/nested/freshly-edited-page", lastmod=_dt(1)),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected[0] == f"{SEED}/docs/stable-reference"


def test_recency_among_otherwise_identical_pages_favors_the_more_recent_one() -> None:
    candidates = [
        _url(f"{SEED}/docs/a", lastmod=_dt(1)),
        _url(f"{SEED}/docs/b", lastmod=_dt(15)),
        _url(f"{SEED}/docs/c", lastmod=_dt(30)),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected == [f"{SEED}/docs/c", f"{SEED}/docs/b", f"{SEED}/docs/a"]


def test_naive_and_timezone_aware_lastmod_values_compare_without_raising() -> None:
    """The landmine the ticket names explicitly: comparing a naive and an aware `datetime`
    raises `TypeError` unless every value is normalized first."""
    candidates = [
        _url(f"{SEED}/docs/aware", lastmod=_dt(1, tz=True)),
        _url(f"{SEED}/docs/naive", lastmod=_dt(15, tz=False)),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)  # must not raise

    assert result.selected == [f"{SEED}/docs/naive", f"{SEED}/docs/aware"]


def test_a_declared_priority_of_0_5_scores_identically_to_no_priority_at_all() -> None:
    candidates = [
        _url(f"{SEED}/docs/explicit-neutral", priority=0.5),
        _url(f"{SEED}/docs/no-priority", priority=None),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    # Same depth, same doc-shape, both scored neutrally on priority and lastmod: the only
    # remaining tie-break is the URL itself.
    assert result.selected == sorted(candidate.url for candidate in candidates)


# --- Each drop rule, on its own ----------------------------------------------------------------


def test_unparseable_urls_are_dropped_and_never_raise() -> None:
    candidates = [
        _url(f"{SEED}/docs/kept"),
        _url("ftp://example.test/file.zip"),
        _url("javascript:void(0)"),
        _url("not a url at all"),
        _url(""),
        _url(f"{SEED}:notaport/docs/x"),
        _url("https:///no-host-at-all"),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected == [f"{SEED}/docs/kept"]
    assert result.dropped["unparseable"] == 6


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(f"{SEED}/docs/intro", f"{SEED}/docs/intro/", id="trailing-slash"),
        pytest.param("https://EXAMPLE.test/docs/intro", f"{SEED}/docs/intro", id="host-case"),
        pytest.param(
            f"{SEED}/docs/intro?utm_source=newsletter",
            f"{SEED}/docs/intro",
            id="tracking-param",
        ),
        pytest.param(f"{SEED}/docs/intro#section", f"{SEED}/docs/intro", id="fragment"),
    ],
)
def test_duplicate_normalized_urls_keep_only_the_first_occurrence(first: str, second: str) -> None:
    candidates = [_url(first), _url(second)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert len(result.selected) == 1
    assert result.dropped["duplicate"] == 1
    _assert_reconciles(result)


def test_path_case_is_never_normalized_so_it_does_not_cause_a_false_duplicate() -> None:
    """Paths are case-sensitive on the servers that host them; only the host is
    case-folded (module docstring, step 1). `/DOCS/intro` and `/docs/intro` are two
    distinct URLs, not a duplicate."""
    candidates = [_url(f"{SEED}/DOCS/intro"), _url(f"{SEED}/docs/intro")]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert set(result.selected) == {f"{SEED}/DOCS/intro", f"{SEED}/docs/intro"}
    assert "duplicate" not in result.dropped


def test_seed_url_itself_is_dropped_and_never_selected() -> None:
    candidates = [
        _url(SEED),
        _url(f"{SEED}/"),
        _url(f"{SEED}/docs/intro"),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert SEED not in result.selected
    assert f"{SEED}/" not in result.selected
    assert result.dropped["seed"] >= 1
    _assert_reconciles(result)


@pytest.mark.parametrize(
    "off_origin_url",
    [
        pytest.param("https://other.test/docs/intro", id="different-host"),
        pytest.param("http://example.test/docs/intro", id="different-scheme"),
        pytest.param("https://example.test:8443/docs/intro", id="different-port"),
        pytest.param("https://www.example.test/docs/intro", id="www-is-a-different-host"),
    ],
)
def test_off_origin_urls_are_dropped(off_origin_url: str) -> None:
    candidates = [_url(f"{SEED}/docs/kept"), _url(off_origin_url)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert off_origin_url not in result.selected
    assert result.dropped["off_origin"] == 1


def test_off_origin_comparison_collapses_the_schemes_default_port() -> None:
    """`https://example.test` and `https://example.test:443` are the same origin — the same
    equality `websites/internals/url_normalize.py` already uses for `origin` — so a
    candidate spelled with the explicit default port is not dropped as `"off_origin"`. Its
    normalized URL keeps the port exactly as authored; only the origin *comparison*
    collapses it, per the module docstring's note on `_NormalizedCandidate.port`."""
    candidates = [_url(f"{SEED}:443/docs/intro")]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected == [f"{SEED}:443/docs/intro"]
    assert result.dropped == {}


@pytest.mark.parametrize(
    "dated_url",
    [
        pytest.param(f"{SEED}/2024/03/", id="year-then-month"),
        pytest.param(f"{SEED}/blog/2023/", id="nested-year-only"),
        pytest.param(f"{SEED}/news/1999/12/predictions", id="nineties"),
        pytest.param(f"{SEED}/archive/2099/report", id="upper-bound-of-range"),
    ],
)
def test_dated_archive_paths_are_dropped(dated_url: str) -> None:
    candidates = [_url(f"{SEED}/docs/kept"), _url(dated_url)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert dated_url not in result.selected
    assert result.dropped["dated_archive"] == 1


@pytest.mark.parametrize(
    "not_dated_url",
    [
        pytest.param(f"{SEED}/docs/v2024-release-notes", id="year-glued-to-other-text"),
        pytest.param(f"{SEED}/docs/2024release", id="year-not-its-own-segment"),
        pytest.param(f"{SEED}/pricing/2024a", id="not-purely-digits"),
    ],
)
def test_a_year_like_substring_that_is_not_its_own_segment_is_not_dated_archive(
    not_dated_url: str,
) -> None:
    """`_is_dated_archive` matches a whole path segment, not a substring — `2024release` is
    one segment, not a year followed by something else."""
    result = select_urls([_url(not_dated_url)], seed_url=SEED, limit=10)

    assert not_dated_url in result.selected


@pytest.mark.parametrize(
    "taxonomy_url",
    [
        pytest.param(f"{SEED}/tag/observability", id="tag"),
        pytest.param(f"{SEED}/tags/observability", id="tags"),
        pytest.param(f"{SEED}/category/tutorials", id="category"),
        pytest.param(f"{SEED}/categories/tutorials", id="categories"),
        pytest.param(f"{SEED}/author/jane-doe", id="author"),
        pytest.param(f"{SEED}/authors/jane-doe", id="authors"),
    ],
)
def test_taxonomy_listing_pages_are_dropped(taxonomy_url: str) -> None:
    candidates = [_url(f"{SEED}/docs/kept"), _url(taxonomy_url)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert taxonomy_url not in result.selected
    assert result.dropped["taxonomy"] == 1


@pytest.mark.parametrize(
    "paginated_url",
    [
        pytest.param(f"{SEED}/blog?page=2", id="query-param"),
        pytest.param(f"{SEED}/blog/page/3/", id="path-segment"),
        pytest.param(f"{SEED}/blog/page/3", id="path-segment-no-trailing-slash"),
    ],
)
def test_pagination_urls_are_dropped(paginated_url: str) -> None:
    candidates = [_url(f"{SEED}/docs/kept"), _url(paginated_url)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert paginated_url not in result.selected
    assert result.dropped["pagination"] == 1


@pytest.mark.parametrize(
    "feed_url",
    [
        pytest.param(f"{SEED}/feed", id="feed-segment"),
        pytest.param(f"{SEED}/blog/feed", id="nested-feed-segment"),
        pytest.param(f"{SEED}/rss.xml", id="rss-suffix"),
        pytest.param(f"{SEED}/atom.xml", id="atom-suffix"),
        pytest.param(f"{SEED}/blog/index.xml", id="xml-suffix"),
    ],
)
def test_feed_urls_are_dropped(feed_url: str) -> None:
    candidates = [_url(f"{SEED}/docs/kept"), _url(feed_url)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert feed_url not in result.selected
    assert result.dropped["feed"] == 1


@pytest.mark.parametrize(
    "changelog_url",
    [
        pytest.param(f"{SEED}/changelog", id="changelog"),
        pytest.param(f"{SEED}/change-log", id="change-log"),
        pytest.param(f"{SEED}/release-notes", id="release-notes"),
        pytest.param(f"{SEED}/release-notes/v3", id="nested-release-notes"),
    ],
)
def test_changelog_shaped_paths_are_dropped(changelog_url: str) -> None:
    candidates = [_url(f"{SEED}/docs/kept"), _url(changelog_url)]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert changelog_url not in result.selected
    assert result.dropped["changelog"] == 1


def test_changelog_rule_does_not_eat_docs_releases() -> None:
    """The false-positive risk the ticket calls out by name: `/docs/releases` is a reference
    page worth keeping, and shares no *whole* path segment with the narrow changelog
    vocabulary (`changelog`, `change-log`, `release-notes`)."""
    candidates = [
        _url(f"{SEED}/docs/releases"),
        _url(f"{SEED}/docs/releases/v2"),
        _url(f"{SEED}/reference/release-process"),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert set(result.selected) == {candidate.url for candidate in candidates}
    assert "changelog" not in result.dropped


def test_localized_duplicate_is_dropped_only_once_its_delocalized_page_is_selected() -> None:
    candidates = [
        _url(f"{SEED}/docs/intro"),
        _url(f"{SEED}/es/docs/intro"),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected == [f"{SEED}/docs/intro"]
    assert result.dropped == {"localized_duplicate": 1}


def test_localized_duplicate_does_not_fire_when_the_delocalized_page_was_not_selected() -> None:
    """`"localized_duplicate"` is relative to what was actually SELECTED, not merely
    discovered — if the non-localized page did not make the cut, the localized one is judged
    on its own merits rather than being dropped as if the other had won."""
    candidates = [
        _url(f"{SEED}/docs/a"),
        _url(f"{SEED}/es/docs/b"),  # a different page, not a translation of /docs/a
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert f"{SEED}/es/docs/b" in result.selected
    assert "localized_duplicate" not in result.dropped


@pytest.mark.parametrize(
    "generic_segment_url",
    [
        pytest.param(f"{SEED}/ui/components", id="ui-is-not-a-locale"),
        pytest.param(f"{SEED}/os/compatibility", id="os-is-not-a-locale"),
        pytest.param(f"{SEED}/js/sdk-reference", id="js-is-not-a-locale"),
    ],
)
def test_two_letter_path_segments_that_are_not_locales_are_never_treated_as_one(
    generic_segment_url: str,
) -> None:
    """The trap the ticket names explicitly: "any two-letter segment" would eat legitimate
    sections like `/ui/`, `/os/`, `/js/`. None of these are in the known locale vocabulary,
    so a page at `/docs/<same-leaf>` existing too must not cause either to be dropped."""
    leaf = generic_segment_url.rsplit("/", 1)[-1]
    candidates = [_url(generic_segment_url), _url(f"{SEED}/docs/{leaf}")]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert generic_segment_url in result.selected
    assert "localized_duplicate" not in result.dropped


def test_over_limit_counts_every_survivor_beyond_the_cap() -> None:
    candidates = [_url(f"{SEED}/docs/page-{i}") for i in range(5)]

    result = select_urls(candidates, seed_url=SEED, limit=2)

    assert len(result.selected) == 2
    assert result.dropped["over_limit"] == 3
    _assert_reconciles(result)


# --- Determinism -----------------------------------------------------------------------------


def _synthetic_candidates() -> list[DiscoveredUrl]:
    """A candidate set spanning every rule at least once, with no two candidates sharing a
    normalized URL — see `tests/fixtures/candidates_docs_site.json`'s generator for why
    duplicate-with-differing-metadata candidates are deliberately excluded from a shuffle
    test: which literal duplicate "wins" is order-dependent by the rule's own definition
    (module docstring), so a shuffle test needs a candidate set that never puts that
    order-dependence to the test.
    """
    return [
        _url(f"{SEED}/docs/getting-started", priority=0.9, lastmod=_dt(2)),
        _url(f"{SEED}/guide/deploy", priority=0.7, lastmod=_dt(20)),
        _url(f"{SEED}/reference/api", priority=None, lastmod=None),
        _url(f"{SEED}/api/webhooks", priority=0.4, lastmod=_dt(10, tz=False)),
        _url(f"{SEED}/getting-started/quickstart", priority=0.6),
        _url(f"{SEED}/blog/some-post"),
        _url(f"{SEED}/blog/another-post", priority=0.2),
        _url(f"{SEED}/es/docs/getting-started"),
        _url(f"{SEED}/fr/docs/getting-started"),
        _url(f"{SEED}/tag/observability"),
        _url(f"{SEED}/category/tutorials"),
        _url(f"{SEED}/blog/2023/announcement"),
        _url(f"{SEED}/blog?page=2"),
        _url(f"{SEED}/feed"),
        _url(f"{SEED}/changelog"),
        _url(f"{SEED}/docs/releases"),  # must survive the changelog rule
        _url("not a url"),
        _url("https://other.test/docs/intro"),
        _url(SEED),
        _url(f"{SEED}/"),
        _url(f"{SEED}/ui/components"),
    ]


def test_calling_twice_with_the_same_input_is_byte_identical() -> None:
    candidates = _synthetic_candidates()

    first = select_urls(candidates, seed_url=SEED, limit=6)
    second = select_urls(candidates, seed_url=SEED, limit=6)

    assert first == second


def test_shuffling_the_input_does_not_change_the_selection() -> None:
    candidates = _synthetic_candidates()
    shuffled = list(candidates)
    random.Random(1234).shuffle(shuffled)
    assert [c.url for c in shuffled] != [c.url for c in candidates]  # the shuffle did something

    baseline = select_urls(candidates, seed_url=SEED, limit=6)
    from_shuffled = select_urls(shuffled, seed_url=SEED, limit=6)

    assert baseline.selected == from_shuffled.selected
    assert baseline.dropped == from_shuffled.dropped


# --- Cap ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, 1, 3, 10, 1000])
def test_selected_never_exceeds_the_limit_and_never_contains_the_seed(limit: int) -> None:
    candidates = _synthetic_candidates()

    result = select_urls(candidates, seed_url=SEED, limit=limit)

    assert len(result.selected) <= limit
    assert SEED not in result.selected
    assert f"{SEED}/" not in result.selected


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_a_non_positive_limit_selects_nothing_without_raising(limit: int) -> None:
    result = select_urls(_synthetic_candidates(), seed_url=SEED, limit=limit)

    assert result.selected == []
    _assert_reconciles(result)


# --- Edges ---------------------------------------------------------------------------------------


def test_empty_candidates_returns_an_empty_result() -> None:
    result = select_urls([], seed_url=SEED, limit=10)

    assert result == SelectionResult(selected=[], discovered_count=0, dropped={})


def test_candidates_with_no_priority_or_lastmod_still_rank_sensibly_on_path_shape() -> None:
    candidates = [
        _url(f"{SEED}/docs/intro"),
        _url(f"{SEED}/products/enterprise/features/comparison/detailed-table"),
        _url(f"{SEED}/pricing"),
    ]

    result = select_urls(candidates, seed_url=SEED, limit=10)

    assert result.selected[0] == f"{SEED}/docs/intro"
    assert result.selected[-1] == (f"{SEED}/products/enterprise/features/comparison/detailed-table")


# --- Invariant -----------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, 1, 5, 12, 1000])
def test_every_candidate_is_selected_or_dropped_under_exactly_one_rule(limit: int) -> None:
    result = select_urls(_synthetic_candidates(), seed_url=SEED, limit=limit)

    _assert_reconciles(result)


def test_dropped_contains_no_zero_count_rules() -> None:
    result = select_urls(_synthetic_candidates(), seed_url=SEED, limit=6)

    assert all(count > 0 for count in result.dropped.values())


def test_dropped_keys_are_in_the_documented_pipeline_order() -> None:
    """`SelectionResult.dropped`'s keys follow the module docstring's fixed pipeline order —
    pre-filter rules in the order they are checked, then the two during-selection rules —
    regardless of what order candidates happened to trip them in."""
    pipeline_order = [
        "unparseable",
        "duplicate",
        "seed",
        "off_origin",
        "dated_archive",
        "taxonomy",
        "pagination",
        "feed",
        "changelog",
        "localized_duplicate",
        "over_limit",
    ]

    result = select_urls(_synthetic_candidates(), seed_url=SEED, limit=6)

    seen_in_order = [rule for rule in pipeline_order if rule in result.dropped]
    assert list(result.dropped.keys()) == seen_in_order


# --- Fixture ---------------------------------------------------------------------------------


def _load_fixture_candidates() -> list[DiscoveredUrl]:
    fixture_path = Path(__file__).parent / "fixtures" / "candidates_docs_site.json"
    raw = json.loads(fixture_path.read_text())
    return [
        DiscoveredUrl(
            url=item["url"],
            lastmod=datetime.fromisoformat(item["lastmod"]) if item["lastmod"] else None,
            priority=item["priority"],
            source=item["source"],
        )
        for item in raw
    ]


FIXTURE_SEED = "https://docs.fictional-cloud.test/"


def test_fixture_selection_favors_documentation_pages_and_respects_the_cap() -> None:
    candidates = _load_fixture_candidates()

    result = select_urls(candidates, seed_url=FIXTURE_SEED, limit=40)

    assert len(result.selected) == 40
    assert result.discovered_count == len(candidates)
    _assert_reconciles(result)

    # A reviewer-legible sanity check: the vast majority of the top 40 are doc-shaped paths.
    doc_segments = {"docs", "guide", "guides", "reference", "api", "getting-started"}
    doc_shaped = [
        url for url in result.selected if any(segment in doc_segments for segment in url.split("/"))
    ]
    assert len(doc_shaped) >= 35

    # The seed is never selected, no matter how it was spelled in the fixture.
    assert FIXTURE_SEED not in result.selected
    assert FIXTURE_SEED.rstrip("/") not in result.selected


def test_fixture_selection_exercises_every_drop_rule() -> None:
    candidates = _load_fixture_candidates()

    result = select_urls(candidates, seed_url=FIXTURE_SEED, limit=40)

    expected_rules = {
        "unparseable",
        "duplicate",
        "seed",
        "off_origin",
        "dated_archive",
        "taxonomy",
        "pagination",
        "feed",
        "changelog",
        "localized_duplicate",
        "over_limit",
    }
    assert expected_rules <= result.dropped.keys()
    assert all(count > 0 for count in result.dropped.values())


def test_fixture_selection_keeps_docs_releases_out_of_the_changelog_rule() -> None:
    candidates = _load_fixture_candidates()

    result = select_urls(candidates, seed_url=FIXTURE_SEED, limit=1000)

    assert "https://docs.fictional-cloud.test/docs/releases" in result.selected
    assert "https://docs.fictional-cloud.test/docs/releases/v2" in result.selected


def test_fixture_selection_is_deterministic_across_repeated_calls() -> None:
    candidates = _load_fixture_candidates()

    first = select_urls(candidates, seed_url=FIXTURE_SEED, limit=40)
    second = select_urls(candidates, seed_url=FIXTURE_SEED, limit=40)

    assert first == second
