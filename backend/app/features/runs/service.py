"""Business logic for the runs feature: five unscoped reads, and writes reached from two
completely different directions.

**This feature has a writer, and transactions.** `internals/runs_writer.py` exists, and
three methods here open a `transaction()` (`app.infrastructure.db.transaction`,
ARCHITECTURE.md §5). An earlier revision of this docstring said "no writer, and no
transaction anywhere in this file," and predicted that whichever ticket added the first write
"adds `internals/runs_writer.py` and a `transaction()` call beside these methods; it does not
retrofit one into `list_runs` or `get_run`." That is exactly what happened, twice over:

* `trigger_run` — the HTTP write behind `POST /websites/{id}/runs`, with its abuse caps and
  duplicate-run guard in the private helpers beside it.
* `claim_for_processing`, `record_success`, and `record_failure` — the worker's trio, called
  from `app.worker.jobs.crawl_task` by way of `app.features.crawl.service.CrawlService`.
  `claim_for_processing` wraps `RunsWriter.claim_pending`, the atomic `pending -> processing`
  guard the crawl takes before it fetches a single byte. `record_success` wraps
  `RunsWriter.mark_processing_completed`, writing both generated artifacts for a crawl whose
  payload has already been uploaded to Storage. `record_failure` wraps
  `RunsWriter.mark_processing_failed`, for a crawl that never produces an artifact — whether
  because the seed never fetched, the crawl itself raised, or the Storage upload after a
  successful crawl failed. All three are independent
  transactions on purpose — the crawl, and the Storage upload `record_success` requires to
  have already happened, both happen BETWEEN them, and a network call must never run inside a
  transaction (ARCHITECTURE.md §5.1).

* `release_claim` and `reap_stuck_runs` — PER-166's recovery pair, and the reason this
  service now has a method nothing HTTP can reach at all. `release_claim` hands a claimed run
  back to `pending` when the worker holding it is cancelled or hits a transient failure with
  retries left; `reap_stuck_runs` is the cron-driven backstop for every run whose worker
  never got that far. They share one statement (`RunsWriter.return_processing_to_pending`) on
  purpose: a run rescued by its own worker and a run rescued five minutes later by the reaper
  must land in the same state, or the two paths will drift.

`list_runs`, `get_run`, `get_website_stats`, and — as of PER-181 — `get_llms_txt` and
`get_llms_full_txt` are unchanged by any of it, and open no transaction of their own; the
latter two share one private helper, `_get_artifact`, rather than being two independent
copies of the same two `404`s.

**Reads are still unscoped, the same rule `websites/service.py` follows.** None of the five
ever looks at who is asking — `CurrentUserId` is threaded through `app.api.routers.runs`
purely to require a token (ARCHITECTURE.md §4.1).

**`require_owner` appears exactly once, and its absence elsewhere is also deliberate.** It is
called inside `trigger_run`, because that is the one method here reached by an HTTP caller
who could fail to own the resource. It is *not* called by `claim_for_processing` or
`record_failure`, and adding it there would be meaningless rather than safer:
`require_owner` (ARCHITECTURE.md §4.2) compares a resource's owner to a caller, and a
background job acting on its own behalf has no caller to compare against. Ownership on the
worker's writes is a question that does not arise, not one this file forgot to ask.

**`RunService` depends on `WebsiteService`, never on `WebsitesReader`.** `list_runs` and
`trigger_run` both have to 404 on an unknown website before anything else about it means
something, and ARCHITECTURE.md §3.1 is explicit about how one feature gets data another
feature owns: "it calls B's service, never B's reader." Reimplementing "does this website
exist, and who owns it?" against `WebsitesReader` here would be a second copy of a check
`WebsiteService.get_website` already makes, free to drift from it the next time either one
changes.

**`duration_ms` is computed here, in Python, never in SQL.** `_shared_fields` below is the
one function both DTO builders call to turn a `started_at`/`completed_at` pair into a
milliseconds figure. Keeping the computation out of `internals/runs_reader.py` is what
guarantees it can never end up referenced by an `ORDER BY` there — a computed sort key
cannot use the `(website_id, started_at DESC)` index the keyset query depends on, at any
cost from "slower" to "a sequential scan", and that module's docstring spells out exactly
what the working alternative costs instead. There is exactly one function that computes
`duration_ms` in this whole codebase, which is what makes "compute it in Python" and "never
sort on it" impossible to accidentally pull apart.

**Importing `arq.connections.ArqRedis` here, never `app.worker`.** `trigger_run` takes an
already-open `ArqRedis` pool as a parameter, never a `WorkerSettings` and nothing from
`app.worker.jobs`. `arq` is a third-party package, and ARCHITECTURE.md §3.1's import-direction
rule constrains this project's own layers, not third-party libraries — the same point
`websites/service.py` makes about importing FastAPI. Importing `app.worker` itself would be
a different matter: ARCHITECTURE.md §3.3 is explicit that "nothing in `app/api/` or
`app/features/` imports `app/worker/` — the dependency runs the other way, so the queue
never enters a request path." The job this method enqueues is therefore referenced by NAME,
`CRAWL_TASK_JOB_NAME` below, not by importing a function — which also happens to be the only
option available today, since PER-159 is building `app.worker.jobs.crawl_task` concurrently
with this ticket and it does not exist yet. `tests/test_run_trigger_api.py`'s
`test_crawl_task_job_name_matches_the_worker_function` is what turns "this string must stay
character-identical to the worker's function name" from a comment into something CI checks,
automatically, the moment PER-159 lands.

## The insert/enqueue ordering — the sharpest §5.1 case in this codebase

`trigger_run` writes a `runs` row and enqueues an arq job that names it, and those two things
happen in exactly this order, with a COMMITTED transaction boundary between them:

```python
async with transaction(self._pool) as tx:
    row = await RunsWriter(tx).insert_manual(website_id)
# COMMITTED HERE.
await queue.enqueue_job(CRAWL_TASK_JOB_NAME, str(row["id"]))
```

Both orderings have a failure mode. This one is chosen because its failure mode is
recoverable and the other one's is not:

* **Enqueue-first** would let a worker see a `run_id` before the row behind it is committed.
  A worker that dequeues and starts processing in that window finds no row at all, and if
  the transaction then rolled back for an unrelated reason, the job on the queue would
  reference a run that never existed and never will — nothing can reconcile that after the
  fact, because there is no row to attach the correction to.
* **Insert-first** (what this does) can leave a `pending` row with no job behind it, if
  `enqueue_job` raises after the insert already committed. This method does not let that
  failure pass silently — see `except Exception` below — but even a version of this method
  that did nothing about it would still be the safer of the two orderings, because "a row
  exists with nothing acting on it" is a known, nameable state a reaper can sweep, while "a
  job exists that names a row that was never committed" has no state a database query could
  ever find.

**Never enqueue inside `transaction()`.** ARCHITECTURE.md §5.1 forbids holding a database
transaction open across a network call, and enqueuing onto Redis is one. Doing it inside the
block above would tie up a pooled connection for however long Redis takes to acknowledge,
for zero benefit: the enqueue's outcome has no bearing on whether the INSERT that already
happened should be kept.

**`except Exception` on the enqueue, not `except RedisError`.** arq's `enqueue_job` can
surface `redis.exceptions.RedisError`, `OSError`, `asyncio.TimeoutError`, or
`ConnectionError`, depending on exactly how the connection failed, and this method's
contract is "no run row is ever left `pending` with nothing behind it" — narrowing the
`except` to one exception type would leave every other type as a hole in that promise, and
there is no way to enumerate in advance every exception a Redis client can raise across a
bad network. The original exception is logged with `exc_info=True`; the message never
includes `REDIS_URL` or any part of it (ARCHITECTURE.md §9.4), and the caller sees a `503`.

## The races, documented honestly

The duplicate-run guard and both abuse caps below are check-then-act, not atomic with the
INSERT that follows them. Two simultaneous `POST`s for the same website, or from the same
user against the same cap, can both pass every check and both insert — there is no partial
unique index on `(website_id)` scoped to active runs that would make the 409 airtight, and
adding one is a migration this ticket does not own (CLAUDE.md #2: schema changes get their
own ticket, not a side effect of one that was not scoped to carry one). The acceptance
criterion this method is built against is the SEQUENTIAL double-click — the same request,
repeated once the first has landed — and every check below does catch that. A brief
overshoot of a cap under genuine concurrency (two tabs, two devices, a client retry racing
its own original request) is an accepted cost of enforcing this in application code rather
than in the database, and it self-corrects the moment the in-flight run in question finishes
or the 24h window moves.

## Two methods that take an already-open `Connection`, and why they are the only ones

`create_scheduled_run` and `website_ids_with_active_runs` are shaped differently from every
other method on this service: they take a `connection: Connection` keyword argument, run one
reader or writer call on it, and open no transaction of their own. That shape exists for
exactly one caller — `ScheduleService.run_due_schedules`, the cron tick — and for a reason
worth spelling out rather than discovering by reading two call sites and wondering why they
disagree with the rest of this file.

The cron tick has to lock `schedules` rows and insert the `runs` rows they produce in **one**
transaction (§5: one unit of work), and it has to do it without either feature reaching into
the other's `internals/` (§3.1). Those two rules can only both hold if the runs SQL stays
behind `RunService` while executing on the connection the *caller's* `transaction()` opened.
So these two methods take a `Connection` keyword argument, run one reader/writer call on it,
and open no transaction of their own — the same relationship a `Writer` already has to the
connection it is constructed with, lifted one layer up. They are deliberately the only methods
here shaped that way; every other method on this service owns its own unit of work. The
alternative — `ScheduleService` importing `RunsWriter` — is the import §3.1 exists to forbid,
and the other alternative — two transactions — would let a schedule advance without its run,
or a run exist whose schedule never advanced.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from arq.connections import ArqRedis
from asyncpg import Connection, ForeignKeyViolationError, Pool
from fastapi import HTTPException, status
from pydantic import ValidationError

from app.core.auth.ownership import require_owner
from app.core.pagination import Page
from app.core.settings import Settings
from app.features.runs.internals.artifact_filename import (
    ArtifactKind,
    artifact_filename,
    artifact_name,
)
from app.features.runs.internals.run_cursor import RunCursor, encode_cursor
from app.features.runs.internals.run_limits import (
    concurrency_limit_message,
    daily_limit_message,
)
from app.features.runs.internals.runs_reader import RunsReader
from app.features.runs.internals.runs_writer import RunsWriter
from app.features.runs.internals.stats_window import StatsWindow, resolve_window
from app.features.runs.schemas import (
    LatestRunSnapshot,
    RunAlreadyInFlightDetail,
    RunDetailResponse,
    RunIndexDiff,
    RunLimitExceededDetail,
    RunListItemResponse,
    RunStatsPoint,
    RunStatsTotals,
    RunStatusName,
    StatsWindowName,
    TriggerRunResponse,
    WebsiteStatsResponse,
)
from app.features.websites.service import WebsiteService
from app.infrastructure.db.transaction import transaction


logger = logging.getLogger(__name__)

_NOT_FOUND_DETAIL = "No run with that id"


def _missing_artifact_detail(kind: ArtifactKind) -> str:
    """The `404` detail for a run that exists but has not produced this artifact.

    **ONE message for `pending`, `processing`, and `failed`**, and the collapse is deliberate
    rather than lazy. Three reasons, in order:

    * The actionable fact is identical in all three — there is nothing to download — and a
      client that wants the run's state already has `GET /runs/{id}`, which returns it to
      every signed-in user anyway.
    * Any status-derived wording would be a lie waiting to happen: the run can move from
      `processing` to `completed` between the SELECT and the response.
    * Producing it would mean selecting `status` and branching on it in the service for zero
      client benefit, on a path whose entire job is to hand back one string.

    Distinct from `_NOT_FOUND_DETAIL` above by construction, and distinct BETWEEN the two
    artifacts, so a UI with two download buttons can say which one failed. Built from
    `artifact_name`, so it cannot name a different artifact than the filename would.
    """
    return f"This run has no {artifact_name(kind)} artifact"


# Deliberately character-identical to `websites/service.py`'s own "no such website" detail.
# `trigger_run` can produce a `404` about the WEBSITE by two different routes — the
# `get_website` fetch at the top, and the foreign-key violation if that website is deleted in
# the window between that fetch and the INSERT — and a client must not be able to tell the
# two apart. `ScheduleService` reuses one string across the same pair for the same reason.
_WEBSITE_NOT_FOUND_DETAIL = "No website with that id"

# The largest page `list_runs` will ever return, no matter what a caller asks for.
# `GET /websites/{id}/runs?limit=` is `Query(ge=1)` at the router — deliberately no `le=` —
# so a request above this is CLAMPED here rather than rejected with a 422: the contract is
# "you get at most this many", not "you asked for too many and that is an error."
MAX_LIMIT: Final = 100

# The arq job `POST /websites/{id}/runs` enqueues, named by STRING rather than imported —
# see the module docstring's "Importing `arq.connections.ArqRedis`" section for the full
# argument. arq registers a function under its `__qualname__`, so this has to stay
# character-identical to the worker's `async def crawl_task`; `tests/test_run_trigger_api.
# py`'s `test_crawl_task_job_name_matches_the_worker_function` asserts exactly that the
# moment PER-159 lands the function it names.
CRAWL_TASK_JOB_NAME: Final = "crawl_task"

# The `detail` of the `503` `trigger_run` returns when enqueuing fails, whether because
# Redis itself rejected the job or because `require_queue_pool` (`app.api.routers.runs`)
# never got a pool to hand in. Deliberately generic: neither cause is the caller's fault or
# something they can act on beyond "try again shortly", and neither message may say
# anything about Redis, which ARCHITECTURE.md §9.4 reserves for logs only.
_ENQUEUE_FAILED_DETAIL = "Could not start this run right now. Please try again shortly."

# Stored in `runs.error` on the same path, for an operator reading the row rather than the
# HTTP response — distinct wording is not required, but keeping it separate from the detail
# above means a future reword of one is not forced to consider the other.
_ENQUEUE_FAILED_ERROR = "Failed to enqueue the crawl job for this run."

# The most stranded runs one reaper pass may lock and act on, mirroring `DUE_BATCH_LIMIT`
# (`schedules/service.py`) and bounding the same thing: how long one pass can hold a
# transaction open. 100 rather than the tick's 50 because the reaper's per-row work is a
# single guarded UPDATE rather than an INSERT plus two batched advances, and because a
# backlog of stranded runs is by definition already a bad day — clearing it in fewer passes
# is worth the slightly longer transaction. If a pass ever fills this batch, the remainder
# waits five minutes; they have already been stranded for at least fifteen.
REAP_BATCH_LIMIT: Final = 100


def crawl_job_id(run_id: UUID, attempts: int) -> str:
    """The deterministic arq job id the reaper enqueues `crawl_task` under.

    Public so that a test can assert the id the reaper actually used rather than
    reconstructing the format string by hand — the same reason `CRAWL_TASK_JOB_NAME` is a
    named constant. See `RunService._enqueue_reaped` for why only the reaper passes a job id,
    and why `attempts` is part of it.
    """
    return f"crawl:{run_id}:{attempts}"


def _interrupted_error(attempts: int) -> str:
    """What `runs.error` says about a run the reaper gave up on.

    The attempt count is in the sentence for the reason every terminal message in this system
    carries one: it is what lets a reader — and the UI — tell "this site was briefly
    unreachable once" apart from "this has failed every time we have tried", which are the
    same status and very different problems.
    """
    return f"Run was interrupted and did not complete after {attempts} attempt(s)."


@dataclass(frozen=True)
class ReaperSummary:
    """What one call to `RunService.reap_stuck_runs` did, for its log line and for tests.

    Lives here rather than in `schemas.py` for the reason `ScheduleService`'s `TickSummary`
    does: it is not a response shape any HTTP endpoint returns, it is internal bookkeeping
    for the worker's own log and for `tests/test_run_reaper.py` to assert against.
    """

    # How many rows `RunsReader.lock_reapable` locked — the batch this pass had to make a
    # decision about, across both classes of stranded run.
    examined: int

    # `processing` runs with attempts left, handed back to `pending`.
    requeued: int

    # `pending` runs old enough to be presumed to have no job behind them. No write; they
    # were already in the right state.
    orphans_swept: int

    # `processing` runs that had exhausted `max_attempts` and were marked `failed`.
    failed: int

    # How many of `requeued + orphans_swept` actually put a new job on the queue.
    enqueued: int

    # How many did not, because arq already had a job under the same deterministic id — the
    # orphan sweep's anti-amplification guard doing its job, not an error. See
    # `_enqueue_reaped`.
    already_queued: int

    # How many enqueue attempts raised. Each one leaves its run `pending` for the next pass,
    # deliberately un-failed.
    enqueue_failures: int

    # `True` when `examined == REAP_BATCH_LIMIT`: this pass's batch was full, so there MAY be
    # more stranded runs it never looked at.
    limit_reached: bool


@dataclass(frozen=True)
class RunArtifact:
    """One generated artifact, and the filename a browser should save it under.

    Lives here rather than in `schemas.py` for the reason `ReaperSummary` above does: it is
    NOT a response body. `app.api.routers.runs` builds a `PlainTextResponse` out of these two
    fields — the `content` becomes the body verbatim and the `filename` becomes a
    `Content-Disposition` header — so nothing here is ever serialized as JSON and there is no
    OpenAPI schema to derive from it. `schemas.py`'s module docstring reserves that file for
    request/response DTOs; a pydantic model here would render as a component in
    `openapi.json` describing a shape no response ever has.
    """

    content: str
    """The artifact itself, exactly as `record_success` stored it. Never `None` — a run whose
    column is `NULL` produces a `404` in `_get_artifact` instead of an instance of this."""

    filename: str
    """`llms-example-com.txt` / `llms-full-example-com.txt`, from `artifact_filename`. Already
    sanitized to `[a-z0-9-]` plus one `.txt`, which is what lets the router interpolate it
    into a quoted `Content-Disposition` with no escaping and no header-injection surface."""


@dataclass(frozen=True)
class PreviousCompletedIndex:
    """The previous completed run's index, as `RunService.get_previous_completed_index`
    hands it to `CrawlService._build_index_diff` (PER-193).

    Lives here rather than in `schemas.py`, for the same reason `RunArtifact` above does: it
    is not a response body — this is a worker-to-worker handoff between two services' own
    business logic, never serialized as JSON, and has no reason to appear in `openapi.json`.
    """

    run_id: UUID
    """The previous completed run's own id — becomes `index_diff["compared_to_run_id"]`
    (`internals/index_diff.py`)."""

    completed_at: datetime | None
    """When that run completed. Typed optional to match the column it is read from
    (`runs.completed_at` is nullable in general), though a `completed` row always has one in
    practice — `RunsWriter.mark_processing_completed` sets it in the same `UPDATE` that sets
    `status = 'completed'`."""

    llms_txt: str
    """That run's stored `llms.txt` index — what `internals/index_diff.py`'s `parse_index`
    reads to reconstruct the previous run's page list. Never `None`: `get_previous_completed_
    index` returns `None` for the whole lookup, not a `PreviousCompletedIndex` with a `None`
    `llms_txt`, when the nearest completed row has no index (see that method's docstring)."""

    urls_discovered: int | None
    """That run's `runs.stats["urls_discovered"]`, read defensively — `None` when the value
    is absent (a row that predates `RUN_STATS_VERSION` 4) or is not an `int`. Feeds
    `index_diff["urls_discovered_delta"]`, which is `None` under the same condition."""


def _duration_ms(started_at: datetime, completed_at: datetime | None) -> int | None:
    """`None` while a run is still in flight; otherwise its elapsed time in whole ms.

    See `schemas.RunListItemResponse.duration_ms` and the module docstring for why this
    computation lives here and nowhere else.
    """
    if completed_at is None:
        return None
    # Integer floor division by a 1ms timedelta, not `int(total_seconds() * 1000)`. The two
    # agree for every duration a crawl will plausibly have, but `total_seconds()` returns a
    # float, and float multiplication is exact right up until the day it is not. Dividing
    # two timedeltas stays in integer microseconds the whole way.
    return (completed_at - started_at) // timedelta(milliseconds=1)


def _parse_stats(raw: Any) -> dict[str, Any] | None:
    """Decode `runs.stats`, defensively.

    asyncpg decodes a `jsonb` column as a plain `str` rather than a `dict` — there is no
    codec registered that would do otherwise — so every non-`None` value handed to this
    function starts life as JSON text that needs a `json.loads`. `stats` also has no shape
    Postgres enforces (ARCHITECTURE.md §3.4: the crawler milestone is not designed yet), so
    a stored value can be malformed JSON, or valid JSON that decodes to something other
    than an object (an array, a bare number, `null`). Either case yields `None` here —
    "no usable stats" — rather than letting one bad row 500 an otherwise-healthy run's
    history.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _shared_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Every field `RunListItemResponse` has, built from one reader row.

    The single row -> DTO builder both `_to_list_item` and `_to_detail` call, so
    `duration_ms` and `stats` are computed in exactly one place regardless of which
    endpoint is asking.
    """
    return {
        "id": row["id"],
        "website_id": row["website_id"],
        "status": row["status"],
        "trigger": row["trigger"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "duration_ms": _duration_ms(row["started_at"], row["completed_at"]),
        "stats": _parse_stats(row["stats"]),
        "error": row["error"],
    }


def _to_list_item(row: dict[str, Any]) -> RunListItemResponse:
    """Build one `GET /websites/{id}/runs` row. Excludes `llms_txt` / `storage_path` because
    `_LIST_COLUMNS` (`internals/runs_reader.py`) never selected them in the first place."""
    return RunListItemResponse(**_shared_fields(row))


def _to_detail(row: dict[str, Any]) -> RunDetailResponse:
    """Build the full `GET /runs/{id}` body: every list-item field, plus the two `_DETAIL_
    COLUMNS` (`internals/runs_reader.py`) adds on top of `_LIST_COLUMNS`."""
    return RunDetailResponse(
        **_shared_fields(row), llms_txt=row["llms_txt"], storage_path=row["storage_path"]
    )


def _already_in_flight(row: dict[str, Any]) -> HTTPException:
    """Build the `409` for a website that already has a run in progress.

    Returned rather than raised so the `raise` stays visible at the call site — the same
    reason `websites/service.py`'s `_already_exists` is written that way.
    """
    detail = RunAlreadyInFlightDetail(
        message="This website already has a run in progress",
        run_id=row["id"],
        status=row["status"],
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        # `mode="json"` so the UUID is serialized to a string here rather than handed to the
        # JSON encoder as-is — a bare `UUID` in a `detail` dict is not JSON-serializable and
        # would turn this `409` into a `500` at render time (`websites/service.py` documents
        # the same trap for its own `409`).
        detail=detail.model_dump(mode="json"),
    )


def _concurrency_limit_exceeded(limit: int) -> HTTPException:
    """Build the `429` for the per-user concurrency cap. No `resets_at` — see
    `RunLimitExceededDetail.resets_at`'s docstring for why inventing one here would be worse
    than omitting it."""
    detail = RunLimitExceededDetail(
        scope="concurrent",
        message=concurrency_limit_message(limit),
        limit=limit,
        resets_at=None,
    )
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail.model_dump(mode="json")
    )


def _daily_limit_exceeded(limit: int, resets_at: datetime, now: datetime) -> HTTPException:
    """Build the `429` for the rolling-24h daily cap, with the reset time it does have."""
    detail = RunLimitExceededDetail(
        scope="daily",
        message=daily_limit_message(limit, resets_at, now),
        limit=limit,
        resets_at=resets_at,
    )
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        # `mode="json"` again: `resets_at` is a `datetime` here, and the same
        # not-JSON-serializable trap applies to it as to the UUID above.
        detail=detail.model_dump(mode="json"),
    )


def _as_int(value: Any) -> int | None:
    """`value` if it is genuinely an `int`, else `None` — the one place this module reads a
    number back out of a `runs.stats` jsonb value defensively. `bool` is deliberately
    excluded even though `isinstance(True, int)` is `True` in Python: `runs.stats` never
    stores a boolean where these fields expect a count, but guarding against it here costs
    nothing and matches the same defensiveness `_parse_stats` already applies to the dict
    itself."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _to_stats(
    window: StatsWindow, rows: list[dict[str, Any]], latest_row: dict[str, Any] | None
) -> WebsiteStatsResponse:
    """Build the full `GET /websites/{id}/stats` body from `_WEBSITE_STATS`'s rows and
    `_LATEST_COMPLETED_INDEX`'s single row.

    `rows` is never empty — `RunsReader.website_stats`'s `generate_series` always yields
    `window.bucket_count` of them — so `totals` is read off `rows[0]` rather than recomputed
    in Python from the series. The `assert` below documents that invariant instead of silently
    indexing into a list that could, in principle, be empty.

    All PRESENTATION rounding happens here, in exactly one place, never in SQL: `avg(...)`
    already zero-filled every empty bucket and the whole-window totals, so this only rounds
    values SQL already computed correctly.
    """
    assert rows, "generate_series always yields window.bucket_count rows"

    series = [
        RunStatsPoint(
            t=row["bucket_start"],
            runs=row["runs"],
            completed=row["completed"],
            failed=row["failed"],
            avg_pages=round(row["avg_pages"], 2),
            avg_duration_ms=round(row["avg_duration_ms"]),
            runs_compared=row["runs_compared"],
            pages_added=row["pages_added"],
            pages_removed=row["pages_removed"],
            # Never `round(...)`ed or otherwise touched — `index_pages`/`index_bytes` are
            # already `None` or a plain `int` off the row, and rounding a possibly-`None`
            # value is exactly the mistake the null-vs-zero discipline exists to prevent.
            index_pages=row["index_pages"],
            index_bytes=row["index_bytes"],
        )
        for row in rows
    ]

    total_runs = rows[0]["total_runs"]
    total_completed = rows[0]["total_completed"]
    totals = RunStatsTotals(
        total_runs=total_runs,
        completed=total_completed,
        failed=rows[0]["total_failed"],
        # `None`, never `0.0`, when there are no runs to compute a rate over — see
        # `RunStatsTotals.success_rate`.
        success_rate=round(total_completed / total_runs, 4) if total_runs > 0 else None,
        avg_duration_ms=round(rows[0]["total_avg_duration_ms"]),
        avg_pages=round(rows[0]["total_avg_pages"], 2),
        last_run_at=rows[0]["last_run_at"],
        runs_compared=rows[0]["total_runs_compared"],
        pages_added=rows[0]["total_pages_added"],
        pages_removed=rows[0]["total_pages_removed"],
    )

    return WebsiteStatsResponse(
        window=window.name,
        bucket=window.bucket,
        series=series,
        totals=totals,
        latest=_to_latest(latest_row),
    )


def _to_latest(row: dict[str, Any] | None) -> LatestRunSnapshot | None:
    """Build `WebsiteStatsResponse.latest` from `RunsReader.latest_completed_index`'s row, or
    `None` when the window holds no completed run at all (`row is None`).

    **Defensive, mirroring `_parse_stats`.** `runs.stats` has no shape Postgres enforces
    (ARCHITECTURE.md §3.4) — a stored `index_diff` can be malformed JSON that decoded to
    something other than an object, an object with the wrong keys, or simply absent on a row
    that predates `RUN_STATS_VERSION` 7. Every one of those degrades to `diff_state=
    "not_recorded"` here rather than raising, so a single bad row can never 500 this endpoint
    for every OTHER website.
    """
    if row is None:
        return None

    stats = _parse_stats(row["stats"]) or {}
    index_pages = _as_int(stats.get("links_emitted"))
    index_bytes = _as_int(stats.get("llms_txt_bytes"))
    raw_diff = stats.get("index_diff")

    diff_state: Literal["compared", "first_run", "not_recorded"]
    previous_run_completed: bool | None = None
    diff: RunIndexDiff | None = None

    if not isinstance(raw_diff, dict):
        # Covers both a version-5-and-earlier row (no `index_diff` key at all, so `.get`
        # returns `None`) and a stored value that is JSON but not a JSON OBJECT.
        diff_state = "not_recorded"
    else:
        raw_previous_run_completed = raw_diff.get("previous_run_completed")
        if isinstance(raw_previous_run_completed, bool):
            previous_run_completed = raw_previous_run_completed

        state = raw_diff.get("state")
        if state == "first_run":
            diff_state = "first_run"
        elif state == "compared":
            try:
                diff = RunIndexDiff.model_validate(raw_diff)
            except ValidationError:
                logger.warning(
                    "runs: malformed index_diff on run %s; reporting diff_state=not_recorded",
                    row["id"],
                    exc_info=True,
                )
                diff_state = "not_recorded"
            else:
                diff_state = "compared"
        else:
            diff_state = "not_recorded"

    return LatestRunSnapshot(
        run_id=row["id"],
        completed_at=row["completed_at"],
        index_pages=index_pages,
        index_bytes=index_bytes,
        diff_state=diff_state,
        previous_run_completed=previous_run_completed,
        diff=diff,
    )


class RunService:
    """Business logic for runs. Constructed per request from the shared pool."""

    def __init__(self, pool: Pool, website_service: WebsiteService, settings: Settings) -> None:
        self._pool = pool
        # Bound to the pool, like `WebsitesReader` in `websites/service.py` — each read
        # borrows a connection and returns it immediately. `RunsWriter` is NOT built here —
        # it is constructed inside `trigger_run`'s `transaction()` block, from the
        # connection that block yields, so its one statement joins that unit of work.
        self._reader = RunsReader(pool)
        # Injected rather than constructed here so the router's provider function
        # (`get_run_service`, `app.api.routers.runs`) controls both services' lifetimes
        # the same way, and so a test can hand this a `WebsiteService` built against a
        # different pool or with dependencies overridden.
        self._website_service = website_service
        # Injected, NOT read from the `app.core.settings.settings` module singleton, even
        # though `app.main` and `app.worker.settings` both do read that singleton directly.
        # The difference is that this is a per-request service whose behaviour the two caps
        # define: `app.api.deps.get_settings` exists precisely "so that a test can substitute
        # a different configuration", and reading the singleton here would put the one
        # requirement this ticket states as configurable — "both caps configurable via
        # settings" — beyond the reach of any test that did not also mutate global state for
        # every other test in the process.
        self._settings = settings

    async def list_runs(
        self,
        website_id: UUID,
        *,
        cursor: RunCursor | None,
        status: RunStatusName | None,
        limit: int,
    ) -> Page[RunListItemResponse]:
        """Return one page of `website_id`'s run history, newest first.

        `cursor` arrives already decoded — the router's `parse_cursor` dependency
        (`app.api.routers.runs`) turns a bad `?cursor=` into a `422` before this method
        ever runs. That is what keeps this method directly unit-testable without
        constructing base64 in a test: a `RunCursor` is built by hand instead.

        Raises:
            HTTPException: `404` if there is no website with that id — delegated to
                `WebsiteService.get_website`, whose return value is unused here. This
                method needs only the fact that the row exists, not any of its fields
                (ARCHITECTURE.md §3.1: a feature calls another feature's service, never
                its reader).
        """
        await self._website_service.get_website(website_id)

        capped_limit = min(limit, MAX_LIMIT)
        # Ask for one row more than a page holds. Getting exactly `capped_limit + 1` rows
        # back is how the presence of a next page is detected without a second query — a
        # `COUNT(*)` would cost as much as the page query itself and go stale the instant
        # a new run is inserted (`core/pagination.py`'s module docstring).
        rows = await self._reader.list_by_website(
            website_id, cursor=cursor, status=status, limit=capped_limit + 1
        )

        has_next_page = len(rows) > capped_limit
        page_rows = rows[:capped_limit]

        next_cursor = None
        if has_next_page:
            # Built from the last row THIS PAGE KEEPS, never from the probe row dropped
            # above. The client never sees that row, so a cursor built from it would point
            # one position too far forward and silently skip it (and any tie-mates sharing
            # its `started_at`) on every later page.
            last = page_rows[-1]
            next_cursor = encode_cursor(last["started_at"], last["id"])

        return Page[RunListItemResponse](
            items=[_to_list_item(row) for row in page_rows], next_cursor=next_cursor
        )

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        """Return one run in full, for any signed-in caller.

        Raises:
            HTTPException: `404` if there is no run with that id.
        """
        row = await self._reader.get_by_id(run_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
        return _to_detail(row)

    async def get_llms_txt(self, run_id: UUID) -> RunArtifact:
        """Return one run's `llms.txt` index, for any signed-in caller.

        Takes no `user_id` and never asks who is calling (ARCHITECTURE.md §4.1) — same
        contract as `get_run` above.

        Raises:
            HTTPException: `404` with `_NOT_FOUND_DETAIL` if there is no run with that id;
                `404` with a DIFFERENT detail if the run exists but has produced no index —
                see `_get_artifact`.
        """
        return await self._get_artifact(run_id, "index")

    async def get_llms_full_txt(self, run_id: UUID) -> RunArtifact:
        """Return one run's `llms-full.txt` expansion, for any signed-in caller. Identical
        contract to `get_llms_txt` above, including both `404`s."""
        return await self._get_artifact(run_id, "full")

    async def _get_artifact(self, run_id: UUID, kind: ArtifactKind) -> RunArtifact:
        """The one implementation behind both public methods above.

        Two `404`s, deliberately distinguishable by `detail` alone (the status codes are
        identical because both mean "there is nothing at this URL"):

        * The run does not exist — `_NOT_FOUND_DETAIL`, character-identical to `get_run`'s,
          so a client cannot learn from these routes whether an id it guessed is real.
        * The run exists and its column is `NULL` — `_missing_artifact_detail(kind)`. That is
          ONE message for `pending`, `processing`, and `failed`, on purpose: see that
          function.
        """
        row = await self._reader.get_artifact(run_id, kind=kind)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

        content: str | None = row["content"]
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_missing_artifact_detail(kind)
            )

        return RunArtifact(content=content, filename=artifact_filename(row["origin"], kind))

    async def trigger_run(
        self, website_id: UUID, user_id: UUID, queue: ArqRedis
    ) -> TriggerRunResponse:
        """Start a manual crawl of `website_id`, owned by `user_id`.

        The canonical write path for this codebase (ARCHITECTURE.md §4.2): fetch the
        website, `require_owner` on it with nothing in between, and only then do anything
        else. See the module docstring for the insert/enqueue ordering this method commits
        to, and for the check-then-act races every step below is subject to.

        `now` is captured once, here, with `datetime.now(UTC)` — before either cap query
        runs and before the transaction opens — for the same reason
        `ScheduleService.upsert_schedule` captures its own clock in the service rather than
        letting Postgres supply one: the 24h window bound, the reset-time arithmetic in
        `_enforce_run_caps`, and the `completed_at` `abandon_unqueued_run` would write on
        the failure path all have to agree with each other, and a pure function (or, here,
        several separate queries and a possible later write) cannot be handed `now()` without
        first evaluating it in application code.

        Raises:
            HTTPException: `404` if there is no website with that id. `403` (from
                `require_owner`) if the caller does not own it. `409`, carrying the existing
                run's id and status, if that website already has a `pending` or
                `processing` run — of ANY trigger, see `internals/runs_reader.py`'s
                asymmetry section. `429`, naming the limit and — for the daily cap only — a
                reset time, if the caller is over either abuse cap
                (`app.core.settings.Settings.max_concurrent_runs_per_user` /
                `.max_runs_per_day_per_user`). `503` if the job could not be enqueued after
                the run row was already committed; that row is marked `failed` before this
                raises, so it is never left `pending` with nothing behind it (barring the
                second failure `abandon_unqueued_run`'s own docstring covers).
        """
        website = await self._website_service.get_website(website_id)  # 404
        require_owner(website, user_id)  # 403 — nothing between these two lines

        active = await self._reader.get_active_for_website(website_id)
        if active is not None:
            raise _already_in_flight(active)  # 409

        now = datetime.now(UTC)
        await self._enforce_run_caps(user_id, now)  # 429

        try:
            async with transaction(self._pool) as tx:  # short, no network call inside it
                row = await RunsWriter(tx).insert_manual(website_id)
        except ForeignKeyViolationError as error:
            # The website existed at the `get_website` fetch above and was deleted before
            # this INSERT ran — `runs_website_id_fkey` is what catches it. The same race
            # `ScheduleService.upsert_schedule` converts for its own foreign key, converted
            # the same way and to the same `404` the sequential case produces, so a client
            # cannot tell which of the two paths answered it.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_WEBSITE_NOT_FOUND_DETAIL
            ) from error
        # COMMITTED HERE. Enqueuing below is deliberately outside this block — see the
        # module docstring's "insert/enqueue ordering" section for why that order, and not
        # the reverse, is the one whose failure mode is recoverable.

        run_id: UUID = row["id"]
        try:
            await queue.enqueue_job(CRAWL_TASK_JOB_NAME, str(run_id))
        except Exception:
            # Broad on purpose — see the module docstring's "`except Exception`" section.
            # `exc_info=True` logs the real cause for an operator; `REDIS_URL` is never
            # interpolated into this message or any other (ARCHITECTURE.md §9.4).
            logger.error(
                "Failed to enqueue %s for run %s", CRAWL_TASK_JOB_NAME, run_id, exc_info=True
            )
            await self.abandon_unqueued_run(run_id, now)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_ENQUEUE_FAILED_DETAIL
            ) from None

        logger.info("Triggered manual run %s for website %s", run_id, website_id)
        return TriggerRunResponse(**row)

    async def _enforce_run_caps(self, user_id: UUID, now: datetime) -> None:
        """Raise `429` if `user_id` is over either abuse cap; otherwise return `None`.

        Concurrency checked first, deliberately: it is the cheaper of the two queries (a
        count against the partial `runs_status_active_idx`, versus a count-and-min over a
        24h window), and it is the cap a sequential double-click actually hits, so failing
        on it skips the more expensive query entirely on the case this method exists for.

        The daily reset time is a FLOOR, not a promise: `resets_at` is
        `oldest_started_at + 24h`, the earliest instant a slot can free. If the caller is
        over the cap by more than one — possible under the check-then-act races the module
        docstring documents — the actual next slot may free later than this states, because
        a second run older than the one this computed from could still be inside the
        window. Restating that precisely in the 429 body would require counting exactly how
        far over the cap the caller is, for a number the client cannot act on differently
        either way; the floor is the honest, useful thing to say.
        """
        concurrency_limit = self._settings.max_concurrent_runs_per_user
        concurrent = await self._reader.count_active_manual_for_user(user_id)
        if concurrent >= concurrency_limit:
            raise _concurrency_limit_exceeded(concurrency_limit)

        daily_limit = self._settings.max_runs_per_day_per_user
        since = now - timedelta(hours=24)
        window = await self._reader.count_and_oldest_since_for_user(user_id, since)
        if window["total"] >= daily_limit:
            # `oldest_started_at` cannot be NULL here. Reaching this line means
            # `total >= daily_limit`, and `daily_limit` is `Field(ge=1)` in
            # `app.core.settings` — so at least one row is inside the window, and `min()`
            # over a non-empty set is not NULL. That `ge=1` is load-bearing rather than
            # decorative: a cap of 0 would make this branch reachable with no rows at all,
            # and `None + timedelta` is a `TypeError`, i.e. a 500 from the code path whose
            # entire job is to answer 429 politely. The constraint turns that
            # misconfiguration into a refusal to boot instead.
            resets_at = window["oldest_started_at"] + timedelta(hours=24)
            raise _daily_limit_exceeded(daily_limit, resets_at, now)

    async def abandon_unqueued_run(self, run_id: UUID, now: datetime) -> None:
        """Mark `run_id` `failed` after its enqueue failed, so it is not left `pending`
        with nothing acting on it.

        Public — not `_abandon_unqueued_run` — because it is called from two places for the
        same reason: `trigger_run`'s `503` path below, and `ScheduleService.run_due_schedules`'
        `_enqueue`, after the cron tick's own insert-then-enqueue ordering hits the same
        failure mode `trigger_run`'s module docstring argues at length. Both callers need the
        identical guarantee — a run is never left `pending` with nothing behind it — so this
        is one method rather than two copies that could drift.

        `now` is the SAME instant the caller captured before opening its insert transaction,
        passed through rather than re-read, so `started_at` and `completed_at` on the same row
        cannot disagree about what "now" meant by a few milliseconds.

        This is itself wrapped in `try`/`except` because it is the last line of defense:
        if THIS write also fails — the database becoming unreachable at the worst possible
        moment — the run is exactly the orphan a stuck-run reaper (ARCHITECTURE.md §6.4)
        exists to sweep, and the honest response to the caller is still whatever failure
        `trigger_run` or the tick raises/logs immediately after this returns, not a `500`
        that implies a bug in this service rather than an infrastructure failure it could not
        route around.
        """
        try:
            async with transaction(self._pool) as tx:
                await RunsWriter(tx).mark_failed(
                    run_id, error=_ENQUEUE_FAILED_ERROR, completed_at=now
                )
        except Exception:
            logger.error(
                "Failed to mark run %s failed after its enqueue also failed; it may be "
                "left pending until the stuck-run reaper sweeps it",
                run_id,
                exc_info=True,
            )

    async def get_website_stats(
        self, website_id: UUID, *, window: StatsWindowName
    ) -> WebsiteStatsResponse:
        """Return `website_id`'s run statistics over `window`, pre-aggregated into fixed,
        zero-filled buckets plus a whole-window summary and the newest completed run's index
        diff — three reads, no per-bucket queries.

        Reuses `WebsiteService.get_website` purely for its `404`, exactly like `list_runs`
        above — its return value is unused here (ARCHITECTURE.md §3.1: a feature calls
        another feature's service, never its reader). Never scoped by caller: reads are
        unscoped in this codebase (ARCHITECTURE.md §4.1), and there is no `require_owner`
        call anywhere in this path.

        **Three reads, not two — the one deliberate widening PER-193 makes here.**
        `RunsReader.website_stats`'s own claim ("one aggregate query, no per-bucket queries")
        is about `_WEBSITE_STATS` specifically and still holds unchanged; `latest_completed_
        index` is a second, `LIMIT 1` lookup added purposefully so the whole `stats` jsonb of
        the newest completed run — samples included — never has to ride every row of the
        168-bucket aggregate to answer one question only the single newest row can answer.
        See `_LATEST_COMPLETED_INDEX`'s own comment.

        Raises:
            HTTPException: `404` if there is no website with that id.
        """
        await self._website_service.get_website(website_id)

        resolved = resolve_window(window, datetime.now(UTC))
        rows = await self._reader.website_stats(website_id, window=resolved)
        latest_row = await self._reader.latest_completed_index(
            website_id, start=resolved.start, end=resolved.end
        )
        return _to_stats(resolved, rows, latest_row)

    async def claim_for_processing(self, run_id: UUID) -> int | None:
        """Atomically flip `run_id` from `pending` to `processing`, or refuse to.

        The correctness guard `app.features.crawl.service.CrawlService.execute_run` relies
        on before it fetches a single byte: `arq`'s `MAX_JOBS = 2` (`app/worker/policy.py`)
        makes concurrent delivery of the same job a real scenario, and only a single, atomic
        conditional `UPDATE` — never a `SELECT` followed by an `UPDATE` — can guarantee that
        at most one caller ever comes away with the claim for a given `run_id`.

        Returns:
            The run's `attempts` value AFTER this claim (1 on a first attempt, 2 on a retry)
            if this call won the race and the row is now `processing`; `None` if it was not
            `pending` — already claimed, already terminal, or nonexistent. The caller treats
            every `None` the same way: do not crawl. The number matters because the retry
            policy is counted against `runs.attempts`, not against arq's per-job-id
            `ctx["job_try"]` — see `app/worker/policy.py`'s `MAX_ATTEMPTS` for why the run's
            own counter is the only one a reaper in another process can read.
        """
        async with transaction(self._pool) as tx:
            return await RunsWriter(tx).claim_pending(run_id)

    async def release_claim(self, run_id: UUID) -> bool:
        """Hand a claimed run back to the queue: `processing` -> `pending`, attempts intact.

        Called by `app.features.crawl.service.CrawlService` on the two paths where an attempt
        ends without producing an outcome AND without being terminal: a transient failure
        with budget left to retry, and a cancellation (a deploy's SIGTERM, or arq's
        `job_timeout`) on a run that has not exhausted its attempts.

        **This is the same statement the reaper uses**, deliberately — see
        `internals/runs_writer.py`'s `_RETURN_PROCESSING_TO_PENDING`. A run rescued by the
        worker that was crawling it and a run rescued five minutes later by the reaper end up
        in exactly the same state, because it is exactly the same write; the only difference
        is how long the user waited.

        Returns:
            `True` if the row was `processing` and is now `pending`. `False` if it was not —
            logged at WARNING rather than raised, because by the time this is called the
            attempt is already over and there is nothing this method could usefully do about
            a row something else already moved.
        """
        async with transaction(self._pool) as tx:
            released = await RunsWriter(tx).return_processing_to_pending(run_id)
        if not released:
            logger.warning(
                "Run %s was no longer processing when its claim was released; leaving it "
                "alone — something else reached a terminal state first",
                run_id,
            )
        return released

    async def reap_stuck_runs(
        self,
        queue: ArqRedis,
        *,
        stuck_before: datetime,
        orphaned_before: datetime,
        max_attempts: int,
    ) -> ReaperSummary:
        """The stuck-run reaper. Rescue every run nothing is acting on any more.

        Called every `REAPER_INTERVAL_MINUTES` by `app.worker.jobs.reaper_tick`, registered
        as the second arq cron job in `app/worker/settings.py`. Not reachable from any HTTP
        route.

        **The deadlock this exists to break.** A run stuck in `processing` does not merely
        look broken in the UI. `RunService.trigger_run`'s duplicate-run guard refuses to
        start a new run for a website that has one in flight, so a single stranded row makes
        that website permanently un-crawlable — every trigger answers `409` against a run
        that will never finish. Nothing else in this system can recover from that; there is
        no user-facing cancel, and the worker that stranded the row is, by definition, gone.

        **It rescues `RunService.record_failure`'s own failure, which nothing else can.**
        That method opens a transaction and writes a terminal row; if Postgres is unreachable
        at exactly that moment the write raises, the exception propagates out of the job, and
        the run stays `processing` forever — the process that knew what happened has no way
        left to record it. This reaper is the only thing that closes that hole, which is why
        it reads the database rather than trusting any in-process bookkeeping.

        **Ordering, and it is the same ordering the cron tick commits to:**

        ```python
        async with transaction(self._pool) as tx:
            rows = await RunsReader(tx).lock_reapable(...)   # FOR UPDATE SKIP LOCKED
            ...                                              # guarded UPDATEs only
        # COMMITTED HERE. Nothing above this line touched the network.
        await self._enqueue_reaped(to_enqueue, queue)
        ```

        The Redis enqueue happens strictly after the commit (ARCHITECTURE.md §5.1): holding
        up to `REAP_BATCH_LIMIT` row locks across an unreachable Redis would turn a queue
        outage into a `runs`-table outage for every crawl worker and every API read in the
        system.

        **Two decisions, one per class of stranded run:**

        * A `processing` run whose claim is older than `stuck_before` is either handed back
          to `pending` and re-enqueued (it has attempts left) or marked `failed` (it does
          not). `attempts` is what decides, and it is never rolled back — a run cancelled by
          every deploy must eventually stop retrying rather than loop forever.
        * A `pending` run older than `orphaned_before` is re-enqueued with no write at all.
          It is already in the state it should be in; what it is missing is a job.

        Args:
            queue: arq's own `ArqRedis` pool, handed in by `reaper_tick` from `ctx["redis"]`
                — the same relationship `ScheduleService.run_due_schedules` has to it.
            stuck_before: `processing` runs claimed before this instant are abandoned.
            orphaned_before: `pending` runs created before this instant have no job.
            max_attempts: The retry budget, `MAX_ATTEMPTS` from `app/worker/policy.py`.
                Passed in rather than imported: ARCHITECTURE.md §3.3 forbids anything under
                `app/features/` from importing `app/worker/`, and a test wants to set it to 1
                without touching global state.

        Returns:
            A `ReaperSummary`, whose counts are for the log line and for tests — never for a
            caller to branch on.
        """
        to_enqueue: list[tuple[UUID, int]] = []
        requeued = 0
        orphans = 0
        failed = 0

        async with transaction(self._pool) as tx:
            rows = await RunsReader(tx).lock_reapable(
                stuck_before=stuck_before,
                orphaned_before=orphaned_before,
                limit=REAP_BATCH_LIMIT,
            )
            writer = RunsWriter(tx)
            for row in rows:
                run_id: UUID = row["id"]
                attempts: int = row["attempts"]

                if row["status"] == "pending":
                    # No write: the row is already in the state it should be in. What it is
                    # missing is a job, and that is not something a transaction can hand it.
                    orphans += 1
                    to_enqueue.append((run_id, attempts))
                    continue

                if attempts >= max_attempts:
                    await writer.mark_processing_failed(run_id, _interrupted_error(attempts))
                    failed += 1
                    # `run_id` in `extra=` and not only in the message: `reaper_tick` binds a
                    # tick id, not a run id, and one pass can give up on several runs, so
                    # this is the one line that ties a terminal failure to its run. ERROR
                    # rather than WARNING because a terminal failure IS the incident record —
                    # there is no error-tracking service holding a second copy
                    # (ARCHITECTURE.md §3.8).
                    logger.error(
                        "Reaper: giving up on run %s after %d attempt(s); it was interrupted "
                        "and never recorded an outcome",
                        run_id,
                        attempts,
                        extra={"run_id": str(run_id), "attempts": attempts},
                    )
                    continue

                if await writer.return_processing_to_pending(run_id):
                    requeued += 1
                    to_enqueue.append((run_id, attempts))
                    logger.warning(
                        "Reaper: run %s was abandoned mid-flight after %d attempt(s); "
                        "returning it to the queue",
                        run_id,
                        attempts,
                        extra={"run_id": str(run_id), "attempts": attempts},
                    )
        # COMMITTED HERE. Nothing above this line touched the network — see the docstring.

        enqueued, already_queued, enqueue_failures = await self._enqueue_reaped(to_enqueue, queue)

        summary = ReaperSummary(
            examined=len(rows),
            requeued=requeued,
            orphans_swept=orphans,
            failed=failed,
            enqueued=enqueued,
            already_queued=already_queued,
            enqueue_failures=enqueue_failures,
            limit_reached=len(rows) == REAP_BATCH_LIMIT,
        )
        # One line per pass, every pass, including the overwhelmingly common all-zero one:
        # "is the reaper actually running?" has to be answerable from the log alone, and a
        # job that logs only when it finds something is indistinguishable from a job that
        # stopped firing. INFO, with the same numbers repeated in `extra=` so a `jq` filter
        # can select on e.g. `failed > 0` without parsing the message back apart.
        logger.info(
            "Reaper: examined=%d requeued=%d orphans_swept=%d failed=%d enqueued=%d "
            "already_queued=%d enqueue_failures=%d limit_reached=%s",
            summary.examined,
            summary.requeued,
            summary.orphans_swept,
            summary.failed,
            summary.enqueued,
            summary.already_queued,
            summary.enqueue_failures,
            summary.limit_reached,
            extra={
                "examined": summary.examined,
                "requeued": summary.requeued,
                "orphans_swept": summary.orphans_swept,
                "failed": summary.failed,
                "enqueued": summary.enqueued,
                "already_queued": summary.already_queued,
                "enqueue_failures": summary.enqueue_failures,
                "limit_reached": summary.limit_reached,
            },
        )
        if summary.limit_reached:
            logger.warning(
                "Reaper hit its batch limit (%d) — there may be more stranded runs than this "
                "pass could examine. The next pass, five minutes later, takes the rest; a "
                "sustained full batch means something is stranding runs faster than they can "
                "be rescued, which is a different problem from a slow reaper.",
                REAP_BATCH_LIMIT,
                extra={"examined": summary.examined, "batch_limit": REAP_BATCH_LIMIT},
            )
        return summary

    async def _enqueue_reaped(
        self, runs: list[tuple[UUID, int]], queue: ArqRedis
    ) -> tuple[int, int, int]:
        """Enqueue `crawl_task` for every `(run_id, attempts)` pair, under a DETERMINISTIC
        job id, and return `(enqueued, already_queued, failures)`.

        **`_job_id` is what keeps the orphan sweep from amplifying.** A `pending` run is
        re-enqueued with no write to show for it, so nothing stops the NEXT pass, five
        minutes later, from finding the same row and enqueuing a second job — and the pass
        after that a third. `f"crawl:{run_id}:{attempts}"` closes that: arq refuses to
        enqueue a job whose id already exists and returns `None` instead, so every pass after
        the first is a no-op rather than another job. Without it, a genuinely backlogged
        queue (a `pending` run legitimately waiting behind `MAX_JOBS = 2` for an hour) would
        collect one junk job every five minutes for as long as the backlog lasted.

        `attempts` is part of the id, not decoration. It is what makes the id unique per
        ATTEMPT rather than per run, so that a run legitimately re-enqueued after a later
        claim is not silently deduplicated against a job from a previous attempt whose result
        key is still in Redis (arq keeps results for `keep_result`, an hour by default). The
        pairing is safe in the other direction too: `attempts` only ever increases, and it
        increases exactly when a job claims the run, so the reaper can never need the same
        `(run_id, attempts)` id twice for two different pieces of work.

        This is deliberately the ONLY enqueue site in the codebase that passes `_job_id`.
        `trigger_run` and the cron tick each enqueue exactly once for a freshly-inserted run,
        so they have nothing to deduplicate against; giving them ids would buy nothing and
        would put a second, subtler failure mode (a stale result key silently swallowing a
        real enqueue) on the two paths a user actually waits on. The cost of leaving them
        alone is bounded and one-off: the first sweep of a run that IS still queued under
        arq's own random id adds one duplicate job, which `claim_pending` absorbs — every
        sweep after that dedupes against this id.

        Broad `except Exception`, not `except RedisError`, for the reason
        `ScheduleService._enqueue` gives for its own loop. A failure here is counted and the
        loop continues; unlike the tick's enqueue path, it does NOT mark the run failed. The
        run is legitimately `pending` and the next pass will try again in five minutes, which
        is a strictly better answer than converting a transient Redis blip into a terminal
        failure on a run that has done nothing wrong.
        """
        enqueued = 0
        already_queued = 0
        failures = 0
        for run_id, attempts in runs:
            try:
                job = await queue.enqueue_job(
                    CRAWL_TASK_JOB_NAME, str(run_id), _job_id=crawl_job_id(run_id, attempts)
                )
            except Exception:
                failures += 1
                # `exc_info=True` logs the real cause; `REDIS_URL` is never interpolated into
                # this message or any other (ARCHITECTURE.md §9.4).
                logger.error(
                    "Reaper: failed to enqueue %s for run %s; it stays pending and the next "
                    "pass will try again",
                    CRAWL_TASK_JOB_NAME,
                    run_id,
                    exc_info=True,
                    extra={"run_id": str(run_id)},
                )
                continue
            if job is None:
                already_queued += 1
            else:
                enqueued += 1
        return enqueued, already_queued, failures

    async def get_previous_completed_index(
        self, website_id: UUID, *, before_started_at: datetime, before_run_id: UUID
    ) -> tuple[PreviousCompletedIndex | None, bool | None]:
        """The previous COMPLETED run's index, and whether the run immediately before the
        current one actually completed — what `CrawlService._build_index_diff`
        (`crawl/service.py`, PER-193) needs to build this run's `index_diff` block.

        Called only from the worker path, on `CrawlService.execute_run`'s no-transaction
        window between `claim_for_processing` and `record_success` — the same window the
        Storage upload and the enrichment call already occupy (`crawl/service.py`'s module
        docstring, §5.1). **No transaction, no ownership check**, for the identical reasoning
        `claim_for_processing` gives for itself: a background job acting on its own behalf has
        no caller to check ownership against, and this read has no write to be atomic with.

        Args:
            website_id: The current run's own website.
            before_started_at: The current run's `started_at` — "previous" is relative to
                THIS run, never to `datetime.now(UTC)`.
            before_run_id: The current run's own id, the tie-break half of the keyset pair.

        Returns:
            `(previous, previous_run_completed)`.

            `previous` is `None` in two cases the caller cannot tell apart from this return
            value alone (and does not need to — both feed `build_index_diff`'s `state:
            "first_run"` branch identically): there is no completed run before this one at
            all, or the nearest completed row has `llms_txt IS NULL` — a completed run with
            no index cannot be compared against, so it is treated the same as none existing.
            Otherwise it carries that run's id, `completed_at`, `llms_txt`, and a defensively
            parsed `urls_discovered` (`int` only; `None` for anything else, including a row
            that predates `RUN_STATS_VERSION` 4 or has unparseable `stats`) — read through
            `_parse_stats`, the same defensive decoder `_shared_fields` uses for every other
            read of this column.

            `previous_run_completed` is a tri-state independent of `previous`: `None` when
            there is no earlier run of ANY status, `False` when the immediately preceding run
            exists but is not `completed`, `True` when it is. Note that `previous is not None`
            implies `previous_run_completed is True`, but not the reverse — the run
            immediately before this one can be `completed` while still having no `llms_txt`,
            which is the one case where `previous_run_completed` is `True` and `previous` is
            still `None`.
        """
        row = await self._reader.previous_completed_index(
            website_id, before_started_at=before_started_at, before_run_id=before_run_id
        )

        previous_status: RunStatusName | None = row["previous_status"]
        previous_run_completed = None if previous_status is None else previous_status == "completed"

        if row["run_id"] is None or row["llms_txt"] is None:
            return None, previous_run_completed

        stats = _parse_stats(row["stats"]) or {}
        raw_urls_discovered = stats.get("urls_discovered")
        urls_discovered = raw_urls_discovered if isinstance(raw_urls_discovered, int) else None

        return (
            PreviousCompletedIndex(
                run_id=row["run_id"],
                completed_at=row["completed_at"],
                llms_txt=row["llms_txt"],
                urls_discovered=urls_discovered,
            ),
            previous_run_completed,
        )

    async def record_success(
        self,
        run_id: UUID,
        *,
        llms_txt: str,
        llms_full_txt: str,
        storage_path: str,
        stats: dict[str, Any],
    ) -> None:
        """Record a completed crawl for a run this worker had already claimed: both generated
        artifacts (`llms.txt` and its `llms-full.txt` expansion), where the payload landed,
        and the run's stats.

        Both artifacts arrive already generated — `internals/llms_txt.py` produced them from
        the fetched pages before the upload below, and this method neither inspects nor
        reshapes either one. Adding `llms_full_txt` (PER-179) changed nothing about the
        ordering argument that follows: it is one more column in the same single UPDATE,
        inside the same single transaction, opened at the same moment.

        **The Storage upload has ALREADY HAPPENED by the time this is called.**
        `storage_path` is `SupabaseStorage.upload`'s return value, not a location this method
        is about to write to — `app.features.crawl.service.CrawlService.execute_run` uploads
        the payload first and calls this only once that upload has succeeded. Reversing that
        order — opening this transaction and uploading from inside it — is exactly the
        ordering ARCHITECTURE.md §5.1 forbids: it would hold a database transaction open
        across a network call whose duration this codebase does not control, and it would let
        a run be recorded `completed` before the artifact it points at necessarily exists.

        Opens exactly one `transaction(self._pool)` and calls
        `RunsWriter.mark_processing_completed` inside it. **Nothing else may go in that
        block** — no network call, no second statement — for the same reason every other
        transaction in this codebase is kept to one unit of work (ARCHITECTURE.md §5).

        If the writer returns `False`, the row was no longer `processing` — something else
        (a redelivered job that raced this one, a reaper) already reached a terminal state.
        That is logged at WARNING, naming `run_id`, rather than raised: the object this run's
        payload was uploaded to is now an orphan, which is the benign half of the
        upload-then-write ordering trade — a wasted upload costs a fraction of a cent, while
        the alternative ordering could have left a `completed` row pointing at nothing. See
        ARCHITECTURE.md §11 for where that orphan is tracked as known debt.
        """
        async with transaction(self._pool) as tx:
            completed = await RunsWriter(tx).mark_processing_completed(
                run_id,
                llms_txt=llms_txt,
                llms_full_txt=llms_full_txt,
                storage_path=storage_path,
                stats=stats,
            )
        if not completed:
            logger.warning(
                "crawl: run %s was no longer processing when its success was recorded; its "
                "uploaded payload at %s is now orphaned",
                run_id,
                storage_path,
            )

    async def record_failure(
        self, run_id: UUID, error: str, stats: dict[str, Any] | None = None
    ) -> None:
        """Record the terminal failure of a run this worker had already claimed.

        Calls `RunsWriter.mark_processing_failed`, NOT `mark_failed`. The two are not
        interchangeable: `mark_failed` is `trigger_run`'s enqueue-undo and matches only a
        `pending` row, so using it here — after `claim_for_processing` has already moved the
        run to `processing` — would silently update nothing and leave the run stuck. See
        `internals/runs_writer.py`'s module docstring for why the two statements are
        deliberately separate rather than one unguarded `UPDATE`.

        `error` must already be sanitized by the caller
        (`app.features.crawl.service.CrawlService`'s "Sanitizing" note) — this method does
        not inspect, truncate, or otherwise second-guess it, because `runs.error` is
        readable by every signed-in user (ARCHITECTURE.md §4.1) and this feature has no way
        to tell a safe message from an unsafe one after the fact.

        `stats` is threaded straight through to `RunsWriter.mark_processing_failed`, which
        `COALESCE`s it against whatever is already stored rather than overwriting with `NULL`
        — this method does not second-guess that either. Pass whatever partial numbers the
        caller has (`internals/run_stats.py`'s `build_run_stats`, if a `CrawlResult` exists to
        build it from) or omit it entirely when nothing usable exists yet.
        """
        async with transaction(self._pool) as tx:
            await RunsWriter(tx).mark_processing_failed(run_id, error, stats)

    async def create_scheduled_run(
        self, website_id: UUID, schedule_id: UUID, *, connection: Connection
    ) -> UUID:
        """Insert one cron-triggered, `pending` run for `website_id` and return its id.

        Called only from `ScheduleService.run_due_schedules`, on the `Connection` that
        method's own `transaction()` already opened — see the module docstring's "Two
        methods that take an already-open `Connection`" section for why this method's shape
        is the exception rather than the rule on this service, and why it opens no
        transaction of its own.

        Args:
            website_id: The website to run. Already known to exist and to be active — the
                tick locked its `schedules` row inside the same transaction this connection
                belongs to.
            schedule_id: The schedule that produced this run.
            connection: The `Connection` the cron tick's own `transaction()` yielded. This
                method neither commits nor rolls it back.

        Returns:
            The new run's id.
        """
        row = await RunsWriter(connection).insert_scheduled(website_id, schedule_id)
        run_id: UUID = row["id"]
        return run_id

    async def website_ids_with_active_runs(
        self, website_ids: Sequence[UUID], *, connection: Connection
    ) -> set[UUID]:
        """Return the subset of `website_ids` that already have a `pending`/`processing` run.

        Called only from `ScheduleService.run_due_schedules`, on the same `Connection` as
        `create_scheduled_run` above — see that method's docstring and the module docstring's
        "Two methods that take an already-open `Connection`" section for why.

        Args:
            website_ids: The websites behind one tick's locked batch of due schedules.
            connection: The `Connection` the cron tick's own `transaction()` yielded.

        Returns:
            The subset of `website_ids` blocked by an in-flight run — see
            `RunsReader.website_ids_with_active_runs` for why this is a `set`.
        """
        return await RunsReader(connection).website_ids_with_active_runs(website_ids)


def build_run_service(pool: Pool, settings: Settings) -> RunService:
    """Build a `RunService` for the worker, from what `app.worker.jobs.reaper_tick` has on
    `ctx`: the process-wide pool and the module `Settings`.

    Mirrors `app.features.crawl.service.build_crawl_service` and
    `app.features.schedules.service.build_schedule_service`, and exists for the same reason
    both of those do — the router-level provider (`app.api.routers.runs.get_run_service`) is
    wired for FastAPI's dependency injection, which does not exist inside an arq cron job.

    Constructs its own `WebsiteService` because `RunService` requires one, even though the
    reaper never reaches a method that uses it: the reaper touches `runs` alone. Handing it
    the same `Settings` the API path gets, rather than a stripped-down stand-in, is what keeps
    a worker-built `RunService` from quietly diverging from a request-built one.
    """
    return RunService(pool, WebsiteService(pool), settings)
