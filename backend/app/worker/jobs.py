"""ARQ job functions.

Thin by contract (ARCHITECTURE.md §3.3): a job parses its typed arguments, calls one
service method, and returns. Jobs are enqueued with ids and primitives only — never ORM
objects, never Pydantic models — because everything on the queue is serialized and has to
survive a deploy that changes those classes.

**`crawl_task` lives here, not in `app/features/crawl/task.py`.** The ticket that added it
said the latter; ARCHITECTURE.md §3.3 says "Job functions live in `backend/app/worker/`" and
lists `worker/jobs.py` as "the job functions themselves" — and per CLAUDE.md, the document
wins over a ticket that contradicts it. The task itself stays exactly as thin as `noop`
below: coerce the id, pull the two resources it needs off `ctx`, call one service method,
log, and return. Every real decision — the idempotency claim, the network call, the
sanitized failure message — lives in `app.features.crawl.service.CrawlService`.

`crawl_task`'s public signature is exactly `async def crawl_task(ctx, run_id)`. A sibling
ticket enqueues it **by name** — arq registers a bare coroutine under its `__qualname__`
(`arq.worker.func`), so `"crawl_task"` is the registered name regardless of which module it
lives in. Do not rename it, wrap it in `arq.worker.func(..., name=...)`, or add a required
parameter.

`run_id` arrives as a `str`, not a `UUID`: arq serializes job arguments with msgpack, which a
`UUID` does not survive, and ARCHITECTURE.md §3.3 says jobs take "ids and primitives" for
exactly this reason. `crawl_task` accepts `str | UUID` and coerces with `UUID(str(run_id))`;
a malformed id is logged and the job returns rather than raising.

**"A worker must not raise at arq" now has exactly one exception, and PER-166 added it
deliberately.** `crawl_task` raises `arq.worker.Retry` — and only that — when
`CrawlService` reports a transient failure with attempts left on the run's budget. The old
contract was written when a raise had nowhere useful to go: arq 0.28 retries only on `Retry`,
`RetryJob`, and `CancelledError`, and turns every other exception into a terminal job
failure, so raising an application error at arq really would have accomplished nothing but a
noisier log. Asking for a redelivery is the one thing a raise can accomplish, and it is the
one thing this file raises for. Every other failure — a bad `run_id`, a missing run, a lost
claim race, a permanent crawl failure, an exhausted retry budget — is still logged and
returned as a string.

**The retry policy lives in `app/worker/policy.py`, and the number of attempts lives in the
database.** `runs.attempts`, incremented by the claim, is what decides whether there is
budget left; `policy.retry_delay_seconds` turns that number into a backoff. Neither is
`ctx["job_try"]`, which counts deliveries of one arq job id and resets whenever the reaper
re-enqueues a run under a new one.

**Each job binds its own correlation id, and that is the one thing a job does that is not
"call one service method".** `crawl_task` opens `run_id_context(...)` around its body and
`schedule_tick` opens `tick_id_context(...)` around its; both are `contextvar` scopes
(`app.core.logging`), so every line logged anywhere beneath them — in `CrawlService`, in
`internals/fetcher.py`, in `internals/ssrf.py` — carries the id automatically, with no
function in between having to accept one. It belongs here, in the job, rather than in the
service, because the job is the outermost thing that knows which run this is: a service
method called from a route would have no run to bind, and binding it twice at different
depths is how a correlation id starts disagreeing with itself.

**`schedule_tick` is the second job, and it takes no arguments beyond `ctx`.** It is not
enqueued by anything — arq's own cron scheduler calls it once a minute (`app/worker/
settings.py`'s `cron_jobs`), which is what makes it a cron job rather than a task something
else enqueues. It builds a `ScheduleService` from `ctx["db_pool"]` and the module `settings`
and calls `ScheduleService.run_due_schedules`, exactly as thin as `crawl_task` above.

**`reaper_tick` is the third, and the second cron job.** Same shape as `schedule_tick`, a
different cadence (every five minutes) and a different service method
(`RunService.reap_stuck_runs`). Both bind `tick_id_context`; see `reaper_tick`'s own
docstring for why they share that contextvar rather than introducing a second one.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from arq.worker import Retry

from app.core.logging import run_id_context, tick_id_context
from app.core.settings import settings
from app.features.crawl.service import TransientCrawlError, build_crawl_service
from app.features.runs.service import build_run_service
from app.features.schedules.service import build_schedule_service
from app.worker.policy import (
    MAX_ATTEMPTS,
    ORPHANED_PENDING_AFTER_SECONDS,
    STUCK_AFTER_SECONDS,
    retry_delay_seconds,
)


logger = logging.getLogger(__name__)


async def noop(ctx: dict[Any, Any]) -> str:
    """A no-op job that proves the queue is wired end to end.

    Named `noop` rather than `ping` on purpose: it shares a log stream with redis-py's own
    `PING` traffic and with `GET /health`'s Redis probe, and three different things called
    "ping" in one log is a bad half-hour for whoever is reading it at the time.

    **This is not filler, and it should not be deleted when the crawl task lands.** It
    exists for two reasons:

    1. **arq refuses to start with an empty function list.** `arq.worker.Worker.__init__`
       raises `RuntimeError('at least one function or cron_job must be registered')` before
       it ever connects to Redis. A `WorkerSettings` with `functions = []` therefore does
       not idle harmlessly waiting for its first real job — it crashes on boot, and Fly
       restarts it in a loop that is easy to miss because nothing is serving HTTP to fail.
    2. **It is the only way to verify the queue without the crawl task.** "A job enqueued
       from the API is picked up by the worker within ~5s" is an acceptance criterion of
       the ticket that introduced this file, and `poll_delay` is a setting whose regression
       is otherwise silent. Enqueue this, watch the worker log it, and both the Redis wiring
       and the poll interval have been observed rather than assumed.

    It touches nothing: no database, no network beyond the Redis round trip arq already
    made to deliver it. `ctx` carries the shared resources `on_startup` put there
    (`ctx["db_pool"]`) plus arq's own `ctx["redis"]`, and this job deliberately uses
    neither — a probe that can fail for a second reason is a worse probe.

    Returns:
        A short string, which arq stores as the job result under `keep_result` (1 hour by
        default). `Job.result()` returning it is the API-side half of the round trip.
    """
    logger.info("noop job received (job_id=%s, attempt=%s)", ctx.get("job_id"), ctx.get("job_try"))
    return "ok"


async def crawl_task(ctx: dict[Any, Any], run_id: str | UUID) -> str:
    """Crawl the website behind `run_id`. Thin by contract — see the module docstring.

    `ctx["db_pool"]`, `ctx["http_client"]`, and `ctx["storage"]` are all published once per
    process by `app/worker/settings.py`'s `open_worker_resources`, the same pattern `noop`
    documents for `ctx["db_pool"]` alone. This job builds a fresh `CrawlService` from them on
    every call — the service itself is cheap to construct (see `build_crawl_service`) —
    rather than caching one on `ctx`, so a service's dependencies are exactly what
    `app.features.crawl.service.build_crawl_service`'s signature says they are, with
    nothing smuggled in through job-to-job state.

    Args:
        ctx: arq's job context. Must already carry `"db_pool"`, `"http_client"`, and
            `"storage"` — true for any job this `WorkerSettings` runs, and never true in a
            plain unit test constructing this function's arguments by hand.
        run_id: The run to crawl, as arq's msgpack serialization actually delivers it — see
            the module docstring for why that is a `str` rather than a `UUID` in practice,
            and why the parameter still accepts either.

    Returns:
        A short, human-readable outcome string for arq's job result — never anything a
        caller is expected to parse. A bad `run_id`, a missing run, a lost claim race, and a
        terminally failed crawl (including a failed Storage upload or database write) are all
        logged and returned rather than raised.

    Raises:
        arq.worker.Retry: the one deliberate exception to "a worker must not raise at arq" —
            see the module docstring. The run is already back in `pending` when this is
            raised, so the redelivered job has something to claim.
        asyncio.CancelledError: arq's job timeout, or SIGTERM. Not caught here.
            `CrawlService.execute_run` catches it one layer down, hands the run back to the
            queue (or fails it, if the budget is spent), and re-raises — so by the time it
            reaches this frame the database already reflects what happened.

            Whether arq then redelivers depends on WHICH cancellation it was, and only
            SIGTERM is redelivered: it cancels `run_job`'s own frame, so arq sees
            `CancelledError` and re-queues. A `job_timeout` cancels the inner task, and
            `asyncio.wait_for` hands arq a `TimeoutError` instead, which is not in its retry
            set — that run waits for the reaper's orphan sweep. See
            `CrawlService.execute_run`'s docstring for the full argument, and
            `tests/test_crawl_retry.py` for the test that pins it.
    """
    try:
        parsed_run_id = UUID(str(run_id))
    except ValueError:
        logger.error("crawl_task: %r is not a valid run id; skipping", run_id)
        return "invalid run id"

    # THE ONE PLACE `run_id` IS BOUND, and the reason nothing below it — not
    # `CrawlService`, not `internals/fetcher.py`, not `internals/ssrf.py` — has to be handed
    # a run id to log one. `run_id_context` sets a `contextvar`, which arq's job task
    # inherits a private copy of, so two crawls running concurrently on the same worker
    # (`max_jobs = 2`) can never show each other's id. See app/core/logging.py.
    with run_id_context(str(parsed_run_id)):
        service = build_crawl_service(ctx["db_pool"], ctx["http_client"], ctx["storage"], settings)
        try:
            outcome = await service.execute_run(parsed_run_id, max_attempts=MAX_ATTEMPTS)
        except TransientCrawlError as retry:
            # THE ONE PLACE THIS CODEBASE ASKS ARQ FOR A REDELIVERY, and the reason
            # `CrawlService` raises its own exception type rather than `arq.worker.Retry`
            # directly: `app/features/` may not import `app/worker/`, and arq is a queue
            # detail the crawl feature has no business knowing about (ARCHITECTURE.md §3.3).
            # The service decided THAT this attempt deserves another go; this line decides
            # HOW LONG to wait and says so in arq's own vocabulary.
            #
            # `defer` is sized from `retry.attempts` — the run's own counter — not from
            # `ctx["job_try"]`. See `TransientCrawlError.__init__` and `app/worker/policy.py`.
            #
            # The run is already back in `pending` by the time this runs, so the redelivered
            # job has something to claim. Raising `Retry` is what arq 0.28 requires: it
            # retries on `Retry`, `RetryJob`, and `CancelledError`, and treats every other
            # exception as a terminal job failure.
            defer_seconds = retry_delay_seconds(retry.attempts)
            logger.warning(
                "crawl_task: asking arq to retry in %ds (attempt %d of %d)",
                defer_seconds,
                retry.attempts,
                MAX_ATTEMPTS,
                extra={"defer_seconds": defer_seconds, "attempt": retry.attempts},
            )
            raise Retry(defer=defer_seconds) from None

        if outcome is None:
            logger.info("crawl_task: no outcome; see CrawlService's own logs for why")
            return "no outcome"

        logger.info(
            "crawl_task: fetched %d page(s)",
            outcome.stats.get("pages_crawled", 0),
            extra={
                "pages_crawled": outcome.stats.get("pages_crawled", 0),
                "storage_path": outcome.storage_path,
            },
        )
        return "ok"


async def schedule_tick(ctx: dict[Any, Any]) -> str:
    """Run one cron tick: turn every due schedule into a `runs` row (or advance it past being
    due), and enqueue `crawl_task` for each one. Thin by contract — see the module docstring.

    `ctx["db_pool"]` is the same process-wide pool `crawl_task` reads, published once by
    `app/worker/settings.py`'s `open_worker_resources`. `ctx["redis"]` is different from
    both: it is **arq's own** `ArqRedis` connection pool, set by `arq.worker.Worker.main`
    itself before `on_startup` ever runs (every job's `ctx` carries it, not just this one) —
    not something `open_worker_resources` publishes, and not something this job opens for
    itself. That is why `ScheduleService.run_due_schedules` takes a queue pool as a plain
    argument rather than this job constructing one: the connection already exists, arq made
    it, and reusing it is exactly what `crawl_task` does one line up for `ctx["http_client"]`.

    **Never raises.** `ScheduleService.run_due_schedules` is wrapped in its own
    `try`/`except Exception`, logged at `logger.error` with `exc_info=True`, and answered with
    a short failure string — the same "a worker must not raise at arq" contract `crawl_task`
    follows. `app/worker/settings.py` registers this cron job with `max_tries=1`: a tick that
    fails is not retried immediately against whatever partially-advanced state it left behind;
    the NEXT tick, one minute later, retries it against fresh state instead.
    `asyncio.CancelledError` (arq's job timeout, or SIGTERM) is not caught here either, for the
    same reason `crawl_task` does not catch it — a plain `except Exception` already lets it
    propagate, since `CancelledError` is not a subclass of `Exception`.

    Returns:
        A short, human-readable outcome string for arq's job result, built from the tick's
        `TickSummary` — never anything a caller is expected to parse.
    """
    # `crawl_task`'s `run_id_context` for a job that has no id of its own to bind. A tick is
    # not addressed by anything — arq's scheduler calls it, nothing enqueues it — so the id
    # is minted here, once per tick, purely so that the handful of lines one tick emits can
    # be selected out of a stream shared with however many crawls are running beside it.
    with tick_id_context(str(uuid4())):
        try:
            service = build_schedule_service(ctx["db_pool"], settings)
            summary = await service.run_due_schedules(ctx["redis"])
        except Exception:
            logger.error("schedule_tick: cron tick failed", exc_info=True)
            return "failed"

        return (
            f"examined={summary.examined} runs_created={summary.runs_created} "
            f"skipped_active={summary.skipped_active} "
            f"enqueue_failures={summary.enqueue_failures} "
            f"limit_reached={summary.limit_reached}"
        )


async def reaper_tick(ctx: dict[str, Any]) -> str:
    """Run one pass of the stuck-run reaper. Thin by contract — see the module docstring.

    The second cron job, fired every `REAPER_INTERVAL_MINUTES` by arq's own scheduler
    (`app/worker/settings.py`'s `cron_jobs`), and shaped exactly like `schedule_tick` above:
    mint a correlation id, build one service, call one method, answer with a string.

    **It is the only thing in this system that can un-stick a run**, which is why it is a
    cron job rather than something a request path could trigger. Every other actor that could
    have recorded an outcome for a stranded run is, by definition, gone — the worker was OOM
    killed, the machine restarted, or `RunService.record_failure`'s own write hit an
    unreachable Postgres and took the only in-process record of the failure down with it. The
    reaper reads the database and nothing else, which is what makes it able to rescue cases
    no amount of in-process error handling can.

    **Both thresholds are computed here, from ONE `now`.** They are worker policy
    (`app/worker/policy.py`), and `app/features/` may not import `app/worker/`
    (ARCHITECTURE.md §3.3), so the job resolves them into absolute instants and hands those
    to the service — the same relationship `ScheduleService.run_due_schedules` has with the
    clock it captures once and threads through every query in the pass.

    **Binds a `tick_id`, sharing `tick_id_context` with `schedule_tick`.** Both are cron
    passes with no id of their own, and the field means "which pass of a cron job was this";
    the `logger` name and the message text are what say which cron job. A third contextvar
    for the second cron job would make `jq 'select(.tick_id == "…")'` a question you have to
    ask twice.

    **Never raises**, exactly like `schedule_tick`, and registered with the same
    `max_tries=1`: a pass that fails is not re-run immediately against whatever partial state
    it left behind — the next pass, five minutes later, re-evaluates from the database.
    `asyncio.CancelledError` is not caught here either, for the same reason it is not there:
    a plain `except Exception` already lets it propagate.

    Returns:
        A short, human-readable outcome string for arq's job result, built from the pass's
        `ReaperSummary` — never anything a caller is expected to parse.
    """
    with tick_id_context(str(uuid4())):
        try:
            # One clock for the whole pass, for the reason every other multi-query unit of
            # work in this codebase captures one: two thresholds derived from two different
            # `datetime.now()` calls could disagree about which side of a boundary a row
            # falls on, and the disagreement would be invisible.
            now = datetime.now(UTC)
            service = build_run_service(ctx["db_pool"], settings)
            summary = await service.reap_stuck_runs(
                ctx["redis"],
                stuck_before=now - timedelta(seconds=STUCK_AFTER_SECONDS),
                orphaned_before=now - timedelta(seconds=ORPHANED_PENDING_AFTER_SECONDS),
                max_attempts=MAX_ATTEMPTS,
            )
        except Exception:
            logger.error("reaper_tick: reaper pass failed", exc_info=True)
            return "failed"

        return (
            f"examined={summary.examined} requeued={summary.requeued} "
            f"orphans_swept={summary.orphans_swept} failed={summary.failed} "
            f"enqueued={summary.enqueued} already_queued={summary.already_queued} "
            f"enqueue_failures={summary.enqueue_failures}"
        )
