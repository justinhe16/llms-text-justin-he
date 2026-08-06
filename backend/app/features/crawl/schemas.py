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
    `internals/ssrf.py` actually dialed. This is what `generate_llms_txt` (a later phase)
    lists, and what a caller resolving a relative link found on this page would resolve it
    against."""

    status: int
    """The HTTP status code of the final, non-redirect response."""

    title: str | None
    """Always `None` in this milestone. Extraction — reading a `<title>` out of `content`
    — lives behind the `generate_llms_txt` seam (ARCHITECTURE.md §3.4, CLAUDE.md #9), and
    there is nothing upstream of that seam that parses HTML yet. The field exists now so
    that seam's signature does not have to widen the day extraction is designed."""

    content: str
    """The response body, decoded at the transport level (`response.encoding or "utf-8"`,
    `errors="replace"`) and nothing more — not parsed, not stripped of markup. Parsing is
    the crawler milestone's problem, not this feature's."""

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


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    """What `CrawlService.execute_run` hands back to the worker job once a run has already
    been persisted: the artifact, the numbers `runs.stats` stores, and where the raw payload
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
    """The generated artifact — `generate_llms_txt(pages)`'s return value, unmodified. The
    same value already written to `runs.llms_txt`."""

    stats: dict[str, Any]
    """The same shape `runs.stats` stores as jsonb — built by `internals/run_stats.py`'s
    `build_run_stats`: `pages_crawled`, `pages_failed`, `bytes_fetched`, `duration_ms`,
    `cap_hit`, `links_emitted`, and `version`. Typed loosely (`dict[str, Any]`) to match
    `app.features.runs.schemas.RunListItemResponse.stats`, which reads this same column back
    out with the same justification — the shape belongs to this feature, which is why it is
    built here rather than validated against a model owned by `runs`."""

    storage_path: str
    """The bucket-qualified path the payload landed at, e.g.
    `crawl-payloads/{website_id}/{run_id}.jsonl.gz` — `SupabaseStorage.upload`'s return
    value, and the same value already written to `runs.storage_path`."""
