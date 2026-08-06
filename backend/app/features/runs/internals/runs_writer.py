"""The two writes for the runs feature: the atomic claim a worker takes before crawling, and
the failure record it leaves if the crawl never produces an artifact.

**This writer never commits, never rolls back, and never opens a transaction.** It executes
one statement against whatever it was constructed with and returns. The unit of work belongs
to `transaction()` in the service (ARCHITECTURE.md §5), which is what lets a `RunsWriter`
call share a transaction with anything else a future write needs alongside it — neither
assumes it owns the connection it is running on. Mirrors `SchedulesWriter` (`app.features.
schedules.internals.schedules_writer`):

```python
async with transaction(self._pool) as tx:
    claimed = await RunsWriter(tx).claim_pending(run_id)
```

**No ownership check happens here, and none belongs here.** Both writes below run from
`app.worker.jobs.crawl_task`, acting on its own behalf — there is no caller whose ownership
could be checked (ARCHITECTURE.md §4 is about HTTP callers; a background job is not one).
"""

from typing import Final
from uuid import UUID

from app.infrastructure.db.base_repository import Writer


# The atomic idempotency guard: a conditional UPDATE, never read-then-write. Two concurrent
# workers racing the same `run_id` (arq's `MAX_JOBS = 2` makes double delivery a real
# scenario — see `app/worker/settings.py`) can both issue this statement; Postgres's row
# lock ensures only one `UPDATE` actually matches `status = 'pending'`, so only one of them
# gets a row back. The enum is cast explicitly, matching `runs_reader.py`'s
# `_LIST_BY_WEBSITE_WITH_STATUS` and `conftest.py`'s `seed_run`.
_CLAIM_PENDING: Final = """
    UPDATE runs
    SET status = 'processing'::run_status
    WHERE id = $1 AND status = 'pending'::run_status
    RETURNING id
"""

_MARK_FAILED: Final = """
    UPDATE runs
    SET status = 'failed'::run_status, completed_at = now(), error = $2
    WHERE id = $1
    RETURNING id
"""


class RunsWriter(Writer):
    """Writes for the runs feature. Private to it — only `RunService` calls this."""

    async def claim_pending(self, run_id: UUID) -> bool:
        """Atomically flip `run_id` from `pending` to `processing`.

        Returns:
            `True` if this call won the race and the row is now `processing` under it;
            `False` if the row was not `pending` at all — already claimed by another
            worker, already terminal, or the id does not exist. The caller cannot tell
            those three apart from this return value alone, and does not need to: all
            three mean "do not crawl."
        """
        row = await self.fetch_one(_CLAIM_PENDING, run_id)
        return row is not None

    async def mark_failed(self, run_id: UUID, error: str) -> None:
        """Record a run's terminal failure: `status = 'failed'`, `completed_at = now()`,
        and the given (already-sanitized) `error` message.

        Does not branch on whether a row was found — the caller (`RunService.
        record_failure`) has no fallback behavior for "the run vanished between claiming
        it and recording the failure," and a `mark_failed` call racing a row's deletion is
        not this method's problem to solve.
        """
        await self.execute(_MARK_FAILED, run_id, error)
