"""Tests for `app.features.crawl.internals.extract` — the pure, no-I/O module that turns one
fetched page's HTML into a title, a description, and a clean markdown body.

No database, no queue, no network: every input here is either a literal in this file or an
HTML fixture read off disk from `tests/fixtures/`, and `extract_content` never fetches the URL
it is handed. That matters for more than tidiness — CI has no outbound network for these
tests, so an extractor that quietly tried to resolve something would fail here rather than in
production.

**The four generator fixtures are the point of this file.** Docusaurus, Mintlify, VitePress
and Nextra are what the documentation sites this crawler targets are actually built with, and
each wraps its content in a different arrangement of navbar, sidebar, table of contents,
pagination rail and footer. Asserting that the real prose survives *and* that each of those
chrome elements does not is the only way to check the thing trafilatura was chosen for. The
fixtures are trimmed to the parts that matter — enough real markup to be representative, not
a byte-for-byte capture of a live page.
"""

from pathlib import Path

import pytest

from app.features.crawl.internals.extract import (
    MIN_BODY_CHARS,
    ExtractedContent,
    extract_content,
)


_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class _Generator:
    """One documentation generator's fixture and what it is expected to yield.

    A class rather than a tuple so the parametrized cases below read as named fields at the
    call site instead of five positional strings a reader has to count.
    """

    def __init__(
        self,
        *,
        fixture: str,
        url: str,
        title: str,
        description: str | None,
        body: list[str],
        chrome: list[str],
    ) -> None:
        self.fixture = fixture
        self.url = url
        self.title = title
        self.description = description
        self.body = body
        self.chrome = chrome

    def __repr__(self) -> str:
        # pytest renders this as the test id, so `-k docusaurus` selects the right case.
        return self.fixture.removesuffix("_page.html")


_GENERATORS = [
    _Generator(
        fixture="docusaurus_page.html",
        url="https://docs.example.com/docs/config",
        title="Configuration | Acme Docs",
        description=(
            "How to configure the Acme CLI, including every supported field and its default."
        ),
        body=[
            "# Configuration",
            "The Acme CLI reads its configuration from",
            "## Where the configuration file is found",
            "## Environment overrides",
            # The fenced code block and the table both survive, which is what
            # include_tables=True and markdown output are for.
            '"concurrency": 4',
            "| Field | Default | Description |",
            "Retry attempts per failed request.",
            # include_links=True, resolved against the page URL rather than left relative.
            "(https://docs.example.com/docs/deploy)",
        ],
        chrome=[
            "Join our Discord",  # navbar
            "Troubleshooting",  # sidebar
            "Edit this page",  # article footer
            "Privacy Policy",  # site footer
            "Copyright © 2025 Acme, Inc.",  # site footer
            "Built with Docusaurus",
            "gtag",  # <script>
            "position:sticky",  # <style>
            "dataLayer",  # <script>
        ],
    ),
    _Generator(
        fixture="mintlify_page.html",
        url="https://api.example.com/authentication",
        title="Authentication - Acme API",
        description=(
            "Authenticate requests to the Acme API with a bearer token or a signed request."
        ),
        body=[
            "# Authentication",
            "Every request to the Acme API must be authenticated.",
            "## Bearer tokens",
            "## Signed requests",
            "HMAC-SHA256",
            "(https://api.example.com/errors)",
        ],
        chrome=[
            "Open Dashboard",  # navbar
            "Search or ask",  # navbar search
            "Webhooks",  # sidebar
            "Powered by Mintlify",  # footer
            "On this page",  # table of contents
            "posthog",  # <script>
            "backdrop-filter",  # <style>
        ],
    ),
    _Generator(
        fixture="vitepress_page.html",
        url="https://guide.example.com/guide/deploy",
        title="Deployment | Acme Guide",
        description=(
            "Deploy the Acme server behind a reverse proxy, with health checks and "
            "zero-downtime restarts."
        ),
        body=[
            "# Deployment",
            "The Acme server is a single static binary",
            "## Behind a reverse proxy",
            "## Zero-downtime restarts",
            "X-Forwarded-Proto",
            "| SIGKILL | Immediate termination; in-flight requests are lost. |",
            "(https://guide.example.com/guide/monitoring)",
        ],
        chrome=[
            "Changelog",  # navbar
            "Installation",  # sidebar
            "Edit this page on GitHub",  # doc footer
            "Released under the MIT License",  # site footer
            "Acme Contributors",  # site footer
            "On this page",  # outline
            "__VP_HASH_MAP__",  # <script>
            "--vp-c-brand-1",  # <style>
        ],
    ),
    _Generator(
        fixture="nextra_page.html",
        url="https://framework.example.com/docs/middleware",
        title="Middleware – Acme Framework",
        # Deliberately absent from this fixture: plenty of real documentation pages ship no
        # meta description at all, and `None` (never `""`) is what that has to produce.
        description=None,
        body=[
            "# Middleware",
            "Middleware runs before a request reaches your route handler",
            "## Defining middleware",
            "## Matchers",
            "## Execution order",
            "Response.redirect",
            "(https://framework.example.com/docs/deployment)",
        ],
        chrome=[
            "Showcase",  # navbar
            "Search documentation",  # navbar search
            "Data fetching",  # sidebar
            "On This Page",  # table of contents
            "Give us feedback",  # toc footer
            "MIT 2025 © Acme.",  # site footer
            "__NEXT_DATA__",  # <script>
            "nextra-sidebar-container{width",  # <style>
        ],
    ),
]


@pytest.mark.parametrize("case", _GENERATORS, ids=repr)
def test_extracts_the_real_body_text_of_every_generator(case: _Generator) -> None:
    """The page's actual prose, headings, code, tables and links come through as markdown."""
    result = extract_content(_load(case.fixture), url=case.url)

    assert not result.is_empty
    for phrase in case.body:
        assert phrase in result.markdown, f"{case!r}: missing body content {phrase!r}"


@pytest.mark.parametrize("case", _GENERATORS, ids=repr)
def test_strips_navigation_chrome_and_inline_assets_from_every_generator(
    case: _Generator,
) -> None:
    """Navbar, sidebar, table of contents, pagination, footer, `<script>` and `<style>` are
    all absent from the extracted body.

    This is the assertion the whole ticket rests on. Every string checked here appears
    somewhere in the fixture's markup; none of it is content.
    """
    result = extract_content(_load(case.fixture), url=case.url)

    for phrase in case.chrome:
        assert phrase not in result.markdown, f"{case!r}: leaked chrome {phrase!r}"


@pytest.mark.parametrize("case", _GENERATORS, ids=repr)
def test_reads_title_and_description_from_page_metadata(case: _Generator) -> None:
    """Both come from the document's own `<title>`/`<meta>`/OpenGraph tags, and a field the
    page does not carry is `None` rather than an empty string."""
    result = extract_content(_load(case.fixture), url=case.url)

    assert result.title == case.title
    assert result.description == case.description


def test_a_missing_meta_description_is_none_rather_than_an_empty_string() -> None:
    """Stated on its own, not just as one row of the table above, because `"" == None` is
    false but `not "" == not None` is true — a regression that returned `""` here would slip
    past any caller written as `if not description`."""
    result = extract_content(_load("nextra_page.html"), url="https://framework.example.com/x")

    assert result.description is None


def test_a_javascript_shell_is_empty_and_keeps_the_metadata_it_does_have() -> None:
    """A page whose `<body>` is an empty mount div: no body to extract, so `is_empty` is set
    and `markdown` is empty — and it does not raise.

    The title and description survive, which is a deliberate choice rather than an oversight.
    A shell like this still carries real `<head>` metadata, and it is exactly the page most
    likely to need the deterministic fallback PER-180 will build on that metadata. Discarding
    it because the body came back empty would throw the useful half away with the useless
    half. See `extract_content`'s own comment on this.
    """
    result = extract_content(_load("js_shell_page.html"), url="https://console.example.com/")

    assert result.is_empty
    assert result.markdown == ""
    assert result.title == "Acme Console"
    assert result.description == "The Acme Console."


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("json", '{"openapi":"3.1.0","info":{"title":"Acme API","version":"1.0.0"},"paths":{}}'),
        ("plain_text", "User-agent: *\nDisallow: /private\nSitemap: https://x.example.com/s.xml\n"),
        ("empty", ""),
        ("whitespace", "   \n\t  \n"),
    ],
)
def test_a_non_html_payload_is_empty_rather_than_an_exception(label: str, payload: str) -> None:
    """A crawl frontier will hand this function whatever a URL returned, and not every URL
    returns HTML. None of these is an error worth failing a run over."""
    result = extract_content(payload, url="https://x.example.com/thing")

    assert result.is_empty, label
    assert result.markdown == "", label
    assert result.title is None, label
    assert result.description is None, label


def test_unclosed_tags_and_broken_nesting_do_not_raise() -> None:
    """Real sites ship markup like this. The parser is expected to cope, and the content it
    does manage to recover is still returned."""
    html = (
        "<html><head><title>Broken</title><body><div class='content'>"
        "<p>The configuration loader reads every file in the directory and merges them in "
        "lexical order, so a later file overrides an earlier one."
        "<div><ul><li>first item<li>second item</ul>"
        "<p>Unclosed paragraph and a stray nesting error follow this sentence, and the "
        "parser is expected to cope with both.</div>"
    )

    result = extract_content(html, url="https://x.example.com/broken")

    assert result.title == "Broken"
    assert "The configuration loader reads every file" in result.markdown
    assert "- first item" in result.markdown


def test_a_body_shorter_than_the_threshold_counts_as_empty_but_is_still_returned() -> None:
    """`is_empty` is not merely "markdown == ''". A page that extracted a real but tiny body
    is counted alongside the JavaScript shells, because from the outside the two are the same
    signal — and the text is still handed back, since `is_empty` is instrumentation and not a
    decision to discard anything.
    """
    html = (
        "<html><head><title>Stub</title></head><body><article><h1>Stub</h1>"
        "<p>This endpoint is deprecated and will be removed in the next major release.</p>"
        "</article></body></html>"
    )

    result = extract_content(html, url="https://x.example.com/stub")

    assert 0 < len(result.markdown) < MIN_BODY_CHARS
    assert result.is_empty
    assert "deprecated" in result.markdown


@pytest.mark.parametrize("case", _GENERATORS, ids=repr)
def test_extracting_the_same_page_twice_gives_an_identical_result(case: _Generator) -> None:
    """Purity: no clock, no network, no accumulated state between calls. The same input maps
    to the same output every time, which is what makes an extracted body safe to compare
    across two runs of the same crawl."""
    html = _load(case.fixture)

    first = extract_content(html, url=case.url)
    second = extract_content(html, url=case.url)

    assert first == second


def test_returns_the_declared_shape() -> None:
    """`ExtractedContent` is frozen, so a caller cannot edit a parse result after the fact —
    the same guarantee `CrawledPage` gives about a fetch result."""
    result = extract_content(_load("docusaurus_page.html"), url="https://docs.example.com/x")

    assert isinstance(result, ExtractedContent)
    with pytest.raises(AttributeError):
        result.markdown = "rewritten"
