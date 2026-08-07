"""Tests for `app.features.crawl.internals.llms_txt` — the two artifacts a run publishes.

No database, no network, no clock: both generators are pure, and every test here calls them
directly with hand-built `CrawledPage` lists. `tests/test_url_ranking.py` and
`tests/test_crawl_extract.py` are the closest siblings in shape — this suite follows the same
pattern: one test (or a parametrized group) per named behaviour, named for what it asserts
rather than how.

This file was `test_llms_txt_stub.py` until PER-179. The determinism tests are the ones that
survived the rewrite unchanged in spirit: while the artifact was a placeholder, "a shuffled
list produces byte-identical output" was the ONLY property worth asserting, and it is still
the property everything else here is built on top of.
"""

import inspect
import random
from datetime import UTC, datetime

import pytest

from app.features.crawl.internals import llms_txt
from app.features.crawl.internals.llms_txt import (
    MAX_FULL_TEXT_BYTES,
    MAX_PAGE_TEXT_BYTES,
    count_full_txt_truncations,
    count_indexed_pages,
    generate_llms_full_txt,
    generate_llms_txt,
)
from app.features.crawl.schemas import CrawledPage


# Long enough to be a plausible non-empty extraction: `internals/extract.py` sets `is_empty`
# below `MIN_BODY_CHARS` (200), so a helper defaulting to `is_empty=False` needs a body that
# would genuinely have cleared that bar. Tests that care about a body's CONTENT pass their own.
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
    """A `CrawledPage` with only the fields these two functions read spelled out.

    `is_empty=False` by default, the opposite of `tests/test_crawl_payload.py`'s helper:
    almost every case here is about a page that IS indexed, and the skip rule gets its own
    tests rather than being the default every other test has to opt out of.
    """
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


def _section_of(output: str, heading: str) -> list[str]:
    """Every bullet under `## {heading}`, up to the next H2."""
    lines = output.splitlines()
    start = lines.index(f"## {heading}") + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            body.append(line)
    return body


# --- purity, and the language the stub left behind ---------------------------------------


def test_the_module_performs_no_io_no_network_and_reads_no_clock() -> None:
    """The seam's oldest promise, and the one a future edit is most likely to break quietly:
    both artifacts must be a pure function of the pages handed in. Asserted over the source
    rather than by mocking, the same way `tests/test_url_ranking.py` asserts it, because the
    failure being guarded against is an import someone added — not a call some test happened
    to exercise."""
    source = inspect.getsource(llms_txt)

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
    # The caps here are format invariants of the artifact, not operational knobs — reading
    # them from settings would make a pure function configuration-dependent.
    assert "app.core.settings" not in source


def test_neither_the_module_nor_its_output_calls_itself_a_placeholder() -> None:
    """PER-179's most visible acceptance criterion. The stub's blockquote announced itself as
    a placeholder and pointed at an undesigned milestone; a real artifact that still said so
    would be worse than one that said nothing."""
    assert "placeholder" not in inspect.getsource(llms_txt).lower()

    pages = [_page("https://example.test/docs/a", title="A", description="First.")]
    assert "placeholder" not in generate_llms_txt(pages).lower()
    assert "placeholder" not in generate_llms_full_txt(pages).lower()
    assert "placeholder" not in generate_llms_txt([]).lower()


# --- the llmstxt.org shape ----------------------------------------------------------------


def test_the_document_has_exactly_one_h1_and_exactly_one_blockquote() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/guide/b", title="B"),
        _page("https://example.test/other/c", title="C"),
    ]
    lines = generate_llms_txt(pages).splitlines()

    assert sum(1 for line in lines if line.startswith("# ")) == 1
    assert sum(1 for line in lines if line.startswith("> ")) == 1


def test_the_h1_names_the_sites_own_title_when_the_root_page_carries_one() -> None:
    pages = [
        _page("https://example.test/", title="Acme Docs"),
        _page("https://example.test/docs/a", title="Configuration | Acme Docs"),
    ]

    assert generate_llms_txt(pages).splitlines()[0] == "# Acme Docs"


def test_the_h1_falls_back_to_the_origin_when_no_root_page_has_a_title() -> None:
    """A deep page's title describes that page, not the site, so it is never promoted to the
    H1 — a bare origin is obviously an identifier, where a subpage's name on the whole
    artifact is a quiet mislabelling."""
    pages = [
        _page("https://example.test/api/z", title="Z Endpoint"),
        _page("https://example.test/docs/a", title="Configuration"),
    ]

    assert generate_llms_txt(pages).splitlines()[0] == "# https://example.test"


def test_the_root_pages_title_names_the_site_even_when_that_page_was_empty() -> None:
    """The JavaScript-shell case. `extract.py` deliberately keeps a shell's `<title>`, and a
    documentation SPA whose homepage is a mount div is exactly why: the page is skipped from
    the index, but it is still the only page that knows what the site is called."""
    pages = [
        _page("https://example.test/", title="Acme Docs", markdown="", is_empty=True),
        _page("https://example.test/docs/a", title="A"),
    ]
    output = generate_llms_txt(pages)

    assert output.splitlines()[0] == "# Acme Docs"
    assert _section_of(output, "Docs") == ["- [A](https://example.test/docs/a)"]
    assert "## Other" not in output, "the empty root page is still skipped from the index"


def test_the_blockquote_states_the_indexed_page_count_and_the_origin() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B"),
    ]
    blockquote = generate_llms_txt(pages).splitlines()[2]

    assert blockquote == "> An index of 2 pages from https://example.test."


def test_the_blockquote_accounts_for_pages_left_out_of_the_index() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", markdown="", is_empty=True),
    ]
    blockquote = generate_llms_txt(pages).splitlines()[2]

    assert blockquote == (
        "> An index of 1 page from https://example.test. "
        "Excludes 1 page with no extractable content."
    )


def test_a_bullet_is_a_titled_link_annotated_with_the_pages_description() -> None:
    pages = [_page("https://example.test/docs/a", title="Configuration", description="How to.")]
    output = generate_llms_txt(pages)

    assert "- [Configuration](https://example.test/docs/a): How to." in output


def test_a_page_with_no_description_gets_a_bullet_with_no_trailing_colon() -> None:
    """A description is optional in the llmstxt.org shape, and a dangling `: ` is not an
    empty description — it is a broken line."""
    pages = [_page("https://example.test/docs/a", title="Configuration")]

    assert "- [Configuration](https://example.test/docs/a)\n" in generate_llms_txt(pages)


def test_a_page_with_no_title_is_labelled_from_its_last_path_segment() -> None:
    pages = [_page("https://example.test/docs/getting-started")]

    assert "- [Getting Started](https://example.test/docs/getting-started)" in (
        generate_llms_txt(pages)
    )


def test_a_page_with_no_title_drops_a_filename_extension_from_its_label() -> None:
    pages = [_page("https://example.test/docs/intro.html")]

    assert "- [Intro](https://example.test/docs/intro.html)" in generate_llms_txt(pages)


def test_a_titleless_root_page_is_labelled_with_its_host() -> None:
    pages = [_page("https://example.test/")]

    assert "- [example.test](https://example.test/)" in generate_llms_txt(pages)


def test_titles_and_descriptions_are_collapsed_onto_one_line() -> None:
    """Extraction reads both out of markup where a newline is insignificant. Left alone, one
    would end the bullet and turn the remainder into a stray paragraph."""
    pages = [
        _page(
            "https://example.test/docs/a",
            title="Configuration\n  |  Acme",
            description="First line.\n\tSecond line.",
        )
    ]
    output = generate_llms_txt(pages)

    assert "- [Configuration | Acme](https://example.test/docs/a): First line. Second line." in (
        output
    )


def test_a_whitespace_only_title_falls_back_rather_than_emitting_an_empty_link() -> None:
    pages = [_page("https://example.test/docs/intro", title="   \n  ")]

    assert "- [Intro](https://example.test/docs/intro)" in generate_llms_txt(pages)


def test_bracket_characters_in_a_title_are_escaped_so_the_link_survives() -> None:
    pages = [_page("https://example.test/docs/a", title="Arrays [and] slices")]

    assert "- [Arrays \\[and\\] slices](https://example.test/docs/a)" in generate_llms_txt(pages)


def test_parentheses_in_a_url_are_encoded_so_the_link_survives() -> None:
    """Rare — `url_normalize.py` has been over every URL that reaches here — but a single
    unescaped `)` silently truncates the link target and corrupts the rest of the line."""
    pages = [_page("https://example.test/docs/f(x)", title="F")]
    output = generate_llms_txt(pages)

    assert "- [F](https://example.test/docs/f%28x%29)" in output


# --- sections ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/docs/a", "Docs"),
        ("/doc/a", "Docs"),
        ("/documentation/a", "Docs"),
        ("/guide/a", "Guide"),
        ("/guides/a", "Guide"),
        ("/api/a", "API"),
        ("/reference/a", "Reference"),
        ("/blog/a", "Blog"),
        ("/getting-started/a", "Getting Started"),
        ("/api-reference/a", "API Reference"),
        ("/docs", "Docs"),
    ],
)
def test_a_leading_path_segment_becomes_a_readable_section_name(path: str, section: str) -> None:
    """The curated table exists for the names humanizing gets wrong (`api` -> "Api") and for
    merging synonyms (`doc`/`docs`/`documentation`); everything else is humanized, so a site
    using `/blog/` or `/getting-started/` gets a real section rather than a bucket."""
    output = generate_llms_txt([_page(f"https://example.test{path}", title="T")])

    assert f"## {section}" in output


@pytest.mark.parametrize("path", ["/", "/sitemap.xml", "/-/"])
def test_a_page_with_no_readable_leading_segment_lands_in_other(path: str) -> None:
    output = generate_llms_txt([_page(f"https://example.test{path}", title="T")])

    assert "## Other" in output


def test_sections_are_ordered_curated_first_then_alphabetically_then_other() -> None:
    pages = [
        _page("https://example.test/", title="Root"),
        _page("https://example.test/zebra/a", title="Z"),
        _page("https://example.test/api/a", title="A"),
        _page("https://example.test/blog/a", title="B"),
        _page("https://example.test/docs/a", title="D"),
        _page("https://example.test/guide/a", title="G"),
    ]
    headings = [line for line in generate_llms_txt(pages).splitlines() if line.startswith("## ")]

    assert headings == ["## Docs", "## Guide", "## API", "## Blog", "## Zebra", "## Other"]


def test_pages_within_a_section_stay_in_url_order() -> None:
    pages = [
        _page("https://example.test/docs/z", title="Z"),
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/m", title="M"),
    ]

    assert _section_of(generate_llms_txt(pages), "Docs") == [
        "- [A](https://example.test/docs/a)",
        "- [M](https://example.test/docs/m)",
        "- [Z](https://example.test/docs/z)",
    ]


# --- determinism ---------------------------------------------------------------------------


def _mixed_pages() -> list[CrawledPage]:
    return [
        _page("https://example.test/", title="Acme"),
        _page("https://example.test/api/b", title="B", description="Second."),
        _page("https://example.test/docs/a", title="A", description="First."),
        _page("https://example.test/docs/c", title="C"),
        _page("https://example.test/zebra/d", title="D"),
        _page("https://example.test/docs/e", markdown="", is_empty=True),
    ]


@pytest.mark.parametrize("seed", range(8))
def test_a_shuffled_pages_list_produces_byte_identical_output(seed: int) -> None:
    """THE contract this module has carried since it was a stub. `crawl_site`'s frontier
    fetches race each other, so the order pages arrive in is not reproducible between two runs
    of the same crawl; an artifact that depended on it would differ between two runs that
    fetched exactly the same pages."""
    pages = _mixed_pages()
    shuffled = list(pages)
    random.Random(seed).shuffle(shuffled)

    assert generate_llms_txt(shuffled) == generate_llms_txt(pages)
    assert generate_llms_full_txt(shuffled) == generate_llms_full_txt(pages)


def test_two_pages_sharing_a_url_cannot_reorder_the_output() -> None:
    """`crawl_site` visits each URL once, so this list is not one it can produce — but a
    stable sort keyed on the URL alone would leave these two in input order, which is the one
    way a shuffle could still change the output."""
    first = _page("https://example.test/docs/a", title="A")
    second = _page("https://example.test/docs/a", title="B")

    assert generate_llms_txt([first, second]) == generate_llms_txt([second, first])


# --- the empty and all-empty cases ---------------------------------------------------------


def test_empty_input_returns_a_stable_document_rather_than_raising() -> None:
    assert generate_llms_txt([]) == generate_llms_txt([])
    assert generate_llms_full_txt([]) == generate_llms_full_txt([])
    assert "No pages were fetched" in generate_llms_txt([])


def test_the_empty_document_still_has_one_h1_and_one_blockquote() -> None:
    """Same SHAPE as a full artifact, so a consumer never has to special-case a run that
    fetched nothing. Never `""`, which in a `text` column is indistinguishable from a bug."""
    for output in (generate_llms_txt([]), generate_llms_full_txt([])):
        lines = output.splitlines()
        assert sum(1 for line in lines if line.startswith("# ")) == 1
        assert sum(1 for line in lines if line.startswith("> ")) == 1


def test_empty_pages_are_omitted_from_the_index() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B", markdown="", is_empty=True),
    ]
    output = generate_llms_txt(pages)

    assert "https://example.test/docs/a" in output
    assert "https://example.test/docs/b" not in output


def test_a_run_whose_pages_are_all_empty_still_produces_a_document_with_no_sections() -> None:
    """Not the same case as `pages == []`: there is still an origin and a project name, so the
    header is real. What there must not be is a bare `## ` with nothing under it."""
    pages = [
        _page("https://example.test/", title="Acme", markdown="", is_empty=True),
        _page("https://example.test/docs/a", markdown="", is_empty=True),
    ]
    output = generate_llms_txt(pages)

    assert output.splitlines()[0] == "# Acme"
    assert "> An index of 0 pages from https://example.test." in output
    assert "## " not in output
    assert count_indexed_pages(pages) == 0


def test_count_indexed_pages_counts_only_the_pages_the_index_lists() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B"),
        _page("https://example.test/docs/c", markdown="", is_empty=True),
    ]

    assert count_indexed_pages(pages) == 2
    assert len(pages) == 3, "this is the divergence RUN_STATS_VERSION 3 records"


def test_count_indexed_pages_matches_the_bullets_the_artifact_actually_emits() -> None:
    pages = _mixed_pages()
    bullets = [line for line in generate_llms_txt(pages).splitlines() if line.startswith("- ")]

    assert count_indexed_pages(pages) == len(bullets)


# --- llms-full.txt --------------------------------------------------------------------------


def test_llms_full_txt_emits_a_heading_and_the_pages_markdown_for_each_page() -> None:
    pages = [_page("https://example.test/docs/a", title="Configuration", markdown="Body text.")]
    output = generate_llms_full_txt(pages)

    assert "## Configuration" in output
    assert "Body text." in output


def test_llms_full_txt_follows_the_index_order_not_plain_url_order() -> None:
    """The two artifacts are read together, so a consumer that found a link in the index must
    find the bodies in the order the links were in. `/api/a` sorts before `/docs/b` by URL,
    but Docs precedes API by section, which is the ordering that wins."""
    pages = [
        _page("https://example.test/api/a", title="API Page"),
        _page("https://example.test/docs/b", title="Docs Page"),
    ]
    output = generate_llms_full_txt(pages)

    assert output.index("## Docs Page") < output.index("## API Page")


def test_llms_full_txt_never_emits_firecrawl_page_separators() -> None:
    """Firecrawl emits `<|firecrawl-page-N-lllmstxt|>` between pages and then strips them out
    again with a regex before anything consumes the result. Copying a marker whose own author
    removes it would be copying the bug."""
    output = generate_llms_full_txt(_mixed_pages())

    assert "firecrawl" not in output.lower()
    assert "<|" not in output


def test_llms_full_txt_omits_the_pages_the_index_omits() -> None:
    pages = [
        _page("https://example.test/docs/a", title="Kept", markdown="Kept body."),
        _page("https://example.test/docs/b", title="Skipped", markdown="", is_empty=True),
    ]
    output = generate_llms_full_txt(pages)

    assert "## Kept" in output
    assert "## Skipped" not in output


def test_a_heading_in_the_full_text_is_not_bracket_escaped() -> None:
    """The same label is a markdown link text in the index and a heading here, and only the
    first needs its brackets escaped — a `\\[` in a heading renders literally."""
    pages = [_page("https://example.test/docs/a", title="Arrays [and] slices")]

    assert "## Arrays [and] slices" in generate_llms_full_txt(pages)


# --- the caps -------------------------------------------------------------------------------


def test_a_page_over_the_per_page_cap_is_truncated_and_says_so() -> None:
    oversized = "x" * (MAX_PAGE_TEXT_BYTES + 5_000)
    pages = [_page("https://example.test/docs/a", title="Big", markdown=oversized)]
    output = generate_llms_full_txt(pages)

    assert "Truncated" in output
    assert oversized not in output
    assert count_full_txt_truncations(pages) == 1


def test_the_per_page_cap_is_measured_in_utf8_bytes_not_characters() -> None:
    """30 000 CJK characters are 30 000 characters and 90 000 bytes. A character budget would
    let this through at three times the column size it advertises."""
    body = "あ" * 30_000
    pages = [_page("https://example.test/docs/a", title="Big", markdown=body)]

    assert len(body) < MAX_PAGE_TEXT_BYTES < len(body.encode())
    assert count_full_txt_truncations(pages) == 1


def test_truncation_never_splits_a_multibyte_character() -> None:
    """A cut landing mid-character must drop that character, not emit a lone continuation byte
    or a replacement character."""
    pages = [_page("https://example.test/docs/a", title="Big", markdown="あ" * 30_000)]
    output = generate_llms_full_txt(pages)

    assert "�" not in output
    output.encode()  # would raise on a lone surrogate


def test_the_run_cap_stops_at_a_page_boundary_and_says_how_many_were_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-page cut could land inside a fenced code block and leave it unterminated, and
    would make a trimmed page indistinguishable from a complete one."""
    monkeypatch.setattr(llms_txt, "MAX_FULL_TEXT_BYTES", 2_000)
    body = "y" * 400
    pages = [
        _page(f"https://example.test/docs/{index:02d}", title=f"P{index}", markdown=body)
        for index in range(10)
    ]
    output = generate_llms_full_txt(pages)

    assert "Truncated" in output
    assert output.count(body) >= 1, "whole pages, never a partial one"
    included = output.count("\n## ")
    assert 0 < included < 10
    assert count_full_txt_truncations(pages) == 10 - included


def test_the_run_cap_leaves_the_index_and_its_link_count_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two caps bound the expansion, not the index — a run that could not inline every
    body still lists every page it fetched."""
    monkeypatch.setattr(llms_txt, "MAX_FULL_TEXT_BYTES", 2_000)
    pages = [
        _page(f"https://example.test/docs/{index:02d}", title=f"P{index}", markdown="y" * 400)
        for index in range(10)
    ]

    assert count_indexed_pages(pages) == 10
    assert generate_llms_txt(pages).count("\n- ") == 10


def test_the_full_text_never_exceeds_the_run_cap_at_realistic_sizes() -> None:
    """The test that actually proves the column bound, at the sizes that would breach it: 150
    pages each at the per-page cap is 7.5 MiB of input against a 5 MiB budget."""
    pages = [
        _page(
            f"https://example.test/docs/{index:03d}",
            title=f"Page {index}",
            markdown="z" * (MAX_PAGE_TEXT_BYTES + 100),
        )
        for index in range(150)
    ]
    output = generate_llms_full_txt(pages)

    assert len(output.encode()) <= MAX_FULL_TEXT_BYTES
    assert count_full_txt_truncations(pages) > 0


def test_full_txt_truncated_is_zero_when_every_page_fits() -> None:
    assert count_full_txt_truncations(_mixed_pages()) == 0
    assert count_full_txt_truncations([]) == 0


def test_a_page_both_trimmed_and_then_dropped_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count of pages missing content, not of truncation events — otherwise the number
    exceeds the page count and stops meaning anything."""
    monkeypatch.setattr(llms_txt, "MAX_FULL_TEXT_BYTES", 2_000)
    pages = [
        _page(
            f"https://example.test/docs/{index}",
            title=f"P{index}",
            markdown="w" * (MAX_PAGE_TEXT_BYTES + 100),
        )
        for index in range(4)
    ]

    assert count_full_txt_truncations(pages) == len(pages)


def test_a_page_with_no_markdown_body_is_not_counted_as_truncated() -> None:
    """Only constructible by hand — `is_empty` is derived from the body's length — but nothing
    was cut, so nothing is reported as cut."""
    pages = [_page("https://example.test/docs/a", title="A", markdown="", is_empty=False)]
    output = generate_llms_full_txt(pages)

    assert "## A" in output
    assert count_full_txt_truncations(pages) == 0
