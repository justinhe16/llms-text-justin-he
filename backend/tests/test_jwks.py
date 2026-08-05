"""Tests for app.core.auth.jwks: the JWKS cache and its rate-limited refetch.

Every test drives a real `httpx.AsyncClient` against a fake JWKS endpoint (`_JwksServer`,
`tests/conftest.py`) built on `httpx.MockTransport`, so `request_count` measures genuine
HTTP requests moving through the real client's request pipeline rather than a seam this
module owns. See `_JwksServer`'s docstring in `conftest.py` for why that distinction is
what makes this ticket's two "must be measured, not inferred" acceptance criteria
(no I/O on a cache hit; at most one refetch per rate-limit window) actually measured.
"""

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import _FakeClock, _JwksServer, _SigningKey
from jwt import PyJWK

from app.core.auth import jwks as jwks_module
from app.core.auth.jwks import JwksCache, close_jwks_cache, get_jwks_cache, open_jwks_cache
from app.core.settings import Settings


# Matches what `_settings()` below builds via `_build_jwks_url` — spelled out explicitly so
# the URL-construction test can assert against a literal rather than calling the private
# helper it exists to test.
_JWKS_URL = "https://test-project.supabase.co/auth/v1/.well-known/jwks.json"


def _settings(supabase_url: str = "https://test-project.supabase.co") -> Settings:
    """Build Settings with an explicit SUPABASE_URL, ignoring the ambient environment."""
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost:5432/placeholder",
        redis_url="redis://localhost:6379/0",
        supabase_url=supabase_url,
        supabase_secret_key="placeholder",
    )


@pytest.fixture(autouse=True)
def _reset_jwks_cache_singleton() -> Iterator[None]:
    """Isolate `jwks._cache` per test, mirroring `test_pool.py`'s pattern for `_pool`.

    Several tests below call `open_jwks_cache` without going through the app's own
    lifespan, so nothing else would clear the module singleton between them.
    """
    jwks_module._cache = None
    yield
    jwks_module._cache = None


async def test_the_startup_fetch_populates_the_cache_keyed_by_kid(
    jwks_server: _JwksServer, es256_key: _SigningKey, rs256_key: _SigningKey
) -> None:
    """`refresh()` replaces the cache with every usable key, addressable by its own kid."""
    jwks_server.keys = [es256_key.jwk, rs256_key.jwk]
    cache = JwksCache(jwks_server.client, _JWKS_URL)

    await cache.refresh()

    assert cache.key_count() == 2
    for signing_key in (es256_key, rs256_key):
        key = await cache.get_key(signing_key.kid)
        assert key is not None
        assert key.key_id == signing_key.kid
    assert jwks_server.request_count == 1


@pytest.mark.parametrize("supabase_url", ["https://p.supabase.co", "https://p.supabase.co/"])
async def test_the_jwks_url_is_built_from_supabase_url(
    jwks_server: _JwksServer, supabase_url: str
) -> None:
    """A trailing slash on SUPABASE_URL must not produce a doubled slash in the JWKS URL.

    Asserts against `jwks_server.requested_urls[0]` — the URL the REAL httpx client
    actually issued the request against — rather than calling the private
    `_build_jwks_url` helper directly.
    """
    expected = "https://p.supabase.co/auth/v1/.well-known/jwks.json"

    try:
        await open_jwks_cache(_settings(supabase_url), jwks_server.client)
    finally:
        await close_jwks_cache()

    assert jwks_server.requested_urls == [expected]
    assert "//auth" not in expected


async def test_startup_failure_logs_loudly_and_leaves_an_empty_cache(
    jwks_server: _JwksServer, caplog: pytest.LogCaptureFixture
) -> None:
    """A startup fetch failure degrades to an empty cache instead of crashing boot."""
    jwks_server.failure = httpx.ConnectError("boom")

    with caplog.at_level(logging.ERROR):
        cache = await open_jwks_cache(_settings(), jwks_server.client)

    assert cache.key_count() == 0
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    # Type name only (ARCHITECTURE.md §9.4) — never the URL or the exception's own message.
    assert "test-project" not in message
    assert "supabase.co" not in message
    assert "boom" not in message


async def test_a_dns_style_failure_does_not_crash_startup(
    jwks_server: _JwksServer, caplog: pytest.LogCaptureFixture
) -> None:
    """This is the failure shape CI's build-check exercises on every run — its SUPABASE_URL
    does not resolve. A narrower `except httpx.HTTPError` in open_jwks_cache would pass the
    test above and fail this one, because socket.gaierror is an OSError, not an httpx one.
    """
    jwks_server.failure = OSError("Name or service not known")

    with caplog.at_level(logging.ERROR):
        cache = await open_jwks_cache(_settings(), jwks_server.client)

    assert cache.key_count() == 0
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1


async def test_startup_failure_does_not_open_the_rate_limit_window(
    jwks_server: _JwksServer, es256_key: _SigningKey
) -> None:
    """After a failed boot, `_last_refetch_at` is still None: the very next unknown-kid
    lookup refetches immediately, with no 60s wait. This is the mechanic that makes "a
    transient blip should not prevent boot" literally true rather than true-with-an-
    asterisk — see jwks.py's module docstring.
    """
    jwks_server.failure = httpx.ConnectError("boom")
    cache = await open_jwks_cache(_settings(), jwks_server.client)
    assert cache.key_count() == 0
    assert jwks_server.request_count == 1  # the failed startup attempt

    jwks_server.failure = None
    jwks_server.keys = [es256_key.jwk]

    key = await cache.get_key(es256_key.kid)

    assert jwks_server.request_count == 2  # refetched immediately, no window suppressed it
    assert key is not None
    assert key.key_id == es256_key.kid


@pytest.mark.parametrize(
    "body",
    [{"not_keys": []}, {"keys": "nope"}, [], "nope", None],
    ids=["missing-keys-field", "keys-not-a-list", "top-level-list", "top-level-string", "null"],
)
async def test_a_malformed_jwks_document_is_rejected_rather_than_half_parsed(
    jwks_server: _JwksServer, caplog: pytest.LogCaptureFixture, body: Any
) -> None:
    """A broken endpoint must be a FAILED fetch, never a cache half-updated from garbage."""
    jwks_server.body = body

    cache = JwksCache(jwks_server.client, _JWKS_URL)
    with pytest.raises(ValueError):
        await cache.refresh()

    with caplog.at_level(logging.ERROR):
        booted = await open_jwks_cache(_settings(), jwks_server.client)
    assert booted.key_count() == 0
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1


async def test_an_unusable_key_entry_is_skipped_and_the_rest_survives(
    jwks_server: _JwksServer, es256_key: _SigningKey, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad entry must not poison the whole document — only itself."""
    jwks_server.keys = [es256_key.jwk, {"kty": "wat"}, {"alg": "ES256"}]
    cache = JwksCache(jwks_server.client, _JWKS_URL)

    with caplog.at_level(logging.WARNING):
        await cache.refresh()

    assert cache.key_count() == 1
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "2" in message  # a count of the two unusable entries
    assert "wat" not in message  # never the entry itself


async def test_a_symmetric_key_entry_is_never_cached(jwks_server: _JwksServer) -> None:
    """An `oct` (shared-secret) JWKS entry is a public signing secret and must never be
    cached, no matter what the document says — the second of two independent guards
    against algorithm confusion (the allowlist in dependencies.py is the first)."""
    jwks_server.keys = [{"kty": "oct", "k": "not-a-real-secret", "kid": "sym"}]
    cache = JwksCache(jwks_server.client, _JWKS_URL)
    await cache.refresh()

    assert cache.key_count() == 0
    assert await cache.get_key("sym") is None


@pytest.mark.parametrize("status_code", [404, 429, 500, 503])
async def test_a_non_200_response_is_a_failed_fetch(
    jwks_server: _JwksServer, status_code: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Proves `raise_for_status()` is actually called — a 4xx/5xx body is never parsed as
    if it were a JWKS document."""
    jwks_server.status_code = status_code
    cache = JwksCache(jwks_server.client, _JWKS_URL)
    with pytest.raises(httpx.HTTPStatusError):
        await cache.refresh()

    with caplog.at_level(logging.ERROR):
        booted = await open_jwks_cache(_settings(), jwks_server.client)
    assert booted.key_count() == 0
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1


async def test_a_cached_kid_is_returned_without_any_http_request(
    jwks_server: _JwksServer, es256_key: _SigningKey
) -> None:
    """MEASURED: verification performs no network I/O when the kid is already cached.

    Asserted at the real HTTP layer via `jwks_server.request_count` (see its docstring),
    never inferred from reading `get_key`'s source.
    """
    jwks_server.keys = [es256_key.jwk]
    cache = JwksCache(jwks_server.client, _JWKS_URL)
    await cache.refresh()
    baseline = jwks_server.request_count

    for _ in range(50):
        key = await cache.get_key(es256_key.kid)
        assert key is not None

    assert jwks_server.request_count == baseline


async def test_an_unknown_kid_triggers_exactly_one_refetch_then_returns_none(
    jwks_server: _JwksServer,
) -> None:
    cache = JwksCache(jwks_server.client, _JWKS_URL)
    await cache.refresh()
    baseline = jwks_server.request_count

    result = await cache.get_key("does-not-exist")

    assert result is None
    assert jwks_server.request_count == baseline + 1


async def test_a_flood_of_unknown_kids_refetches_once_per_window(
    jwks_server: _JwksServer, fake_clock: _FakeClock
) -> None:
    """MEASURED: an unknown-kid refetch happens at most once per rate-limit window.

    Drives a flood well past a single retry and asserts the counter moved by exactly one —
    then advances the fake clock past the window and asserts exactly one more.
    """
    cache = JwksCache(jwks_server.client, _JWKS_URL, clock=fake_clock)
    await cache.refresh()
    baseline = jwks_server.request_count

    for _ in range(50):
        assert await cache.get_key("nope") is None
    assert jwks_server.request_count == baseline + 1

    fake_clock.advance(59.0)
    for _ in range(10):
        assert await cache.get_key("nope") is None
    assert jwks_server.request_count == baseline + 1  # still inside the window

    fake_clock.advance(1.5)  # now 60.5s since the one refetch above
    assert await cache.get_key("nope") is None
    assert jwks_server.request_count == baseline + 2


async def test_a_concurrent_burst_of_unknown_kid_lookups_refetches_once(
    jwks_server: _JwksServer, fake_clock: _FakeClock
) -> None:
    """MEASURED (concurrency): a burst arriving in the same instant still costs one refetch.

    What actually holds this invariant is `_last_refetch_at` being set *before* the `await`
    that fetches: the window check and that assignment have no suspension point between
    them, so on a single-threaded event loop they are atomic and the losing coroutines see
    a closed window. Verified by mutation — removing `async with self._lock:` leaves this
    test passing, so it does NOT prove the lock is load-bearing, and this docstring
    deliberately no longer claims it does. The lock earns its place in the sibling test
    below (`..._for_a_rotated_kid_all_get_the_new_key`), which does fail without it.

    Meaningful only because `_JwksServer.handle()` suspends with `await asyncio.sleep(0)`
    — without that, an `asyncio.gather` burst would run to completion one coroutine at a
    time and this would be vacuous.
    """
    cache = JwksCache(jwks_server.client, _JWKS_URL, clock=fake_clock)
    await cache.refresh()
    baseline = jwks_server.request_count

    results = await asyncio.gather(*[cache.get_key("nope") for _ in range(50)])

    assert all(result is None for result in results)
    assert jwks_server.request_count == baseline + 1


async def test_a_concurrent_burst_for_a_rotated_kid_all_get_the_new_key(
    jwks_server: _JwksServer, fake_clock: _FakeClock, rotated_key: _SigningKey
) -> None:
    """MEASURED (concurrency): **this is the test that proves the lock is load-bearing.**

    Every caller queued behind the winning refetch finds the new key already in the cache
    instead of being told "unknown" and refetching again itself — otherwise a rotation
    would look like an outage to every request but the one that won the race. Verified by
    mutation: replacing `async with self._lock:` with `if True:` fails this test (the
    losers get `None`) while leaving the sibling burst test above green, which is why the
    lock's justification lives here rather than there.
    """
    cache = JwksCache(jwks_server.client, _JWKS_URL, clock=fake_clock)
    await cache.refresh()
    jwks_server.keys = [rotated_key.jwk]
    baseline = jwks_server.request_count

    results = await asyncio.gather(*[cache.get_key(rotated_key.kid) for _ in range(20)])

    assert all(isinstance(result, PyJWK) for result in results)
    assert jwks_server.request_count == baseline + 1


async def test_a_failed_refetch_still_opens_the_rate_limit_window(
    jwks_server: _JwksServer, fake_clock: _FakeClock
) -> None:
    """`_last_refetch_at` is set BEFORE the fetch attempt and regardless of its outcome — a
    persistently broken JWKS endpoint must be hit at most once per window too, or the
    window does nothing to protect it from exactly the case it exists for.
    """
    cache = JwksCache(jwks_server.client, _JWKS_URL, clock=fake_clock)
    await cache.refresh()
    baseline = jwks_server.request_count

    jwks_server.failure = httpx.ConnectError("boom")
    assert await cache.get_key("nope") is None
    assert jwks_server.request_count == baseline + 1

    assert await cache.get_key("nope") is None
    assert jwks_server.request_count == baseline + 1


def test_get_jwks_cache_before_open_jwks_cache_raises_a_clear_error() -> None:
    """get_jwks_cache() must fail loudly, not with an AttributeError deep in a dependency."""
    with pytest.raises(RuntimeError, match="open_jwks_cache"):
        get_jwks_cache()


async def test_open_jwks_cache_twice_raises_instead_of_replacing_the_first_cache(
    jwks_server: _JwksServer,
) -> None:
    """A second open_jwks_cache() must fail loudly rather than silently orphan the first."""
    first = await open_jwks_cache(_settings(), jwks_server.client)
    try:
        with pytest.raises(RuntimeError, match="already open"):
            await open_jwks_cache(_settings(), jwks_server.client)
        assert get_jwks_cache() is first
    finally:
        await close_jwks_cache()


async def test_close_jwks_cache_is_safe_to_call_twice(jwks_server: _JwksServer) -> None:
    await open_jwks_cache(_settings(), jwks_server.client)
    await close_jwks_cache()
    await close_jwks_cache()  # must not raise
