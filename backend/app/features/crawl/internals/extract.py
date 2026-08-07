"""Turning one fetched page's HTML into a title, a description, and a clean markdown body.

Pure, feature-owned, no I/O — the same category `internals/payload.py` and
`internals/run_stats.py` beside it are: nothing here opens a socket, reads a clock, or
touches the database. `trafilatura.extract` parses a string that is already in memory; it
never fetches the URL it is handed, which is only used to resolve relative links found in
the document.

**`internals/fetcher.py`'s `fetch_page` is the only caller** — one `extract_content` call per
fetched page whose `Content-Type` looks like HTML, its result copied straight onto
`CrawledPage` (PER-177). This module deliberately landed *unwired* one ticket earlier
(PER-173), so that the extractor could be reviewed and tested as a pure function against real
fixtures rather than as a behaviour change buried inside the fetch path; that ordering is
history now, not a description of the present. What has not changed is what keeps the
`generate_llms_txt` seam (`internals/llms_txt.py`, ARCHITECTURE.md §3.4, CLAUDE.md #9)
honest: this module parses a page, and it stops there. It ranks nothing, summarizes nothing,
and calls no model. Being called from the fetch path does not widen that remit — extraction
feeds the seam's input type, it is not the seam.

**Why trafilatura rather than hand-rolled rules.** Its own evaluation puts it at F1 0.958
against Mozilla Readability (0.947), newspaper4k (0.949) and goose3 (0.896), and the
SIGIR '23 empirical comparison independently finds Readability and Trafilatura the two robust
extractors. Internally it is a cascade of hand-tuned XPath queries that falls back to jusText
and Readability when its own pass comes up empty — the layered behaviour we would otherwise
write ourselves, worse. It also extracts metadata in the same pass, which is what will give
PER-180 a deterministic title and description when the LLM flag is off or the API call fails.

**Two calls, not one.** `trafilatura.extract_with_metadata` looks like it should collapse the
two calls below into one, and it does return a `Document` carrying both the body and the
metadata — but with `output_format="markdown"` it renders the metadata into the body as a
YAML front-matter block (`---\\ntitle: ...\\nurl: ...\\n---`). That is the opposite of what
this module is for: `markdown` here is a page's prose, and the title and description are
separate fields precisely so that a caller can use one without having to parse the other back
out of the other's text. So the body comes from `trafilatura.extract` (which emits no front
matter, because it does not carry metadata) and the metadata from
`trafilatura.extract_metadata`.
"""

import logging
from dataclasses import dataclass
from typing import Final

import trafilatura


logger = logging.getLogger(__name__)

MIN_BODY_CHARS: Final = 200
"""How short an extracted body has to be before the page is counted as contentless.

This is the JavaScript-rendering instrumentation, and it is **a counter, not a trigger**.
A single-page app served as an empty mount div (`<div id="root"></div>` plus a bundle) is
indistinguishable, to an HTTP fetch, from a page with nothing on it — the content only exists
after a browser runs the JavaScript. Whether it is worth paying for a headless browser to
render those pages is a real decision with a real cost, and this constant exists to *measure*
how often it would matter, not to make that decision. That counting is implemented: every run
records how many of its fetched pages came back empty as `runs.stats["pages_empty_content"]`
(`internals/crawler.py`, on every row from `RUN_STATS_VERSION` 2 onwards). Exactly one thing
branches on `is_empty`
beyond counting it, and that "later" has since arrived: `internals/llms_txt.py`'s
`generate_llms_txt` declines to list a page with no content (PER-179). Nothing UPSTREAM of
that seam branches on it — such a page is still fetched, still stored in the run's payload,
and still counted. In particular, `CrawledPage.title` is never nulled because a page is
empty: a JavaScript shell is real HTML with a real `<title>`, which is exactly why
`extract_content` below reads metadata even when the body came back empty — and why an empty
homepage can still be what names the whole artifact.

200 characters, and deliberately generous. The strings these shells actually ship are short —
"You need to enable JavaScript to run this app." is 46 characters, and the longest common
variant is under 100 — so the threshold has to clear them with room to spare or the counter
undercounts the exact thing it was added to count. The cost of erring the other way is small
and bounded: a genuinely thin page (a stub reference entry, a redirect notice) is counted as
empty when it is merely short. A counter that overcounts by a few real-but-tiny pages still
answers "should we render JavaScript?"; one that misses shells because the threshold was set
below their boilerplate does not.
"""


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    """What one page's HTML yielded. Frozen for the same reason `CrawledPage` is: a parse
    result is a fact about a document, and nothing downstream should be able to edit it after
    the fact.
    """

    title: str | None
    """The page's title, or `None` when the document carries nothing usable as one.

    `None`, never `""` — a caller choosing between "use the page's title" and "fall back to
    something else" should be able to write `title or fallback` and have it do the right
    thing, which an empty string quietly breaks by being falsy but not `None` in an `is None`
    check. `_blank_to_none` below enforces that regardless of what trafilatura returns.

    **Not always the `<title>` element.** trafilatura prefers, in order, JSON-LD and
    OpenGraph metadata (`og:title`), then a lone `<h1>`, then `<title>`. On a documentation
    page that usually means the site-suffixed social title ("Configuration | Acme Docs") when
    the generator emits `og:title`, and the bare heading ("Configuration") when it does not.
    Both are the page's real title; which one a given site yields is a property of that site's
    markup, not a bug here."""

    description: str | None
    """The page's meta description, or `None` when it has none. `None` rather than `""` for
    the same reason as `title`. Plenty of real documentation pages omit it entirely — the
    Nextra fixture in `tests/fixtures/` is one — so this being absent is the normal case, not
    an error case."""

    markdown: str
    """The page's main content as markdown: headings, prose, lists, tables, code blocks, and
    links, with the navigation, sidebar, table of contents, pagination, footer, `<script>`
    and `<style>` stripped out.

    Empty (`""`) whenever extraction produced nothing, which is the same condition
    `is_empty` reports — but the two are not redundant, because `is_empty` is also set for a
    body that is present and merely too short to be real content (see `MIN_BODY_CHARS`)."""

    is_empty: bool
    """Whether this page should be counted as having no extractable content — either because
    extraction yielded nothing at all, or because what it yielded is shorter than
    `MIN_BODY_CHARS`. See that constant for why this is instrumentation rather than a
    decision."""


_EMPTY: Final = ExtractedContent(title=None, description=None, markdown="", is_empty=True)
"""The result every failure path returns. Safe to share as one instance because
`ExtractedContent` is frozen — there is no way for one caller to mutate the value another
caller is holding."""


def _blank_to_none(value: str | None) -> str | None:
    """`None` for anything that is missing or is only whitespace, the value stripped
    otherwise. Applied to both metadata fields so that "absent" has exactly one
    representation for a caller to test, rather than three that all mean the same thing.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def extract_content(html: str, *, url: str) -> ExtractedContent:
    """Parse `html` into a title, a description, and a clean markdown body.

    **Never raises.** Malformed markup, an empty body, a payload that is not HTML at all
    (a JSON API spec, a `robots.txt`), or trafilatura simply finding nothing all come back as
    a value, not an exception. A page that cannot be parsed is one bullet missing from an
    index, not a failed run — and this function is called once per fetched page, so a raise
    here would take down a crawl that had already succeeded at everything else it did.

    Deterministic: the same `html` and `url` produce an identical `ExtractedContent` on every
    call. Nothing here reads the clock or the network. (trafilatura's metadata pass *can*
    hunt for a publication date, which is the one clock-dependent thing in its repertoire —
    `extensive=False` below turns that hunt off, and the date is discarded regardless.)

    Args:
        html: The response body as text, exactly as `internals/fetcher.py` decoded it —
            not pre-cleaned, not pre-validated. Being handed something that is not HTML is
            an expected input, not a caller error.
        url: The page's final, post-redirect URL. Keyword-only so a two-string call site
            cannot silently transpose its arguments. Used **only** to resolve relative links
            found in the document into absolute ones; it is never fetched, and passing a URL
            that no longer resolves is harmless.

    Returns:
        An `ExtractedContent`. On any failure, `_EMPTY` — no title, no description, empty
        markdown, `is_empty=True`.
    """
    try:
        body = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            # Precision over recall: a curated index is better served by clean text than by
            # complete text. Recall mode keeps borderline blocks — related-article rails,
            # promo boxes — that would read as content in an llms.txt and are not.
            favor_precision=True,
        )
        # `extensive=False` narrows trafilatura's *date* search only (it is passed straight
        # through to htmldate as the date config). Title and description are unaffected, the
        # date is a field this module does not return, and skipping the hunt is both faster
        # per page and the one thing standing between this function and a clock read.
        metadata = trafilatura.extract_metadata(html, default_url=url, extensive=False)
    except Exception:
        # Broad on purpose. trafilatura's cascade runs lxml, jusText and Readability over
        # attacker-supplied bytes; enumerating which exception each layer can raise on which
        # malformed input would be a list that goes stale the first time any of them is
        # upgraded. The contract this function offers its caller is "never raises", and the
        # only way to actually offer that is to catch everything.
        logger.warning("extract: failed to parse %s", url, exc_info=True, extra={"url": url})
        return _EMPTY

    markdown = (body or "").strip()
    result = ExtractedContent(
        # Metadata is read even when the body came back empty, rather than short-circuiting
        # to `_EMPTY` the moment `body` is falsy. A JavaScript shell is exactly the page that
        # has no body and a perfectly good `<title>` — discarding it would throw away the
        # metadata PER-180 needs for its deterministic fallback on the very pages most likely
        # to need one. A payload that genuinely carries no metadata (JSON, plain text) yields
        # `None` for both fields here anyway, so the degenerate case still comes out as
        # `_EMPTY`'s shape without having to be special-cased into it.
        title=_blank_to_none(metadata.title),
        description=_blank_to_none(metadata.description),
        markdown=markdown,
        is_empty=len(markdown) < MIN_BODY_CHARS,
    )
    # Lengths and flags, never the extracted text: `markdown` is the page's actual content
    # and has no business in `fly logs`, the same line `internals/fetcher.py` and
    # `internals/payload.py` both draw about the bodies they handle.
    logger.debug(
        "extract: %s -> %d chars (empty=%s)",
        url,
        len(markdown),
        result.is_empty,
        extra={
            "url": url,
            "chars": len(markdown),
            "is_empty": result.is_empty,
            "has_title": result.title is not None,
            "has_description": result.description is not None,
        },
    )
    return result
