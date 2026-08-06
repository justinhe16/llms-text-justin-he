"""Every write for the schedules feature: the upsert behind `PUT /websites/{id}/schedule`, plus
the two batched advances the cron tick makes after locking a batch of due schedules.

**This writer never commits, never rolls back, and never opens a transaction.** Every method
below executes one statement against whatever it was constructed with and returns. The unit of
work belongs to `transaction()` in the service (ARCHITECTURE.md §5), which is what lets a
writer's call share a transaction with anything else a caller needs alongside it: neither
assumes it owns the connection it is running on.

Consequently a `SchedulesWriter` is built *inside* a service's `transaction()` block, from the
`Connection` that block yields — mirroring `WebsitesWriter` (`websites/internals/
websites_writer.py`):

```python
async with transaction(self._pool) as tx:
    row = await SchedulesWriter(tx).upsert(...)
```

**No ownership check happens here.** By the time `upsert` runs, `ScheduleService.
upsert_schedule` has already fetched the parent website and called `require_owner` on it —
`schedules` itself has no `user_id` to check anything against. Putting any such check here
would have nothing to compare. The cron tick's two methods below have no caller to check
either, for the same reason `runs_writer.py`'s worker-facing methods do not: a background job
acts on its own behalf.

## Why the tick's advance is two statements, not one

`advance_after_run` sets `last_run_at`; `advance_without_running` does not touch it at all.
`last_run_at` means "when this schedule last actually produced a run" — a schedule the tick
SKIPPED because its website already had a run in flight produced nothing, so that column must
not move. But its `next_run_at` still has to advance, or the row stays due forever and every
future tick re-locks it, re-evaluates it, and re-skips it for no reason. A schedule that ran
needs both columns moved together; a schedule that was skipped needs exactly one of them
moved. No single `UPDATE` can express "sometimes touch this column, sometimes don't" as
cleanly as two named statements that each say, in their own `SET` list, exactly what they
touch.

## Why `unnest(...)` and not one `UPDATE` per row

Every schedule in a tick's batch gets a genuinely DIFFERENT `next_run_at` — a different
`interval_minutes`, and an independent jitter draw per row (`advance_next_run_at`,
`internals/next_run.py`) — so a single `UPDATE ... SET next_run_at = $2` cannot express the
whole batch; each row needs its own value. `unnest($1::uuid[], $2::timestamptz[])` zips two
parallel arrays into a row set and joins it back onto `schedules` by id, so the whole batch
lands in one round trip instead of up to `DUE_BATCH_LIMIT` (`schedules/service.py`) separate
statements — while still being fully parameterized: both arrays travel as ordinary bind
parameters, never string-interpolated (`base_repository.py`'s "never interpolate a value into
SQL").
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Writer


# `ON CONFLICT (website_id)` infers `schedules_website_id_key` — the `UNIQUE` index on
# `website_id` (db/schema.prisma's `Schedule.websiteId @unique`) — which is the thing that
# makes "one schedule per website" true and makes repeated PUTs impossible to duplicate: two
# `PUT`s for the same website always resolve to the same row, never two competing ones.
#
# The `DO UPDATE SET` list deliberately omits `last_run_at` and `auto_publish`. That is not a
# convention this writer has to remember to honor on every future edit — it is a fact about
# the SQL: neither column CAN be touched by this statement, because neither is assigned here.
# "A PUT never touches `last_run_at`" therefore holds by construction, not by discipline.
_UPSERT: Final = """
    INSERT INTO schedules (website_id, active, interval_minutes, next_run_at)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (website_id) DO UPDATE
        SET active = EXCLUDED.active,
            interval_minutes = EXCLUDED.interval_minutes,
            next_run_at = EXCLUDED.next_run_at
    RETURNING id, website_id, active, interval_minutes, next_run_at, last_run_at
"""

# The cron tick's advance for a schedule that DID produce a run this tick. See the module
# docstring's two sections above for why this is a separate statement from
# `_ADVANCE_WITHOUT_RUNNING` below, and why both use `unnest` rather than one `UPDATE` per row.
_ADVANCE_AFTER_RUN: Final = """
    UPDATE schedules AS s
    SET next_run_at = v.next_run_at, last_run_at = $3
    FROM unnest($1::uuid[], $2::timestamptz[]) AS v(id, next_run_at)
    WHERE s.id = v.id
"""

# The cron tick's advance for a schedule that was SKIPPED because its website already had a
# run in flight. Deliberately does not assign `last_run_at` — see the module docstring's "why
# the tick's advance is two statements" section for why that omission is the entire point of
# this statement existing separately from `_ADVANCE_AFTER_RUN`.
_ADVANCE_WITHOUT_RUNNING: Final = """
    UPDATE schedules AS s
    SET next_run_at = v.next_run_at
    FROM unnest($1::uuid[], $2::timestamptz[]) AS v(id, next_run_at)
    WHERE s.id = v.id
"""


class SchedulesWriter(Writer):
    """Writes for the schedules feature. Private to it — only `ScheduleService` calls this."""

    async def upsert(
        self,
        *,
        website_id: UUID,
        active: bool,
        interval_minutes: int,
        next_run_at: datetime | None,
    ) -> dict[str, Any]:
        """Create or replace the schedule for `website_id`, and return the row that results.

        Args:
            website_id: The parent website. Ownership has already been checked by the caller
                — see the module docstring.
            active: Whether the schedule should run automatically.
            interval_minutes: Minutes between runs.
            next_run_at: The next run time to persist, already computed by
                `internals.next_run.compute_next_run_at` — this method performs no scheduling
                logic of its own.

        Returns:
            The upserted row, including `last_run_at`, which this statement never assigns and
            which therefore comes back unchanged from whatever it already was (or `None`, for
            a schedule that has never run).

        Raises:
            RuntimeError: If `RETURNING` somehow produced no row, which would mean an
                `INSERT ... ON CONFLICT DO UPDATE` that reported success without producing
                one — mirrors `WebsitesWriter.insert`.
        """
        row = await self.fetch_one(_UPSERT, website_id, active, interval_minutes, next_run_at)
        if row is None:
            # Unreachable: this statement always has exactly one row to insert or update, and
            # it always has a RETURNING clause — it either raises or returns a row. Asserted
            # rather than `assert`ed so it survives `python -O` (see WebsitesWriter.insert).
            raise RuntimeError("INSERT INTO schedules ... RETURNING produced no row")
        return row

    async def advance_after_run(
        self, advances: Sequence[tuple[UUID, datetime]], *, last_run_at: datetime
    ) -> str:
        """Advance `next_run_at` AND `last_run_at` for every schedule that produced a run
        this tick.

        Args:
            advances: `(schedule_id, new_next_run_at)` pairs, one per schedule that ran —
                `new_next_run_at` already computed by `internals.next_run.advance_next_run_at`,
                a different draw per schedule. Empty is a legitimate, common call (a tick where
                every due schedule was skipped): returned early, with no round trip, rather
                than sent to Postgres as a statement with two empty arrays.
            last_run_at: The ONE instant to record for every row in this call — the tick's own
                clock (`ScheduleService.run_due_schedules`'s `now`), not a per-row value. Every
                schedule this call touches ran within the same tick, so they share the same
                "last ran at".

        Returns:
            asyncpg's status tag (e.g. `"UPDATE 3"`), or `"UPDATE 0"` if `advances` was empty.
        """
        if not advances:
            return "UPDATE 0"
        ids = [schedule_id for schedule_id, _ in advances]
        next_run_ats = [next_run_at for _, next_run_at in advances]
        return await self.execute(_ADVANCE_AFTER_RUN, ids, next_run_ats, last_run_at)

    async def advance_without_running(self, advances: Sequence[tuple[UUID, datetime]]) -> str:
        """Advance ONLY `next_run_at` for every schedule the tick skipped.

        Args:
            advances: `(schedule_id, new_next_run_at)` pairs, one per schedule that was locked
                this tick but not run because its website already had a `pending`/`processing`
                run — see `ScheduleService.run_due_schedules`. Empty is the common case (a tick
                where every due schedule ran): returned early, with no round trip.

        Returns:
            asyncpg's status tag, or `"UPDATE 0"` if `advances` was empty.
        """
        if not advances:
            return "UPDATE 0"
        ids = [schedule_id for schedule_id, _ in advances]
        next_run_ats = [next_run_at for _, next_run_at in advances]
        return await self.execute(_ADVANCE_WITHOUT_RUNNING, ids, next_run_ats)
