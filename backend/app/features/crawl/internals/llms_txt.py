"""THE STUB SEAM (ARCHITECTURE.md §3.4, CLAUDE.md #9): `generate_llms_txt` is the only
function in this codebase allowed to turn fetched pages into an `llms.txt` artifact.

**Nothing downstream of a fetched page has been designed yet.** No content ranking, no
summarization, no LLM call — anywhere in this feature, in `internals/crawler.py`, in
`app.features.crawl.service`, or here. This function is a deterministic placeholder, not a
first draft of the real thing: adding any of the above inside it would not be "starting the
real implementation early," it would be building the undesigned pipeline in the one place a
reviewer is least likely to look for it.

**What HAS landed upstream, and why it is not a breach of the above.** HTML parsing and title
extraction now happen before this function is ever called: `internals/fetcher.py` runs every
HTML response through `internals/extract.py`, so the `CrawledPage`s handed here already carry
a `title`, a `description`, and a `markdown` body (PER-177). That is deliberately *upstream*
of this seam — it enriches the seam's INPUT TYPE rather than deciding anything about the
artifact — and this function still ignores all three, listing one bullet per fetched URL
exactly as it did before. Deciding which pages belong in an `llms.txt`, how they are ranked,
and how their content is summarized remains undesigned and remains this function's problem
alone. URL-shape ranking exists too (`internals/url_ranking.py`), but it runs entirely before
a fetch and is still called by nothing (PER-176).

**Deterministic regardless of fetch order.** `internals/crawler.py`'s frontier fetches race
each other, so the order pages arrive in `CrawlResult.pages` is not reproducible between two
runs of the same crawl. Every value this function derives — the origin in the heading, and
the bullet list — is computed from the pages sorted by URL first, so the artifact this
function returns is a pure function of *which* pages were fetched, never of the order they
finished in.
"""

from urllib.parse import urlsplit

from app.features.crawl.schemas import CrawledPage


_EMPTY_PLACEHOLDER = "# llms.txt\n\n> No pages were fetched during this run.\n"


def generate_llms_txt(pages: list[CrawledPage]) -> str:
    """Build a placeholder `llms.txt` body from `pages`.

    STUB. The real implementation — deciding what belongs in an `llms.txt`, how pages are
    ranked, and how their content is summarized — is a separate, not-yet-designed milestone
    (ARCHITECTURE.md §3.4). Do not add crawling, parsing, ranking, or LLM-calling logic here
    or anywhere upstream of this function.

    Args:
        pages: Every page `internals/crawler.py`'s `crawl_site` collected for one run, in
            whatever order they finished fetching. Never sorted by the caller — sorting
            happens here, once, so every caller gets the same guarantee for free.

    Returns:
        A stable placeholder for `pages == []` (a crawl whose seed failed never reaches this
        function — `CrawlService.execute_run` short-circuits on `CrawlResult.seed_error`
        first — but a frontier that legitimately yielded nothing is not this function's
        problem to raise about). Otherwise: an H1 naming the origin of the alphabetically
        first fetched URL, a blockquote noting this is a placeholder, and a bullet list of
        every fetched URL, sorted.
    """
    if not pages:
        return _EMPTY_PLACEHOLDER

    urls = sorted(page.url for page in pages)
    first = urlsplit(urls[0])
    origin = f"{first.scheme}://{first.netloc}"

    lines = [
        f"# {origin}",
        "",
        f"> Placeholder llms.txt for {len(urls)} page(s) fetched during this run. Real "
        "extraction, ranking, and summarization are a separate, not-yet-designed milestone "
        "(ARCHITECTURE.md §3.4).",
        "",
    ]
    lines.extend(f"- {url}" for url in urls)
    lines.append("")
    return "\n".join(lines)
