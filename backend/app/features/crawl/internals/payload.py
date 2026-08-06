"""Serializing one crawl run's fetched pages into the artifact `runs.storage_path` names.

Pure, feature-owned, no I/O — the same category `websites/internals/url_normalize.py` is:
logic that is neither a DTO nor a query, with nothing here that opens a socket, reads a
clock, or touches the database. `app.features.crawl.service.CrawlService.execute_run` is the
only caller, and it does the actual upload; this module only decides what bytes go where.

**JSONL, not a JSON array — because the file has to stay append-safe.** One page is one line,
terminated by its own trailing newline, so a future consumer could append another page's line
to the end of the file without parsing and rewriting everything already there. A single JSON
array does not have that property: appending an element means rewriting the closing `]` (and
every byte between it and the new element), which turns "add one more page" into "rewrite the
whole file" the moment the file already exists. Nothing in this milestone appends to an
existing object — every run uploads a complete, fresh payload — but the format is chosen for
the operation the ticket's own object-path scheme anticipates, not merely for what this
milestone happens to do with it today.

**The object path is `{website_id}/{run_id}.jsonl.gz` — website-scoped, not run-scoped at the
top level — and that prefix is load-bearing.** Grouping every run's payload for a website
under one prefix is what makes "delete everything this website ever produced" a single
prefix-delete operation against Storage, rather than a query that first has to enumerate
every run id a website ever had. ARCHITECTURE.md §11 records that no such delete exists yet
(deleting a website today cascades its `runs` rows but leaves their Storage objects behind);
this prefix is what makes the eventual fix cheap to write, whichever of the two candidates in
that section lands.
"""

import gzip
import json
import logging
from collections.abc import Sequence
from typing import Final
from uuid import UUID

from app.features.crawl.schemas import CrawledPage


logger = logging.getLogger(__name__)

PAYLOAD_CONTENT_TYPE: Final = "application/x-ndjson"
"""The `Content-Type` `SupabaseStorage.upload` sends for a crawl payload. Newline-delimited
JSON has no IANA-registered type of its own; `application/x-ndjson` is the de facto
convention several NDJSON-producing tools already use, and it is closer to the truth than
`application/json` (which implies a single JSON value) or `text/plain` (which implies nothing
about structure at all)."""

PAYLOAD_SUFFIX: Final = ".jsonl.gz"
"""The extension every payload object is stored under. Declared here, once, so
`payload_object_path` and anything that later needs to recognize one of these objects by
name (a cleanup sweep, say — ARCHITECTURE.md §11) read it from the same place rather than
restating the literal."""


def payload_object_path(website_id: UUID, run_id: UUID) -> str:
    """The path within the bucket this run's payload is stored at: `{website_id}/{run_id}
    .jsonl.gz`.

    Not the bucket-qualified path `runs.storage_path` eventually stores — that prefix is
    added by `SupabaseStorage.upload`, which is the one place that knows the bucket name.
    This function only builds the part that is the same regardless of which bucket a
    deployment configures.
    """
    return f"{website_id}/{run_id}{PAYLOAD_SUFFIX}"


def _page_to_json_line(page: CrawledPage) -> str:
    """One page, as one line of JSONL: exactly the six keys the ticket specifies, and
    nothing computed that is not already sitting on `page`.

    `ensure_ascii=False` so non-Latin page content survives as UTF-8 rather than being
    escaped into `\\uXXXX` sequences that would triple the size of a payload that is about
    to be gzip-compressed anyway (gzip's dictionary works far better against real UTF-8 text
    than against ASCII-escaped runs of digits).
    """
    record = {
        "url": page.url,
        "status": page.status,
        "title": page.title,
        "content": page.content,
        "fetched_at": page.fetched_at.isoformat(),
        "bytes": page.content_bytes,
    }
    return json.dumps(record, ensure_ascii=False)


def serialize_payload(pages: Sequence[CrawledPage]) -> bytes:
    """Build the gzip-compressed JSONL payload for one run: one line per page, in the order
    given, each terminated by `\\n` — including the last line, so the decompressed file is
    append-safe (see the module docstring). `pages == ()` produces a valid, empty-but-not-
    corrupt gzip member, decompressing to `b""`: a run whose frontier legitimately yielded
    nothing still uploads something `SupabaseStorage.upload` can store and a later read can
    decompress without special-casing "zero pages" as an error.

    Encoded as UTF-8 and compressed with `gzip.compress(..., mtime=0)`. Pinning `mtime` to
    an epoch value rather than leaving it as gzip's default of "now" is what makes this
    function deterministic: the same `pages` serialize to byte-identical output on every
    call, regardless of when it runs. That determinism is what `tests/test_crawl_payload.py`
    checks directly, and it is also what makes `mark_processing_completed`'s idempotent
    Storage upload (`x-upsert: true`, `SupabaseStorage.upload`'s docstring) actually
    idempotent at the byte level on a redelivered job, rather than merely "successful both
    times with two different objects."
    """
    raw = "".join(f"{_page_to_json_line(page)}\n" for page in pages).encode("utf-8")
    result = gzip.compress(raw, mtime=0)
    # The compressed bytes, never the payload itself: this is the same "no page content in
    # `fly logs`" line CrawlService's own upload log draws — logging `result` or `raw` here
    # would put every fetched page's text on stdout one call earlier than that line does.
    logger.debug(
        "crawl: serialized payload", extra={"pages": len(pages), "compressed_bytes": len(result)}
    )
    return result
