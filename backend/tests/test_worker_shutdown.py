"""Does SIGTERM actually let an in-flight job finish?

The ticket that added the worker asked for this to be *verified rather than assumed*, and
it is the one behaviour here that cannot be checked by reading a constant: arq's default
handler cancels running jobs, and the only thing standing between a routine deploy and a
crawl killed halfway through is `job_completion_wait`.

Runs a real `arq.Worker` against the real test Redis, because the mechanism under test is
arq's signal handling and its interaction with its own poll loop. There is nothing left to
test once either of those is replaced by a fake.

**The signal is delivered by calling arq's handler directly rather than by
`os.kill(SIGTERM)`.** A real signal would be delivered to the pytest process, taking the
test runner down with it the first time an assertion path changed;
`handle_sig_wait_for_completion` is precisely the callable arq registers with
`loop.add_signal_handler`, so invoking it is delivering the signal without the collateral
damage.
"""

import asyncio
import signal

import pytest
from arq import create_pool
from arq.connections import ArqRedis
from arq.worker import Worker
from conftest import TEST_QUEUE_NAME, TEST_REDIS_URL

from app.infrastructure.queue.pool import redis_settings_from_url


# Long enough that the worker is unambiguously mid-job when the signal lands, short enough
# that the suite does not notice. The drain budget is many times this, so a correct
# implementation never comes close to it and the test is not timing-sensitive.
_JOB_DURATION_SECONDS = 0.4
_DRAIN_BUDGET_SECONDS = 10

# Bounds the whole test, so a worker that never exits fails with a clear assertion instead
# of hanging the suite.
_WORKER_EXIT_TIMEOUT_SECONDS = 30

# Fast, unlike production: this file is about what happens on shutdown, not about the
# Upstash command budget that makes `poll_delay = 5` correct in production.
_TEST_POLL_DELAY_SECONDS = 0.05

_STARTED_KEY = "per_157_shutdown_started"
_FINISHED_KEY = "per_157_shutdown_finished"


async def _slow_job(ctx: dict[object, object]) -> str:
    """Record that it started, sleep, then record that it ran all the way to the end.

    Two markers rather than one: "cancelled halfway" and "never started" are different
    failures, and a single completion flag cannot tell them apart. They are written to
    Redis rather than to a Python variable because the assertion has to survive the job
    being cancelled mid-await.
    """
    redis = ctx["redis"]
    assert isinstance(redis, ArqRedis)
    await redis.set(_STARTED_KEY, b"1")
    await asyncio.sleep(_JOB_DURATION_SECONDS)
    await redis.set(_FINISHED_KEY, b"1")
    return "finished"


async def _wait_for_key(redis: ArqRedis, key: str, timeout: float) -> bool:
    """Poll for `key` to exist, up to `timeout`. Returns whether it appeared."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await redis.exists(key):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def worker_redis(queue_pool: ArqRedis) -> ArqRedis:
    """A Redis connection owned by the worker under test, not the session fixture's.

    `queue_pool` is session-scoped and every other test in the run shares it. A worker
    handed that pool would close it out from under them on teardown, so each worker gets
    its own connection to the same Redis and the assertions keep using `queue_pool`.

    Depends on `queue_pool` so this inherits its skip when Redis is unreachable, rather
    than failing separately with a connection error.
    """
    redis_settings = redis_settings_from_url(TEST_REDIS_URL)
    redis_settings.conn_retries = 0
    return await create_pool(redis_settings, default_queue_name=TEST_QUEUE_NAME)


def _build_worker(redis: ArqRedis, *, job_completion_wait: int) -> Worker:
    """A worker on the test queue, with signal handling left to the test.

    `handle_signals=False` stops arq installing SIGINT/SIGTERM handlers on pytest's event
    loop — it never removes them, so the session would lose Ctrl-C for the rest of the run.
    The handler is invoked directly instead; see the module docstring.
    """
    return Worker(
        functions=[_slow_job],
        redis_pool=redis,
        queue_name=TEST_QUEUE_NAME,
        poll_delay=_TEST_POLL_DELAY_SECONDS,
        job_completion_wait=job_completion_wait,
        handle_signals=False,
        max_tries=1,
        retry_jobs=False,
    )


async def _run_until_job_started(worker: Worker, queue_pool: ArqRedis) -> asyncio.Task[None]:
    """Start the worker, enqueue the slow job, and return once it is genuinely in flight."""
    run: asyncio.Task[None] = asyncio.create_task(worker.async_run())
    await queue_pool.enqueue_job(_slow_job.__qualname__, _queue_name=TEST_QUEUE_NAME)

    started = await _wait_for_key(queue_pool, _STARTED_KEY, timeout=5)
    assert started, "the worker never picked the job up, so nothing was in flight"
    return run


async def _await_worker_exit(run: asyncio.Task[None]) -> None:
    """Wait for the worker task to finish, without re-raising its cancellation.

    `asyncio.wait` rather than `await run`: the worker always ends by cancelling its own
    main task, so awaiting it directly would raise `CancelledError` into the test — and
    inside an `asyncio.timeout` block that is ambiguous with the timeout's own
    cancellation.
    """
    _, pending = await asyncio.wait({run}, timeout=_WORKER_EXIT_TIMEOUT_SECONDS)
    assert not pending, "the worker did not exit after the signal"


async def _teardown(run: asyncio.Task[None], redis: ArqRedis) -> None:
    """Stop the worker task and release its connection.

    Deliberately does NOT call `Worker.close()`: that gathers the worker's already-cancelled
    job tasks, which re-raises `CancelledError` out of a teardown path, and it would close
    the connection pool a second time.
    """
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    await redis.aclose(close_connection_pool=True)


@pytest.fixture(autouse=True)
async def _clear_markers(queue_pool: ArqRedis) -> None:
    """Remove both markers so one case in this file cannot satisfy the next."""
    await queue_pool.delete(_STARTED_KEY, _FINISHED_KEY)


async def test_sigterm_lets_an_in_flight_job_finish(
    queue_pool: ArqRedis,
    worker_redis: ArqRedis,
) -> None:
    """The acceptance criterion, executed: SIGTERM drains rather than cancels.

    Enqueue a job that takes a measurable amount of time, wait until the worker has
    genuinely picked it up, then deliver the signal. The job must still reach its last
    line, and the worker must stop claiming new work immediately.
    """
    worker = _build_worker(worker_redis, job_completion_wait=_DRAIN_BUDGET_SECONDS)
    run = await _run_until_job_started(worker, queue_pool)

    try:
        worker.handle_sig_wait_for_completion(signal.SIGTERM)

        # First half of the contract: no new work is claimed from the moment the signal
        # lands, even though the process is still alive and still polling.
        assert worker.allow_pick_jobs is False

        await _await_worker_exit(run)

        assert await queue_pool.exists(_FINISHED_KEY), (
            "the in-flight job did not reach its last line, so SIGTERM cancelled it "
            "instead of draining it — check job_completion_wait in app/worker/settings.py"
        )
    finally:
        await _teardown(run, worker_redis)


async def test_the_default_handler_would_have_cancelled_it(
    queue_pool: ArqRedis,
    worker_redis: ArqRedis,
) -> None:
    """The negative control, without which the test above proves nothing.

    With `job_completion_wait` at arq's default of 0, the identical sequence cancels the
    job mid-flight. If this ever stops being true, arq has changed its shutdown behaviour
    and the setting in `app.worker.settings` may no longer do what its comment claims —
    which is worth learning from a red test rather than from a truncated crawl.
    """
    worker = _build_worker(worker_redis, job_completion_wait=0)
    run = await _run_until_job_started(worker, queue_pool)

    try:
        worker.handle_sig(signal.SIGTERM)
        await _await_worker_exit(run)

        # Comfortably longer than the job had left to run, so this asserts cancellation
        # rather than racing it.
        await asyncio.sleep(_JOB_DURATION_SECONDS * 2)
        assert not await queue_pool.exists(_FINISHED_KEY)
    finally:
        await _teardown(run, worker_redis)
