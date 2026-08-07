"""Tests for `app.worker.jobs.crawl_task`, driven against a real Postgres.

Every website seeded here uses a public IP literal (`8.8.8.8`) as its URL, never a hostname.
That sidesteps a real problem specific to this suite: `internals/ssrf.py`'s DNS resolution
goes through the event loop's real `getaddrinfo`, not through `httpx`, so this suite's
autouse `_forbid_real_network` fixture (which only patches `httpx.AsyncHTTPTransport`) does
not — and cannot — stop a hostname lookup from reaching a real resolver. An IP literal skips
DNS entirely (`internals/ssrf.py`'s `_parse_ip_literal`), so every fetch in this file is
fully hermetic: `ctx["http_client"]` is always built with `transport=httpx.MockTransport(...)`,
and nothing here ever asks a real question of a real network.

`ctx["storage"]` is `conftest.py`'s `FakeStorage` — a structural stand-in for
`SupabaseStorage` that records calls without another `httpx.MockTransport` for the upload
leg. `tests/test_run_persistence.py` is the suite that asserts on what `FakeStorage` records;
this file only needs it present so `crawl_task` has something to build a `CrawlService` from.
A run that reaches a successful crawl now ends `completed`, not `processing` — persisting the
result is this ticket's whole point (PER-163) — so the happy-path test below asserts that,
where an earlier revision of this suite (written before persistence existed) asserted the
opposite.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from asyncpg import Pool
from conftest import TEST_USER_A_ID, FakeStorage, seed_run, seed_website

from app.worker.jobs import crawl_task
from app.worker.policy import MAX_ATTEMPTS


_NOW = datetime.now(UTC)

# Any globally-routable IPv4 literal works; Google's public DNS resolver is a stable,
# well-known one and — because it is a literal, not a hostname — `validate_url` never
# resolves it, so which real service actually answers on it is irrelevant to this suite.
_SEED_IP = "8.8.8.8"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_a_pending_run_is_claimed_and_the_task_records_both_artifacts(
    websites_db: Pool,
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/claim-ok")
    run_id = await seed_run(websites_db, website_id, started_at=_NOW, status="pending")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello world")

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        result = await crawl_task(ctx, str(run_id))

    assert result == "ok"

    row = await websites_db.fetchrow(
        "SELECT status, llms_txt, llms_full_txt, storage_path FROM runs WHERE id = $1", run_id
    )
    assert row is not None
    assert row["status"] == "completed"
    assert row["llms_txt"] is not None
    assert row["llms_full_txt"] is not None
    assert row["storage_path"] is not None


@pytest.mark.parametrize("status", ["processing", "completed", "failed"])
async def test_a_non_pending_run_is_skipped_and_left_untouched(
    websites_db: Pool, status: str
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/skip-{status}")
    run_id = await seed_run(websites_db, website_id, started_at=_NOW, status=status)
    before = dict(await websites_db.fetchrow("SELECT * FROM runs WHERE id = $1", run_id))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("crawl_task must not fetch anything for a non-pending run")

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        result = await crawl_task(ctx, str(run_id))

    assert result == "no outcome"

    after = dict(await websites_db.fetchrow("SELECT * FROM runs WHERE id = $1", run_id))
    assert before == after


async def test_a_run_id_naming_no_run_returns_rather_than_raising(websites_db: Pool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("crawl_task must not fetch anything for a nonexistent run")

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        result = await crawl_task(ctx, str(uuid4()))

    assert result == "no outcome"


async def test_a_malformed_run_id_returns_rather_than_raising(websites_db: Pool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("crawl_task must not fetch anything for a malformed run id")

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        result = await crawl_task(ctx, "not-a-uuid")

    assert result == "invalid run id"


async def test_seed_url_failure_leaves_the_run_failed(websites_db: Pool) -> None:
    """A connect error on the seed is RETRYABLE (PER-166), so this run is seeded one attempt
    short of the cap: the claim takes it to `MAX_ATTEMPTS` and the failure is therefore the
    terminal one, which is the case this test has always been about. The budget-remaining
    half — the same failure returning the run to `pending` and asking arq for a redelivery —
    lives in tests/test_crawl_retry.py."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/seed-fails")
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        status="pending",
        attempts=MAX_ATTEMPTS - 1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        result = await crawl_task(ctx, str(run_id))

    assert result == "no outcome"

    row = await websites_db.fetchrow(
        "SELECT status, error, completed_at, attempts FROM runs WHERE id = $1", run_id
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    assert row["attempts"] == MAX_ATTEMPTS
    # The attempt count is IN the message, which is what lets the UI tell a site that has
    # been down all morning apart from one that blipped once.
    assert row["error"] == f"Failed after {MAX_ATTEMPTS} attempts: Could not connect to the site."


async def test_the_recorded_error_never_leaks_exception_internals(websites_db: Pool) -> None:
    """A sanitizing function that merely happens not to mention anything dangerous in its
    fixed strings would still pass a test built only around those fixed strings. This test
    puts dangerous content INSIDE the underlying exception's own message instead, so it only
    passes if `_safe_error_message` actually replaces `str(exc)` rather than including it."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/seed-leaks")
    # One attempt short of the cap, so this failure is terminal and actually writes a row —
    # see `test_seed_url_failure_leaves_the_run_failed` above.
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        status="pending",
        attempts=MAX_ATTEMPTS - 1,
    )

    dangerous_detail = (
        "Connection to 10.1.2.3 refused: httpx.ConnectError Traceback (most recent call last): ..."
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(dangerous_detail, request=request)

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        await crawl_task(ctx, str(run_id))

    row = await websites_db.fetchrow("SELECT error FROM runs WHERE id = $1", run_id)
    assert row is not None
    error = row["error"]
    assert error is not None
    assert "10.1.2.3" not in error
    assert "httpx." not in error
    assert "Traceback" not in error
    assert dangerous_detail not in error


async def test_two_concurrent_calls_for_the_same_run_only_one_claims_it(
    websites_db: Pool,
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/race")
    run_id = await seed_run(websites_db, website_id, started_at=_NOW, status="pending")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": websites_db, "http_client": http_client, "storage": FakeStorage()}
        results = await asyncio.gather(crawl_task(ctx, str(run_id)), crawl_task(ctx, str(run_id)))

    assert sorted(results) == ["no outcome", "ok"]
