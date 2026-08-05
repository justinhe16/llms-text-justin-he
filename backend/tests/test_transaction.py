"""Tests for transaction() against real Postgres: commit, rollback, and HTTPException.

Runs against the scratch table `tests/conftest.py`'s session-scoped `db_pool` fixture
creates. Each test writes a row keyed by a fresh UUID and deletes it in a `finally` (or
asserts it was never written), so these tests can run in any order without one passing
because of another's leftover data.
"""

import asyncio
import uuid

import pytest
from asyncpg import Pool
from fastapi import HTTPException

from app.infrastructure.db.transaction import transaction


# Must match tests/conftest.py's db_pool fixture, which owns creating and dropping it.
_SCRATCH_TABLE = "per_145_transaction_scratch_test"


async def _row_exists(pool: Pool, key: str) -> bool:
    """Look up `key` using the pool directly.

    That is a connection independent of any transaction under test, so a positive result
    proves the data actually reached Postgres rather than merely being visible inside the
    same open transaction.
    """
    value = await pool.fetchval(f"SELECT 1 FROM {_SCRATCH_TABLE} WHERE key = $1", key)
    return value is not None


async def test_transaction_commits_on_success(db_pool: Pool) -> None:
    key = f"commit-{uuid.uuid4()}"
    try:
        async with transaction(db_pool) as conn:
            await conn.execute(
                f"INSERT INTO {_SCRATCH_TABLE} (key, value) VALUES ($1, $2)",
                key,
                "committed",
            )

        assert await _row_exists(db_pool, key)
    finally:
        await db_pool.execute(f"DELETE FROM {_SCRATCH_TABLE} WHERE key = $1", key)


async def test_transaction_rolls_back_on_exception(db_pool: Pool) -> None:
    key = f"rollback-{uuid.uuid4()}"

    with pytest.raises(RuntimeError, match="simulated failure"):
        async with transaction(db_pool) as conn:
            await conn.execute(
                f"INSERT INTO {_SCRATCH_TABLE} (key, value) VALUES ($1, $2)",
                key,
                "should-not-land",
            )
            raise RuntimeError("simulated failure inside the transaction")

    # Prove absence by querying, not by inspecting the context manager.
    assert not await _row_exists(db_pool, key)


async def test_http_exception_propagates_with_status_and_rolls_back(db_pool: Pool) -> None:
    key = f"http-409-{uuid.uuid4()}"

    with pytest.raises(HTTPException) as exc_info:
        async with transaction(db_pool) as conn:
            await conn.execute(
                f"INSERT INTO {_SCRATCH_TABLE} (key, value) VALUES ($1, $2)",
                key,
                "should-not-land",
            )
            raise HTTPException(status_code=409, detail="conflict")

    assert exc_info.value.status_code == 409
    assert not await _row_exists(db_pool, key)


async def test_transaction_rolls_back_when_the_caller_is_cancelled(db_pool: Pool) -> None:
    """Cancellation is an exception too, and must roll back like any other.

    `transaction()` documents rollback on ANY exception, and `asyncio.CancelledError` is
    the one that arrives without anybody raising it — a client disconnecting mid-request,
    or a shutdown cancelling in-flight work. It inherits from `BaseException`, not
    `Exception`, so a handler written in terms of `Exception` would miss it entirely;
    this asserts the guarantee holds rather than leaving it to inspection.
    """
    key = f"cancelled-{uuid.uuid4()}"
    started = asyncio.Event()

    async def unit_of_work() -> None:
        async with transaction(db_pool) as conn:
            await conn.execute(
                f"INSERT INTO {_SCRATCH_TABLE} (key, value) VALUES ($1, $2)",
                key,
                "should-not-land",
            )
            started.set()
            await asyncio.sleep(30)  # cancelled below, mid-transaction

    task = asyncio.create_task(unit_of_work())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not await _row_exists(db_pool, key)
