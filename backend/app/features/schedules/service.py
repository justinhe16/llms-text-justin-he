"""Business logic and transaction boundaries for the schedules feature.

**This is the first cross-feature call in this codebase**, and it exists because `schedules`
has no `user_id` of its own. `websites/__init__.py` blesses exactly this pattern already: "If
another feature needs website data, it calls `WebsiteService`" — never another feature's
`internals/` (ARCHITECTURE.md §3.1's "no feature imports another feature's internals" rule).
`ScheduleService` is the first feature to actually need it, because both halves of the
authorization contract for this feature are facts about the *parent* website, not about the
`schedules` row itself:

* The `404` — "does this website exist?" — is answered by `WebsiteService.get_website`, not
  by anything in `SchedulesReader`. A schedule can be entirely absent (a website with no
  schedule yet is normal, §4.1) while its website is very much real, so "no schedule row"
  must never be confused with "no such website".
* The `403` — "does the caller own it?" — is answered by calling `require_owner` on the
  *website* `WebsiteService.get_website` returns, because `require_owner` needs a `.user_id`
  to compare against and `schedules` does not have one. Ownership of a schedule is ownership
  of the website it belongs to; there is no separate concept to check.

Everything else here follows the same shape `WebsiteService` established (ARCHITECTURE.md
§3.1's "first feature module is the reference implementation"): reads are unscoped, writes
fetch-then-`require_owner`-then-transact, and the transaction is opened here and nowhere else.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from asyncpg import ForeignKeyViolationError, Pool
from fastapi import HTTPException, status

from app.core.auth.ownership import require_owner
from app.features.schedules.internals.next_run import (
    CurrentSchedule,
    RequestedSchedule,
    compute_next_run_at,
)
from app.features.schedules.internals.schedules_reader import SchedulesReader
from app.features.schedules.internals.schedules_writer import SchedulesWriter
from app.features.schedules.schemas import ScheduleResponse, UpsertScheduleRequest
from app.features.websites.service import WebsiteService
from app.infrastructure.db.transaction import transaction


logger = logging.getLogger(__name__)

# Reused for both the sequential 404 (the website is already gone when `get_website` runs)
# and the race-condition 404 below (it is deleted between that fetch and the INSERT), so a
# client sees the same message either way and cannot tell which path produced it.
_NOT_FOUND_DETAIL = "No website with that id"


def _to_current_schedule(row: dict[str, Any] | None) -> CurrentSchedule | None:
    """Adapt a reader row to the pure `next_run` module's input shape, or pass through `None`.

    A small seam between "what the database returns" and "what a pure function needs" — it
    keeps `compute_next_run_at` ignorant of dict keys and column names, which is part of what
    makes that function trivially testable without a database at all.
    """
    if row is None:
        return None
    return CurrentSchedule(
        active=row["active"],
        interval_minutes=row["interval_minutes"],
        next_run_at=row["next_run_at"],
    )


class ScheduleService:
    """Business logic for schedules. Constructed per request from the shared pool."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        # Bound to the pool: each read borrows a connection and returns it immediately.
        self._reader = SchedulesReader(pool)
        # The cross-feature dependency this module's docstring justifies. Calling
        # `WebsiteService` — never `app.features.websites.internals` — is what keeps the
        # dependency graph acyclic (ARCHITECTURE.md §3.1).
        self._websites = WebsiteService(pool)

    async def get_schedule(self, website_id: UUID) -> ScheduleResponse | None:
        """Return the schedule for `website_id`, for any signed-in caller.

        Takes no `user_id` — reads are unscoped (ARCHITECTURE.md §4.1), and a schedule has no
        owner independent of its website's.

        A `None` return is a normal `200` (a website with no schedule yet), never a `404`.
        The only `404` this method can raise is about the *website* — `schedules` is a
        singleton sub-resource that may legitimately not exist yet, and "the parent is
        missing" is a different fact than "the child is missing", which is exactly why the
        first line below discards the result of the website fetch: it is called purely for
        its `404`.

        Raises:
            HTTPException: `404` if there is no website with that id.
        """
        await self._websites.get_website(website_id)
        row = await self._reader.get_by_website_id(website_id)
        return ScheduleResponse.model_validate(row) if row is not None else None

    async def upsert_schedule(
        self, website_id: UUID, request: UpsertScheduleRequest, user_id: UUID
    ) -> ScheduleResponse:
        """Create or replace the schedule for `website_id`, owned by `user_id`.

        The canonical write path for this codebase (ARCHITECTURE.md §4.2): fetch the parent
        website, `require_owner` on it with nothing in between, THEN — and only then — compute
        the new `next_run_at` and open a transaction to persist it.

        `now` is captured here, in the service, with `datetime.now(UTC)` — **before** the
        transaction opens — and passed through as an ordinary `timestamptz` bind parameter to
        `SchedulesWriter.upsert`. The obvious alternative, letting Postgres supply `now()`
        itself, does not work here: `next_run_at` is not a column default like `created_at`'s
        `CURRENT_TIMESTAMP` — it is a *conditional* value, derived from whether the previous
        row was active, whether the interval changed, and whether it already had a
        `next_run_at`, all of which the ticket requires a pure, exhaustively-tested function
        (`compute_next_run_at`) to decide. A pure function needs its clock passed in; there is
        no way to hand it `now()` without first evaluating it in application code. The
        resulting few milliseconds of app/database clock skew is irrelevant against a 60
        minute minimum interval, and the value is always timezone-aware UTC, so there is no
        naive-timestamp hazard the way there would be for a wall-clock string built by hand.

        Raises:
            HTTPException: `404` if there is no website with that id — including the rare
                race where it existed at the fetch above but was deleted before the INSERT
                below runs, converted from `asyncpg.ForeignKeyViolationError`. `403` (from
                `require_owner`) if the caller does not own the website. Nothing about
                `interval_minutes` can produce a `422` here — pydantic already rejected an
                invalid one before this method runs.
        """
        website = await self._websites.get_website(website_id)  # 404
        require_owner(website, user_id)  # 403 — nothing between these two lines

        current = await self._reader.get_by_website_id(website_id)
        next_run_at = compute_next_run_at(
            _to_current_schedule(current),
            RequestedSchedule(active=request.active, interval_minutes=request.interval_minutes),
            now=datetime.now(UTC),
        )

        try:
            async with transaction(self._pool) as tx:
                row = await SchedulesWriter(tx).upsert(
                    website_id=website_id,
                    active=request.active,
                    interval_minutes=request.interval_minutes,
                    next_run_at=next_run_at,
                )
        except ForeignKeyViolationError:
            # The fetch above and this INSERT are not atomic, so the website can be deleted
            # in between — mirroring how `WebsiteService.create_website` recovers from
            # `UniqueViolationError` racing its own pre-check. Caught OUTSIDE the `async
            # with`: the failed INSERT has already aborted the transaction, and
            # `transaction()` has rolled it back and released the connection by the time
            # this runs. Reported as the same 404 the sequential "website never existed"
            # path gives, so a client cannot distinguish the two timings of the same fact.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL
            ) from None

        logger.info("Upserted schedule for website %s", website_id)
        return ScheduleResponse.model_validate(row)
