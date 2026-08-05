"""Health check endpoint."""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Literal

from arq.connections import ArqRedis
from asyncpg import Pool
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DbPool, OptionalArqPool


logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Not meant to be operator-tunable per environment — it exists to keep one slow backing
# service from turning `GET /health` itself into the thing that makes Fly's poll time out.
# `1` second is generous for `SELECT 1` against a healthy connection pool, and for a Redis
# round trip, and short enough that a struggling dependency doesn't make this endpoint slow
# too. The two probes run concurrently, so this is the budget for the endpoint as a whole,
# not per check.
_CHECK_TIMEOUT_SECONDS = 1.0

# What a single dependency's probe can report. Deliberately three values rather than a
# bool: "timeout" and "error" are different operational stories — the first is a dependency
# that is up but not keeping up, the second is one that answered with a failure or could
# not be reached at all.
ComponentStatus = Literal["ok", "error", "timeout"]


class HealthResponse(BaseModel):
    """Response body for `GET /health`.

    This DTO lives beside its route rather than in `app/features/` because health is an
    operational endpoint, not a product feature with a service, a reader, and a writer.
    It does touch Postgres and Redis — a bounded `SELECT 1` and a bounded `PING`, see
    `_probe` — but those are liveness probes rather than data access, which is why there is
    no repository behind them.

    `db` and `redis` are required, never absent, because the checks that produce them
    always run — there is no code path that returns this response without having attempted
    both. `redis` reports `"error"` when the API booted without a queue connection at all,
    which is a startup outcome the lifespan deliberately allows (see `app.main.lifespan`).

    **Field order is part of the contract**, because Pydantic serializes in declaration
    order and two places compare this body as an exact string: the `build-check` job in
    ci-backend.yml, and the one assertion in tests/test_health.py written against the raw
    response text for precisely this reason. The rest of that suite compares parsed dicts,
    which is order-independent, and the `smoke` job in deploy-backend.yml reads `db` and
    `redis` individually with `jq`, which is too — so neither of those would notice a
    reorder, and it is worth knowing which layer is actually holding this invariant.
    """

    status: Literal["ok", "degraded"]
    db: ComponentStatus
    redis: ComponentStatus


def _discard_outcome(probe: asyncio.Task[Any]) -> None:
    """Consume an abandoned probe's result so asyncio never logs it as unretrieved.

    A probe left pending by `_probe` below is cancelled but deliberately not awaited.
    Without this callback, a probe that fails (rather than observing its cancellation)
    would surface as a bare "Task exception was never retrieved" traceback at an
    unpredictable later moment, with no indication it came from a health check.

    An abandoned probe that resolves with a real error rather than its own cancellation is
    saying something more specific than "timed out", so it is logged at debug rather than
    dropped silently — without the DSN, as everywhere else.
    """
    if probe.cancelled():
        return
    error = probe.exception()
    if error is not None:
        logger.debug("Health check: abandoned probe finished with %s", type(error).__name__)


async def _probe(check: Awaitable[Any], component: str) -> ComponentStatus:
    """Await `check` under `_CHECK_TIMEOUT_SECONDS`, reporting the outcome rather than raising.

    **Why this does not use the client library's own `timeout=` argument.** When Postgres
    stops answering (an overloaded server or a network partition, as opposed to a refused
    connection), asyncpg's per-call `timeout=` fires and then tries to cancel the running
    query *gracefully* — which means opening a second connection to the same unresponsive
    server to send a cancellation request. That second connection hangs too, so the call
    that was given a 1s budget can block for tens of seconds. `asyncio.wait_for` around it
    behaves identically, because it awaits that same cancellation before returning.
    Measured against a frozen Postgres: both took >25s for a 1s budget.

    So the probe runs as a task and is waited on with a plain timeout. If it has not
    finished within the budget, it is cancelled and **not awaited** — the endpoint returns
    `"timeout"` immediately and the driver's cleanup finishes on its own time. That is what
    keeps a hung dependency from turning `GET /health` into the slow thing that Fly's own
    poll times out on, which is the entire point of having a budget here.

    An abandoned probe keeps holding whatever it acquired until its cancellation completes.
    That is bounded and self-limiting: at worst a sustained outage consumes
    `db_pool_max_size` connections, after which the probe fails fast on acquisition instead
    of on the query, and this endpoint keeps answering within its budget either way.

    Args:
        check: an already-constructed awaitable — `Awaitable` rather than `Coroutine`
            because redis-py's `ping()` is typed as the former. It is always wrapped in a
            task, so it is never left un-awaited even on the timeout path.
        component: what to call this dependency in a log line. Never a connection string.
    """
    probe: asyncio.Task[Any] = asyncio.ensure_future(check)
    _, pending = await asyncio.wait({probe}, timeout=_CHECK_TIMEOUT_SECONDS)

    if pending:
        probe.cancel()
        probe.add_done_callback(_discard_outcome)
        logger.warning(
            "Health check: %s exceeded its %.1fs budget",
            component,
            _CHECK_TIMEOUT_SECONDS,
        )
        return "timeout"

    try:
        probe.result()
    except Exception:
        # Never log the DSN or any exception attribute that might carry it
        # (ARCHITECTURE.md §9.4) — the exception type and message are enough to act on.
        logger.exception("Health check: %s probe failed", component)
        return "error"
    return "ok"


async def _check_db(pool: Pool) -> ComponentStatus:
    """Run `SELECT 1` against the Postgres pool."""
    return await _probe(pool.fetchval("SELECT 1"), "database")


async def _check_redis(pool: ArqRedis | None) -> ComponentStatus:
    """Run `PING` against the ARQ Redis pool.

    `None` means the lifespan never got a connection open, which is a deliberate,
    survivable startup outcome rather than a bug — so it reports `"error"` like any other
    unreachable Redis, instead of raising and taking the endpoint down with it.
    """
    if pool is None:
        return "error"
    return await _probe(pool.ping(), "redis")


@router.get("/health", response_model=HealthResponse)
async def health(pool: DbPool, arq_pool: OptionalArqPool) -> HealthResponse:
    """Liveness-and-readiness probe, polled by Fly every 10 seconds.

    **The HTTP status is always 200 as long as this process is alive — dependency health is
    reported in the body, never the status code.** This is deliberate: restarting a healthy
    web machine does nothing to fix a Postgres or an Upstash outage, it just adds a flapping
    fleet of machines on top of one. A non-200 here would make Fly do exactly that. The
    `[[http_service.checks]]` block in backend/fly.toml is scoped to the `app` process for
    a related reason — the worker serves no HTTP, so a check against it could only ever
    fail.

    Both checks always run, so `db` and `redis` are always present: `"ok"` when the probe
    succeeds, `"timeout"` if it exceeds its budget, `"error"` for anything else. They run
    **concurrently**, so one slow dependency does not spend the other's budget and the
    endpoint stays inside `_CHECK_TIMEOUT_SECONDS` overall.

    `status` is `"ok"` only when both are `"ok"`; otherwise it's `"degraded"`, so a caller
    that reads only `status` still gets an accurate signal without having to know the
    component values. A degraded queue is a real degradation — the API can still serve every
    read, but it cannot start a crawl — and saying so here is free, precisely because
    nothing restarts a machine over this body. What "non-fatal" buys is the 200, not a
    flattering summary.

    **Redis is probed on every call, which is not free.** Fly polls this every 10s, so the
    check alone costs 6 × 60 × 24 = 8,640 Redis commands a day. That is real, but it is not
    where the money is — an idle worker at `poll_delay = 5` spends twice that (see
    `app.worker.settings` for the arithmetic). If it ever needs to come down, cache the
    probe result for a few seconds here rather than slowing the Fly check, which is also
    the failure detector.
    """
    db_status, redis_status = await asyncio.gather(
        _check_db(pool),
        _check_redis(arq_pool),
    )
    healthy = db_status == "ok" and redis_status == "ok"
    return HealthResponse(
        status="ok" if healthy else "degraded",
        db=db_status,
        redis=redis_status,
    )
