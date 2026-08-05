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
"""

from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Reader


# `auto_publish` is not here — see the module docstring. Named once so the reader and the
# writer's `RETURNING` clause cannot drift apart from each other or from `ScheduleResponse`.
_COLUMNS: Final = "id, website_id, active, interval_minutes, next_run_at, last_run_at"

_GET_BY_WEBSITE_ID: Final = f"""
    SELECT {_COLUMNS}
    FROM schedules
    WHERE website_id = $1
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
