"""The first write path in the runs feature: `INSERT`s and `UPDATE`s for
`POST /websites/{id}/runs`.

**This writer never commits, never rolls back, and never opens a transaction** — the same
contract `websites/internals/websites_writer.py` documents at length, and the reason it is
not repeated in full here. A `RunsWriter` is built *inside* `RunService.trigger_run`'s
`transaction()` block, from the `Connection` that block yields:

```python
async with transaction(self._pool) as tx:
    row = await RunsWriter(tx).insert_manual(website_id)
```

**No ownership check happens here**, for the same reason none happens in
`WebsitesWriter`: by the time either method below runs, `RunService.trigger_run` has already
fetched the website and called `require_owner`. Neither statement takes a `user_id`.
"""

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Writer


# `"trigger"` is quoted because it is a reserved SQL keyword — the same reason
# `tests/conftest.py`'s `seed_run` quotes it, and the generated migration
# (db/migrations/20260805092204_init/migration.sql) quotes the column name everywhere it
# appears. Leaving the quotes off is a syntax error, not a lint nit.
#
# `schedule_id` is written as an explicit `NULL` rather than left to the column's default —
# which is also `NULL` — because "a manual run has no schedule" is a fact this endpoint's
# contract asserts, not an accident of what the column happens to default to. A reader of
# this statement should not have to check the schema to know the value; leaving the column
# out of the INSERT list entirely would ask them to.
#
# `started_at` is NOT supplied: it is `DEFAULT CURRENT_TIMESTAMP`, so the database is the
# only place that value is decided, and `RETURNING` is the only place it can be read back —
# reconstructing it from a Python `datetime.now(UTC)` captured a moment earlier would be a
# guess that could disagree with what was actually persisted by a matter of milliseconds.
_INSERT_MANUAL: Final = """
    INSERT INTO runs (website_id, "trigger", status, schedule_id)
    VALUES ($1, 'manual', 'pending', NULL)
    RETURNING id, status, started_at
"""

# Used exactly once, on `RunService.trigger_run`'s enqueue-failure path: the insert
# committed, then `queue.enqueue_job` raised, and this is what stops that row from sitting
# in `pending` forever with no job behind it. `AND status = 'pending'` is a guard, not a
# redundant restatement of what the row already is — this statement runs strictly after the
# INSERT that created the row and strictly before anything else could have touched it, so
# the guard should always match, but it is there for the same reason `require_owner` checks
# rather than assumes: if a future change ever lets something else claim the run first (a
# worker racing ahead, a retry path added later), this must not stomp a `processing` or
# `completed` row back to `failed`. A no-op `UPDATE` here is the safe failure mode; silently
# overwriting a real result would not be.
_MARK_FAILED: Final = """
    UPDATE runs
    SET status = 'failed', error = $2, completed_at = $3
    WHERE id = $1 AND status = 'pending'
"""


class RunsWriter(Writer):
    """Writes for the runs feature. Private to it — only `RunService` calls this."""

    async def insert_manual(self, website_id: UUID) -> dict[str, Any]:
        """Insert one manually-triggered, `pending` run for `website_id` and return it.

        Args:
            website_id: The website this run belongs to. Ownership has already been
                checked by the caller (`RunService.trigger_run`); this statement carries no
                predicate that could fail an authorization check, because it is not the
                layer that performs one.

        Returns:
            A dict with `id`, `status` (always `"pending"`), and `started_at` — exactly
            what `TriggerRunResponse` needs, so the caller costs one round trip rather than
            an INSERT followed by a SELECT.

        Raises:
            asyncpg.ForeignKeyViolationError: if `website_id` names a website deleted
                between `RunService.trigger_run`'s fetch and this INSERT — the same race
                `ScheduleService.upsert_schedule` documents for its own foreign key.
                Deliberately not caught here: turning it into the right HTTP response is the
                service's job, not this writer's.
            RuntimeError: if `RETURNING` somehow produced no row, exactly as
                `WebsitesWriter.insert` guards the same impossible case.
        """
        row = await self.fetch_one(_INSERT_MANUAL, website_id)
        if row is None:
            raise RuntimeError("INSERT INTO runs ... RETURNING produced no row")
        return row

    async def mark_failed(self, run_id: UUID, error: str, completed_at: datetime) -> str:
        """Mark `run_id` `failed`, but only if it is still `pending`. See `_MARK_FAILED`.

        Args:
            run_id: The run whose enqueue failed.
            error: A message safe to store and eventually surface — never a raw exception
                repr, since `RunService.trigger_run` catches broadly and an arbitrary
                exception's `str()` is not vetted for anything sensitive it might contain.
            completed_at: The same `now` `RunService.trigger_run` captured before opening
                its insert transaction, so `started_at`, `completed_at`, and the moment the
                429/503 arithmetic used all agree with each other to the microsecond.

        Returns:
            asyncpg's status tag (`"UPDATE 1"` or `"UPDATE 0"`). The service does not branch
            on it — by the time this runs, the outcome is a `503` either way; see the module
            docstring's note on why the guard exists without being load-bearing here.
        """
        return await self.execute(_MARK_FAILED, run_id, error, completed_at)
