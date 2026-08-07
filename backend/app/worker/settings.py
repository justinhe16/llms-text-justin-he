"""`WorkerSettings` — the configuration `arq` loads to run the worker process.

Started as `arq app.worker.settings.WorkerSettings`, by the `worker` process group in
backend/fly.toml and by `scripts/dev.sh` under `make dev`. Both name this exact path; if it
moves, both move with it.

**Every setting arq reads must be declared directly on the class below.**
`arq.worker.get_kwargs()` builds the `Worker(...)` call from `WorkerSettings.__dict__` and
intersects it with `Worker.__init__`'s parameter names. `__dict__` does not include
inherited attributes, so a setting moved onto a base class is not "refactored", it is
**silently dropped** and the worker starts with arq's default in its place. That failure is
invisible — no error, no log line, just a worker polling ten times too often. Do not
introduce a base class here, and do not set these from a loop or a mixin.
"""

import logging
from typing import Any, Final

from arq import cron

from app.core.logging import configure_logging
from app.core.settings import settings
from app.features.crawl.anthropic_client import build_anthropic_client
from app.features.crawl.http_client import build_crawl_client
from app.infrastructure.db.pool import close_pool, open_pool
from app.infrastructure.queue.pool import redis_settings_from_url
from app.infrastructure.storage.supabase_storage import build_storage_client, build_supabase_storage
from app.worker.jobs import crawl_task, noop, reaper_tick, schedule_tick
from app.worker.policy import (
    CRON_TICK_TIMEOUT_SECONDS,
    JOB_COMPLETION_WAIT_SECONDS,
    JOB_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_JOBS,
    POLL_DELAY_SECONDS,
    REAPER_INTERVAL_MINUTES,
    REAPER_TIMEOUT_SECONDS,
)


logger = logging.getLogger(__name__)

# AT IMPORT, NOT IN on_startup, and not left to arq.
#
# `arq`'s CLI configures logging for the `arq` logger alone, so without this every `app.*`
# log line falls through to a handler-less root logger and Python's `lastResort` fallback:
# INFO is dropped entirely — including the startup messages below — and WARNING and above
# print with no level, name, or timestamp. Doing it here rather than in `on_startup` means
# an exception raised while this module is being imported is still logged properly, and
# that is exactly when a misconfigured worker fails.
#
# `"worker"` is the `process` field stamped on every line this process emits, and it is the
# name backend/fly.toml's `[processes]` block uses, so `fly logs --process worker` and a
# `jq 'select(.process == "worker")'` over the same stream select the same lines.
configure_logging("worker")


# HOW ARQ'S OWN LINES BECOME JSON. Passed to the worker as
# `--custom-log-dict app.worker.settings.ARQ_LOG_CONFIG`, in backend/fly.toml's `worker`
# process command and in scripts/dev.sh — tests/test_logging.py asserts that both of them
# still name this symbol, because a typo in either is a worker that fails to boot.
#
# THIS CANNOT BE DONE FROM `configure_logging()` ABOVE, and that is the only reason the dict
# exists. `arq.cli` imports this module FIRST and calls `logging.config.dictConfig(...)`
# SECOND, so anything the import-time call did to the `arq` logger is undone a moment later
# by arq's own default config — which attaches a plain-text handler
# (`'%(asctime)s: %(message)s'`) and would leave arq's job lines as the only non-JSON
# output the worker produced. `--custom-log-dict` replaces that default with this, which
# gives the `arq` logger no handler of its own and lets its records propagate to the JSON
# handler on the root logger instead.
#
# `disable_existing_loggers: False` matters as much as the rest of it: the default is True,
# and dictConfig would otherwise disable every `app.*` logger created during the import
# that has already happened — silencing the whole application to configure one library.
ARQ_LOG_CONFIG: Final[dict[str, Any]] = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "arq": {"handlers": [], "level": settings.log_level, "propagate": True},
    },
}


async def open_worker_resources(ctx: dict[Any, Any]) -> None:
    """Open the process-wide Postgres pool and publish it on the ARQ context.

    The pool is created with the **same factory the API uses**
    (`app.infrastructure.db.pool.open_pool`) rather than a second one written for the
    worker, so the two processes connect identically and Supabase's connection budget is
    sized against one implementation instead of two.

    It is registered in both of the places a caller might reach for, and that is
    deliberate rather than redundant:

    * `open_pool()` sets the module singleton behind `get_pool()`. That is what lets a
      **service** written for the API run unchanged inside a job (ARCHITECTURE.md §3.3 —
      a background job is a service call with a different trigger). In the API a service
      receives its pool through FastAPI dependency injection; there is no injector here,
      so `get_pool()` is the equivalent.
    * `ctx["db_pool"]` is arq's own idiom, and it is the pattern this function follows for
      every other resource it wants to build once instead of per job. `ctx["http_client"]`
      (the crawl task's shared `httpx.AsyncClient`,
      `app.features.crawl.http_client.build_crawl_client`) and `ctx["storage_client"]` /
      `ctx["storage"]` (Supabase Storage's client and the thin wrapper around it,
      `app.infrastructure.storage.supabase_storage`), both added below, are the rest of
      those: every one is read by `crawl_task` (`jobs.py`) on every call rather than opened
      fresh per job. One place to add them, one place to close them.

    `storage_client` and `storage` are two separate `ctx` keys, deliberately, mirroring the
    relationship `SupabaseStorage` documents in its own module: `SupabaseStorage` is handed a
    client and never owns it, so the thing that built the client (`build_storage_client`) is
    the thing `close_worker_resources` below closes, and the thing built from it
    (`build_supabase_storage`) is a separate value with nothing of its own to close.

    **`ctx["anthropic_client"]` is CONDITIONAL — this is acceptance criterion 1 (PER-180).**
    Built only `if settings.crawl_enrich_with_llm`, unlike every other key above, which is
    always set. With the flag off, this key is simply absent from `ctx`: no `AsyncAnthropic`
    is constructed, no `ANTHROPIC_API_KEY` is read, and `app.worker.jobs.crawl_task` reads it
    back with `ctx.get("anthropic_client")` rather than `ctx["anthropic_client"]` for exactly
    this reason. Building it unconditionally — the way `http_client` and `storage_client` are
    built — would be harmless in itself (`build_anthropic_client`, like `build_crawl_client`,
    opens no socket), but it would also make "was enrichment supposed to run" a question with
    no clean answer from `ctx` alone once the client existed either way.

    Errors are logged at CRITICAL and re-raised. arq's `Worker.run()` only swallows
    `CancelledError`, so re-raising exits the process with a traceback; the log line above
    it exists because a worker that dies here dies with no HTTP listener to fail a health
    check, so Fly restarts it in a loop and the only evidence is
    `fly logs --app llms-text-justin-he`.
    """
    logger.info("ARQ worker starting (environment=%s)", settings.environment)
    try:
        ctx["db_pool"] = await open_pool(settings)
    except Exception:
        # Never interpolate the DSN or any part of it (ARCHITECTURE.md §9.4).
        logger.critical(
            "ARQ worker could not open the Postgres pool and is exiting. Fly will restart "
            "it; if this repeats, the worker is in a restart loop with nothing serving "
            "HTTP to notice it. Check `fly logs --app llms-text-justin-he --process worker`."
        )
        raise
    # After the pool, and unguarded: constructing an `httpx.AsyncClient` opens no socket
    # and makes no network call (see `build_crawl_client`'s own docstring), so there is no
    # way for these lines to fail the way the pool open above can.
    ctx["http_client"] = build_crawl_client(settings)
    ctx["storage_client"] = build_storage_client(settings)
    ctx["storage"] = build_supabase_storage(ctx["storage_client"], settings)
    # THE GUARD IS ACCEPTANCE CRITERION 1: nothing under this `if` runs at all with the flag
    # off, so a deployment (or this test suite) that never sets `CRAWL_ENRICH_WITH_LLM`
    # never constructs an `AsyncAnthropic`, never reads `ANTHROPIC_API_KEY`, and boots
    # exactly as it did before this feature existed. See `ctx["anthropic_client"]`'s own
    # paragraph above for why this key is conditional where every other one here is not.
    if settings.crawl_enrich_with_llm:
        ctx["anthropic_client"] = build_anthropic_client(settings)
    logger.info("ARQ worker ready (poll_delay=%ss, max_jobs=%s)", POLL_DELAY_SECONDS, MAX_JOBS)


async def close_worker_resources(ctx: dict[Any, Any]) -> None:
    """Close the shared `httpx.AsyncClient`s, the shared `AsyncAnthropic` client, and the
    Postgres pool.

    Called by `Worker.close()` after the in-flight jobs have finished or been cancelled,
    and before arq closes its own Redis connection. Safe if `on_startup` never got as far
    as opening any of these resources: every `ctx.get(...)` below is `None` and skipped when
    that is so, and `close_pool()` is a no-op when there is nothing to close — all of that
    matters because `Worker.run()`'s `finally` calls this even on a failed startup. It is
    also how a flag-off worker reaches this function safely: `ctx.get("anthropic_client")` is
    `None` because `open_worker_resources` never set the key at all, not because startup
    failed partway through — the same `ctx.get(...)` shape, doing double duty.

    Closes `storage_client`, not `storage` — `SupabaseStorage` never owns the client it is
    handed (see its own module docstring), so it has nothing to close itself.

    **`AsyncAnthropic.close()` — a coroutine — not `.aclose()`, unlike every `httpx.AsyncClient`
    above.** The Anthropic SDK's async client spells this method differently from httpx's, and
    copying the `await client.aclose()` idiom used for `http_client`/`storage_client` two lines
    up onto an `AsyncAnthropic` is an `AttributeError` at shutdown — `aclose` does not exist on
    this class. `tests/test_worker_settings.py` pins this with a spy that exposes `close` and
    deliberately NOT `aclose`, so a copy-paste of the wrong name fails the test rather than
    failing silently on the next real deploy.
    """
    logger.info("ARQ worker shutting down")
    http_client = ctx.get("http_client")
    if http_client is not None:
        await http_client.aclose()
    storage_client = ctx.get("storage_client")
    if storage_client is not None:
        await storage_client.aclose()
    anthropic_client = ctx.get("anthropic_client")
    if anthropic_client is not None:
        await anthropic_client.close()
    await close_pool()


# --- the numbers, and where they live now ----------------------------------------------

# EVERY TUNABLE THIS FILE USED TO DECLARE NOW LIVES IN `app/worker/policy.py`, imported
# above and re-exported by nothing — the names below are the same names, with the same
# values and the same arguments attached to them, in a module `app/worker/jobs.py` can also
# import. PER-166 moved them because the stuck-run reaper's staleness threshold is derived
# from `JOB_TIMEOUT_SECONDS`, and the job that applies it is imported BY this module: leaving
# the constant here would have made that derivation a circular import, and restating the
# number in `jobs.py` with a "keep these in sync" comment is exactly the drift the reaper
# spends its life cleaning up after.
#
# The module docstring's warning is unaffected and still absolute: what arq reads is
# `WorkerSettings.__dict__`, so every setting must still be ASSIGNED directly in the class
# body below. Where the value on the right-hand side came from has never mattered —
# `redis_settings` has been a function call since this file was written.


class WorkerSettings:
    """Loaded by `arq app.worker.settings.WorkerSettings`.

    A plain class with class attributes, not a dataclass or a Pydantic model: arq reads
    `__dict__` directly. See the module docstring for why nothing here may be inherited.
    """

    # TLS is decided by the URL scheme inside redis_settings_from_url() — `rediss://` on
    # Upstash, plain `redis://` against the local container — so this one line is correct
    # in both environments and neither hardcodes the other's transport.
    redis_settings = redis_settings_from_url(settings.redis_url)

    # `noop` IS NOT A PLACEHOLDER, NOW THAT `crawl_task` HAS LANDED BESIDE IT.
    # `arq.worker.Worker.__init__` raises `RuntimeError('at least one function or cron_job
    # must be registered')` on an empty list, so emptying this list would not leave an idle
    # worker — it would leave one that crashes before it opens a socket, restart-looping on
    # Fly with no HTTP listener to fail a health check. `noop` is also the probe that makes
    # "enqueue -> picked up" an observable event independent of whether a crawl itself
    # succeeds. See jobs.py.
    functions = [noop, crawl_task]

    # THE CRON TICK. Declared directly on the class, like every other setting here — the
    # module docstring's warning about inherited attributes applies just as much to
    # `cron_jobs` as to `functions` or `poll_delay`, because `arq.worker.get_kwargs()` reads
    # `WorkerSettings.__dict__` for this name exactly like every other one.
    #
    # `second=0` is what "every 60 seconds" means to arq's cron scheduler: it fires once,
    # at second 0 of every minute, forever — not a `while True: sleep(60)` loop this process
    # would have to keep alive itself. That is the whole reason this is a cron job and not a
    # background task the worker starts by hand: arq's scheduler does not drift the way a
    # sleep loop measured against its own wall-clock eventually does, and it survives a
    # worker restart with no state to recover — the next minute boundary just fires it again.
    #
    # `max_tries=1`: a tick that raises is retried by the NEXT tick, one minute later,
    # against WHATEVER STATE IS TRUE THEN — never immediately re-run against the same,
    # possibly partially-advanced state that made it fail the first time. arq's default
    # retry behaviour (backoff and re-run the same job) is right for a job with one clear
    # unit of work to redo; it is wrong for a tick, which would otherwise retry a failure
    # from a few seconds ago instead of re-evaluating what is due right now.
    #
    # `unique=True` (arq's own default, left unset here rather than restated) reduces
    # duplicate ticks across more than one worker process, but it is NOT what makes this
    # tick correct — it is a best-effort lock arq keeps in Redis, and this system does not
    # lean on it. `FOR UPDATE SKIP LOCKED` (`SchedulesReader.lock_due`) is what makes two
    # ticks running at the same instant, on the same worker or two different ones, safe
    # rather than a source of duplicate runs — and it has to be, because arq's uniqueness
    # guarantee is exactly as good as its connection to Redis at the moment it matters.
    #
    # `timeout=CRON_TICK_TIMEOUT_SECONDS` — see that constant's own comment.
    #
    # THE SECOND ENTRY IS THE STUCK-RUN REAPER, and everything the paragraphs above say
    # about the schedule tick applies to it unchanged: same `max_tries=1` for the same
    # reason (a pass that fails is re-evaluated from scratch five minutes later, never
    # re-run against the state it half-left), and the same reliance on `FOR UPDATE SKIP
    # LOCKED` — `RunsReader.lock_reapable` this time — rather than on arq's best-effort
    # `unique=True`.
    #
    # `minute=set(range(0, 60, REAPER_INTERVAL_MINUTES))` with `second=0` is how arq spells
    # "every five minutes": a set of the minute values to fire on, evaluated against local
    # time inside the poll loop. Written as a comprehension over the interval rather than as
    # a literal `{0, 5, 10, ...}` so that the cadence is stated once, in `policy.py`, where
    # `ORPHANED_PENDING_AFTER_SECONDS` and `STUCK_AFTER_SECONDS` are reasoned about beside
    # it — a literal here could drift from the interval those thresholds assume, and the
    # symptom of that drift (a reaper firing more often than its thresholds expect) is
    # exactly nothing until the day it duplicates a crawl.
    #
    # `60 % REAPER_INTERVAL_MINUTES == 0` is not asserted anywhere, and does not need to be:
    # an interval that does not divide the hour simply produces a shorter gap across the
    # hour boundary, which the reaper is entirely indifferent to.
    cron_jobs = [
        cron(schedule_tick, second=0, max_tries=1, timeout=CRON_TICK_TIMEOUT_SECONDS),
        cron(
            reaper_tick,
            minute=set(range(0, 60, REAPER_INTERVAL_MINUTES)),
            second=0,
            max_tries=1,
            timeout=REAPER_TIMEOUT_SECONDS,
        ),
    ]

    # Named differently from the settings they land on, rather than the `on_startup =
    # on_startup` the arq README shows: inside a class body that reads as a self-
    # assignment and only works because the name resolves to the module global.
    on_startup = open_worker_resources
    on_shutdown = close_worker_resources

    poll_delay = POLL_DELAY_SECONDS
    max_jobs = MAX_JOBS
    job_timeout = JOB_TIMEOUT_SECONDS
    job_completion_wait = JOB_COMPLETION_WAIT_SECONDS

    # ARQ'S OWN RETRY CEILING, and it is a BACKSTOP rather than the retry policy itself.
    # arq's default is 5; the policy this system actually runs is `MAX_ATTEMPTS` counted
    # against `runs.attempts`, enforced in `CrawlService`, which stops asking for retries
    # before this number is ever reached on the happy path.
    #
    # Set to the same value anyway, for the case where the two counters legitimately
    # disagree: a job cancelled by SIGTERM increments arq's `job_try` without incrementing
    # `runs.attempts`, so a run redeployed through three times would keep asking for
    # redeliveries that the run's own budget still permits. This caps that at three, after
    # which arq abandons the job id — and the run, left `pending`, is picked up by the
    # reaper's orphan sweep under a fresh id with a fresh `job_try`. Slower than a
    # redelivery, and bounded, which is the trade.
    #
    # NOTE what this does NOT do: arq 0.28 does not retry a job that raises an ordinary
    # exception at all (`Worker.run_job` retries only on `Retry`, `RetryJob`, and
    # `CancelledError`), so raising this number would not make anything retry that does not
    # already ask to.
    max_tries = MAX_ATTEMPTS
