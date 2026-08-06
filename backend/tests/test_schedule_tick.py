"""Tests for `ScheduleService.run_due_schedules` — the ARQ cron tick that turns due
schedules into `runs` rows.

Driven against the real Postgres and, for one test, the real Redis behind `queue_pool` —
`build_schedule_service(websites_db, settings)` builds the exact object
`app.worker.jobs.schedule_tick` builds in production, just from this suite's fixtures instead
of arq's `ctx`.

**The global-state guard.** Unlike every other suite in this backend, the tick is NOT scoped
to a test user: `SchedulesReader.lock_due` reads across the whole `schedules` table on
purpose (it is the tick's hot path, not an API endpoint's). `websites_db` only cleans up rows
owned by this suite's two fake users, so a developer's local Supabase database can hold a real,
active, due schedule created by hand while working on the frontend — and if it is, every exact
`TickSummary` count in this file would silently include it too. `_assert_no_foreign_due_
schedules` below checks for exactly that and `pytest.skip`s with a clear message rather than
let it produce a flaky, environment-dependent failure; every test that asserts an exact count
calls it first.

**The headline test is `test_two_concurrent_ticks_produce_exactly_one_run_per_due_schedule`**
— the ticket's acceptance criterion for `FOR UPDATE SKIP LOCKED`, driven with `asyncio.gather`
against the real pool exactly the way `tests/test_crawl_task.py`'s claim-race test drives its
own lock. The `SKIP LOCKED`-specific test beside it explains, in its own docstring, why that
test alone is not sufficient proof.

**Both locking tests were mutation-tested, and both mutations are worth knowing about**, since
each catches something the other cannot:

* Deleting `SKIP LOCKED` and leaving `FOR UPDATE` — the headline test stays GREEN (under READ
  COMMITTED the blocked tick re-evaluates the row against its advanced `next_run_at` and
  correctly does nothing), and only the `SKIP LOCKED` test fails, by timing out.
* Deleting `FOR UPDATE SKIP LOCKED` outright — both fail.

The headline test's own docstring records why it had to be rewritten to achieve the second of
those; the version that seeded a single schedule passed even with all locking removed.

Two module-local queue stand-ins play the same role `tests/test_run_trigger_api.py`'s
`_FailingQueuePool` plays for the manual trigger: `_RecordingQueuePool` appends instead of
touching Redis, and `_FailFirstQueuePool` raises on its first call and then succeeds, for the
"one bad enqueue must not abort the batch" test. The real `queue_pool` is used for exactly one
test — the happy path — to prove a job actually lands on the queue with the right name and
argument; every other test only needs to know whether `enqueue_job` was called and with what,
which the stand-ins answer without a network round trip.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from arq.connections import ArqRedis
from asyncpg import Pool
from conftest import (
    TEST_QUEUE_NAME,
    TEST_USER_A_ID,
    TEST_USER_B_ID,
    seed_run,
    seed_schedule,
    seed_website,
)

from app.core.settings import settings
from app.features.runs.service import CRAWL_TASK_JOB_NAME
from app.features.schedules.internals.schedules_reader import SchedulesReader
from app.features.schedules.service import DUE_BATCH_LIMIT, ScheduleService, build_schedule_service


_NOW = datetime.now(UTC)


# -----------------------------------------------------------------------------------------
# Fixtures and stand-ins
# -----------------------------------------------------------------------------------------


@pytest.fixture
def schedule_service(websites_db: Pool) -> ScheduleService:
    """The exact object `app.worker.jobs.schedule_tick` builds in production, against this
    suite's database. `RunService`'s per-user caps never come into play on this path — the
    cron tick's `RunService.create_scheduled_run` skips them entirely — so the process-wide
    `settings` singleton is fine here; no test in this file needs a lowered cap."""
    return build_schedule_service(websites_db, settings)


@pytest.fixture
async def real_queue(queue_pool: ArqRedis) -> AsyncIterator[ArqRedis]:
    """The real, throwaway Redis pool behind `queue_pool`, flushed on entry and exit — the
    same contract `tests/test_run_trigger_api.py`'s `queue_override` keeps, for the same
    reason: `queue_pool` is session-scoped, so a job left behind by an earlier test would
    make "exactly one job queued" assertions pass or fail for the wrong reason."""
    await queue_pool.flushdb()
    try:
        yield queue_pool
    finally:
        await queue_pool.flushdb()


class _RecordingQueuePool:
    """A stand-in `ArqRedis` whose `enqueue_job` appends instead of touching Redis."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.enqueued.append((function, args))


class _FailFirstQueuePool:
    """Raises on its FIRST call only, then records every call after that like
    `_RecordingQueuePool`. Exists for exactly one test: proving that one enqueue failure in a
    batch does not strand the rest of it."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, tuple[Any, ...]]] = []
        self._first_call = True

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        if self._first_call:
            self._first_call = False
            raise ConnectionError("simulated Redis failure (test double)")
        self.enqueued.append((function, args))


async def _assert_no_foreign_due_schedules(pool: Pool) -> None:
    """Skip loudly if a schedule outside this suite's two test users is already due.

    See the module docstring's "global-state guard" section. Every test that asserts an
    exact `TickSummary` count calls this before seeding its own fixtures.
    """
    count = await pool.fetchval(
        """
        SELECT count(*)
        FROM schedules s
        JOIN websites w ON w.id = s.website_id
        WHERE s.active AND s.next_run_at <= now() AND w.user_id != ALL($1::uuid[])
        """,
        [TEST_USER_A_ID, TEST_USER_B_ID],
    )
    if count:
        pytest.skip(
            f"{count} schedule(s) outside this suite's two test users are already active "
            "and due in TEST_DATABASE_URL. The cron tick is not scoped to a user (by "
            "design — see SchedulesReader.lock_due), so an exact TickSummary count cannot "
            "be trusted while that is true. Clean up TEST_DATABASE_URL's `schedules` table "
            "(or point TEST_DATABASE_URL at a fresh database) and re-run."
        )


async def _run_count_for_website(pool: Pool, website_id: UUID) -> int:
    count: int = await pool.fetchval("SELECT count(*) FROM runs WHERE website_id = $1", website_id)
    return count


async def _schedule_row(pool: Pool, schedule_id: UUID) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT next_run_at, last_run_at FROM schedules WHERE id = $1", schedule_id
    )
    assert row is not None
    return dict(row)


# -----------------------------------------------------------------------------------------
# 1. The happy path — a run, an advanced schedule, and a real queued job.
# -----------------------------------------------------------------------------------------


async def test_a_due_schedule_produces_exactly_one_run(
    websites_db: Pool, schedule_service: ScheduleService, real_queue: ArqRedis
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-happy-path.example")
    interval_minutes = 360
    schedule_id = await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=interval_minutes,
        next_run_at=_NOW - timedelta(minutes=5),
    )

    before = datetime.now(UTC)
    summary = await schedule_service.run_due_schedules(real_queue)
    after = datetime.now(UTC)

    assert summary.examined == 1
    assert summary.runs_created == 1
    assert summary.skipped_active == 0
    assert summary.enqueue_failures == 0
    assert summary.limit_reached is False

    run_row = await websites_db.fetchrow(
        'SELECT id, status, "trigger", schedule_id FROM runs WHERE website_id = $1', website_id
    )
    assert run_row is not None
    assert run_row["status"] == "pending"
    assert run_row["trigger"] == "scheduled"
    assert run_row["schedule_id"] == schedule_id

    schedule_row = await _schedule_row(websites_db, schedule_id)
    interval = timedelta(minutes=interval_minutes)
    assert before + interval * 0.95 <= schedule_row["next_run_at"] <= after + interval * 1.05
    assert before <= schedule_row["last_run_at"] <= after

    jobs = await real_queue.queued_jobs(queue_name=TEST_QUEUE_NAME)
    assert len(jobs) == 1
    assert jobs[0].function == CRAWL_TASK_JOB_NAME
    assert jobs[0].args == (str(run_row["id"]),)


# -----------------------------------------------------------------------------------------
# 2-4. Schedules the tick must leave completely alone.
# -----------------------------------------------------------------------------------------


async def test_a_not_yet_due_schedule_is_untouched(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-not-due.example")
    future_next_run_at = _NOW + timedelta(hours=1)
    seeded_last_run_at = _NOW - timedelta(days=1)
    schedule_id = await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=60,
        next_run_at=future_next_run_at,
        last_run_at=seeded_last_run_at,
    )

    queue = _RecordingQueuePool()
    summary = await schedule_service.run_due_schedules(queue)

    assert summary.examined == 0
    assert summary.runs_created == 0
    assert queue.enqueued == []
    assert await _run_count_for_website(websites_db, website_id) == 0

    row = await _schedule_row(websites_db, schedule_id)
    assert row["next_run_at"] == future_next_run_at
    assert row["last_run_at"] == seeded_last_run_at


async def test_an_inactive_schedule_with_a_past_next_run_at_is_ignored(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-inactive.example")
    past_next_run_at = _NOW - timedelta(minutes=5)
    schedule_id = await seed_schedule(
        websites_db, website_id, active=False, interval_minutes=60, next_run_at=past_next_run_at
    )

    queue = _RecordingQueuePool()
    summary = await schedule_service.run_due_schedules(queue)

    assert summary.examined == 0
    assert await _run_count_for_website(websites_db, website_id) == 0

    row = await _schedule_row(websites_db, schedule_id)
    assert row["next_run_at"] == past_next_run_at


async def test_an_active_schedule_with_a_null_next_run_at_is_ignored(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    """`NULL <= $1` is `NULL`, never true — see `_LOCK_DUE`'s own comment. This is the
    integration proof that the row really is excluded, not merely that the SQL reads that
    way."""
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://tick-null-next-run.example"
    )
    schedule_id = await seed_schedule(
        websites_db, website_id, active=True, interval_minutes=60, next_run_at=None
    )

    queue = _RecordingQueuePool()
    summary = await schedule_service.run_due_schedules(queue)

    assert summary.examined == 0
    assert await _run_count_for_website(websites_db, website_id) == 0

    row = await _schedule_row(websites_db, schedule_id)
    assert row["next_run_at"] is None


# -----------------------------------------------------------------------------------------
# 5. THE HEADLINE TEST — two concurrent ticks over one schedule produce exactly one run.
# -----------------------------------------------------------------------------------------


# How many due schedules the headline test below seeds. Not 1, and the number is the whole
# point of the test — see its docstring's "why a batch" section.
_CONCURRENT_BATCH = 25


async def test_two_concurrent_ticks_produce_exactly_one_run_per_due_schedule(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    """The ticket's headline acceptance criterion for `FOR UPDATE SKIP LOCKED`, driven the
    way it will actually be exercised in production: two overlapping ticks, not two
    overlapping HTTP requests. Mirrors `tests/test_crawl_task.py`'s
    `test_two_concurrent_calls_for_the_same_run_only_one_claims_it`, which proves a different
    lock (`claim_pending`'s conditional `UPDATE`) the same way — `asyncio.gather` against the
    real pool, not two mocked calls.

    **Why a batch of schedules and not one, and why the pool is warmed first.** Both details
    are here because the obvious version of this test — seed ONE due schedule, `gather` two
    ticks, assert one run — was written first and then found to be theater: it passed with
    `FOR UPDATE SKIP LOCKED` deleted from `SchedulesReader._LOCK_DUE` entirely. It passed
    because the two ticks never actually overlapped. The second coroutine's first `await` is
    `pool.acquire()`, and `conftest.db_pool` is built `min_size=1`, so that call had to open a
    brand-new Postgres connection — several milliseconds of TCP and auth — while the first
    tick, already holding the pool's only live connection, ran its three quick statements and
    committed. The second tick then began, found `next_run_at` already advanced, and correctly
    did nothing. One run, green test, no locking involved anywhere.

    So this test removes both halves of that accident. Warming the pool means neither
    coroutine pays for connection setup at the moment it matters. Seeding
    `_CONCURRENT_BATCH` schedules means the winning tick holds its transaction open across
    that many INSERT round trips instead of one, which is a window the loser cannot skip past
    by accident. With row locking removed, the loser reads the same rows and inserts a second
    run for every one of them, and the per-website assertion below goes red — which is what
    makes this test worth having.
    """
    await _assert_no_foreign_due_schedules(websites_db)
    website_ids = [
        await seed_website(websites_db, TEST_USER_A_ID, f"https://concurrent-tick-{i}.example")
        for i in range(_CONCURRENT_BATCH)
    ]
    schedule_ids = [
        await seed_schedule(
            websites_db,
            website_id,
            active=True,
            interval_minutes=60,
            next_run_at=_NOW - timedelta(minutes=5),
        )
        for website_id in website_ids
    ]

    # Warm the pool: three live connections, so neither tick's `pool.acquire()` blocks on
    # opening one. See the docstring — this is not incidental setup.
    warm = await asyncio.gather(*(websites_db.acquire() for _ in range(3)))
    await asyncio.gather(*(websites_db.release(connection) for connection in warm))

    before = datetime.now(UTC)
    summaries = await asyncio.gather(
        schedule_service.run_due_schedules(_RecordingQueuePool()),
        schedule_service.run_due_schedules(_RecordingQueuePool()),
    )
    after = datetime.now(UTC)

    # Exactly one run per due schedule, across both ticks — the acceptance criterion, stated
    # per-website so a duplicate anywhere in the batch fails rather than averaging out.
    assert sum(summary.runs_created for summary in summaries) == _CONCURRENT_BATCH
    for website_id in website_ids:
        assert await _run_count_for_website(websites_db, website_id) == 1

    # Advanced exactly once each: applying the jitter twice in sequence (one tick's advance
    # landing on top of the other's) would land well outside one interval's ±5% window,
    # which is what this bound actually rules out.
    interval = timedelta(minutes=60)
    for schedule_id in schedule_ids:
        row = await _schedule_row(websites_db, schedule_id)
        assert before + interval * 0.95 <= row["next_run_at"] <= after + interval * 1.05


# -----------------------------------------------------------------------------------------
# 6. SKIP LOCKED, specifically — not merely "concurrency didn't duplicate a run".
# -----------------------------------------------------------------------------------------


async def test_skip_locked_lets_the_tick_proceed_past_a_row_someone_else_is_holding(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    """Holds a plain `FOR UPDATE` lock on one due schedule from a SEPARATE connection, then
    runs one real tick and requires it to return within `timeout=5`.

    **What this protects:** with plain `FOR UPDATE` (no `SKIP LOCKED`), `SchedulesReader.
    lock_due`'s query would BLOCK on the held lock until this test's own transaction ends —
    so a regression here shows up as `asyncio.wait_for` timing out, not as a wrong count.

    The previous test (two concurrent ticks over ONE schedule) does NOT prove this on its
    own: under READ COMMITTED, a plain `FOR UPDATE` would also eventually unblock once the
    other transaction commits, re-evaluate the row against its now-ADVANCED `next_run_at`,
    find nothing left to do, and still produce exactly one run — the same passing result, for
    a much slower and entirely different reason. Seeding a SECOND, unlocked schedule that the
    tick must still process promptly, while the first stays locked for the whole test, is
    what actually rules that out.
    """
    await _assert_no_foreign_due_schedules(websites_db)
    locked_website = await seed_website(
        websites_db, TEST_USER_A_ID, "https://skip-locked-held.example"
    )
    locked_schedule_id = await seed_schedule(
        websites_db,
        locked_website,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
    )
    unlocked_website = await seed_website(
        websites_db, TEST_USER_A_ID, "https://skip-locked-free.example"
    )
    await seed_schedule(
        websites_db,
        unlocked_website,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
    )

    holder = await websites_db.acquire()
    tx = holder.transaction()
    await tx.start()
    try:
        await holder.fetchval(
            "SELECT id FROM schedules WHERE id = $1 FOR UPDATE", locked_schedule_id
        )

        queue = _RecordingQueuePool()
        summary = await asyncio.wait_for(schedule_service.run_due_schedules(queue), timeout=5)

        assert summary.examined == 1
        assert summary.runs_created == 1
        assert await _run_count_for_website(websites_db, unlocked_website) == 1
        assert await _run_count_for_website(websites_db, locked_website) == 0

        locked_row = await _schedule_row(websites_db, locked_schedule_id)
        assert locked_row["next_run_at"] == _NOW - timedelta(minutes=5)  # unchanged
    finally:
        await tx.rollback()
        await websites_db.release(holder)


# -----------------------------------------------------------------------------------------
# 7. A website with an active run is skipped, but its schedule still advances.
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("active_status", ["pending", "processing"])
async def test_a_website_with_an_active_run_is_skipped_but_its_schedule_still_advances(
    websites_db: Pool, schedule_service: ScheduleService, active_status: str
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://tick-skip-active-{active_status}.example"
    )
    await seed_run(websites_db, website_id, started_at=_NOW, status=active_status)
    seeded_last_run_at = _NOW - timedelta(days=2)
    schedule_id = await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
        last_run_at=seeded_last_run_at,
    )

    before = datetime.now(UTC)
    queue = _RecordingQueuePool()
    summary = await schedule_service.run_due_schedules(queue)
    after = datetime.now(UTC)

    assert summary.examined == 1
    assert summary.runs_created == 0
    assert summary.skipped_active == 1
    assert queue.enqueued == []
    # Only the one run this test seeded — the tick did not create a second one.
    assert await _run_count_for_website(websites_db, website_id) == 1

    row = await _schedule_row(websites_db, schedule_id)
    # last_run_at is UNTOUCHED: this tick produced no run for this schedule.
    assert row["last_run_at"] == seeded_last_run_at
    # next_run_at DID advance — otherwise this schedule would be re-examined forever.
    interval = timedelta(minutes=60)
    assert before + interval * 0.95 <= row["next_run_at"] <= after + interval * 1.05


# -----------------------------------------------------------------------------------------
# 8. Jitter, observed through the service rather than the pure function directly.
# -----------------------------------------------------------------------------------------


async def test_jitter_two_consecutive_ticks_draw_different_offsets(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    """The service actually applies `advance_next_run_at`'s jitter — not a hardcoded
    `now + interval` — and draws a fresh offset every time it runs, not the same one twice.

    Uses a daily (1440-minute) interval for the widest jitter window (±72 minutes): the wider
    the window, the more astronomically unlikely it is for two independent uniform draws to
    land within a second of each other by chance, which is what keeps the final assertion
    below from ever being flaky. `tests/test_next_run.py`'s own `advance_next_run_at` tests
    already prove the bound and the non-determinism of the pure function in isolation over
    hundreds of draws; this test exists to prove the SERVICE is the one drawing them.
    """
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-jitter.example")
    interval_minutes = 1440
    interval = timedelta(minutes=interval_minutes)
    schedule_id = await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=interval_minutes,
        next_run_at=_NOW - timedelta(minutes=5),
    )

    offsets: list[timedelta] = []
    for _ in range(2):
        before = datetime.now(UTC)
        await schedule_service.run_due_schedules(_RecordingQueuePool())
        after = datetime.now(UTC)
        row = await _schedule_row(websites_db, schedule_id)
        midpoint = before + (after - before) / 2

        # Every observed next_run_at lands inside the documented ±5% bound.
        assert before + interval * 0.95 <= row["next_run_at"] <= after + interval * 1.05
        offsets.append(row["next_run_at"] - (midpoint + interval))

        # Force it due again for the next iteration — seeded directly, bypassing the
        # service, the same way every other fixture in this file is seeded.
        await websites_db.execute(
            "UPDATE schedules SET next_run_at = $1 WHERE id = $2",
            _NOW - timedelta(minutes=5),
            schedule_id,
        )

    # The two draws differ by more than a trivial fraction of a second — a hardcoded,
    # never-jittered `now + interval` could never do this twice in a row.
    assert abs((offsets[0] - offsets[1]).total_seconds()) > 1


# -----------------------------------------------------------------------------------------
# 10. The batch limit.
# -----------------------------------------------------------------------------------------


async def test_51_due_schedules_hits_the_batch_limit_and_the_next_tick_picks_up_the_rest(
    websites_db: Pool, schedule_service: ScheduleService, caplog: pytest.LogCaptureFixture
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_ids: list[UUID] = []
    schedule_ids: list[UUID] = []
    for i in range(DUE_BATCH_LIMIT + 1):
        website_id = await seed_website(
            websites_db, TEST_USER_A_ID, f"https://tick-limit-{i}.example"
        )
        schedule_id = await seed_schedule(
            websites_db,
            website_id,
            active=True,
            interval_minutes=60,
            next_run_at=_NOW - timedelta(minutes=5),
        )
        website_ids.append(website_id)
        schedule_ids.append(schedule_id)

    with caplog.at_level(logging.WARNING, logger="app.features.schedules.service"):
        summary = await schedule_service.run_due_schedules(_RecordingQueuePool())

    assert summary.examined == DUE_BATCH_LIMIT
    assert summary.runs_created == DUE_BATCH_LIMIT
    assert summary.limit_reached is True
    assert any("batch limit" in record.getMessage() for record in caplog.records)

    total_runs = await websites_db.fetchval(
        "SELECT count(*) FROM runs WHERE website_id = ANY($1::uuid[])", website_ids
    )
    assert total_runs == DUE_BATCH_LIMIT

    still_due = await websites_db.fetchval(
        "SELECT count(*) FROM schedules WHERE id = ANY($1::uuid[]) AND next_run_at <= $2",
        schedule_ids,
        _NOW,
    )
    assert still_due == 1

    # The next tick picks up exactly the one schedule the first tick's cap left behind.
    second_summary = await schedule_service.run_due_schedules(_RecordingQueuePool())
    assert second_summary.examined == 1
    assert second_summary.runs_created == 1
    assert second_summary.limit_reached is False

    total_runs_after = await websites_db.fetchval(
        "SELECT count(*) FROM runs WHERE website_id = ANY($1::uuid[])", website_ids
    )
    assert total_runs_after == DUE_BATCH_LIMIT + 1


# -----------------------------------------------------------------------------------------
# 11. An enqueue failure marks only that run failed; the rest of the batch is unaffected.
# -----------------------------------------------------------------------------------------


async def test_an_enqueue_failure_marks_only_that_run_failed_and_the_batch_continues(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_a = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-enqueue-a.example")
    await seed_schedule(
        websites_db,
        website_a,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
    )
    website_b = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-enqueue-b.example")
    await seed_schedule(
        websites_db,
        website_b,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
    )

    queue = _FailFirstQueuePool()
    summary = await schedule_service.run_due_schedules(queue)

    assert summary.examined == 2
    assert summary.runs_created == 2
    assert summary.enqueue_failures == 1

    rows = await websites_db.fetch(
        "SELECT status, error FROM runs WHERE website_id = ANY($1::uuid[])",
        [website_a, website_b],
    )
    statuses = [row["status"] for row in rows]
    assert sorted(statuses) == ["failed", "pending"]

    failed_row = next(row for row in rows if row["status"] == "failed")
    assert failed_row["error"] is not None

    # The batch was not aborted: the second run's enqueue succeeded and was recorded.
    assert len(queue.enqueued) == 1


# -----------------------------------------------------------------------------------------
# 12. last_run_at moves for a schedule that ran, and replaces whatever it already held.
# -----------------------------------------------------------------------------------------


async def test_last_run_at_is_replaced_for_a_schedule_that_ran(
    websites_db: Pool, schedule_service: ScheduleService
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-last-run-at.example")
    old_last_run_at = _NOW - timedelta(days=5)
    schedule_id = await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
        last_run_at=old_last_run_at,
    )

    before = datetime.now(UTC)
    await schedule_service.run_due_schedules(_RecordingQueuePool())
    after = datetime.now(UTC)

    row = await _schedule_row(websites_db, schedule_id)
    assert row["last_run_at"] != old_last_run_at
    assert before <= row["last_run_at"] <= after


# -----------------------------------------------------------------------------------------
# 13. The per-tick summary log carries all five numbers.
# -----------------------------------------------------------------------------------------


async def test_the_summary_log_contains_all_five_numbers(
    websites_db: Pool, schedule_service: ScheduleService, caplog: pytest.LogCaptureFixture
) -> None:
    await _assert_no_foreign_due_schedules(websites_db)
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tick-summary-log.example")
    await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW - timedelta(minutes=5),
    )

    with caplog.at_level(logging.INFO, logger="app.features.schedules.service"):
        summary = await schedule_service.run_due_schedules(_RecordingQueuePool())

    tick_log = next(
        record.getMessage() for record in caplog.records if "Cron tick" in record.getMessage()
    )
    assert f"examined={summary.examined}" in tick_log
    assert f"runs_created={summary.runs_created}" in tick_log
    assert f"skipped_active={summary.skipped_active}" in tick_log
    assert f"enqueue_failures={summary.enqueue_failures}" in tick_log
    assert f"limit_reached={summary.limit_reached}" in tick_log


# -----------------------------------------------------------------------------------------
# 14. The Pool guard on the locking read — the one defensive check that cannot self-verify.
# -----------------------------------------------------------------------------------------


async def test_lock_due_refuses_to_run_on_a_pool(websites_db: Pool) -> None:
    """`SchedulesReader(pool).lock_due(...)` raises rather than silently taking useless locks.

    This is the one check in the tick's path that no other test can reach, precisely because
    every caller does the right thing: `run_due_schedules` always constructs its reader from
    the `Connection` a `transaction()` yields. Left untested, the guard could be deleted or
    inverted and the whole suite would stay green — while production quietly went back to
    acquiring a connection, taking row locks, and handing both back to the pool before the
    caller saw a single row. That failure has no symptom until a second worker machine exists,
    which is the same reason `SKIP LOCKED` itself needs its own test.
    """
    with pytest.raises(RuntimeError, match="Pool"):
        await SchedulesReader(websites_db).lock_due(now=datetime.now(UTC), limit=DUE_BATCH_LIMIT)
