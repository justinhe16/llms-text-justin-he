"""Business logic and transaction boundaries for the schedules feature.

**This is the first cross-feature call in this codebase**, and it exists because `schedules`
has no `user_id` of its own. `websites/__init__.py` blesses exactly this pattern already: "If
another feature needs website data, it calls `WebsiteService`" — never another feature's
`internals/` (ARCHITECTURE.md §3.1's "no feature imports another feature's internals" rule).
`ScheduleService` needed it first, because both halves of the authorization contract for this
feature are facts about the *parent* website, not about the `schedules` row itself:

* The `404` — "does this website exist?" — is answered by `WebsiteService.get_website`, not
  by anything in `SchedulesReader`. A schedule can be entirely absent (a website with no
  schedule yet is normal, §4.1) while its website is very much real, so "no schedule row"
  must never be confused with "no such website".
* The `403` — "does the caller own it?" — is answered by calling `require_owner` on the
  *website* `WebsiteService.get_website` returns, because `require_owner` needs a `.user_id`
  to compare against and `schedules` does not have one. Ownership of a schedule is ownership
  of the website it belongs to; there is no separate concept to check.

**The cron tick is the second.** `run_due_schedules` below needs `RunService` for the same
underlying reason `get_schedule`/`upsert_schedule` need `WebsiteService`: the tick has to lock
`schedules` rows and insert the `runs` rows they produce, and `runs` is a table this feature
does not own. See `RunService`'s module docstring, "Two methods that take an already-open
`Connection`", for the mechanics of how that call stays inside the tick's own transaction
without this feature importing `app.features.runs.internals`.

Everything else here follows the same shape `WebsiteService` established (ARCHITECTURE.md
§3.1's "first feature module is the reference implementation"): reads are unscoped, writes
fetch-then-`require_owner`-then-transact, and the transaction is opened here and nowhere else.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from random import Random
from typing import Any, Final
from uuid import UUID

from arq.connections import ArqRedis
from asyncpg import ForeignKeyViolationError, Pool
from fastapi import HTTPException, status

from app.core.auth.ownership import require_owner
from app.core.settings import Settings
from app.features.runs.service import CRAWL_TASK_JOB_NAME, RunService
from app.features.schedules.internals.next_run import (
    CurrentSchedule,
    RequestedSchedule,
    advance_next_run_at,
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

# The most schedules one tick may lock and act on. At one tick a minute (see
# `app/worker/settings.py`'s `cron_jobs`), 50 is 3,000 potential runs an hour — far more than
# this system will ever need to queue in its current form — and the cap exists to bound how
# long any ONE tick can hold its transaction open: `app/worker/settings.py`'s worker runs
# `max_jobs = 2` concurrent jobs, so a tick that tried to process an unbounded batch could
# monopolize a worker process for the whole minute it has before the next tick is due. If more
# than this many schedules are due at once, the remainder simply waits for the next tick —
# they were already overdue by however long the tick was saturated, and one more minute does
# not change the shape of that problem.
DUE_BATCH_LIMIT: Final = 50

# One module-level generator, shared by every tick this process ever runs — not constructed
# fresh per tick, and not `random.SystemRandom`. Jitter (`advance_next_run_at`,
# `internals/next_run.py`) exists to spread load, not to resist an adversary who can predict
# it, so the extra cost of a cryptographically secure source buys nothing here.
_JITTER_RNG: Final = Random()


@dataclass(frozen=True)
class TickSummary:
    """What one call to `run_due_schedules` did, for its own log line and for tests.

    Lives here, not in `schemas.py`: this is not a response shape any HTTP endpoint returns,
    it is an internal bookkeeping record for the worker's own log and for
    `tests/test_schedule_tick.py` to assert against directly.
    """

    # How many schedules this tick locked with `SchedulesReader.lock_due` — the size of the
    # batch it had to make a decision about, whether or not every one of them ran.
    examined: int

    # How many of those schedules produced a new `runs` row.
    runs_created: int

    # How many were skipped because their website already had a `pending`/`processing` run —
    # see `run_due_schedules`'s docstring for why their `next_run_at` still moved.
    skipped_active: int

    # How many of `runs_created`'s enqueue attempts raised — see `_enqueue` below. Each one
    # left its run `failed` rather than orphaned `pending`.
    enqueue_failures: int

    # `True` when `examined == DUE_BATCH_LIMIT`: this tick's batch was full, which means there
    # MAY be more due schedules this tick never even looked at. `False` proves nothing was
    # left behind by the cap — every schedule that was due got examined.
    limit_reached: bool


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

    def __init__(self, pool: Pool, run_service: RunService) -> None:
        self._pool = pool
        # Bound to the pool: each read borrows a connection and returns it immediately.
        self._reader = SchedulesReader(pool)
        # The cross-feature dependency this module's docstring justifies. Calling
        # `WebsiteService` — never `app.features.websites.internals` — is what keeps the
        # dependency graph acyclic (ARCHITECTURE.md §3.1).
        self._websites = WebsiteService(pool)
        # INJECTED, not constructed here — mirroring how `RunService` itself receives its own
        # `WebsiteService` rather than building one internally. `RunService.__init__`'s own
        # comment explains why: it needs `Settings`, and this service must not read the
        # `app.core.settings.settings` module singleton to get one, because that would put
        # `RunService`'s per-user caps beyond the reach of a test that overrides `Settings`
        # only through dependency injection. `get_schedule_service` (`api/routers/
        # schedules.py`) and `build_schedule_service` below are the two places that decide
        # what `RunService` this ends up with.
        self._runs = run_service

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

        **One accepted race, named here rather than left to be discovered** — the same
        convention `runs/service.py`'s "The races, documented honestly" section follows. The
        `get_by_website_id` read above is unlocked and happens outside the transaction below,
        so a no-op `PUT` (identical `active` and `interval_minutes`) can interleave with the
        cron tick like this:

        1. `run_due_schedules` locks the schedule with `FOR UPDATE` and starts its work.
        2. This method's read observes the row as it was BEFORE that tick — `next_run_at`
           still in the past — and `compute_next_run_at` row 5 keeps it, which is exactly
           right for the no-op case it was written for.
        3. `SchedulesWriter.upsert` blocks on the tick's row lock.
        4. The tick commits, having created a run and advanced `next_run_at` into the future.
        5. This method's `ON CONFLICT DO UPDATE` unblocks and writes the stale value back.

        The schedule is then due again immediately and fires one extra, out-of-cadence run on
        the next tick. It is not corruption and it does not compound: the next tick advances
        `next_run_at` properly, and the tick's own "skip a website that already has a run in
        flight" guard usually absorbs the extra run before it becomes a second crawl.

        Closing it would mean a compare-and-swap on `_UPSERT`'s `DO UPDATE` (`... WHERE
        schedules.next_run_at IS NOT DISTINCT FROM $5`) plus a re-read path for the zero-rows
        case — new write semantics on a shipped endpoint, to buy back one extra crawl of one
        website in a window a few milliseconds wide, once per interval at worst. That is a
        deliberate trade, not an oversight; if the cadence guarantee ever needs to be exact,
        the CAS is the fix and this paragraph is the ticket.

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

    async def run_due_schedules(self, queue: ArqRedis) -> TickSummary:
        """The cron tick. Lock every due schedule, turn most of them into a `runs` row, and
        advance every one of them — whether it ran or not — so it is not re-examined forever.

        Called once a minute by `app.worker.jobs.schedule_tick`, which is registered as an arq
        cron job in `app/worker/settings.py`. Not reachable from any HTTP route.

        **Ordering, and it is load-bearing:**

        ```python
        now = datetime.now(UTC)                       # one clock for the whole tick
        async with transaction(self._pool) as tx:
            due = await SchedulesReader(tx).lock_due(now=now, limit=DUE_BATCH_LIMIT)
            blocked = await self._runs.website_ids_with_active_runs(..., connection=tx)
            for row in due:
                next_at = advance_next_run_at(now=now, interval_minutes=..., rng=_JITTER_RNG)
                if row["website_id"] in blocked:
                    skipped.append((row["id"], next_at))
                    continue
                run_id = await self._runs.create_scheduled_run(..., connection=tx)
                created_run_ids.append(run_id)
                ran.append((row["id"], next_at))
            await SchedulesWriter(tx).advance_after_run(ran, last_run_at=now)
            await SchedulesWriter(tx).advance_without_running(skipped)
        # COMMITTED HERE. Nothing above this line touched the network.
        enqueue_failures = await self._enqueue(created_run_ids, queue)
        ```

        **`now` is captured once, before the transaction opens, and threaded through every
        write below it** — the same discipline `trigger_run` and `upsert_schedule` already
        follow, for the same reason: the row `lock_due` selects, the `next_run_at` every
        branch computes, and the `last_run_at` a ran schedule gets all have to agree with each
        other, which they can only do if none of them calls `datetime.now()` a second time.

        **No network call happens inside the `async with` block** (ARCHITECTURE.md §5.1). The
        Redis enqueue is the one thing in this method that talks to the network, and it runs
        strictly after the transaction has committed — the `# COMMITTED HERE.` comment in the
        source marks that boundary the same way `trigger_run`'s module docstring marks its
        own. Locking up to `DUE_BATCH_LIMIT` rows and holding them across a slow or unreachable
        Redis would turn a queue outage into a `schedules`-table outage for every other reader
        and writer in the system; committing first means the worst a bad enqueue can do is
        leave its own run `failed` or `pending`, never hold anyone else's lock hostage.

        **Every schedule `lock_due` returns gets its `next_run_at` advanced, whether it ran or
        was skipped.** A schedule left in the past — because its run failed to insert, or
        because this method special-cased "skipped" into a no-op — would be re-selected by
        every tick that follows, forever, burning a lock and a query on it each time for no
        new information. Advancing it is what turns "this website was busy a minute ago" into
        "we'll check again next time it's actually due" instead of "we'll ask every 60 seconds
        forever".

        **`last_run_at` is deliberately NOT advanced for a skipped schedule.** The column
        means "when this schedule last actually produced a run"; a tick that skipped a
        schedule produced nothing, and moving `last_run_at` anyway would erase the one
        diagnostic that answers "why hasn't my schedule fired in three days?" — a stale
        `last_run_at` next to a moving `next_run_at` is exactly the signature of "this website
        keeps having a run in flight when its schedule comes due", and collapsing the two
        columns into always-move would hide that.

        **The `for` loop draws a fresh jitter offset per schedule, every time.** Hoisting one
        draw outside the loop would give every schedule in a batch the identical offset,
        which defeats the entire point of jittering (`advance_next_run_at`'s docstring): a
        cohort that all became due together would drift back into lockstep instead of
        spreading out.

        **A crash between the commit above and the last `_enqueue` call below leaves up to
        `DUE_BATCH_LIMIT` runs `pending` with nothing behind them.** This is the same class of
        risk `trigger_run`'s module docstring accepts for its own insert-then-enqueue
        ordering — insert-first is the recoverable failure mode, because "a `pending` row with
        no job" is a state a query can find and a reaper can sweep, while the reverse ordering
        risks a job that names a row that was never committed, which no query can ever find.
        This method just accepts that cost N-at-a-time instead of one-at-a-time. No reaper is
        built here: ARCHITECTURE.md §6.4 already names the stuck-run reaper as a separate,
        future ticket, and this docstring is what points at it rather than inventing a
        smaller, undocumented one inline.

        Returns:
            A `TickSummary` with all five counts, even when `due` comes back empty — an empty
            batch is not special-cased into a separate return path (see the source): every
            step below already does the right, no-op thing for an empty list, so falling
            through produces an all-zero summary through the same code every other tick runs.
        """
        now = datetime.now(UTC)
        created_run_ids: list[UUID] = []
        ran: list[tuple[UUID, datetime]] = []
        skipped: list[tuple[UUID, datetime]] = []

        async with transaction(self._pool) as tx:
            due = await SchedulesReader(tx).lock_due(now=now, limit=DUE_BATCH_LIMIT)

            blocked = await self._runs.website_ids_with_active_runs(
                [row["website_id"] for row in due], connection=tx
            )

            for row in due:
                # Drawn fresh for every row — see the docstring's "fresh jitter offset" note.
                next_at = advance_next_run_at(
                    now=now, interval_minutes=row["interval_minutes"], rng=_JITTER_RNG
                )
                if row["website_id"] in blocked:
                    skipped.append((row["id"], next_at))
                    continue
                run_id = await self._runs.create_scheduled_run(
                    row["website_id"], row["id"], connection=tx
                )
                created_run_ids.append(run_id)
                ran.append((row["id"], next_at))

            writer = SchedulesWriter(tx)
            await writer.advance_after_run(ran, last_run_at=now)
            await writer.advance_without_running(skipped)
        # COMMITTED HERE. Nothing above this line touched the network — see the docstring's
        # "no network call inside the async with block" section.

        enqueue_failures = await self._enqueue(created_run_ids, queue)

        summary = TickSummary(
            examined=len(due),
            runs_created=len(created_run_ids),
            skipped_active=len(skipped),
            enqueue_failures=enqueue_failures,
            limit_reached=len(due) == DUE_BATCH_LIMIT,
        )
        # One line, every tick, with all five numbers: "why didn't my schedule fire?" should
        # be answerable from this log alone — was it even examined, and if so, did it run,
        # get skipped for an active run, or fail to enqueue?
        logger.info(
            "Cron tick: examined=%d runs_created=%d skipped_active=%d enqueue_failures=%d "
            "limit_reached=%s",
            summary.examined,
            summary.runs_created,
            summary.skipped_active,
            summary.enqueue_failures,
            summary.limit_reached,
        )
        if summary.limit_reached:
            logger.warning(
                "Cron tick hit its batch limit (%d) — there may be more schedules due than "
                "this tick could examine. Sustained saturation means scaling workers, not "
                "raising this number: see DUE_BATCH_LIMIT's own comment.",
                DUE_BATCH_LIMIT,
            )
        return summary

    async def _enqueue(self, run_ids: list[UUID], queue: ArqRedis) -> int:
        """Enqueue `crawl_task` for every id in `run_ids`, and return how many failed.

        Runs strictly after `run_due_schedules`'s transaction has committed — see that
        method's docstring. Each enqueue is independent: one unreachable moment must not
        strand the other runs in the same batch, so a failure here is caught, counted, and
        the loop continues rather than propagating and abandoning the rest of `run_ids`.

        Broad `except Exception`, not `except RedisError`, for the same reason
        `RunService.trigger_run`'s module docstring gives for its own enqueue: arq's
        `enqueue_job` can surface `redis.exceptions.RedisError`, `OSError`,
        `asyncio.TimeoutError`, or a plain `ConnectionError` depending on exactly how the
        connection failed, and narrowing this to one exception type would leave every other
        type as a hole in the "no run is left pending with nothing behind it" guarantee.

        On failure, `RunService.abandon_unqueued_run` is called with `datetime.now(UTC)`
        read FRESH here, never the tick's own `now`. `started_at` on the run row was assigned
        by the database's column default at INSERT time, strictly inside the transaction that
        already committed by the time this method runs — which is strictly AFTER the tick's
        `now` was captured. Passing that stale `now` through as `completed_at` could produce a
        `completed_at` earlier than `started_at`, which would render as a negative duration
        anywhere this run's history is displayed. `trigger_run` does not have this problem
        because its `now` is captured before its OWN insert, on the same synchronous path;
        this method's enqueue loop runs after a transaction whose insert already happened at
        an earlier, database-decided instant it has no way to recover.

        Args:
            run_ids: The ids `run_due_schedules` just committed as new `pending` runs.
            queue: The arq `ArqRedis` pool to enqueue onto.

        Returns:
            How many of `run_ids` failed to enqueue.
        """
        failures = 0
        for run_id in run_ids:
            try:
                await queue.enqueue_job(CRAWL_TASK_JOB_NAME, str(run_id))
            except Exception:
                failures += 1
                # `exc_info=True` logs the real cause for an operator; `REDIS_URL` is never
                # interpolated into this message or any other (ARCHITECTURE.md §9.4).
                logger.error(
                    "Cron tick: failed to enqueue %s for run %s",
                    CRAWL_TASK_JOB_NAME,
                    run_id,
                    exc_info=True,
                )
                await self._runs.abandon_unqueued_run(run_id, datetime.now(UTC))
        return failures


def build_schedule_service(pool: Pool, settings: Settings) -> ScheduleService:
    """Build a `ScheduleService` for the worker, from the resources
    `app.worker.jobs.schedule_tick` already has: the process-wide pool and the module
    `Settings`.

    Constructs its own `WebsiteService` and `RunService` rather than importing either
    feature's router-level provider function — those are wired for FastAPI's dependency
    injection, which does not exist inside an arq cron job. Mirrors
    `app.features.crawl.service.build_crawl_service`, which builds the same
    `WebsiteService`/`RunService` pair for the same reason.
    """
    website_service = WebsiteService(pool)
    run_service = RunService(pool, website_service, settings)
    return ScheduleService(pool, run_service)
