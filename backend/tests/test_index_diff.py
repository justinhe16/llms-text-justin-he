"""Tests for `app.features.crawl.internals.index_diff` — diffing one run's `llms.txt` index
against the previous completed run's.

No database, no network, no clock: both `parse_index` and `build_index_diff` are pure, and
every test here calls them directly with hand-built strings or `CrawledPage` lists run through
the real `generate_llms_txt` (`tests/test_llms_txt.py`'s own generator). `tests/test_run_stats.py`
(pure-function, in-memory) and `tests/test_llms_txt.py` (format assertions, adversarial
metadata) are this suite's two closest siblings in shape.

**`test_parse_index_round_trips_generate_llms_txt` is the drift gate.** `parse_index` and
`_bullet` (`llms_txt.py`) are two descriptions of one format, and that test — with adversarial
input, not a self-consistency check — is the only thing tying them together. See its own
docstring and the cross-reference comment on `_bullet` itself.
"""

import random
from datetime import UTC, datetime

from app.features.crawl.internals import llms_txt
from app.features.crawl.internals.index_diff import (
    MAX_SAMPLE_TITLE_CHARS,
    SAMPLE_LIMIT,
    PreviousIndex,
    build_index_diff,
    parse_index,
)
from app.features.crawl.internals.llms_txt import generate_llms_txt
from app.features.crawl.schemas import CrawledPage


# Long enough to clear `internals/extract.py`'s `MIN_BODY_CHARS`, matching
# `tests/test_llms_txt.py`'s own `_BODY` — a page built with it is one the extractor would
# have accepted as non-empty, so it always survives into `generate_llms_txt`'s index.
_BODY = (
    "This paragraph stands in for a real page's extracted markdown. It is comfortably longer "
    "than the two hundred characters `extract.py` requires before it stops calling a page "
    "empty, so a page built with it is one the extractor would have accepted rather than one "
    "this suite merely asserts about."
)


def _page(
    url: str,
    *,
    title: str | None = None,
    description: str | None = None,
    markdown: str = _BODY,
) -> CrawledPage:
    """A `CrawledPage` with only the fields `generate_llms_txt` reads spelled out, mirroring
    `tests/test_llms_txt.py`'s own helper of the same name — `is_empty=False` always, since
    every test in this file is about a page that IS indexed."""
    return CrawledPage(
        url=url,
        status=200,
        title=title,
        content="",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_bytes=0,
        description=description,
        markdown=markdown,
        is_empty=False,
    )


def _previous(
    current_llms_txt: str,
    *,
    urls_discovered: int | None = 0,
    run_id: str = "11111111-1111-1111-1111-111111111111",
    completed_at: str | None = "2026-01-01T00:00:00+00:00",
) -> PreviousIndex:
    """A `PreviousIndex` for the previous side of a diff — the JSON-safe shape
    `RunService.get_previous_completed_index` would have handed `CrawlService._build_index_diff`
    in production, built here directly since this suite never touches `RunService`."""
    return PreviousIndex(
        run_id=run_id,
        completed_at=completed_at,
        llms_txt=current_llms_txt,
        urls_discovered=urls_discovered,
    )


# -----------------------------------------------------------------------------------------
# `parse_index` — the reverse parser, and the drift gate tying it to `_bullet`
# -----------------------------------------------------------------------------------------


def test_parse_index_round_trips_generate_llms_txt() -> None:
    """Adversarial metadata on both sides of a bullet: a title containing `[`, `]`, and a
    backslash; a URL containing a space, both parens, and a pre-existing `%2F` that must
    survive untouched (the reverse of exactly the five `_TARGET_ESCAPES` pairs, never
    `urllib.parse.unquote`). Hand-written expected tuples, not a self-consistency check —
    `parse_index(generate_llms_txt(pages)) == pages` would still pass if both sides of the
    round trip shared the same bug.
    """
    pages = [
        _page("https://example.test/docs/intro", title="Intro"),
        _page(
            "https://example.test/a (b)/c d?x=%2Fy",
            title="A [Note] \\ Title",
            description='A <desc> with "quotes" and [brackets]',
        ),
    ]

    output = generate_llms_txt(pages)
    entries = parse_index(output)

    actual = [(entry.section, entry.url, entry.title, entry.description) for entry in entries]
    assert actual == [
        ("Docs", "https://example.test/docs/intro", "Intro", None),
        (
            "A (b)",
            "https://example.test/a (b)/c d?x=%2Fy",
            "A [Note] \\ Title",
            'A <desc> with "quotes" and [brackets]',
        ),
    ]


def test_parse_index_returns_nothing_for_the_empty_document() -> None:
    assert parse_index(llms_txt._EMPTY_DOCUMENT) == []


# -----------------------------------------------------------------------------------------
# `build_index_diff` — the metrics
# -----------------------------------------------------------------------------------------


def test_a_trailing_slash_change_is_not_a_page_swap() -> None:
    """The named acceptance criterion: URL matching is normalized on `key`
    (`normalize_url`), so a trailing-slash-only change compares as the SAME page rather than
    one removed and a different one added."""
    previous_txt = generate_llms_txt([_page("https://example.test/docs/intro")])
    current_txt = generate_llms_txt([_page("https://example.test/docs/intro/")])

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0
    assert diff["pages_changed"] == 0


def test_a_title_change_on_the_same_url_is_a_change_not_an_add_and_a_remove() -> None:
    previous_txt = generate_llms_txt([_page("https://example.test/docs/intro", title="Old Title")])
    current_txt = generate_llms_txt([_page("https://example.test/docs/intro", title="New Title")])

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0
    assert diff["pages_changed"] == 1
    assert diff["changed_sample"] == [
        {"url": "https://example.test/docs/intro", "title": "New Title"}
    ]


def test_a_description_change_alone_counts_as_changed() -> None:
    previous_txt = generate_llms_txt(
        [_page("https://example.test/docs/intro", title="Intro", description="Old description.")]
    )
    current_txt = generate_llms_txt(
        [_page("https://example.test/docs/intro", title="Intro", description="New description.")]
    )

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0
    assert diff["pages_changed"] == 1


def test_samples_are_capped_at_ten_and_the_true_count_is_recorded() -> None:
    previous_txt = generate_llms_txt([_page("https://example.test/docs/seed")])
    added_pages = [_page(f"https://example.test/docs/page-{i:02d}") for i in range(25)]
    current_txt = generate_llms_txt([_page("https://example.test/docs/seed"), *added_pages])

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=26,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 25
    assert len(diff["added_sample"]) == SAMPLE_LIMIT == 10


def test_samples_are_deterministic_under_a_shuffled_input() -> None:
    """Samples are sorted by `key`, so the physical order bullets happen to appear in the
    ARTIFACT — which mirrors section order, then URL order within a section, never insertion
    order — cannot change which entries a sample contains or what order it lists them in.
    Two documents holding the same twenty-five bullets in a different physical order must
    produce byte-identical samples."""
    bullets = [f"- [Page {i}](https://example.test/docs/page-{i:02d})" for i in range(25)]
    shuffled_bullets = list(bullets)
    random.Random(0).shuffle(shuffled_bullets)
    assert shuffled_bullets != bullets, "the shuffle must actually reorder something"

    ordered_txt = "# Test\n\n> summary\n\n## Docs\n\n" + "\n".join(bullets) + "\n"
    shuffled_txt = "# Test\n\n> summary\n\n## Docs\n\n" + "\n".join(shuffled_bullets) + "\n"

    previous = _previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=0)

    ordered_diff = build_index_diff(
        current_llms_txt=ordered_txt,
        current_urls_discovered=25,
        previous=previous,
        previous_run_completed=True,
    )
    shuffled_diff = build_index_diff(
        current_llms_txt=shuffled_txt,
        current_urls_discovered=25,
        previous=previous,
        previous_run_completed=True,
    )

    assert ordered_diff["added_sample"] == shuffled_diff["added_sample"]


def test_sections_delta_lists_only_sections_that_changed() -> None:
    previous_txt = generate_llms_txt(
        [_page("https://example.test/docs/a"), _page("https://example.test/guide/a")]
    )
    current_txt = generate_llms_txt(
        [
            _page("https://example.test/docs/a"),
            _page("https://example.test/docs/b"),
            _page("https://example.test/guide/a"),
        ]
    )

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=3,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    # "Guide" holds one entry on both sides -- its count did not change, so it is absent
    # rather than present with a `0`, the same "only rules that actually fired" discipline
    # `SelectionResult.dropped` already holds itself to.
    assert diff["sections_delta"] == {"Docs": 1}


def test_selection_churn_ratio_is_none_when_both_indexes_are_empty() -> None:
    diff = build_index_diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=0,
        previous=_previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=0),
        previous_run_completed=True,
    )

    assert diff["selection_churn"] == 0
    assert diff["selection_churn_ratio"] is None


def test_urls_discovered_delta_is_none_when_the_previous_run_recorded_none() -> None:
    """A previous row that predates `RUN_STATS_VERSION` 4 has no `urls_discovered` of its
    own — `None`, not `0`, is what a delta against an unknown value must be."""
    diff = build_index_diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=10,
        previous=_previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=None),
        previous_run_completed=True,
    )

    assert diff["urls_discovered_delta"] is None


def test_a_first_run_block_carries_no_metrics() -> None:
    diff = build_index_diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=0,
        previous=None,
        previous_run_completed=None,
    )

    assert diff == {"state": "first_run", "previous_run_completed": None}
    assert "pages_added" not in diff


def test_previous_run_completed_is_threaded_through_both_states() -> None:
    first_run = build_index_diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=0,
        previous=None,
        previous_run_completed=False,
    )
    assert first_run["state"] == "first_run"
    assert first_run["previous_run_completed"] is False

    compared = build_index_diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=0,
        previous=_previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=0),
        previous_run_completed=True,
    )
    assert compared["state"] == "compared"
    assert compared["previous_run_completed"] is True


def test_llms_txt_bytes_delta_is_measured_in_utf8_bytes() -> None:
    """A CJK title makes a character count and a UTF-8 byte count disagree, which is exactly
    what pins this to `.encode()` rather than `len(str)`."""
    previous_txt = generate_llms_txt([_page("https://example.test/docs/a", title="A")])
    current_txt = generate_llms_txt([_page("https://example.test/docs/a", title="文档标题")])

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["llms_txt_bytes_delta"] == len(current_txt.encode()) - len(previous_txt.encode())
    assert diff["llms_txt_bytes_delta"] != len(current_txt) - len(previous_txt)


def test_sample_titles_are_truncated_at_the_documented_cap() -> None:
    long_title = "T" * (MAX_SAMPLE_TITLE_CHARS + 50)
    current_txt = generate_llms_txt(
        [_page("https://example.test/docs/long-title", title=long_title)]
    )

    diff = build_index_diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=0),
        previous_run_completed=True,
    )

    sample_title = diff["added_sample"][0]["title"]
    assert len(sample_title) == MAX_SAMPLE_TITLE_CHARS + 1, "the extra character is the ellipsis"
    assert sample_title.startswith("T" * MAX_SAMPLE_TITLE_CHARS)
