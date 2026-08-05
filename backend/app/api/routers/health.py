"""Health check endpoint."""

import asyncio
import logging
from typing import Any, Literal

from asyncpg import Pool
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DbPool


logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Not meant to be operator-tunable per environment — it exists to keep one slow database
# from turning `GET /health` itself into the thing that makes Fly's poll time out. `1`
# second is generous for `SELECT 1` against a healthy connection pool and short enough
# that a struggling database doesn't make this endpoint slow too.
_DB_CHECK_TIMEOUT_SECONDS = 1.0


class HealthResponse(BaseModel):
    """Response body for `GET /health`.

    This DTO lives beside its route rather than in `app/features/` because health is an
    operational endpoint, not a product feature with a service, a reader, and a writer.
    It does touch Postgres — a bounded `SELECT 1`, see `_check_db` — but that is a
    liveness probe rather than data access, which is why it has no repository behind it.

    `db` is required, never absent, because the check that produces it always runs —
    there is no code path that returns this response without having attempted it.
    """

    status: Literal["ok", "degraded"]
    db: Literal["ok", "error", "timeout"]


def _discard_outcome(probe: asyncio.Task[Any]) -> None:
    """Consume an abandoned probe's result so asyncio never logs it as unretrieved.

    A probe left pending by `_check_db` below is cancelled but deliberately not awaited.
    Without this callback, a probe that fails (rather than observing its cancellation)
    would surface as a bare "Task exception was never retrieved" traceback at an
    unpredictable later moment, with no indication it came from a health check.

    An abandoned probe that resolves with a real Postgres error rather than its own
    cancellation is saying something more specific than "timed out", so it is logged at
    debug rather than dropped silently — without the DSN, as everywhere else.
    """
    if probe.cancelled():
        return
    error = probe.exception()
    if error is not None:
        logger.debug("Health check: abandoned probe finished with %s", type(error).__name__)


async def _check_db(pool: Pool) -> Literal["ok", "error", "timeout"]:
    """Run `SELECT 1` against the pool, bounded by `_DB_CHECK_TIMEOUT_SECONDS`.

    **Why this does not simply use asyncpg's own `timeout=` argument.** When Postgres
    stops answering (an overloaded server or a network partition, as opposed to a refused
    connection), asyncpg's per-call `timeout=` fires and then tries to cancel the running
    query *gracefully* — which means opening a second connection to the same unresponsive
    server to send a cancellation request. That second connection hangs too, so the call
    that was given a 1s budget can block for tens of seconds. `asyncio.wait_for` around it
    behaves identically, because it awaits that same cancellation before returning.
    Measured against a frozen Postgres: both took >25s for a 1s budget.

    So the probe runs as a task and is waited on with a plain timeout. If it has not
    finished within the budget, it is cancelled and **not awaited** — the endpoint returns
    `"timeout"` immediately and asyncpg's cleanup finishes on its own time. That is what
    keeps a hung database from turning `GET /health` into the slow thing that Fly's own
    poll times out on, which is the entire point of having a budget here.

    An abandoned probe keeps holding its pooled connection until its cancellation
    completes. That is bounded and self-limiting: at worst a sustained outage consumes
    `db_pool_max_size` connections, after which the probe fails fast on acquisition
    instead of on the query, and this endpoint keeps answering within its budget either
    way.
    """
    probe: asyncio.Task[Any] = asyncio.create_task(pool.fetchval("SELECT 1"))
    _, pending = await asyncio.wait({probe}, timeout=_DB_CHECK_TIMEOUT_SECONDS)

    if pending:
        probe.cancel()
        probe.add_done_callback(_discard_outcome)
        logger.warning(
            "Health check: database query exceeded its %.1fs budget",
            _DB_CHECK_TIMEOUT_SECONDS,
        )
        return "timeout"

    try:
        probe.result()
    except Exception:
        # Never log the DSN or any exception attribute that might carry it
        # (ARCHITECTURE.md §9.4) — the exception type and message are enough to act on.
        logger.exception("Health check: database query failed")
        return "error"
    return "ok"


@router.get("/health", response_model=HealthResponse)
async def health(pool: DbPool) -> HealthResponse:
    """Liveness-and-readiness probe, polled by Fly every 10 seconds.

    **The HTTP status is always 200 as long as this process is alive — database health is
    reported in the body, never the status code.** This is deliberate: restarting a
    healthy web machine does nothing to fix a Postgres outage, it just adds a flapping
    fleet of machines on top of one. A non-200 here would make Fly do exactly that.

    The database check (`SELECT 1`, budget `_DB_CHECK_TIMEOUT_SECONDS`) always runs, so
    `db` is always present: `"ok"` when it succeeds, `"timeout"` if it exceeds its budget,
    `"error"` for anything else. `status` is `"ok"` only when `db` is `"ok"`; otherwise
    it's `"degraded"`, so a caller that only reads `status` still gets an accurate signal
    without having to know `db`'s possible values.
    """
    db_status = await _check_db(pool)
    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        db=db_status,
    )
