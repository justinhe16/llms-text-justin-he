"""Every write in the runs feature — seven statements, which arrived from four directions.

`POST /websites/{id}/runs` owns `insert_manual` and `mark_failed`: create a `pending` run,
and undo it if enqueueing the job afterwards fails. The crawl worker
(`app.worker.jobs.crawl_task`, by way of `app.features.crawl.service.CrawlService`) owns
three: `claim_pending` takes a `pending` run atomically; `mark_processing_completed` records
a successful crawl's two artifacts, Storage location, and stats; `mark_processing_failed`
records a terminal failure for a crawl that never produces one. The cron tick
(`ScheduleService.run_due_schedules`, reached only through `RunService.create_scheduled_run`
— ARCHITECTURE.md §3.1) owns `insert_scheduled`: the same shape as `insert_manual`, with the
two differences `_INSERT_SCHEDULED`'s comment names. The stuck-run reaper
(`RunService.reap_stuck_runs`, PER-166) is the fourth direction, and it introduced no
statement of its own except `return_processing_to_pending` — it reuses
`mark_processing_failed` for the runs it gives up on, because "a claimed run that will never
finish" is the same write whether the worker noticed or the reaper did.

**Why there are two "mark this failed" statements rather than one.** They guard on different
statuses, and that is the whole of the difference: `mark_failed` fires on the enqueue-failure
path, where the run must still be `pending`, while `mark_processing_failed` fires from the
worker, after `claim_pending` has already moved the row to `processing`. Collapsing them into
one unguarded `UPDATE` would let either path stomp a row the other is legitimately working
on; collapsing them into one statement guarded on `pending` would make the worker's failure
record a silent no-op, since by then the row is never `pending` any more. Each statement
therefore names the status it is allowed to leave, and a no-op is the safe failure mode for
both. `mark_processing_completed` extends that same argument rather than contradicting it: it
guards on `processing` for exactly the reason `mark_processing_failed` does — a no-op on a row
that has already reached a terminal state by some other path is the safe outcome, not an
error this method needs to raise about (`RunService.record_success` logs it instead; see that
method's docstring for why).

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

import json
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

# The cron tick's insert — same shape as `_INSERT_MANUAL` above, with exactly two deliberate
# differences: `'scheduled'` rather than `'manual'` for `"trigger"`, and a real `$2` for
# `schedule_id` rather than an explicit `NULL`, so a scheduled run always records which
# schedule produced it. That foreign key is `ON DELETE SET NULL` (db/schema.prisma's
# `Run.schedule`), not `CASCADE`: deleting a schedule must not delete the runs it already
# produced, only sever the link — run history outlives the schedule that created it.
# `started_at` is left to the column default for the same reason `_INSERT_MANUAL` leaves it:
# the database is the only place that value is decided, and `RETURNING` is the only place it
# can be read back.
_INSERT_SCHEDULED: Final = """
    INSERT INTO runs (website_id, "trigger", status, schedule_id)
    VALUES ($1, 'scheduled', 'pending', $2)
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
# `MAX_JOBS = 2` (app/worker/policy.py) means two of them can be in flight on one machine at
# the same moment. Both workers may issue this statement; Postgres's row lock serializes
# them, and only the one whose UPDATE actually matches `status = 'pending'` gets a row back
# from `RETURNING`. Read-then-write cannot make that guarantee at any isolation level this
# application runs at — both readers would see `pending` and both would proceed to crawl the
# same run.
#
# `attempts = attempts + 1` AND `claimed_at = now()` RIDE ON THIS EXACT STATEMENT, and that
# is the whole reason the retry budget is trustworthy. Both are incremented by the same
# conditional UPDATE that wins the claim, inside the same row lock, so there is no window in
# which a run is `processing` with a stale attempt count or an unset claim time — and no way
# for a job that LOST the race to inflate either number, because its UPDATE matches zero
# rows. Doing it as a second statement would reintroduce exactly the read-then-write hazard
# the paragraph above rejects.
#
# `RETURNING attempts`, not `id`: the caller needs the number this claim just produced (is
# this the last attempt? what does the error message say?), and reading it back with a
# separate SELECT would be a second round trip returning a value that could already have
# moved.
#
# The enum is cast explicitly, matching `_LIST_BY_WEBSITE_WITH_STATUS` in
# `runs_reader.py` and `seed_run` in `tests/conftest.py`.
_CLAIM_PENDING: Final = """
    UPDATE runs
    SET status = 'processing'::run_status,
        attempts = attempts + 1,
        claimed_at = now()
    WHERE id = $1 AND status = 'pending'::run_status
    RETURNING attempts
"""

# THE ONLY STATEMENT IN THIS CODEBASE THAT MOVES A RUN BACKWARDS. Guarded on `processing`,
# like the two terminal writes below it, and for the same reason: it is the undo for a claim
# this worker made and then could not act on, so a row that has already reached a terminal
# state by some other path must be left exactly where it is.
#
# Two callers, one meaning — "this attempt did not happen, put the run back in the queue":
#
#   * `RunService.release_claim` — `crawl_task` was cancelled (a deploy's SIGTERM, or arq's
#     `job_timeout`) with retries still on the run's budget, or a transient failure is about
#     to be retried. Returning the row to `pending` is what lets the redelivered job claim it
#     again; leaving it `processing` would make the retry a no-op and strand the run until
#     the reaper noticed, minutes later.
#   * `RunService.reap_stuck_runs` — the reaper found a run whose worker never came back.
#
# `attempts` is deliberately NOT decremented. The attempt was really made: a worker really
# claimed the row, really opened sockets, and really consumed a slot. Rolling the counter
# back would let a run that is cancelled on every deploy retry forever, which is precisely
# the unbounded loop `MAX_ATTEMPTS` exists to prevent.
#
# `claimed_at = NULL` IS cleared, because it means "when the CURRENT processing attempt was
# claimed" and there is no longer one. That also keeps the reaper's staleness predicate
# honest: the next claim stamps a fresh time, so the run gets a full threshold from when
# work actually restarted rather than inheriting the abandoned attempt's clock.
#
# `error` and `completed_at` are untouched: this is not a failure, it is a re-queue.
_RETURN_PROCESSING_TO_PENDING: Final = """
    UPDATE runs
    SET status = 'pending'::run_status,
        claimed_at = NULL
    WHERE id = $1 AND status = 'processing'::run_status
    RETURNING id
"""

# The worker's success record — the third write path, and the only one that ever moves a run
# to `completed`. Guarded on `processing`, for the same reason `_MARK_PROCESSING_FAILED`
# below is: this only ever runs after `_CLAIM_PENDING` has moved the row out of `pending`, and
# a no-op here (something else already reached a terminal state first) is the safe failure
# mode rather than an overwrite.
#
# `stats` arrives pre-serialized to JSON text, not as a Python dict — see
# `mark_processing_completed`'s docstring for why marshalling happens in this writer rather
# than in `RunService`. `completed_at = now()`, not a value passed in, for the same reason
# `_MARK_PROCESSING_FAILED` decides it in the database rather than in Python: by the time this
# statement runs, the Storage upload it is recording the result of has already taken
# unpredictable, unmeasured wall-clock time, so there is no earlier Python `datetime.now(UTC)`
# that could describe "when this row actually finished" any better than the database's own
# clock at the moment of the UPDATE.
#
# `llms_full_txt` was added by PER-179 and shifted `storage_path` and `stats` to `$4` and
# `$5`. The column sits next to `llms_txt` because the two are one run's two artifacts, and
# asyncpg binds positionally with no names to check: a transposed pair here would write a
# storage path into a stats column and fail only at the jsonb cast, or worse, not fail at
# all. `tests/test_run_persistence.py` asserts on `storage_path` as well as the artifacts for
# exactly that reason.
_MARK_PROCESSING_COMPLETED: Final = """
    UPDATE runs
    SET status = 'completed'::run_status,
        llms_txt = $2,
        llms_full_txt = $3,
        storage_path = $4,
        stats = $5::jsonb,
        completed_at = now()
    WHERE id = $1 AND status = 'processing'::run_status
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
#
# `stats = COALESCE($3::jsonb, stats)`, not a bare `$3` — a caller with nothing new to record
# (`RunService.record_failure`'s `stats=None` default) passes SQL `NULL` for $3, and COALESCE
# is what makes that a no-op against whatever `stats` already holds instead of blanking a
# value nothing else wrote. `CrawlService.execute_run`'s partial-stats-on-failure path is
# exactly the caller that has something to pass instead of `NULL`: a seed or mid-crawl failure
# still knows `pages_crawled`, `cap_hit`, and the rest, and this is where that survives.
_MARK_PROCESSING_FAILED: Final = """
    UPDATE runs
    SET status = 'failed'::run_status,
        error = $2,
        stats = COALESCE($3::jsonb, stats),
        completed_at = now()
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

    async def insert_scheduled(self, website_id: UUID, schedule_id: UUID) -> dict[str, Any]:
        """Insert one cron-triggered, `pending` run for `website_id` and return it.

        See `_INSERT_SCHEDULED`'s comment for the two ways this differs from `insert_manual`
        above, and the module docstring for why no ownership check happens here either: the
        caller is the cron tick, acting on its own behalf, not an HTTP request with a caller
        to check.

        Args:
            website_id: The website this run belongs to.
            schedule_id: The schedule that produced it — always a real id, never `NULL` (that
                is what distinguishes this INSERT from `insert_manual`'s).

        Returns:
            A dict with `id`, `status` (always `"pending"`), and `started_at`, matching
            `insert_manual`'s return shape.

        Raises:
            asyncpg.ForeignKeyViolationError: if `website_id` or `schedule_id` names a row
                deleted since the tick locked it — not expected in practice (the tick holds
                its lock on the `schedules` row across this call), but not caught here either,
                for the same reason `insert_manual` does not catch its own.
            RuntimeError: if `RETURNING` somehow produced no row, exactly as `insert_manual`
                guards the same impossible case.
        """
        row = await self.fetch_one(_INSERT_SCHEDULED, website_id, schedule_id)
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

    async def claim_pending(self, run_id: UUID) -> int | None:
        """Atomically flip `run_id` from `pending` to `processing`, counting the attempt.

        The correctness guard the crawl worker takes before it fetches a single byte. See
        `_CLAIM_PENDING` for why this must be one conditional statement, and why `attempts`
        and `claimed_at` are written by it rather than beside it.

        Returns:
            The run's `attempts` value AFTER this claim — 1 on a first attempt, 2 on a
            retry, and so on — if this call won the race and the row is now `processing`
            under it. `None` if the row was not `pending` at all: already claimed by another
            worker, already terminal, or the id does not exist. The caller cannot tell those
            three apart from this return value alone, and does not need to: all three mean
            "do not crawl."
        """
        row = await self.fetch_one(_CLAIM_PENDING, run_id)
        return None if row is None else int(row["attempts"])

    async def return_processing_to_pending(self, run_id: UUID) -> bool:
        """Hand a claimed run back to the queue, but only if it is still `processing`.

        See `_RETURN_PROCESSING_TO_PENDING` for the two callers, for why `attempts` is not
        decremented, and for why `claimed_at` is.

        Returns:
            `True` if the row was `processing` and is now `pending`; `False` if it was not —
            something else reached a terminal state first, which is the case the guard exists
            to leave alone rather than overwrite. `RunService` logs the `False` case rather
            than raising on it.
        """
        row = await self.fetch_one(_RETURN_PROCESSING_TO_PENDING, run_id)
        return row is not None

    async def mark_processing_completed(
        self,
        run_id: UUID,
        *,
        llms_txt: str,
        llms_full_txt: str,
        storage_path: str,
        stats: dict[str, Any],
    ) -> bool:
        """Record a successful crawl for a run this worker had already claimed.

        Called by `RunService.record_success` only AFTER the Storage upload that produced
        `storage_path` has already succeeded (ARCHITECTURE.md §5.1) — this method has no way
        to enforce that ordering itself, it only records the result of it.

        Args:
            run_id: The run being completed. Must have been claimed by `claim_pending` first
                — this statement matches only a `processing` row.
            llms_txt: The generated index — `generate_llms_txt(pages)`'s return value,
                unmodified.
            llms_full_txt: The generated expansion — `generate_llms_full_txt(pages)`'s return
                value, unmodified (PER-179). `str`, not `str | None`, even though the column
                is nullable: the column is nullable because rows written before that migration
                and rows for runs that failed have no artifact, and this statement runs on
                neither path. A parameter that can only ever be a string should not be typed
                as if callers may omit it.
            stats: The shape `internals/run_stats.py`'s `build_run_stats` produces. Serialized
                with `json.dumps` HERE, in the writer, not by the caller: asyncpg has no jsonb
                codec registered in this project (see `RunService._parse_stats`, which
                `json.loads`s this same column on the way back out), so the parameter has to
                travel as JSON text rather than a Python dict. Marshalling a parameter for the
                database it is headed to is the writer's job; deciding what that parameter
                means is the caller's — the same division `mark_processing_failed` below
                follows for its own `stats` argument.

        Returns:
            `True` if this call won and the row is now `completed`; `False` if the row was no
            longer `processing` — something else (a redelivered job losing a race, a reaper)
            reached a terminal state first. `RunService.record_success` logs that case at
            WARNING rather than raising: the Storage object this run's payload was uploaded to
            is now orphaned, which is the accepted, benign half of the upload-then-write
            ordering trade (see that method's own docstring), not a corruption this writer
            needs to escalate.
        """
        row = await self.fetch_one(
            _MARK_PROCESSING_COMPLETED,
            run_id,
            llms_txt,
            llms_full_txt,
            storage_path,
            json.dumps(stats),
        )
        return row is not None

    async def mark_processing_failed(
        self, run_id: UUID, error: str, stats: dict[str, Any] | None = None
    ) -> str:
        """Record a terminal failure for a run this worker had already claimed.

        Args:
            run_id: The run being failed. Must have been claimed by `claim_pending` first —
                this statement matches only a `processing` row.
            error: A message already sanitized by the caller
                (`app.features.crawl.service.CrawlService._safe_error_message`). Never a raw
                exception string: `runs.error` is readable by every signed-in user
                (ARCHITECTURE.md §4.1), and an `httpx` exception's `str()` carries internal
                hostnames and resolved addresses.
            stats: Whatever partial numbers the crawl produced before it failed — built by
                `internals/run_stats.py`'s `build_run_stats` from a `CrawlResult` the failure
                path still has (a seed failure, or a Storage upload failure after the crawl
                itself succeeded), or `None` if nothing usable exists yet (an exception raised
                before `crawl_site` ever returned). Serialized to JSON text here, same as
                `mark_processing_completed` above and for the same reason. `None` travels as
                SQL `NULL`, which `_MARK_PROCESSING_FAILED`'s `COALESCE($3::jsonb, stats)`
                turns into "leave the column exactly as it was" rather than blanking it — see
                that statement's comment.

        Returns:
            asyncpg's status tag. `"UPDATE 0"` means the row was no longer `processing` —
            something else reached a terminal state first — which is precisely the case the
            guard exists to leave alone rather than overwrite.
        """
        encoded_stats = json.dumps(stats) if stats is not None else None
        return await self.execute(_MARK_PROCESSING_FAILED, run_id, error, encoded_stats)
