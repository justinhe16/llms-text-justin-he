"""Tests for `app.infrastructure.storage.supabase_storage` — `SupabaseStorage` and its two
factories.

Every client here is built with `transport=httpx.MockTransport(...)`, per this suite's
autouse `_forbid_real_network` fixture (`tests/conftest.py`) — no test in this file opens a
real socket, and the one that simulates a connection failure does so by raising from inside
the mock transport, never by dialling anything.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from app.core.settings import Settings
from app.infrastructure.storage.supabase_storage import (
    StorageUploadError,
    SupabaseStorage,
    build_storage_client,
    build_supabase_storage,
)


# An obviously-fake credential — this suite asserts it never leaks into an exception, so it
# has to look damning if it ever did (CLAUDE.md #1: no real secret, ever, in a test fixture).
_FAKE_SERVICE_KEY = "not-a-real-service-key-do-not-use"
_BASE_URL = "https://test-project.supabase.co"
_BUCKET = "crawl-payloads"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql://localhost:5432/llms_text",
        "redis_url": "redis://localhost:6379/0",
        "supabase_url": _BASE_URL,
        "supabase_secret_key": _FAKE_SERVICE_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@asynccontextmanager
async def _storage(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = _BASE_URL,
    bucket: str = _BUCKET,
) -> AsyncIterator[SupabaseStorage]:
    """A `SupabaseStorage` backed by `httpx.MockTransport(handler)`, closed on exit."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield SupabaseStorage(
            client, base_url=base_url, service_key=_FAKE_SERVICE_KEY, bucket=bucket
        )


async def test_upload_success() -> None:
    """Asserts method, full URL, every required header, the exact body bytes, and that the
    return value is the bucket-qualified path."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Key": "ignored"})

    async with _storage(handler) as storage:
        result = await storage.upload(
            "11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222.jsonl.gz",
            b"gzipped-bytes-here",
            content_type="application/x-ndjson",
        )

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == (
        f"{_BASE_URL}/storage/v1/object/{_BUCKET}/"
        "11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222.jsonl.gz"
    )
    assert request.headers["Authorization"] == f"Bearer {_FAKE_SERVICE_KEY}"
    assert request.headers["apikey"] == _FAKE_SERVICE_KEY
    assert request.headers["Content-Type"] == "application/x-ndjson"
    assert request.headers["x-upsert"] == "true"
    assert request.content == b"gzipped-bytes-here"

    assert result == (
        f"{_BUCKET}/11111111-1111-1111-1111-111111111111/"
        "22222222-2222-2222-2222-222222222222.jsonl.gz"
    )


async def test_a_non_default_bucket_lands_in_both_the_url_and_the_return_value() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    async with _storage(handler, bucket="a-different-bucket") as storage:
        result = await storage.upload("website/run.jsonl.gz", b"data", content_type="text/plain")

    assert "/a-different-bucket/" in str(captured[0].url)
    assert result == "a-different-bucket/website/run.jsonl.gz"


async def test_the_object_path_is_percent_encoded() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    async with _storage(handler) as storage:
        await storage.upload("a b/c#d.jsonl.gz", b"data", content_type="text/plain")

    assert "a%20b/c%23d.jsonl.gz" in str(captured[0].url)


async def test_a_non_2xx_response_raises_storage_upload_error() -> None:
    """The service key appears in neither `str(exc)` nor `repr(exc)`, and neither does the
    response body — a body that would be damning if it leaked contains the exact fake key."""
    damning_body = f"internal error: service key {_FAKE_SERVICE_KEY} rejected by upstream"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=damning_body)

    async with _storage(handler) as storage:
        with pytest.raises(StorageUploadError) as exc_info:
            await storage.upload("website/run.jsonl.gz", b"data", content_type="text/plain")

    message = str(exc_info.value)
    assert "403" in message
    assert f"{_BUCKET}/website/run.jsonl.gz" in message
    assert _FAKE_SERVICE_KEY not in message
    assert _FAKE_SERVICE_KEY not in repr(exc_info.value)
    assert damning_body not in message
    assert _BASE_URL not in message


async def test_a_connect_error_becomes_a_storage_upload_error_with_the_cause_chained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    async with _storage(handler) as storage:
        with pytest.raises(StorageUploadError) as exc_info:
            await storage.upload("website/run.jsonl.gz", b"data", content_type="text/plain")

    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)
    message = str(exc_info.value)
    assert _FAKE_SERVICE_KEY not in message
    assert _FAKE_SERVICE_KEY not in repr(exc_info.value)


async def test_build_storage_client_uses_the_storage_timeout_not_the_crawl_timeout() -> None:
    settings = _settings(storage_upload_timeout_s=45.0, crawl_request_timeout_s=10.0)

    client = build_storage_client(settings)
    try:
        assert client.timeout == httpx.Timeout(45.0)
        assert client.timeout != httpx.Timeout(10.0)
    finally:
        await client.aclose()


async def test_build_supabase_storage_wires_settings_through() -> None:
    settings = _settings(supabase_storage_bucket="a-configured-bucket")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        storage = build_supabase_storage(client, settings)
        assert storage.bucket == "a-configured-bucket"
