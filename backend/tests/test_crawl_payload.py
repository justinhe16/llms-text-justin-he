"""Tests for `app.features.crawl.internals.payload` — the pure, no-I/O module that turns a
run's fetched pages into the bytes `SupabaseStorage.upload` actually sends.

No database, no network, no `httpx.MockTransport`: everything here is a round trip through
`gzip` and `json` in memory.
"""

import gzip
import json
from datetime import UTC, datetime
from uuid import uuid4

from app.features.crawl.internals.payload import (
    PAYLOAD_SUFFIX,
    payload_object_path,
    serialize_payload,
)
from app.features.crawl.schemas import CrawledPage


def _page(url: str, *, content: str = "hello world", status: int = 200) -> CrawledPage:
    return CrawledPage(
        url=url,
        status=status,
        title=None,
        content=content,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_bytes=len(content.encode()),
    )


def _decode_lines(payload: bytes) -> list[dict[str, object]]:
    """Decompress `payload` and parse every JSONL line, in order."""
    raw = gzip.decompress(payload).decode("utf-8")
    lines = raw.split("\n")
    assert lines[-1] == "", "the payload must end with a trailing newline (append-safety)"
    return [json.loads(line) for line in lines[:-1]]


def test_round_trips_every_page_with_its_declared_keys() -> None:
    pages = [_page("https://example.test/a"), _page("https://example.test/b", content="second")]

    records = _decode_lines(serialize_payload(pages))

    assert len(records) == 2
    for page, record in zip(pages, records, strict=True):
        assert record == {
            "url": page.url,
            "status": page.status,
            "title": page.title,
            "content": page.content,
            "fetched_at": page.fetched_at.isoformat(),
            "bytes": page.content_bytes,
        }


def test_fetched_at_is_iso_8601() -> None:
    (record,) = _decode_lines(serialize_payload([_page("https://example.test/a")]))

    # Round-trips through fromisoformat without raising, and carries the timezone.
    parsed = datetime.fromisoformat(str(record["fetched_at"]))
    assert parsed == datetime(2026, 1, 1, tzinfo=UTC)


def test_compression_actually_compresses_repetitive_content() -> None:
    """A page with highly repetitive content compresses to materially fewer bytes than the
    raw JSONL it was built from — the whole reason the payload is gzipped rather than
    uploaded as plain text."""
    repetitive = "the quick brown fox jumps over the lazy dog " * 2_000
    pages = [_page("https://example.test/repetitive", content=repetitive)]

    compressed = serialize_payload(pages)
    raw_size = len(json.dumps({"content": repetitive}).encode())

    assert len(compressed) < raw_size / 4


def test_serializing_the_same_pages_twice_is_byte_identical() -> None:
    """`gzip.compress(..., mtime=0)` is what makes this true — gzip's default embeds the
    current time in its header, which would make two calls a second apart disagree."""
    pages = [_page("https://example.test/a"), _page("https://example.test/b")]

    assert serialize_payload(pages) == serialize_payload(pages)


def test_empty_page_list_is_a_valid_payload() -> None:
    payload = serialize_payload([])

    assert gzip.decompress(payload) == b""


def test_payload_object_path_is_website_id_slash_run_id_dot_jsonl_dot_gz() -> None:
    website_id = uuid4()
    run_id = uuid4()

    path = payload_object_path(website_id, run_id)

    assert path == f"{website_id}/{run_id}{PAYLOAD_SUFFIX}"
    assert path == f"{website_id}/{run_id}.jsonl.gz"
