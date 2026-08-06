"""Tests for `POST /websites/{id}/runs` — the manual run trigger and its per-user caps.

Its own file rather than an addition to `tests/test_runs_api.py`, because this is the only
suite in the backend that needs BOTH a real Postgres AND a real Redis. Keeping it separate
means `test_runs_api.py`'s two read endpoints keep exercising and passing purely off
`websites_db` — they have never depended on Redis and should not start skipping (or worse,
silently passing for the wrong reason) the day someone's local Redis container is down.

Every test drives the real application (`app.main.app`) exactly as `test_runs_api.py` and
`test_websites_api.py` do, with one addition: `queue_override` (or one of its two sibling
fixtures) swaps `app.api.routers.runs.require_queue_pool` for something under this test's
control. Overriding THAT function rather than `app.api.deps.get_arq_pool` is deliberate —
`require_queue_pool`'s own docstring explains why it calls `get_arq_pool()` from inside its
own `try` instead of declaring it as a sub-dependency, and the only way a test can prove that
wiring is real (rather than merely plausible) is to exercise `require_queue_pool` itself,
never a stand-in for the function one layer below it.

Seeding uses `conftest.seed_run` / `conftest.seed_website` directly against the database,
never through the API — the same reasoning `test_websites_api.py`'s module docstring gives:
a test that built its own fixtures by POSTing would not be able to tell "the trigger endpoint
is wrong" apart from "the thing I seeded with is wrong".
"""

import importlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from asyncpg import Pool
from conftest import TEST_QUEUE_NAME, TEST_USER_A_ID, TEST_USER_B_ID, seed_run, seed_website
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient

from app.api.deps import get_settings
from app.api.routers.runs import get_run_service, require_queue_pool
from app.core.settings import Settings
from app.features.runs.service import CRAWL_TASK_JOB_NAME, RunService
from app.features.websites.schemas import WebsiteResponse
from app.main import app


_NOW = datetime.now(UTC)


# -----------------------------------------------------------------------------------------
# Module-local fixtures: three ways of controlling what `require_queue_pool` hands back.
# -----------------------------------------------------------------------------------------


@pytest.fixture
async def queue_override(api_app: FastAPI, queue_pool: ArqRedis) -> AsyncIterator[ArqRedis]:
    """Wire `POST /websites/{id}/runs` to the real, throwaway Redis behind `queue_pool`.

    Overrides `require_queue_pool` — not `app.api.deps.get_arq_pool` one layer below it —
    because the whole point of that function's shape (see its docstring) is that FastAPI
    resolves it directly rather than as a declared sub-dependency; overriding the wrong
    layer would still make the happy path pass without proving that wiring is real.

    Restores whatever override, if any, preceded this one, never `.clear()` — the same
    contract `api_app` itself keeps in `conftest.py`, for the same reason: clearing would
    silently discard an override belonging to an enclosing fixture.

    Flushes the queue on entry AND exit. `queue_pool` is session-scoped and only flushes at
    session boundaries (see its own docstring in `conftest.py`), so without this a job left
    behind by an earlier test in this file would make "exactly one job queued" assertions
    pass or fail for the wrong reason.
    """
    await queue_pool.flushdb()
    sentinel = object()
    previous = app.dependency_overrides.get(require_queue_pool, sentinel)
    app.dependency_overrides[require_queue_pool] = lambda: queue_pool
    try:
        yield queue_pool
    finally:
        if previous is sentinel:
            app.dependency_overrides.pop(require_queue_pool, None)
        else:
            app.dependency_overrides[require_queue_pool] = previous
        await queue_pool.flushdb()


class _FailingQueuePool:
    """A stand-in for `ArqRedis` whose `enqueue_job` always raises.

    Exercises `RunService.trigger_run`'s `except Exception` path — see the module docstring
    on `service.py` for why that `except` is deliberately broad rather than
    `except RedisError`. `ConnectionError` is one of the several exception types that
    docstring names as something a real Redis client can raise; it is not `RedisError`
    itself, which is the point — a narrower `except` in the service would let this test
    fail by falling through as an unhandled `500`.
    """

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("simulated Redis failure (test double)")


@pytest.fixture
def failing_queue_override(api_app: FastAPI) -> Iterator[None]:
    """Like `queue_override`, but hands the router a pool that always fails to enqueue.

    Deliberately does not touch the real `queue_pool` fixture's Redis at all — the failure
    happens entirely inside `_FailingQueuePool`, in-process, so this fixture needs no
    database or network setup of its own beyond the override.
    """
    sentinel = object()
    previous = app.dependency_overrides.get(require_queue_pool, sentinel)
    app.dependency_overrides[require_queue_pool] = lambda: _FailingQueuePool()
    try:
        yield
    finally:
        if previous is sentinel:
            app.dependency_overrides.pop(require_queue_pool, None)
        else:
            app.dependency_overrides[require_queue_pool] = previous


def _raise_queue_unavailable() -> ArqRedis:
    """What `require_queue_pool` itself does when `get_arq_pool()` raises — reproduced here
    directly so `queue_unavailable_override` below can install it without a real `ArqPool`
    dependency override underneath it."""
    raise HTTPException(status_code=503, detail="Queue unavailable (test)")


@pytest.fixture
def queue_unavailable_override(api_app: FastAPI) -> Iterator[None]:
    """Simulate Redis never having been reachable at all — `require_queue_pool` raises its
    own `503` before `RunService.trigger_run` is ever called.

    The distinction from `failing_queue_override` is the entire point of this fixture: that
    one hands the service a pool that exists but fails on use (a row IS created, then
    marked `failed`); this one never hands the service anything, because FastAPI resolves
    `Depends(require_queue_pool)` before the route handler's body runs at all — so the
    correct outcome here is `503` with NO row ever created, not even a `failed` one.
    """
    sentinel = object()
    previous = app.dependency_overrides.get(require_queue_pool, sentinel)
    app.dependency_overrides[require_queue_pool] = _raise_queue_unavailable
    try:
        yield
    finally:
        if previous is sentinel:
            app.dependency_overrides.pop(require_queue_pool, None)
        else:
            app.dependency_overrides[require_queue_pool] = previous


async def _run_count_for_website(pool: Pool, website_id: UUID) -> int:
    count: int = await pool.fetchval("SELECT count(*) FROM runs WHERE website_id = $1", website_id)
    return count


# -----------------------------------------------------------------------------------------
# 1. The happy path — 202, the row, and the queued job
# -----------------------------------------------------------------------------------------


async def test_owner_triggers_a_run(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://trigger-happy.example")

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert "started_at" in body
    run_id = UUID(body["id"])

    row = await websites_db.fetchrow(
        'SELECT status, "trigger", schedule_id FROM runs WHERE id = $1', run_id
    )
    assert row is not None
    assert row["status"] == "pending"
    assert row["trigger"] == "manual"
    assert row["schedule_id"] is None

    jobs = await queue_override.queued_jobs(queue_name=TEST_QUEUE_NAME)
    assert len(jobs) == 1
    assert jobs[0].function == CRAWL_TASK_JOB_NAME
    assert jobs[0].args == (str(run_id),)


# -----------------------------------------------------------------------------------------
# 2-4. Ownership and the duplicate-run guard
# -----------------------------------------------------------------------------------------


async def test_a_non_owner_cannot_trigger_a_run(
    second_user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    """403, and — the assertion that actually matters — no row was created at all. A test
    that only checked the status code would pass even if `require_owner` ran AFTER a write."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://not-mine-trigger.example")
    before = await _run_count_for_website(websites_db, website_id)

    response = await second_user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 403
    assert await _run_count_for_website(websites_db, website_id) == before
    assert await queue_override.queued_jobs(queue_name=TEST_QUEUE_NAME) == []


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_an_active_run_on_the_same_website_is_409(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis, status: str
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"https://active-{status}.example")
    existing = await seed_run(websites_db, website_id, started_at=_NOW, status=status)

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "run_already_in_flight"
    assert detail["run_id"] == str(existing)
    assert detail["status"] == status
    assert await _run_count_for_website(websites_db, website_id) == 1
    assert await queue_override.queued_jobs(queue_name=TEST_QUEUE_NAME) == []


@pytest.mark.parametrize("previous_status", ["completed", "failed"])
async def test_a_finished_previous_run_does_not_block_a_new_trigger(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis, previous_status: str
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://finished-{previous_status}.example"
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW - timedelta(hours=1),
        status=previous_status,
        completed_at=_NOW,
        error="boom" if previous_status == "failed" else None,
    )

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 202


# -----------------------------------------------------------------------------------------
# 5-8. The per-user concurrency cap
# -----------------------------------------------------------------------------------------


async def test_a_third_concurrent_manual_run_across_different_websites_hits_the_cap(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    """Covers two of the ticket's bullets at once, because they are the same scenario: a
    "third concurrent run" cannot be reproduced against a SINGLE website (the second active
    run on that same website would already be a 409 from the duplicate guard above), so
    proving the concurrency cap counts across a user's websites — not per website — is not
    an extra scenario on top of "hits the cap", it is the only way to construct that case at
    all. Two active manual runs on two different websites the caller owns, then a third
    trigger on a third: 429, naming the configured limit, with no reset time.
    """
    website_a = await seed_website(websites_db, TEST_USER_A_ID, "https://cap-a.example")
    website_b = await seed_website(websites_db, TEST_USER_A_ID, "https://cap-b.example")
    website_c = await seed_website(websites_db, TEST_USER_A_ID, "https://cap-c.example")
    await seed_run(websites_db, website_a, started_at=_NOW, status="pending", trigger="manual")
    await seed_run(websites_db, website_b, started_at=_NOW, status="processing", trigger="manual")

    response = await user_client.post(f"/websites/{website_c}/runs")

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == "run_limit_exceeded"
    assert detail["scope"] == "concurrent"
    assert detail["limit"] == 2
    assert "2" in detail["message"]
    assert detail["resets_at"] is None
    assert await queue_override.queued_jobs(queue_name=TEST_QUEUE_NAME) == []


async def test_another_users_active_runs_do_not_count_toward_this_users_cap(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    """The test that fails the day the concurrency-cap JOIN drops `websites.user_id = $1` —
    seeded to exactly this codebase's configured limit (2) so that if the join were ever
    scoped to something else (or dropped entirely), this would flip from `202` to `429`."""
    other_website_a = await seed_website(websites_db, TEST_USER_B_ID, "https://other-cap-a.example")
    other_website_b = await seed_website(websites_db, TEST_USER_B_ID, "https://other-cap-b.example")
    await seed_run(
        websites_db, other_website_a, started_at=_NOW, status="pending", trigger="manual"
    )
    await seed_run(
        websites_db, other_website_b, started_at=_NOW, status="processing", trigger="manual"
    )
    my_website = await seed_website(
        websites_db, TEST_USER_A_ID, "https://mine-despite-others.example"
    )

    response = await user_client.post(f"/websites/{my_website}/runs")

    assert response.status_code == 202


async def test_active_scheduled_runs_do_not_count_toward_the_concurrency_cap(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    """The deliberate asymmetry `internals/runs_reader.py` documents: the concurrency cap
    counts `trigger = 'manual'` only, so two ACTIVE SCHEDULED runs must not occupy either of
    the caller's two concurrency slots."""
    website_a = await seed_website(
        websites_db, TEST_USER_A_ID, "https://scheduled-active-a.example"
    )
    website_b = await seed_website(
        websites_db, TEST_USER_A_ID, "https://scheduled-active-b.example"
    )
    await seed_run(websites_db, website_a, started_at=_NOW, status="pending", trigger="scheduled")
    await seed_run(
        websites_db, website_b, started_at=_NOW, status="processing", trigger="scheduled"
    )
    website_c = await seed_website(
        websites_db, TEST_USER_A_ID, "https://scheduled-active-c.example"
    )

    response = await user_client.post(f"/websites/{website_c}/runs")

    assert response.status_code == 202


# -----------------------------------------------------------------------------------------
# 9-11. The rolling-24h daily cap
# -----------------------------------------------------------------------------------------


async def test_the_51st_run_in_24h_hits_the_daily_cap(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://daily-cap.example")
    oldest_started_at = _NOW - timedelta(hours=23)
    for i in range(50):
        await seed_run(
            websites_db,
            website_id,
            started_at=oldest_started_at + timedelta(minutes=i),
            status="completed",
            completed_at=_NOW,
        )

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == "run_limit_exceeded"
    assert detail["scope"] == "daily"
    assert detail["limit"] == 50
    assert "50" in detail["message"]
    assert "Resets in" in detail["message"]
    assert datetime.fromisoformat(detail["resets_at"]) == oldest_started_at + timedelta(hours=24)


async def test_runs_older_than_24h_do_not_count_toward_the_daily_cap(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stale-daily-cap.example")
    stale_start = _NOW - timedelta(hours=25)
    for i in range(50):
        await seed_run(
            websites_db,
            website_id,
            started_at=stale_start + timedelta(minutes=i),
            status="completed",
            completed_at=_NOW,
        )

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 202


async def test_scheduled_runs_count_toward_the_daily_cap(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    """The other half of the deliberate asymmetry: unlike the concurrency cap, the daily
    cap counts EVERY trigger, because it bounds total worker time rather than button-clicks
    (`internals/runs_reader.py`'s asymmetry section)."""
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://scheduled-daily-cap.example"
    )
    oldest_started_at = _NOW - timedelta(hours=1)
    for i in range(50):
        await seed_run(
            websites_db,
            website_id,
            started_at=oldest_started_at + timedelta(seconds=i),
            status="completed",
            trigger="scheduled",
            completed_at=_NOW,
        )

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 429
    assert response.json()["detail"]["scope"] == "daily"


# -----------------------------------------------------------------------------------------
# 12-13. The "no silent orphan" guarantee
# -----------------------------------------------------------------------------------------


async def test_an_enqueue_failure_marks_the_run_failed_and_returns_503(
    user_client: AsyncClient,
    websites_db: Pool,
    queue_pool: ArqRedis,
    failing_queue_override: None,
) -> None:
    """The ticket's central acceptance criterion: if `enqueue_job` fails after the INSERT
    already committed, the row must not be left `pending` with nothing behind it. Checks the
    REAL queue (`queue_pool`, not the failing stub) to prove nothing landed there either —
    `_FailingQueuePool` never touches Redis at all, so an empty queue here is not a given
    unless something upstream actually stopped short of enqueuing.
    """
    await queue_pool.flushdb()
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://enqueue-fails.example")

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 503
    row = await websites_db.fetchrow(
        "SELECT status, error, completed_at FROM runs WHERE website_id = $1", website_id
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] is not None
    assert row["completed_at"] is not None
    assert await queue_pool.queued_jobs(queue_name=TEST_QUEUE_NAME) == []


async def test_queue_pool_unavailable_returns_503_with_no_row_created_at_all(
    user_client: AsyncClient, websites_db: Pool, queue_unavailable_override: None
) -> None:
    """The other 503 — Redis was never reachable, so `require_queue_pool` raises before the
    route handler's body runs at all. No row is created, not even a `failed` one, which is
    the assertion that distinguishes this from the enqueue-failure test above."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://queue-down.example")

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 503
    assert await _run_count_for_website(websites_db, website_id) == 0


# -----------------------------------------------------------------------------------------
# 14-15. 404 and 401
# -----------------------------------------------------------------------------------------


async def test_an_unknown_website_is_404(
    user_client: AsyncClient, queue_override: ArqRedis
) -> None:
    """`queue_override` is required here even though this test is about a 404: FastAPI
    resolves `Depends(require_queue_pool)` for every request to this route regardless of
    what `id` names, so an un-overridden (real, absent-in-tests) queue pool would answer
    `503` before `RunService.trigger_run` ever got a chance to look up the website."""
    response = await user_client.post(f"/websites/{uuid4()}/runs")
    assert response.status_code == 404


async def test_an_unauthenticated_request_is_401(
    unauthenticated_client: AsyncClient, queue_override: ArqRedis
) -> None:
    """`queue_override` is included defensively, not because the test needs a working
    queue: it proves `401` wins even when nothing else about the request would fail, which
    is a strictly stronger claim than "401 happens to win because the queue is also down".
    """
    response = await unauthenticated_client.post(f"/websites/{uuid4()}/runs")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


class _VanishingWebsiteService:
    """A `WebsiteService` that reports a website which is not actually in the database.

    This is how the deleted-between-fetch-and-INSERT race in `RunService.trigger_run` is
    reproduced deterministically. The real race needs a `DELETE` to land inside a window a
    few statements wide, which no test can schedule reliably; substituting the ONE
    collaborator whose answer the race invalidates reaches the same line of code — the
    `except ForeignKeyViolationError` around the insert — with no sleeping and no flakiness.

    Ownership is reported as `TEST_USER_A_ID` on purpose: `require_owner` has to PASS, or the
    request would stop at a `403` well before reaching the insert this test is about.
    """

    def __init__(self, user_id: UUID) -> None:
        self._user_id = user_id

    async def get_website(self, website_id: UUID) -> WebsiteResponse:
        return WebsiteResponse(
            id=website_id,
            user_id=self._user_id,
            url="https://vanished.example/",
            origin="https://vanished.example",
            title=None,
            created_at=_NOW,
        )


async def test_a_website_deleted_between_the_fetch_and_the_insert_is_404(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis
) -> None:
    """The website passed the `get_website` check and was gone by the INSERT.

    `runs_website_id_fkey` rejects the row, and the service converts that
    `ForeignKeyViolationError` into the SAME `404` — same status, same detail string — the
    sequential "no such website" case produces, so a client cannot tell the two apart. The
    same race, and the same conversion, that `ScheduleService.upsert_schedule` handles for
    its own foreign key.

    Without that conversion this request is a `500`: an unhandled `asyncpg` error escaping a
    service is a bug report, not an answer.
    """
    missing_id = uuid4()
    sentinel = object()
    previous = app.dependency_overrides.get(get_run_service, sentinel)
    app.dependency_overrides[get_run_service] = lambda: RunService(
        websites_db, _VanishingWebsiteService(TEST_USER_A_ID), get_settings()
    )
    try:
        response = await user_client.post(f"/websites/{missing_id}/runs")
    finally:
        if previous is sentinel:
            app.dependency_overrides.pop(get_run_service, None)
        else:
            app.dependency_overrides[get_run_service] = previous

    assert response.status_code == 404
    assert response.json()["detail"] == "No website with that id"
    assert await queue_override.queued_jobs(queue_name=TEST_QUEUE_NAME) == []


# -----------------------------------------------------------------------------------------
# 16. Both caps are configurable, not hardcoded
#
# Every cap test above asserts against the SHIPPED defaults (2 and 50), which is what the
# ticket specifies — but a service that ignored `Settings` entirely and hardcoded those two
# numbers would pass all of them. These two tests are the ones that fail in that case, and
# they are why `RunService` takes a `Settings` rather than reading the module singleton (see
# `get_run_service` in `app.api.routers.runs`).
# -----------------------------------------------------------------------------------------


@pytest.fixture
def lowered_caps(api_app: FastAPI) -> Iterator[None]:
    """Override `get_settings` with caps of 1 and 1 for the duration of one test.

    A whole `Settings` object rather than a patch of two attributes on the real one:
    `app.core.settings.settings` is a process-wide singleton every other test in this
    session shares, and mutating it in place would leak into whatever ran next if a test
    failed before its teardown. Restores the previous override rather than clearing, the
    same contract every other fixture in this file keeps.
    """
    lowered = Settings(
        database_url="postgresql://localhost:5432/unused-by-this-test",
        redis_url="redis://localhost:6379/15",
        supabase_url="https://test-project.supabase.co",
        supabase_secret_key="not-a-real-key",
        max_concurrent_runs_per_user=1,
        max_runs_per_day_per_user=1,
    )
    sentinel = object()
    previous = app.dependency_overrides.get(get_settings, sentinel)
    app.dependency_overrides[get_settings] = lambda: lowered
    try:
        yield
    finally:
        if previous is sentinel:
            app.dependency_overrides.pop(get_settings, None)
        else:
            app.dependency_overrides[get_settings] = previous


async def test_the_concurrency_cap_honours_a_lowered_setting(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis, lowered_caps: None
) -> None:
    """With `max_concurrent_runs_per_user = 1`, ONE active manual run is already too many —
    a number that is not the shipped default of 2, so a hardcoded `2` fails here."""
    blocked = await seed_website(websites_db, TEST_USER_A_ID, "https://lowered-concurrent.example")
    other = await seed_website(websites_db, TEST_USER_A_ID, "https://lowered-concurrent-2.example")
    await seed_run(websites_db, other, started_at=_NOW, status="processing", trigger="manual")

    response = await user_client.post(f"/websites/{blocked}/runs")

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["scope"] == "concurrent"
    assert detail["limit"] == 1
    assert "1" in detail["message"]
    assert await _run_count_for_website(websites_db, blocked) == 0


async def test_the_daily_cap_honours_a_lowered_setting(
    user_client: AsyncClient, websites_db: Pool, queue_override: ArqRedis, lowered_caps: None
) -> None:
    """With `max_runs_per_day_per_user = 1`, one COMPLETED run inside the window is already
    the whole daily budget — it cannot trip the concurrency cap checked before it, so this
    isolates the daily branch and its reset time on a lowered limit."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://lowered-daily.example")
    started_at = _NOW - timedelta(hours=2)
    await seed_run(
        websites_db, website_id, started_at=started_at, status="completed", completed_at=_NOW
    )

    response = await user_client.post(f"/websites/{website_id}/runs")

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["scope"] == "daily"
    assert detail["limit"] == 1
    assert datetime.fromisoformat(detail["resets_at"]) == started_at + timedelta(hours=24)
    assert await _run_count_for_website(websites_db, website_id) == 1


# -----------------------------------------------------------------------------------------
# 17. The cross-ticket coupling guard
# -----------------------------------------------------------------------------------------


def test_crawl_task_job_name_matches_the_worker_function() -> None:
    """`CRAWL_TASK_JOB_NAME` (`app.features.runs.service`) is a bare string, not an import
    of `app.worker.jobs.crawl_task` — ARCHITECTURE.md §3.3 forbids `app/features/` importing
    `app/worker/`, and PER-159 (which adds that function) is landing concurrently with this
    ticket, so the function does not exist yet at the time this test is written.

    Skips, rather than failing, while that is true — a suite failing for code a different,
    in-flight ticket owns would be noise, not signal. The moment PER-159 lands
    `async def crawl_task(ctx, run_id)`, this starts asserting the one fact that makes the
    string-based coupling between the two tickets safe: arq registers a function under its
    `__qualname__`, so `CRAWL_TASK_JOB_NAME` has to stay character-identical to it, or every
    run this endpoint triggers would enqueue a job the worker never picks up.
    """
    module = importlib.import_module("app.worker.jobs")
    crawl_task = getattr(module, "crawl_task", None)
    if crawl_task is None:
        pytest.skip("app.worker.jobs.crawl_task does not exist yet — see PER-159")
    assert crawl_task.__qualname__ == CRAWL_TASK_JOB_NAME
