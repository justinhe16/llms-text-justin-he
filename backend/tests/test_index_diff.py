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

**PER-194 retrofit.** Every `build_index_diff` call below now passes `current_enrich_applied`
and `current_content_hashes`, and every `_previous` helper call threads the matching
`enrich_applied`/`content_hashes` fields — both default to the "same mode, no hashes recorded"
shape (`enrich_applied=False`, `content_hashes=None`) so every test written before this ticket
keeps asserting exactly what it always did, with `pages_changed`/`changed_sample` renamed to
`metadata_changed`/`metadata_changed_sample` in place. The mode-change and content-hash tests
at the bottom of this file are the new acceptance criteria this ticket adds.
"""

import random
from datetime import UTC, datetime

from app.features.crawl.internals import llms_txt
from app.features.crawl.internals.index_diff import (
    MAX_SAMPLE_TITLE_CHARS,
    SAMPLE_LIMIT,
    PreviousIndex,
    build_content_hashes,
    build_index_diff,
    parse_index,
)
from app.features.crawl.internals.llms_txt import generate_llms_txt
from app.features.crawl.internals.url_ranking import normalize_url
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
    is_empty: bool = False,
) -> CrawledPage:
    """A `CrawledPage` with only the fields `generate_llms_txt`/`build_content_hashes` read
    spelled out, mirroring `tests/test_llms_txt.py`'s own helper of the same name —
    `is_empty=False` by default, since most tests in this file are about a page that IS
    indexed; the content-hash tests below override it to prove `build_content_hashes` never
    reads it."""
    return CrawledPage(
        url=url,
        status=200,
        title=title,
        content="",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_bytes=0,
        description=description,
        markdown=markdown,
        is_empty=is_empty,
    )


def _previous(
    current_llms_txt: str,
    *,
    urls_discovered: int | None = 0,
    run_id: str = "11111111-1111-1111-1111-111111111111",
    completed_at: str | None = "2026-01-01T00:00:00+00:00",
    enrich_applied: bool | None = False,
    content_hashes: dict[str, str] | None = None,
) -> PreviousIndex:
    """A `PreviousIndex` for the previous side of a diff — the JSON-safe shape
    `RunService.get_previous_completed_index` would have handed `CrawlService._build_index_diff`
    in production, built here directly since this suite never touches `RunService`.

    `enrich_applied` defaults to `False` and `content_hashes` to `None` — "the previous run
    was in the same mode as a not-yet-enriching current run, and recorded no content hashes"
    — which is what keeps every call site written before PER-194 asserting exactly what it
    always did."""
    return PreviousIndex(
        run_id=run_id,
        completed_at=completed_at,
        llms_txt=current_llms_txt,
        urls_discovered=urls_discovered,
        enrich_applied=enrich_applied,
        content_hashes=content_hashes,
    )


def _diff(
    *,
    current_llms_txt: str,
    current_urls_discovered: int,
    previous: PreviousIndex | None,
    previous_run_completed: bool | None,
    current_enrich_applied: bool = False,
    current_content_hashes: dict[str, str] | None = None,
):
    """Thin wrapper over `build_index_diff` supplying the two PER-194 arguments with the same
    "not enriching, no hashes" default `_previous` uses — every pre-existing test in this file
    calls through this rather than `build_index_diff` directly, so the mode/hash plumbing is
    one edit rather than one per call site."""
    return build_index_diff(
        current_llms_txt=current_llms_txt,
        current_urls_discovered=current_urls_discovered,
        current_enrich_applied=current_enrich_applied,
        current_content_hashes=current_content_hashes or {},
        previous=previous,
        previous_run_completed=previous_run_completed,
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

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0
    assert diff["metadata_changed"] == 0


def test_a_title_change_on_the_same_url_is_a_change_not_an_add_and_a_remove() -> None:
    previous_txt = generate_llms_txt([_page("https://example.test/docs/intro", title="Old Title")])
    current_txt = generate_llms_txt([_page("https://example.test/docs/intro", title="New Title")])

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0
    assert diff["metadata_changed"] == 1
    assert diff["metadata_changed_sample"] == [
        {"url": "https://example.test/docs/intro", "title": "New Title"}
    ]
    assert diff["metadata_not_comparable_reason"] is None


def test_a_description_change_alone_counts_as_changed() -> None:
    previous_txt = generate_llms_txt(
        [_page("https://example.test/docs/intro", title="Intro", description="Old description.")]
    )
    current_txt = generate_llms_txt(
        [_page("https://example.test/docs/intro", title="Intro", description="New description.")]
    )

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(previous_txt),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0
    assert diff["metadata_changed"] == 1


def test_samples_are_capped_at_ten_and_the_true_count_is_recorded() -> None:
    previous_txt = generate_llms_txt([_page("https://example.test/docs/seed")])
    added_pages = [_page(f"https://example.test/docs/page-{i:02d}") for i in range(25)]
    current_txt = generate_llms_txt([_page("https://example.test/docs/seed"), *added_pages])

    diff = _diff(
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

    ordered_diff = _diff(
        current_llms_txt=ordered_txt,
        current_urls_discovered=25,
        previous=previous,
        previous_run_completed=True,
    )
    shuffled_diff = _diff(
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

    diff = _diff(
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
    diff = _diff(
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
    diff = _diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=10,
        previous=_previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=None),
        previous_run_completed=True,
    )

    assert diff["urls_discovered_delta"] is None


def test_a_first_run_block_carries_no_metrics() -> None:
    diff = _diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=0,
        previous=None,
        previous_run_completed=None,
    )

    assert diff == {"state": "first_run", "previous_run_completed": None}
    assert "pages_added" not in diff


def test_previous_run_completed_is_threaded_through_both_states() -> None:
    first_run = _diff(
        current_llms_txt=llms_txt._EMPTY_DOCUMENT,
        current_urls_discovered=0,
        previous=None,
        previous_run_completed=False,
    )
    assert first_run["state"] == "first_run"
    assert first_run["previous_run_completed"] is False

    compared = _diff(
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

    diff = _diff(
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

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        previous=_previous(llms_txt._EMPTY_DOCUMENT, urls_discovered=0),
        previous_run_completed=True,
    )

    sample_title = diff["added_sample"][0]["title"]
    assert len(sample_title) == MAX_SAMPLE_TITLE_CHARS + 1, "the extra character is the ellipsis"
    assert sample_title.startswith("T" * MAX_SAMPLE_TITLE_CHARS)


# -----------------------------------------------------------------------------------------
# `build_content_hashes` (PER-194)
# -----------------------------------------------------------------------------------------


def test_content_hashes_are_keyed_by_the_same_normalized_form_index_entries_use() -> None:
    """The exact-join acceptance criterion: `build_content_hashes`' key and `IndexEntry.key`
    must be the SAME string for the same underlying page, even when the two URLs that produced
    them differ syntactically (a trailing slash here, a tracking parameter there)."""
    page = _page("https://example.com/a/")
    listed_txt = (
        "# Example\n\n> 1 page indexed for https://example.com.\n\n"
        "## Docs\n\n- [A](https://example.com/a?utm_source=x)\n"
    )

    hashes = build_content_hashes([page])
    (entry,) = parse_index(listed_txt)

    assert list(hashes.keys()) == [entry.key]


def test_content_changed_counts_only_keys_whose_body_hash_differs() -> None:
    previous_page = _page("https://example.test/docs/a", markdown=_BODY)
    current_page_changed = _page("https://example.test/docs/a", markdown=_BODY + " Changed body.")
    unchanged_page_previous = _page("https://example.test/docs/b", markdown=_BODY)
    unchanged_page_current = _page("https://example.test/docs/b", markdown=_BODY)

    previous_txt = generate_llms_txt([previous_page, unchanged_page_previous])
    current_txt = generate_llms_txt([current_page_changed, unchanged_page_current])

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=2,
        current_content_hashes=build_content_hashes([current_page_changed, unchanged_page_current]),
        previous=_previous(
            previous_txt,
            content_hashes=build_content_hashes([previous_page, unchanged_page_previous]),
        ),
        previous_run_completed=True,
    )

    assert diff["content_changed"] == 1
    # No `title=` was given, so `generate_llms_txt` falls back to a label derived from the
    # URL itself — not `None`, which is only ever what `IndexEntry.title` is typed to allow
    # in principle (see that field's own docstring).
    assert diff["content_changed_sample"] == [{"url": "https://example.test/docs/a", "title": "A"}]
    # Metadata is untouched on both sides — this is a body-only change.
    assert diff["metadata_changed"] == 0


def test_content_changed_is_none_when_the_previous_run_recorded_no_hashes() -> None:
    """A previous row that predates `RUN_STATS_VERSION` 8 has no `content_hashes` of its own —
    `None`, not `0`, is what a comparison against an unknown map must report."""
    page = _page("https://example.test/docs/a")
    txt = generate_llms_txt([page])

    diff = _diff(
        current_llms_txt=txt,
        current_urls_discovered=1,
        current_content_hashes=build_content_hashes([page]),
        previous=_previous(txt, content_hashes=None),
        previous_run_completed=True,
    )

    assert diff["content_changed"] is None
    assert diff["content_changed_sample"] == []


def test_content_hashes_skip_a_page_with_no_markdown_and_never_read_is_empty() -> None:
    """Mirrors `tests/test_crawl_enrich.py`'s
    `test_whether_a_page_is_sent_depends_on_markdown_never_on_is_empty` — the two signals
    disagree in both directions, and `build_content_hashes` must follow `markdown` alone."""
    empty_markdown_not_flagged = _page("https://example.test/shell", markdown="   ", is_empty=False)
    flagged_but_has_markdown = _page("https://example.test/flagged", markdown=_BODY, is_empty=True)

    hashes = build_content_hashes([empty_markdown_not_flagged, flagged_but_has_markdown])

    assert normalize_url("https://example.test/shell") not in hashes
    assert normalize_url("https://example.test/flagged") in hashes


def test_extra_hash_keys_with_no_index_entry_never_join() -> None:
    """`build_content_hashes`' key set is a superset of the index — a page whose markdown is
    non-empty but whose `is_empty` extraction flag kept it out of `generate_llms_txt`'s index
    (a shorter body than `MIN_BODY_CHARS`, in the real pipeline) must not silently widen
    `content_changed`'s denominator."""
    indexed_previous = _page("https://example.test/docs/a", markdown=_BODY)
    indexed_current = _page("https://example.test/docs/a", markdown=_BODY)
    # Has real markdown (so it gets a hash) but is never listed in either `llms.txt` — the
    # honest stand-in for "not chosen for the index," since this suite's `generate_llms_txt`
    # calls list only the pages passed to them.
    extra_previous = _page("https://example.test/uncounted", markdown=_BODY + " extra one.")
    extra_current = _page("https://example.test/uncounted", markdown=_BODY + " extra two.")

    previous_txt = generate_llms_txt([indexed_previous])
    current_txt = generate_llms_txt([indexed_current])

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        current_content_hashes=build_content_hashes([indexed_current, extra_current]),
        previous=_previous(
            previous_txt,
            content_hashes=build_content_hashes([indexed_previous, extra_previous]),
        ),
        previous_run_completed=True,
    )

    # The extra key's hash differs on both sides, but it names no index entry, so it must
    # never surface as a "changed" page.
    assert diff["content_changed"] == 0


# -----------------------------------------------------------------------------------------
# The enrichment-mode retrofit (PER-194)
# -----------------------------------------------------------------------------------------


def test_a_mode_change_still_reports_a_genuine_content_change() -> None:
    """The named "suppression is not wholesale" regression test. A mode flip
    (`enrich_applied` False -> True) makes `metadata_changed` not-comparable, but every other
    signal — added, removed, `urls_discovered_delta`, `content_changed`, `sections_delta`,
    `selection_churn`/`selection_churn_ratio` — still reports a real number."""
    kept_previous = _page("https://example.test/docs/kept", markdown=_BODY, title="Kept")
    kept_current = _page(
        "https://example.test/docs/kept", markdown=_BODY + " A genuine body change.", title="Kept"
    )
    removed_page = _page("https://example.test/docs/removed", markdown=_BODY, title="Removed")
    added_page = _page("https://example.test/guide/added", markdown=_BODY, title="Added")

    previous_txt = generate_llms_txt([kept_previous, removed_page])
    current_txt = generate_llms_txt([kept_current, added_page])

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=5,
        current_enrich_applied=True,
        current_content_hashes=build_content_hashes([kept_current, added_page]),
        previous=_previous(
            previous_txt,
            urls_discovered=2,
            enrich_applied=False,
            content_hashes=build_content_hashes([kept_previous, removed_page]),
        ),
        previous_run_completed=True,
    )

    assert diff["pages_added"] == 1
    assert diff["pages_removed"] == 1
    assert diff["content_changed"] == 1
    assert isinstance(diff["urls_discovered_delta"], int)
    assert diff["urls_discovered_delta"] == 3
    assert diff["sections_delta"] != {}
    assert diff["selection_churn"] == 2
    assert isinstance(diff["selection_churn_ratio"], float)

    assert diff["metadata_changed"] is None
    assert diff["metadata_changed_sample"] == []
    assert diff["metadata_not_comparable_reason"] == "enrichment_enabled"


def test_two_runs_in_the_same_mode_diff_exactly_as_they_do_today() -> None:
    """The named same-mode regression test — both sides `enrich_applied=False`, exactly the
    pre-PER-194 behaviour every other test in this file already pins, restated once more here
    as the explicit contrast to the mode-change test above."""
    previous_txt = generate_llms_txt([_page("https://example.test/docs/a", title="Old Title")])
    current_txt = generate_llms_txt([_page("https://example.test/docs/a", title="New Title")])

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        current_enrich_applied=False,
        previous=_previous(previous_txt, enrich_applied=False),
        previous_run_completed=True,
    )

    assert isinstance(diff["metadata_changed"], int)
    assert diff["metadata_changed"] == 1
    assert diff["metadata_not_comparable_reason"] is None
    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0


def test_an_unknown_previous_mode_is_treated_as_comparable() -> None:
    """A pre-`RUN_STATS_VERSION`-8 previous row has `enrich_applied is None` — treated as
    COMPARABLE, not as an unknown mode that blanks the signal, so every existing website's
    first post-deploy run still reports a real `metadata_changed` count."""
    previous_txt = generate_llms_txt([_page("https://example.test/docs/a", title="Old Title")])
    current_txt = generate_llms_txt([_page("https://example.test/docs/a", title="New Title")])

    diff = _diff(
        current_llms_txt=current_txt,
        current_urls_discovered=1,
        current_enrich_applied=True,
        previous=_previous(previous_txt, enrich_applied=None),
        previous_run_completed=True,
    )

    assert diff["metadata_changed"] == 1
    assert diff["metadata_not_comparable_reason"] is None


def test_the_reason_names_which_direction_the_mode_flipped() -> None:
    """Two runs, both otherwise identical, disagreeing only in which way the flip went — the
    reason string must name the CURRENT run's own mode, not merely "a flip happened"."""
    txt = generate_llms_txt([_page("https://example.test/docs/a", title="A")])

    enabled = _diff(
        current_llms_txt=txt,
        current_urls_discovered=1,
        current_enrich_applied=True,
        previous=_previous(txt, enrich_applied=False),
        previous_run_completed=True,
    )
    assert enabled["metadata_not_comparable_reason"] == "enrichment_enabled"

    disabled = _diff(
        current_llms_txt=txt,
        current_urls_discovered=1,
        current_enrich_applied=False,
        previous=_previous(txt, enrich_applied=True),
        previous_run_completed=True,
    )
    assert disabled["metadata_not_comparable_reason"] == "enrichment_disabled"
