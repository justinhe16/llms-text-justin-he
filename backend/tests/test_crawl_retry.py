"""Tests for PER-166's retry policy: which failures earn another attempt, which do not, and
what the `runs` row says afterwards.

**Driven through `app.worker.jobs.crawl_task`, not through `CrawlService` directly**, because
half of what this ticket added lives in the seam between them: the service decides that an
attempt was transient, and the job turns that into `arq.worker.Retry`. A test that called the
service would prove the classification and miss the translation, which is the part a queue
actually acts on.

Real Postgres, same as `tests/test_crawl_task.py` and for the same reason its module docstring
gives: every website here is seeded with an IP literal, never a hostname, so `internals/
ssrf.py`'s real `getaddrinfo` has nothing to resolve and the autouse `_forbid_real_network`
fixture is never asked to stop a DNS lookup it cannot see. `8.8.8.8` is the public literal;
`127.0.0.1` is the one deliberately used to make `validate_url` refuse.

**Every assertion is on the `runs` row, not on what a function returned.** `crawl_task`
answering `"no outcome"` says nothing about whether the run is recoverable — `pending`,
`processing`, and `failed` all produce that same string — and "no run is ever left
`processing`" is a claim about the database.

`runs.attempts` is seeded directly where a test needs a run part-way through its budget. The
column is only ever written by `claim_pending`, so the alternative would be running two real
crawls to reach the third, which would make every one of these tests a test of the two
attempts it did not care about.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from arq.connections import ArqRedis
from arq.worker import Retry, Worker
from asyncpg import Pool
from conftest import TEST_QUEUE_NAME, TEST_USER_A_ID, FakeStorage, seed_run, seed_website

import app.features.crawl.service as crawl_service
from app.features.crawl.internals.crawler import CrawlResult
from app.worker.jobs import crawl_task
from app.worker.policy import MAX_ATTEMPTS, retry_delay_seconds


_NOW = datetime.now(UTC)
_SEED_IP = "8.8.8.8"

# A link-local literal `internals/ssrf.py` refuses outright. Chosen over a hostname so that
# the refusal comes from `validate_url`'s address classification and not from a DNS lookup
# this suite is not allowed to make.
_BLOCKED_IP = "127.0.0.1"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="hello world")


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("simulated connection failure", request=request)


async def _seed(pool: Pool, suffix: str, *, attempts: int = 0, host: str = _SEED_IP) -> UUID:
    website_id = await seed_website(pool, TEST_USER_A_ID, f"http://{host}/{suffix}")
    return await seed_run(pool, website_id, started_at=_NOW, status="pending", attempts=attempts)


async def _run_task(
    pool: Pool, run_id: UUID, handler: Callable[[httpx.Request], httpx.Response]
) -> str:
    async with _mock_client(handler) as http_client:
        ctx = {"db_pool": pool, "http_client": http_client, "storage": FakeStorage()}
        result: str = await crawl_task(ctx, str(run_id))
        return result


async def _row(pool: Pool, run_id: UUID) -> dict[str, object]:
    row = await pool.fetchrow(
        "SELECT status, error, attempts, claimed_at, completed_at FROM runs WHERE id = $1",
        run_id,
    )
    assert row is not None
    return dict(row)


# -----------------------------------------------------------------------------------------
# 1. The claim is what counts an attempt.
# -----------------------------------------------------------------------------------------


async def test_claiming_a_run_increments_attempts_and_stamps_claimed_at(
    websites_db: Pool,
) -> None:
    """Both columns are written by the same conditional UPDATE that wins the claim.

    Asserted together because they are written together, and the reaper's correctness rests
    on that: a `processing` row with a stale `claimed_at` would be reaped early, and one with
    an un-incremented `attempts` would retry forever.
    """
    run_id = await _seed(websites_db, "claim-counts")
    # Read from the DATABASE clock, not `datetime.now(UTC)`. `claimed_at` is written by
    # Postgres's own `now()`, and the local Postgres runs in a Docker VM whose clock drifts a
    # millisecond or two behind the host's — enough to make a host-clock comparison fail
    # intermittently, which is a flaky test rather than a real signal.
    before: datetime = await websites_db.fetchval("SELECT now()")

    await _run_task(websites_db, run_id, _ok)

    row = await _row(websites_db, run_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 1
    claimed_at = row["claimed_at"]
    assert isinstance(claimed_at, datetime)
    assert claimed_at >= before


async def test_a_run_that_loses_the_claim_race_does_not_consume_an_attempt(
    websites_db: Pool,
) -> None:
    """`attempts` counts attempts that STARTED. A job that arrives to find the run already
    claimed has not attempted anything, and inflating the counter from it would spend a
    budget on a duplicate delivery — the exact thing `claim_pending`'s guard exists to make
    free."""
    run_id = await _seed(websites_db, "claim-race", attempts=1)
    await websites_db.execute(
        "UPDATE runs SET status = 'processing'::run_status WHERE id = $1", run_id
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a run that is already processing must not be fetched")

    result = await _run_task(websites_db, run_id, handler)

    assert result == "no outcome"
    assert (await _row(websites_db, run_id))["attempts"] == 1


# -----------------------------------------------------------------------------------------
# 2. Retryable: the run goes back to the queue, and arq is asked for a redelivery.
# -----------------------------------------------------------------------------------------


async def test_a_transient_failure_with_budget_left_asks_arq_to_retry(
    websites_db: Pool,
) -> None:
    """The whole retry mechanism in one test: a connect error on attempt 1 of 3 puts the run
    back in `pending` — claimable again — and raises `arq.worker.Retry` so a redelivery is
    actually scheduled.

    `Retry` and not some other exception matters more than it looks: arq 0.28 retries a job
    on `Retry`, `RetryJob`, or `CancelledError` and treats every other exception as a terminal
    job failure, so a policy that raised anything else would leave the run `pending` with
    nothing coming for it until the reaper noticed fifteen minutes later.
    """
    run_id = await _seed(websites_db, "transient-retry")

    with pytest.raises(Retry) as raised:
        await _run_task(websites_db, run_id, _connect_error)

    # The deferral is sized from the RUN's attempt counter, not arq's per-job-id `job_try`.
    assert raised.value.defer_score == retry_delay_seconds(1) * 1000

    row = await _row(websites_db, run_id)
    assert row["status"] == "pending", "the redelivered job needs something to claim"
    assert row["attempts"] == 1, "the attempt happened and is not rolled back"
    assert row["claimed_at"] is None, "no attempt is in flight any more"
    assert row["error"] is None, "a retry is not a failure and must not write one"
    assert row["completed_at"] is None


async def test_a_transient_failure_that_succeeds_on_the_second_attempt_ends_completed(
    websites_db: Pool,
) -> None:
    """The acceptance criterion, driven as two real deliveries of the same job rather than
    simulated: the first attempt fails transiently and asks for a retry, the second finds the
    run `pending` again, claims it, and completes it."""
    run_id = await _seed(websites_db, "recovers-on-two")

    with pytest.raises(Retry):
        await _run_task(websites_db, run_id, _connect_error)
    assert (await _row(websites_db, run_id))["status"] == "pending"

    assert await _run_task(websites_db, run_id, _ok) == "ok"

    row = await _row(websites_db, run_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2, "both attempts are counted"
    assert row["error"] is None


async def test_the_retry_deferral_grows_with_the_runs_own_attempt_count(
    websites_db: Pool,
) -> None:
    """Attempt 2's backoff is longer than attempt 1's, and both are read off the run's
    counter.

    This is the assertion that would catch sizing the backoff from `ctx["job_try"]` instead:
    the reaper re-enqueues stranded runs under fresh arq job ids, which resets `job_try` to 1,
    so a run that had been failing for an hour would restart the ladder at ten seconds every
    time it was rescued.
    """
    first = await _seed(websites_db, "defer-first", attempts=0)
    second = await _seed(websites_db, "defer-second", attempts=1)

    with pytest.raises(Retry) as first_retry:
        await _run_task(websites_db, first, _connect_error)
    with pytest.raises(Retry) as second_retry:
        await _run_task(websites_db, second, _connect_error)

    assert second_retry.value.defer_score is not None
    assert first_retry.value.defer_score is not None
    assert second_retry.value.defer_score > first_retry.value.defer_score


async def test_a_transient_failure_on_the_last_attempt_is_terminal(websites_db: Pool) -> None:
    """A task that always raises ends `failed` with a populated `error` after `max_tries` —
    the ticket's first listed test, checked at the moment the budget runs out."""
    run_id = await _seed(websites_db, "budget-spent", attempts=MAX_ATTEMPTS - 1)

    assert await _run_task(websites_db, run_id, _connect_error) == "no outcome"

    row = await _row(websites_db, run_id)
    assert row["status"] == "failed"
    assert row["attempts"] == MAX_ATTEMPTS
    assert row["completed_at"] is not None
    assert row["error"] == f"Failed after {MAX_ATTEMPTS} attempts: Could not connect to the site."


# -----------------------------------------------------------------------------------------
# 3. Permanent: no retry, no waiting, no budget consumed beyond the one attempt.
# -----------------------------------------------------------------------------------------


async def test_an_ssrf_rejection_fails_immediately_without_consuming_retries(
    websites_db: Pool,
) -> None:
    """The ticket's headline classification case. A URL that resolves somewhere it is not
    allowed to will resolve there again in ten seconds and again in sixty; retrying is five
    minutes of latency for a guaranteed identical outcome.

    Asserted as `attempts == 1` on a `failed` row: the run stopped on its FIRST attempt with
    two still nominally available, which is the difference between "permanent" and "ran out
    of budget".
    """
    run_id = await _seed(websites_db, "ssrf", host=_BLOCKED_IP)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a blocked URL must never be fetched")

    assert await _run_task(websites_db, run_id, handler) == "no outcome"

    row = await _row(websites_db, run_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1, "a permanent failure must not burn the retry budget"
    assert row["completed_at"] is not None
    error = row["error"]
    assert isinstance(error, str) and error
    # No attempt-count prefix on a first-and-only attempt: "Failed after 1 attempts" would be
    # noise on every permanent failure, which is most of them.
    assert not error.startswith("Failed after")


async def test_an_unrecognized_exception_is_treated_as_permanent(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything the classifier has never heard of fails once rather than three times.

    An unrecognized exception is far more likely to be a bug in this codebase than a blip on
    the network, and retrying a bug three times over five minutes delays an honest `failed`
    row by five minutes to reach the identical outcome.
    """
    run_id = await _seed(websites_db, "unknown-exception")

    async def boom(*args: object, **kwargs: object) -> CrawlResult:
        raise RuntimeError("something nobody classified")

    monkeypatch.setattr("app.features.crawl.service.crawl_site", boom)

    assert await _run_task(websites_db, run_id, _ok) == "no outcome"

    row = await _row(websites_db, run_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1


# -----------------------------------------------------------------------------------------
# 4. Cancellation — arq's job timeout and a deploy's SIGTERM.
# -----------------------------------------------------------------------------------------


async def _cancel_mid_crawl(pool: Pool, run_id: UUID) -> None:
    """Run `crawl_task` under the exact mechanism arq's `job_timeout` uses, and let it fire.

    `arq.worker.Worker.run_job` does `await asyncio.wait_for(task, timeout_s)` on the job's
    task; when the timeout expires that cancels the coroutine from the outside, which is what
    reaches `execute_run` as `CancelledError`. Reproducing it with a real `wait_for` rather
    than by raising `CancelledError` by hand is the point — a handler that only works when
    the exception is raised synchronously inside the `try` would pass the hand-raised version
    and fail in production.
    """

    async def never_finishes(*args: object, **kwargs: object) -> CrawlResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    original = crawl_service.crawl_site
    crawl_service.crawl_site = never_finishes  # type: ignore[assignment]
    try:
        async with _mock_client(_ok) as http_client:
            ctx = {"db_pool": pool, "http_client": http_client, "storage": FakeStorage()}
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(crawl_task(ctx, str(run_id)), timeout=0.2)
    finally:
        crawl_service.crawl_site = original  # type: ignore[assignment]


async def test_a_cancelled_run_with_budget_left_goes_back_to_the_queue(
    websites_db: Pool,
) -> None:
    """A deploy is not a failed attempt: the run is handed back to `pending`, claimable
    again, rather than written off as `failed`.

    What picks it back up depends on which cancellation this was, and the assertion here is
    deliberately only about the ROW. Under SIGTERM arq re-queues the job itself and the
    redelivery is immediate; under `job_timeout` — the mechanism `_cancel_mid_crawl`
    reproduces — arq does not, and the reaper's orphan sweep is what re-enqueues it. Both
    leave the same row. `test_arq_does_not_redeliver_a_job_its_own_timeout_cancelled` below
    pins that difference against a real `Worker` instead of asserting it in prose.
    """
    run_id = await _seed(websites_db, "cancel-with-budget")

    await _cancel_mid_crawl(websites_db, run_id)

    row = await _row(websites_db, run_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["claimed_at"] is None
    assert row["error"] is None


async def test_an_arq_timeout_on_the_last_attempt_still_writes_terminal_state(
    websites_db: Pool,
) -> None:
    """The ticket's "an ARQ timeout still writes terminal state" test.

    The write happens on the way out of a coroutine that has ALREADY been cancelled, which is
    the part worth testing rather than asserting: a naive handler placed after the `await`
    that got cancelled never runs at all.
    """
    run_id = await _seed(websites_db, "cancel-no-budget", attempts=MAX_ATTEMPTS - 1)

    await _cancel_mid_crawl(websites_db, run_id)

    row = await _row(websites_db, run_id)
    assert row["status"] == "failed", "a cancelled run must not be left processing"
    assert row["attempts"] == MAX_ATTEMPTS
    assert row["completed_at"] is not None
    error = row["error"]
    assert isinstance(error, str)
    assert error.startswith(f"Failed after {MAX_ATTEMPTS} attempts:")


async def test_a_cancelled_run_whose_recovery_write_also_fails_is_left_for_the_reaper(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery is best-effort by design, and its failure mode has to be `processing`
    rather than a different exception.

    If the write that hands a cancelled run back were allowed to raise, it would replace the
    `CancelledError` with an ordinary exception — and arq treats those completely differently
    (a cancellation is re-queued; an exception is not). So the swallow is asserted from both
    sides: the cancellation still propagates, and the row is left in the one state the
    stuck-run reaper is built to sweep.
    """
    run_id = await _seed(websites_db, "cancel-write-fails")

    async def _raise(self: object, run_id: UUID) -> bool:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(
        "app.features.runs.internals.runs_writer.RunsWriter.return_processing_to_pending",
        _raise,
    )

    await _cancel_mid_crawl(websites_db, run_id)

    row = await _row(websites_db, run_id)
    assert row["status"] == "processing"
    assert row["claimed_at"] is not None


# -----------------------------------------------------------------------------------------
# 5. What arq ACTUALLY does with a cancelled job — pinned against a real Worker, not assumed.
# -----------------------------------------------------------------------------------------


_CLEANUP_KEY = "per_166_cancelled_cleanup_count"


async def _job_that_cleans_up_on_cancellation(ctx: dict[str, Any]) -> str:
    """`crawl_task`'s cancellation shape, reduced to its essentials.

    Sleeps past the worker's `job_timeout`, catches the `CancelledError` that arrives, does
    one `await` of recovery work — standing in for `_recover_cancelled_attempt`'s guarded
    UPDATE — and re-raises. That last detail is the one under test: a coroutine that awaits
    inside its cancellation handler is exactly what changes what `asyncio.wait_for` reports
    to arq.
    """
    redis = ctx["redis"]
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        await redis.incr(_CLEANUP_KEY)
        raise
    raise AssertionError("unreachable — the worker's job_timeout must fire first")


async def test_arq_does_not_redeliver_a_job_its_own_timeout_cancelled(
    queue_pool: ArqRedis,
) -> None:
    """A LIBRARY-BEHAVIOUR TEST, and the reason `CrawlService.execute_run`'s docstring
    distinguishes two cancellation causes that look identical from inside the coroutine.

    `arq.worker.Worker.run_job` runs each job as `await asyncio.wait_for(task, timeout_s)` and
    retries only on `Retry`, `RetryJob`, and `CancelledError`. When the JOB TIMEOUT is what
    cancels, `wait_for` converts the outcome into `TimeoutError` at its own call site once the
    coroutine has finished unwinding — and `TimeoutError` is not in that set, so arq records
    the job as failed and never redelivers it. A deploy's SIGTERM is the opposite case: it
    cancels `run_job`'s own frame, arq really does see `CancelledError`, and the job is
    re-queued.

    That distinction is load-bearing for this ticket and completely invisible from inside
    `execute_run`, which sees `CancelledError` either way. It is also not something to take on
    faith from a changelog: it depends on `asyncio.wait_for`'s unwinding semantics AND on
    arq's exception triage, either of which could change under a dependency bump. So it is
    pinned here against a real `Worker` in burst mode.

    **The cleanup assertion is the half that matters for correctness.** Whatever arq decides
    afterwards, the coroutine's cancellation handler must run to completion — that is what
    writes the run back to `pending`, and a version of `asyncio` or `arq` that killed the task
    before its `except` block finished would silently strand every cancelled run. Asserting
    the counter is exactly one proves both that the recovery ran and that it ran only once.

    `handle_signals=False` for the reason tests/test_worker_shutdown.py documents: arq
    otherwise installs SIGINT/SIGTERM handlers on pytest's loop and never removes them.
    """
    await queue_pool.delete(_CLEANUP_KEY)
    worker = Worker(
        functions=[_job_that_cleans_up_on_cancellation],
        redis_pool=queue_pool,
        queue_name=TEST_QUEUE_NAME,
        # Far shorter than the job's own sleep, so the timeout is unambiguously what fires.
        job_timeout=0.25,
        # Deliberately ABOVE 1: if arq were going to redeliver this job, a ceiling of 1 would
        # hide it behind "max retries exceeded" and the test would pass for the wrong reason.
        max_tries=3,
        retry_jobs=True,
        poll_delay=0.05,
        burst=True,
        handle_signals=False,
    )
    try:
        await queue_pool.enqueue_job(
            _job_that_cleans_up_on_cancellation.__qualname__, _queue_name=TEST_QUEUE_NAME
        )
        await asyncio.wait_for(worker.async_run(), timeout=30)

        # The cancellation handler ran, exactly once, all the way through its own `await`.
        assert await queue_pool.get(_CLEANUP_KEY) == b"1"

        # And arq treated it as terminal rather than retrying it — which is precisely why a
        # run left `pending` by a `job_timeout` needs the reaper's orphan sweep and does not
        # simply reappear on the queue.
        assert worker.jobs_failed == 1
        assert worker.jobs_retried == 0
        assert worker.jobs_complete == 0
    finally:
        await queue_pool.delete(_CLEANUP_KEY)
