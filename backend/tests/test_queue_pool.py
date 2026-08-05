"""Tests for `app.infrastructure.queue.pool` — the API's enqueue side of the queue.

Runs against a real Redis (see the `queue_pool` fixture in conftest.py) rather than a fake,
for the same reason ci-backend.yml stands up a real `postgres:16`: what is being verified
is that arq's own serialization and sorted-set queue behave as expected, and a
reimplementation of Redis cannot answer a question about arq.
"""

import pytest
from arq.connections import ArqRedis
from arq.jobs import JobStatus
from conftest import TEST_QUEUE_NAME, TEST_REDIS_URL

from app.core.settings import Settings
from app.infrastructure.queue import pool as queue_pool_module
from app.infrastructure.queue.pool import (
    close_queue_pool,
    create_queue_pool,
    get_queue_pool,
    open_queue_pool,
)
from app.worker.jobs import noop


@pytest.fixture
def queue_settings() -> Settings:
    """Settings pointed at the throwaway test Redis, and at nothing else.

    Built explicitly rather than reusing `app.core.settings.settings` so that these tests
    cannot connect anywhere a developer's environment happens to point. The other required
    fields are obvious non-values: nothing here opens a Postgres or Supabase connection.
    """
    return Settings(
        database_url="postgresql://localhost:5432/unused-by-this-test",
        redis_url=TEST_REDIS_URL,
        supabase_url="https://test-project.supabase.co",
        supabase_secret_key="not-a-real-key",
    )


@pytest.fixture
async def closed_queue_pool_singleton() -> None:
    """Fail fast if a previous test left the module singleton open.

    `open_queue_pool` raises rather than overwriting, so a leaked pool would turn every
    later test in this file into a confusing "already open" failure pointing at innocent
    code. Asserting it here names the real culprit.
    """
    assert queue_pool_module._queue_pool is None, (
        "A previous test left the queue pool singleton open. Every test that calls "
        "open_queue_pool() must close it in a finally block."
    )


async def test_enqueue_from_the_api_side_lands_a_job_on_the_queue(
    queue_pool: ArqRedis,
) -> None:
    """The API's half of the contract: a job it enqueues is really on the queue.

    Asserted through arq's own `Job` handle rather than by reading Redis keys directly, so
    this stays a test of the contract the worker consumes and not of arq's key naming.
    """
    job = await queue_pool.enqueue_job(noop.__qualname__, _queue_name=TEST_QUEUE_NAME)

    assert job is not None, "enqueue_job returned None, which means the job id already existed"
    assert await job.status() == JobStatus.queued

    queued = await queue_pool.zcard(TEST_QUEUE_NAME)
    assert queued == 1

    definition = await job.info()
    assert definition is not None
    assert definition.function == noop.__qualname__


async def test_enqueueing_the_same_job_id_twice_is_refused(queue_pool: ArqRedis) -> None:
    """arq deduplicates on `_job_id`, and the second call returns `None` rather than raising.

    Recorded here because it is the mechanism the crawl ticket's duplicate-run guard will
    be built on, and because "returns None" is an easy thing to write a bug against — a
    caller that assumes a `Job` came back will `AttributeError` on the duplicate path only.
    """
    job_id = "per-157-duplicate-job-id"

    first = await queue_pool.enqueue_job(
        noop.__qualname__, _job_id=job_id, _queue_name=TEST_QUEUE_NAME
    )
    second = await queue_pool.enqueue_job(
        noop.__qualname__, _job_id=job_id, _queue_name=TEST_QUEUE_NAME
    )

    assert first is not None
    assert second is None


async def test_create_queue_pool_connects_and_closes_cleanly(
    queue_settings: Settings,
) -> None:
    """The pure factory reaches Redis, and `aclose` releases the connection pool.

    `arq.create_pool` pings before returning, so a pool handed back has genuinely
    connected; this asserts that, and then that closing it really disconnects rather than
    just returning one connection to a pool that stays open — the leak that would otherwise
    accumulate one deploy at a time.
    """
    pool = await create_queue_pool(queue_settings)

    assert await pool.ping() is True

    # The ping had to open a socket, so there is now a real connection to release. Held by
    # reference across the close, because redis-py's `disconnect()` disconnects the
    # connections it is holding without removing them from the pool's list — so the list
    # itself says nothing, and the sockets are what has to be checked.
    connections = list(pool.connection_pool._available_connections)
    assert connections, "expected the ping to have established a connection"
    assert all(connection.is_connected for connection in connections)

    await pool.aclose(close_connection_pool=True)

    # redis-py exposes no public "is this pool closed?" predicate, and pinging again would
    # transparently reconnect and prove nothing. This is the assertion that fails if
    # `close_connection_pool=True` is ever dropped from close_queue_pool() — the regression
    # that leaks one connection per deploy against Upstash's concurrent-connection cap.
    assert not any(connection.is_connected for connection in connections)


async def test_open_get_and_close_manage_one_process_wide_pool(
    queue_settings: Settings,
    closed_queue_pool_singleton: None,
) -> None:
    """`open_queue_pool` / `get_queue_pool` / `close_queue_pool` behave like the db pool's.

    Same three-function contract as `app.infrastructure.db.pool`, deliberately, so there is
    one shape to learn for both backing services.
    """
    with pytest.raises(RuntimeError, match="not open"):
        get_queue_pool()

    pool = await open_queue_pool(queue_settings)
    try:
        assert get_queue_pool() is pool

        with pytest.raises(RuntimeError, match="already open"):
            await open_queue_pool(queue_settings)
    finally:
        await close_queue_pool()

    with pytest.raises(RuntimeError, match="not open"):
        get_queue_pool()

    # Idempotent, because `app.main`'s lifespan calls it on a shutdown path that may follow
    # a startup where the pool was never opened at all.
    await close_queue_pool()


async def test_close_queue_pool_is_safe_when_nothing_was_opened(
    closed_queue_pool_singleton: None,
) -> None:
    """Shutdown after a failed startup must not raise.

    `app.main.lifespan` deliberately continues when Redis is unreachable, so the shutdown
    path routinely runs with no pool to close. If this raised, an API that started during
    an Upstash outage could not shut down cleanly either.
    """
    await close_queue_pool()
