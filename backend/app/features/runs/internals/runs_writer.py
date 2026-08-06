"""Every write in the runs feature — two write paths, which arrived from two directions.

`POST /websites/{id}/runs` owns `insert_manual` and `mark_failed`: create a `pending` run,
and undo it if enqueueing the job afterwards fails. The crawl worker
(`app.worker.jobs.crawl_task`) owns `claim_pending` and `mark_processing_failed`: take a
`pending` run atomically, and record a terminal failure if the crawl never produces an
artifact.

**Why there are two "mark this failed" statements rather than one.** They guard on different
statuses, and that is the whole of the difference: `mark_failed` fires on the enqueue-failure
path, where the run must still be `pending`, while `mark_processing_failed` fires from the
worker, after `claim_pending` has already moved the row to `processing`. Collapsing them into
one unguarded `UPDATE` would let either path stomp a row the other is legitimately working
on; collapsing them into one statement guarded on `pending` would make the worker's failure
record a silent no-op, since by then the row is never `pending` any more. Each statement
therefore names the status it is allowed to leave, and a no-op is the safe failure mode for
both.

**This writer never commits, never rolls back, and never opens a transaction** — the same
contract `websites/internals/websites_writer.py` documents at length, and the reason it is
not repeated in full here. A `RunsWriter` is built *inside* the calling service method's
`transaction()` block, from the `Connection` that block yields:

```python
async with transaction(self._pool) as tx:
    row = await RunsWriter(tx).insert_manual(website_id)
```

**No ownership check happens here**, and for two different reasons depending on the caller.
On the HTTP path, `RunService.trigger_run` has already fetched the website and called
`require_owner` before any method below runs — the same reason none happens in
`WebsitesWriter`. On the worker path there is no caller to check at all: a background job
acts on its own behalf, and ARCHITECTURE.md §4 is a rule about HTTP callers. No statement
below takes a `user_id` either way.
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

# THE WORKER'S IDEMPOTENCY GUARD, and the reason it is a conditional UPDATE rather than a
# SELECT followed by an UPDATE. arq can deliver the same job more than once, and
# `WorkerSettings.MAX_JOBS = 2` (app/worker/settings.py) means two of them can be in flight
# on one machine at the same moment. Both workers may issue this statement; Postgres's row
# lock serializes them, and only the one whose UPDATE actually matches `status = 'pending'`
# gets a row back from `RETURNING`. Read-then-write cannot make that guarantee at any
# isolation level this application runs at — both readers would see `pending` and both would
# proceed to crawl the same run.
#
# The enum is cast explicitly, matching `_LIST_BY_WEBSITE_WITH_STATUS` in
# `runs_reader.py` and `seed_run` in `tests/conftest.py`.
_CLAIM_PENDING: Final = """
    UPDATE runs
    SET status = 'processing'::run_status
    WHERE id = $1 AND status = 'pending'::run_status
    RETURNING id
"""

# The worker's terminal-failure record. Guarded on `processing` — NOT on `pending`, and not
# unguarded — because it only ever runs after `_CLAIM_PENDING` above has already moved this
# row out of `pending`. See the module docstring for why this is a second statement rather
# than a reuse of `_MARK_FAILED`.
#
# `completed_at = now()` is the transaction's own start time, decided by the database, for
# the same reason `_INSERT_MANUAL` above leaves `started_at` to `DEFAULT CURRENT_TIMESTAMP`:
# a Python `datetime.now(UTC)` captured a moment earlier is a guess about when the row was
# actually written. The HTTP path passes its `completed_at` in explicitly instead, because
# there it has to agree to the microsecond with arithmetic the request already did.
_MARK_PROCESSING_FAILED: Final = """
    UPDATE runs
    SET status = 'failed'::run_status, error = $2, completed_at = now()
    WHERE id = $1 AND status = 'processing'::run_status
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

    async def claim_pending(self, run_id: UUID) -> bool:
        """Atomically flip `run_id` from `pending` to `processing`.

        The correctness guard the crawl worker takes before it fetches a single byte. See
        `_CLAIM_PENDING` for why this must be one conditional statement.

        Returns:
            `True` if this call won the race and the row is now `processing` under it;
            `False` if the row was not `pending` at all — already claimed by another worker,
            already terminal, or the id does not exist. The caller cannot tell those three
            apart from this return value alone, and does not need to: all three mean "do not
            crawl."
        """
        row = await self.fetch_one(_CLAIM_PENDING, run_id)
        return row is not None

    async def mark_processing_failed(self, run_id: UUID, error: str) -> str:
        """Record a terminal failure for a run this worker had already claimed.

        Args:
            run_id: The run being failed. Must have been claimed by `claim_pending` first —
                this statement matches only a `processing` row.
            error: A message already sanitized by the caller
                (`app.features.crawl.service.CrawlService._safe_error_message`). Never a raw
                exception string: `runs.error` is readable by every signed-in user
                (ARCHITECTURE.md §4.1), and an `httpx` exception's `str()` carries internal
                hostnames and resolved addresses.

        Returns:
            asyncpg's status tag. `"UPDATE 0"` means the row was no longer `processing` —
            something else reached a terminal state first — which is precisely the case the
            guard exists to leave alone rather than overwrite.
        """
        return await self.execute(_MARK_PROCESSING_FAILED, run_id, error)
