"""The one write for the schedules feature: the upsert behind `PUT /websites/{id}/schedule`.

**This writer never commits, never rolls back, and never opens a transaction.** It executes
one statement against whatever it was constructed with and returns. The unit of work belongs
to `transaction()` in the service (ARCHITECTURE.md §5), which is what lets this writer's call
share a transaction with anything else a future feature needs alongside it: neither assumes it
owns the connection it is running on.

Consequently a `SchedulesWriter` is built *inside* a service's `transaction()` block, from the
`Connection` that block yields — mirroring `WebsitesWriter` (`websites/internals/
websites_writer.py`):

```python
async with transaction(self._pool) as tx:
    row = await SchedulesWriter(tx).upsert(...)
```

**No ownership check happens here.** By the time this runs, `ScheduleService.upsert_schedule`
has already fetched the parent website and called `require_owner` on it — `schedules` itself
has no `user_id` to check anything against. Putting any such check here would have nothing to
compare.
"""

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
