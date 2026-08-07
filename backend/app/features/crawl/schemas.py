"""Data shapes for the crawl feature.

**Not Pydantic DTOs.** Every other feature's `schemas.py` holds request/response models
because a router serializes them across the HTTP boundary (ARCHITECTURE.md §3.1). This
feature has no router — `CrawledPage` and `CrawlOutcome` never leave the worker process, so
plain frozen dataclasses are the honest shape: no validation to perform on construction,
and no OpenAPI schema for them to appear in.

**Not named `Page`.** `app.core.pagination.Page` already exists — the generic
keyset-pagination envelope, imported as `Page` by `app.features.runs.service` and returned
by `GET /websites/{id}/runs`. A second, unrelated meaning of `Page` in this codebase would
be an import collision waiting to happen and a near-guaranteed misread in code review:
`from app.features.crawl.schemas import Page` and `from app.core.pagination import Page` in
the same file cannot both exist under that name. `CrawledPage` says what it is without
colliding with the name that was already taken.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class CrawledPage:
    """One fetched page, as `internals/fetcher.py` returns it.

    Frozen because a fetch result is a fact about the past — nothing downstream should be
    able to mutate a page after it was collected into a run's frontier.
    """

    url: str
    """The final, host-based URL — after every redirect hop, never the IP address
    `internals/ssrf.py` actually dialed. This is what `generate_llms_txt` lists, what it
    derives a page's section and its fallback label from, and what a caller resolving a
    relative link found on this page would resolve it against."""

    status: int
    """The HTTP status code of the final, non-redirect response."""

    title: str | None
    """The page's extracted title, or `None` when the document carried nothing usable as
    one — because the response was not HTML at all, or because it was HTML and extraction
    still found no JSON-LD title, no `og:title`, no lone `<h1>`, and no `<title>` element to
    fall back to. Set by `internals/fetcher.py`'s `fetch_page`, which hands the decoded body
    to `internals/extract.py`'s `extract_content` and copies its `ExtractedContent.title`
    straight across.

    **Not always the `<title>` element.** trafilatura prefers, in order, JSON-LD and
    OpenGraph metadata (`og:title`), then a lone `<h1>`, before it falls back to `<title>`
    itself — see `extract_content`'s own docstring for the full precedence order. On a
    documentation page that usually means the site-suffixed social title
    ("Configuration | Acme Docs") when the generator emits `og:title`, and the bare heading
    ("Configuration") when it does not; both are the page's real title."""

    content: str
    """The response body, decoded at the transport level (`response.encoding or "utf-8"`,
    `errors="replace"`) and nothing more — not parsed, not stripped of markup. Parsing now
    happens in this feature (`internals/extract.py`, wired in by `internals/fetcher.py`), but
    its output lands on `markdown` below, not here — `content` stays the raw decoded body
    regardless of what extraction made of it. See `markdown`'s own docstring for why both are
    kept rather than just the parsed form."""

    fetched_at: datetime
    """When this page's final response was received, in UTC."""

    content_bytes: int
    """Added last, deliberately: an earlier field inserted anywhere but the end would break
    every positional `CrawledPage(...)` construction already in this codebase, silently
    reordering arguments into the wrong parameters rather than raising. Required, not
    defaulted — a silent `0` here would be worse than the compile error a missing argument
    produces, because a `0` looks like a legitimately empty page rather than a caller that
    forgot to wire this field through.

    The number of body bytes actually charged against the run's `ByteBudget`
    (`internals/fetcher.py`) while streaming this page — what came off the wire, not
    `len(content.encode())`. Those two disagree the moment `content` is not valid UTF-8:
    `_read_body_within_budget` decodes with `errors="replace"`, which can both shrink and
    grow the byte count relative to the original bytes (a truncated multi-byte sequence
    becomes one substitution character; some replacement runs are longer than what they
    replaced), so re-encoding `content` after the fact and counting *that* would report a
    number that never actually crossed the network. `internals/payload.py`'s
    `serialize_payload` writes this value out as the payload's `bytes` field for exactly this
    reason — it is the honest answer to "how large was this page," not a derived
    approximation of it."""

    description: str | None
    """The page's extracted meta description, or `None` when it has none —
    `internals/extract.py`'s `ExtractedContent.description`, copied across unchanged by
    `fetch_page`. `None` rather than `""` for the same reason `title` is: plenty of real
    documentation pages omit a description entirely, and that is the normal case, not an
    error one.

    Appended here, after `content_bytes`, along with `markdown` and `is_empty` below, for
    the same reason `content_bytes` documents its own position: a field inserted anywhere
    but the end of this dataclass would silently reorder every positional `CrawledPage(...)`
    construction already in this codebase into the wrong parameters, rather than raising.
    Required, not defaulted, for the same reason too — a caller that forgot to wire
    extraction through should get a `TypeError` on construction, not a `CrawledPage` that
    quietly claims every page has no description."""

    markdown: str
    """The page's main content as markdown — `internals/extract.py`'s
    `ExtractedContent.markdown`, copied across unchanged by `fetch_page`. Empty (`""`)
    whenever extraction produced nothing, which is the condition `is_empty` below reports.

    **Why this field exists alongside `content` rather than replacing it.** `content` is
    this run's archival record of what the server actually sent — the payload
    `internals/payload.py` writes to Storage is meant to outlive today's extractor, and
    re-extracting from the original markup later (a trafilatura upgrade, a bug fix in
    `internals/extract.py`, a wider extraction pass) is only possible if that markup was
    kept rather than discarded the moment this feature's own pass over it finished.
    `markdown` is that pass's answer for today's callers, most directly the
    `generate_llms_txt` seam (ARCHITECTURE.md §3.4); the two fields serve different readers,
    and neither is a derived form of the other that could be dropped to save space.

    Appended at the end of this dataclass alongside `description` and `is_empty`, for the
    same positional-construction reason `description`'s own docstring gives."""

    is_empty: bool
    """Whether this page yielded no extractable content — `internals/extract.py`'s
    `ExtractedContent.is_empty`, copied across unchanged by `fetch_page`. Counted, once per
    run, as `runs.stats["pages_empty_content"]` (`internals/crawler.py`, at
    `RUN_STATS_VERSION` 3).

    **Exactly one thing branches on it, and it is not upstream of the seam.**
    `internals/llms_txt.py`'s `generate_llms_txt` omits an empty page from the index it builds
    (PER-179): a bullet carrying a URL, no title and no description is not worth a line in a
    curated index. An earlier revision of this docstring said the flag was "otherwise inert"
    and that nothing downstream of this dataclass could act on it — true while the artifact
    was a stub, and no longer. What survives unchanged is the rule that actually mattered:
    nothing UPSTREAM of that seam may decide what to fetch, what to keep, or how to crawl
    because a page is empty. The page is still fetched, still written to the run's payload,
    and still counted. In particular, `title` above is never nulled when this is `True` — see
    `internals/fetcher.py`'s own comment on that decision, and note that `generate_llms_txt`
    now depends on it: an empty homepage's `<title>` is what names the whole artifact.

    Appended at the end of this dataclass alongside `description` and `markdown`, for the
    same positional-construction reason `description`'s own docstring gives."""


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    """What `CrawlService.execute_run` hands back to the worker job once a run has already
    been persisted: both artifacts, the numbers `runs.stats` stores, and where the raw payload
    landed in Storage.

    **No longer "deliberately does not persist anything itself."** An earlier revision of
    this docstring said exactly that, because at the time `execute_run` stopped short of
    writing anything and left the row `processing` for a later ticket. That ticket is this
    one: by the time a `CrawlOutcome` exists, the payload has already been uploaded to
    Supabase Storage and the `runs` row has already been committed as `completed` — see
    `app.features.crawl.service`'s module docstring for the upload-then-write ordering. This
    dataclass is now a report of what already happened, returned so `app.worker.jobs.
    crawl_task` has something to log, not an instruction for something still to do.
    """

    llms_txt: str
    """The generated index — `generate_llms_txt(pages)`'s return value, unmodified. The same
    value already written to `runs.llms_txt`."""

    llms_full_txt: str
    """The generated expansion — `generate_llms_full_txt(pages)`'s return value, unmodified,
    and the same value already written to `runs.llms_full_txt` (PER-179). Placed after
    `llms_txt` because the two are one run's pair of artifacts and reading them apart would
    be reading them wrong; every construction of this dataclass is keyword-based, so the
    position is documentation rather than a compatibility constraint."""

    stats: dict[str, Any]
    """The same shape `runs.stats` stores as jsonb — built by `internals/run_stats.py`'s
    `build_run_stats`: `pages_crawled`, `pages_failed`, `bytes_fetched`, `duration_ms`,
    `cap_hit`, `pages_empty_content`, `links_emitted`, `full_txt_truncated`, and `version`.
    Typed loosely
    (`dict[str, Any]`) to match `app.features.runs.schemas.RunListItemResponse.stats`, which
    reads this same column back out with the same justification — the shape belongs to this
    feature, which is why it is built here rather than validated against a model owned by
    `runs`."""

    storage_path: str
    """The bucket-qualified path the payload landed at, e.g.
    `crawl-payloads/{website_id}/{run_id}.jsonl.gz` — `SupabaseStorage.upload`'s return
    value, and the same value already written to `runs.storage_path`."""
