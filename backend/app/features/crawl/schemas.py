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


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    """What `CrawlService.execute_run` (a later phase of this ticket) hands back to the
    worker job: an artifact plus the numbers the run's `stats` column stores.

    Deliberately does not persist anything itself — writing `llms_txt` and flipping a run
    to `completed` is a separate write path, added alongside the service that produces
    this.
    """

    llms_txt: str
    """The generated artifact — `generate_llms_txt(pages)`'s return value, unmodified."""

    stats: dict[str, Any]
    """The same shape `runs.stats` stores as jsonb: `pages_crawled`, `pages_failed`,
    `bytes_fetched`, `duration_ms`, and `cap_hit`. Typed loosely (`dict[str, Any]`) to
    match `app.features.runs.schemas.RunListItemResponse.stats`, which reads this same
    column back out with the same justification — the shape belongs to this feature, which
    is why it is built here rather than validated against a model owned by `runs`."""
