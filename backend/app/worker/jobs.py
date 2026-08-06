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
a malformed id is logged and the job returns rather than raising — the same "a worker must
not raise at arq" contract `CrawlService.execute_run` itself follows for every other failure
mode.
"""

import logging
from typing import Any
from uuid import UUID

from app.core.settings import settings
from app.features.crawl.service import build_crawl_service


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
        caller is expected to parse. Never raises for an application-level failure: a bad
        `run_id`, a missing run, a lost claim race, and a failed crawl (including a failed
        Storage upload or database write) are all logged and returned, matching
        `CrawlService.execute_run`'s own "never raise HTTPException at arq" contract.
        `asyncio.CancelledError` (arq's job timeout, or SIGTERM) is not caught here either,
        for the same reason `execute_run` does not catch it.
    """
    try:
        parsed_run_id = UUID(str(run_id))
    except ValueError:
        logger.error("crawl_task: %r is not a valid run id; skipping", run_id)
        return "invalid run id"

    service = build_crawl_service(ctx["db_pool"], ctx["http_client"], ctx["storage"], settings)
    outcome = await service.execute_run(parsed_run_id)

    if outcome is None:
        logger.info(
            "crawl_task: run %s produced no outcome; see CrawlService's own logs for why",
            parsed_run_id,
        )
        return "no outcome"

    logger.info(
        "crawl_task: run %s fetched %d page(s), storage_path=%s",
        parsed_run_id,
        outcome.stats.get("pages_crawled", 0),
        outcome.storage_path,
    )
    return "ok"
