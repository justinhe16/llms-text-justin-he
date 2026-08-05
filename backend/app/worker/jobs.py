"""ARQ job functions.

Thin by contract (ARCHITECTURE.md §3.3): a job parses its typed arguments, calls one
service method, and returns. Jobs are enqueued with ids and primitives only — never ORM
objects, never Pydantic models — because everything on the queue is serialized and has to
survive a deploy that changes those classes.

There is exactly one job here today, and it does not call a service because there is no
service for it to call yet. The crawl task lands with its own ticket, behind the
`generate_llms_txt(pages)` seam (§3.4).
"""

import logging
from typing import Any


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
