"""Every `SELECT` for the schedules feature. No writes, ever (ARCHITECTURE.md §3.1).

**THIS READ IS INTENTIONALLY NOT SCOPED TO THE CALLER. DO NOT ADD `WHERE user_id = $1`
HERE** — there is no `user_id` column on `schedules` to add it with, and there should never
be one. `schedules` is owned through its parent `websites` row: whether a schedule may be
*read* is governed by ARCHITECTURE.md §4.1 the same way every other read in this codebase is
("any signed-in user can read every website and every run"), and whether it may be *written*
is checked in `ScheduleService.upsert_schedule` against the parent website's owner, not
against anything in this table. `get_by_website_id` below takes no `user_id` for exactly that
reason — there is nothing for it to filter by.

**The column list deliberately omits `auto_publish`.** `_COLUMNS` names every column this
feature is allowed to touch, and it is a `SELECT {_COLUMNS}`, never `SELECT *` — the one thing
that guarantees `auto_publish` cannot leak into a dict this feature hands to `ScheduleResponse`
by way of a schema change nobody who touches this file would notice. If `SELECT *` were used
here, adding a column to `schedules` for an unrelated feature would silently start returning
it from this reader too.

**`lock_due`, below, is not this reader's API read.** It backs the cron tick
(`ScheduleService.run_due_schedules`), not `GET /websites/{id}/schedule`, and it locks rows
rather than merely reading them — the one place in this feature where "reader" means "the
`SELECT` half of a read-then-write", not "an unscoped, lock-free query". It stays in this file
rather than growing a third file because it is still exactly one `SELECT` statement; only its
shape and its transaction requirement are unusual.
"""

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from asyncpg import Pool

from app.infrastructure.db.base_repository import Reader


# `auto_publish` is not here — see the module docstring. Named once so the reader and the
# writer's `RETURNING` clause cannot drift apart from each other or from `ScheduleResponse`.
_COLUMNS: Final = "id, website_id, active, interval_minutes, next_run_at, last_run_at"

_GET_BY_WEBSITE_ID: Final = f"""
    SELECT {_COLUMNS}
    FROM schedules
    WHERE website_id = $1
"""

# The cron tick's hot-path query. Only three columns — `id`, `website_id`, `interval_minutes`
# — because this is the TICK's projection, not the API's: `_COLUMNS` above is what
# `ScheduleResponse` needs, and merging the two would make this query select (and lock) more
# than the tick actually reads.
#
# `FOR UPDATE SKIP LOCKED` locks every selected row for the CALLER's transaction, so no other
# tick can select the same schedule until this one commits or rolls back. `SKIP LOCKED`
# specifically, not plain `FOR UPDATE`: a concurrent ticker simply skips a row another tick
# already holds, rather than blocking on it. Blocking would only serialize two overlapping
# ticks — the second would still wake up once the first releases the lock, re-evaluate rows
# the first has ALREADY advanced past being due, and do no useful work with them, all while
# holding a pooled connection open for however long the first tick took. Skipping costs
# nothing: the row is due again in one interval (plus jitter), or sooner if it was skipped
# because its website already had a run in flight, and the next tick picks it up either way.
#
# `next_run_at <= $1` compares against a bind parameter, never SQL `now()`. The whole tick —
# this read, the `next_run_at` it writes, and the `last_run_at` it writes — runs off ONE clock
# captured once in the service, so those three things cannot disagree with each other by the
# width of however long the transaction takes. It is still a plain range predicate against a
# constant, so `schedules_active_next_run_at_idx` (`db/schema.prisma`'s `@@index([active,
# nextRunAt])`) answers it exactly as it would answer `next_run_at <= now()`. It also means an
# `active` row with a `NULL next_run_at` is excluded with no explicit `IS NOT NULL`: `NULL <=
# $1` evaluates to `NULL` in SQL, which `WHERE` treats as not-true, same as `False`.
#
# `ORDER BY next_run_at` puts the longest-overdue schedules first, so a tick that hits
# `LIMIT $2` before finishing makes progress in a fair order rather than starving the same
# schedules behind newer ones on every single wake-up.
#
# `LIMIT $2` bounds how much work one tick can take on — see `DUE_BATCH_LIMIT`
# (`schedules/service.py`) for why that number is 50.
_LOCK_DUE: Final = """
    SELECT id, website_id, interval_minutes
    FROM schedules
    WHERE active AND next_run_at <= $1
    ORDER BY next_run_at
    FOR UPDATE SKIP LOCKED
    LIMIT $2
"""


class SchedulesReader(Reader):
    """Reads for the schedules feature. Private to it — only `ScheduleService` calls this."""

    async def get_by_website_id(self, website_id: UUID) -> dict[str, Any] | None:
        """Return the schedule for `website_id`, or `None` if that website has none yet.

        "No schedule" is a normal, common state — most websites never get one — not an error.
        `ScheduleService.get_schedule` turns `None` here into a `200` with a `null` body, not
        a `404`; the `404` for an unknown website is a fact about `websites`, checked before
        this method ever runs.
        """
        return await self.fetch_one(_GET_BY_WEBSITE_ID, website_id)

    async def lock_due(self, *, now: datetime, limit: int) -> list[dict[str, Any]]:
        """Lock and return up to `limit` active, due schedules, oldest-due first.

        **Must be constructed with the `Connection` a `transaction()` block yields, never
        with the pool.** `FOR UPDATE` has no meaning outside a transaction: run through the
        pool, `_LOCK_DUE` would acquire a connection, take its row locks, and release that
        connection back to the pool — and the locks with it — before the caller ever did
        anything with the rows it got back. That would silently defeat the entire point of
        this method (two overlapping ticks both seeing, and both acting on, the same
        schedule), so it is guarded here rather than left to be discovered by a flaky test.

        Args:
            now: The tick's own clock, captured once by `ScheduleService.run_due_schedules`.
                See `_LOCK_DUE`'s comment for why this is a bind parameter and not `now()`.
            limit: The most rows this call may return — `DUE_BATCH_LIMIT`
                (`schedules/service.py`) in production, a smaller number in tests that want
                to exercise the limit itself.

        Raises:
            RuntimeError: if this reader was constructed with a `Pool` rather than a
                `Connection` — see above.
        """
        if isinstance(self._db, Pool):
            raise RuntimeError(
                "SchedulesReader.lock_due() was called on a Pool. FOR UPDATE SKIP LOCKED "
                "only holds its locks for the life of a transaction, and a Pool call "
                "acquires and releases a connection (and therefore the lock) before the "
                "caller can do anything with the rows — construct SchedulesReader from the "
                "Connection yielded by transaction() instead."
            )
        return await self.fetch_all(_LOCK_DUE, now, limit)
