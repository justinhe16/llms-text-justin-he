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
from typing import Any

from app.core.logging import configure_logging
from app.core.settings import settings
from app.features.crawl.http_client import build_crawl_client
from app.infrastructure.db.pool import close_pool, open_pool
from app.infrastructure.queue.pool import redis_settings_from_url
from app.worker.jobs import crawl_task, noop


logger = logging.getLogger(__name__)

# AT IMPORT, NOT IN on_startup, and not left to arq.
#
# `arq`'s CLI configures logging for the `arq` logger alone, so without this every `app.*`
# log line falls through to a handler-less root logger and Python's `lastResort` fallback:
# INFO is dropped entirely — including the startup messages below — and WARNING and above
# print with no level, name, or timestamp. Doing it here rather than in `on_startup` means
# an exception raised while this module is being imported is still logged properly, and
# that is exactly when a misconfigured worker fails.
configure_logging()

# arq's own handler already prints its messages. Its logger propagates by default, so once
# the root logger above has a handler too, every arq line would appear twice.
logging.getLogger("arq").propagate = False


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
      every other resource it wants to build once instead of per job. `ctx["http_client"]`,
      added below, is the first of those: the crawl task's shared `httpx.AsyncClient`
      (`app.features.crawl.http_client.build_crawl_client`), read by `crawl_task`
      (`jobs.py`) on every call rather than opened fresh per job. One place to add them,
      one place to close them.

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
    # way for this line to fail the way the pool open above can.
    ctx["http_client"] = build_crawl_client(settings)
    logger.info("ARQ worker ready (poll_delay=%ss, max_jobs=%s)", POLL_DELAY_SECONDS, MAX_JOBS)


async def close_worker_resources(ctx: dict[Any, Any]) -> None:
    """Close the shared `httpx.AsyncClient` and the Postgres pool.

    Called by `Worker.close()` after the in-flight jobs have finished or been cancelled,
    and before arq closes its own Redis connection. Safe if `on_startup` never got as far
    as opening either resource: `ctx.get("http_client")` is `None` and skipped, and
    `close_pool()` is a no-op when there is nothing to close — both matter because
    `Worker.run()`'s `finally` calls this even on a failed startup.
    """
    logger.info("ARQ worker shutting down")
    http_client = ctx.get("http_client")
    if http_client is not None:
        await http_client.aclose()
    await close_pool()


# --- the numbers, and why they are these numbers --------------------------------------

# HOW OFTEN THE WORKER ASKS REDIS FOR WORK. THIS IS NOT A TUNING PREFERENCE.
#
# An idle arq worker issues exactly one command per poll — a single ZRANGEBYSCORE over the
# queue's sorted set (`Worker._poll_iteration`); the heartbeat beside it early-returns
# until `health_check_interval` elapses, and there are no cron jobs. So the poll interval
# alone sets the idle command rate, and Upstash bills per command:
#
#     poll_delay = 0.5 (arq's default)  ->  2 polls/s   x 86,400 s = 172,800 commands/day
#     poll_delay = 5   (this setting)   ->  0.2 polls/s x 86,400 s =  17,280 commands/day
#
# Ten times cheaper, for doing nothing at all. What it costs is up to 5 seconds of latency
# before a queued job starts, which is irrelevant against a crawl measured in tens of
# seconds. Anyone tempted to lower this for snappier local development should set it in
# their own environment, not here — tests/test_worker_settings.py asserts this exact value
# because regressing it is silent and shows up as a bill.
POLL_DELAY_SECONDS = 5

# Concurrent jobs per worker machine. Crawls are I/O heavy, so the ceiling is memory and
# Postgres connections rather than CPU.
#
# Note what `db_pool_max_size` is NOT: a shared budget. The API and the worker each build
# their own pool from that one setting, so the number Supabase sees is up to
# `db_pool_max_size` per process — today up to 20, not 10 — and raising it raises both.
# That is comfortable at two concurrent crawls and would not be at ten. If crawl
# concurrency ever grows, the pool sizing needs to become per-process before this does.
MAX_JOBS = 2

# How long one job may run before arq cancels it. Generous against a crawl measured in
# tens of seconds, and the first half of the shutdown budget below.
JOB_TIMEOUT_SECONDS = 180

# GRACEFUL SHUTDOWN, AND THE ORDERING THAT MAKES IT WORK.
#
# Fly sends SIGTERM on every deploy. arq's DEFAULT signal handler cancels in-flight jobs
# immediately; setting `job_completion_wait` to a non-zero value swaps in
# `handle_sig_wait_for_completion`, which instead stops claiming new work
# (`allow_pick_jobs = False`) and waits this long for what is already running to finish.
#
# Three timeouts have to be ordered, or the outermost one truncates the ones inside it:
#
#     JOB_TIMEOUT_SECONDS (180)  <  this (200)  <  fly.toml kill_timeout (240s)  <=  300s
#
# 200 rather than 180 so a job that runs its timeout out still gets a moment to record its
# own failure. `kill_timeout` then sits above both, so Fly's SIGKILL lands after arq has
# finished waiting and `on_shutdown` has closed the pool — not through the middle of a
# crawl. 300s is Fly's ceiling for `kill_timeout`, which is what bounds this whole ladder.
JOB_COMPLETION_WAIT_SECONDS = 200


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

    # Named differently from the settings they land on, rather than the `on_startup =
    # on_startup` the arq README shows: inside a class body that reads as a self-
    # assignment and only works because the name resolves to the module global.
    on_startup = open_worker_resources
    on_shutdown = close_worker_resources

    poll_delay = POLL_DELAY_SECONDS
    max_jobs = MAX_JOBS
    job_timeout = JOB_TIMEOUT_SECONDS
    job_completion_wait = JOB_COMPLETION_WAIT_SECONDS
