"""Tests for `app.features.crawl.internals.llms_txt.generate_llms_txt` — THE STUB SEAM.

Every case here is about determinism and shape, never about content quality: this function
is a placeholder (ARCHITECTURE.md §3.4, CLAUDE.md #9), and the one property that actually
matters is that it is a pure function of *which* pages were fetched, never of the order they
finished in.
"""

from datetime import UTC, datetime

from app.features.crawl.internals.llms_txt import generate_llms_txt
from app.features.crawl.schemas import CrawledPage


def _page(url: str) -> CrawledPage:
    return CrawledPage(
        url=url,
        status=200,
        title=None,
        content="",
        fetched_at=datetime.now(UTC),
        content_bytes=0,
        description=None,
        markdown="",
        is_empty=True,
    )


def test_the_same_pages_in_a_different_order_produce_byte_identical_output() -> None:
    pages = [
        _page("https://example.test/a"),
        _page("https://example.test/b"),
        _page("https://example.test/c"),
    ]
    reordered = [pages[2], pages[0], pages[1]]

    assert generate_llms_txt(pages) == generate_llms_txt(reordered)


def test_the_heading_names_the_origin() -> None:
    pages = [_page("https://example.test/a"), _page("https://example.test/b")]
    output = generate_llms_txt(pages)

    assert output.splitlines()[0] == "# https://example.test"


def test_every_fetched_url_appears() -> None:
    pages = [_page("https://example.test/a"), _page("https://example.test/b/c")]
    output = generate_llms_txt(pages)

    assert "- https://example.test/a" in output
    assert "- https://example.test/b/c" in output


def test_the_bullet_list_is_sorted() -> None:
    pages = [_page("https://example.test/z"), _page("https://example.test/a")]
    output = generate_llms_txt(pages)

    a_index = output.index("- https://example.test/a")
    z_index = output.index("- https://example.test/z")
    assert a_index < z_index


def test_empty_input_returns_a_stable_placeholder_rather_than_raising() -> None:
    output = generate_llms_txt([])

    assert output == generate_llms_txt([])
    assert "No pages were fetched" in output
