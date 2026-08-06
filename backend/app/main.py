"""FastAPI application factory.

`app.core.settings` is imported at module scope, which runs configuration validation
before the application object exists. A container that boots and then fails every request
is worse than one that refuses to boot.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware.request_context import (
    RequestContextMiddleware,
    request_id_exception_handler,
)
from app.api.routers import health, runs, schedules, websites
from app.core.auth.jwks import close_jwks_cache, create_jwks_client, open_jwks_cache
from app.core.logging import configure_logging
from app.core.settings import settings
from app.infrastructure.db.pool import close_pool, open_pool
from app.infrastructure.queue.pool import close_queue_pool, open_queue_pool


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown.

    Opens three process-wide resources and closes them in reverse: the Postgres pool via
    `open_pool()`, the JWKS cache via `open_jwks_cache()`, and the ARQ Redis pool the API
    enqueues through via `open_queue_pool()`. Each comes from the same factory the ARQ
    worker's startup hook uses (`app.infrastructure.db.pool`, `app.core.auth.jwks`,
    `app.infrastructure.queue.pool`), so the two processes build them identically.
    `close_pool()` awaits `pool.close()` rather than dropping the reference, so in-flight
    connections finish and are released instead of being torn down mid-query.

    **Two of the three are deliberately non-fatal, and which is which is the whole design.**
    Postgres is fatal because nothing works without it. JWKS and Redis are not, because the
    API keeps serving reads without either — see the comments at each call below.
    """
    logger.info("Starting llms-text API (environment=%s)", settings.environment)
    await open_pool(settings)

    # Deliberately the OPPOSITE of open_pool()'s fail-fast above, and the contrast is the
    # point. A bad DATABASE_URL breaks every request for the life of the process, so
    # refusing to boot is the honest response. An unreachable JWKS endpoint is recoverable
    # on the very next request — get_key() refetches when it sees an unknown kid — so
    # refusing to boot on it would turn a transient Supabase blip into an outage of our own
    # making. open_jwks_cache() therefore logs loudly and continues with an empty cache
    # instead of raising; see its docstring for the full reasoning. CI proves this path on
    # every run: build-check's SUPABASE_URL does not resolve, so the app it boots and
    # probes has always taken the degraded branch.
    jwks_client = create_jwks_client()
    await open_jwks_cache(settings, jwks_client)

    # Non-fatal for the same reason the JWKS cache above is, applied to the queue. THE API
    # ONLY ENQUEUES — it never consumes — so an unreachable Redis costs it the ability to
    # start a crawl and nothing else: every read endpoint still works. Refusing to boot
    # here would convert an Upstash outage into a total outage, and it would do it at the
    # worst moment, since a deploy is exactly when the app is being restarted anyway.
    #
    # This is the same judgement `/health` encodes by keeping its HTTP status at 200 while
    # reporting `redis` in the body. The two have to agree: an API that refuses to boot
    # without Redis makes a non-fatal health field meaningless.
    #
    # `arq.create_pool` pings before returning and retries `conn_retries` (5) times a
    # second apart first, so this branch costs up to ~5s of startup when Redis is down —
    # which is why `grace_period` in backend/fly.toml is set well above it. `get_pool()`
    # left unset is what `GET /health` reports as `redis: "error"`.
    try:
        await open_queue_pool(settings)
    except Exception:
        # Never interpolate REDIS_URL or any part of it — it carries the Upstash password
        # and this repository and its logs are public (ARCHITECTURE.md §9.4).
        logger.error(
            "Could not open the ARQ Redis pool. The API is starting anyway and will serve "
            "reads normally, but enqueuing is unavailable and GET /health will report "
            'redis: "error" until Redis is reachable and this process restarts.',
            exc_info=True,
        )

    yield

    logger.info("Shutting down llms-text API")
    await close_queue_pool()
    await close_jwks_cache()
    await jwks_client.aclose()
    await close_pool()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    # Shared with the ARQ worker (app.core.logging), which configures logging for itself
    # because it never imports this module. `"app"` is the process name every line this
    # process emits is tagged with, and it matches backend/fly.toml's `[processes]` key.
    #
    # First, before the FastAPI object exists: uvicorn has already configured its own
    # loggers by the time it imports this module, and `configure_logging` is what pulls
    # them onto the JSON handler, so anything logged between here and the first request is
    # already structured.
    configure_logging("app")
    app = FastAPI(
        title="llms-text API",
        description="Backend for llms-text: website registration, crawl runs, llms.txt generation",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Correlation ids, the X-Request-ID response header, and the one-line request summary.
    # Added before any router, so it wraps every route this application will ever have
    # rather than the ones that happened to be registered first. It adds no route and no
    # schema of its own, so frontend/lib/api/openapi.json is unaffected by it.
    app.add_middleware(RequestContextMiddleware)

    # The 500 an *unhandled* exception produces is written by Starlette's
    # ServerErrorMiddleware, which sits above every middleware added here — see the
    # handler's own docstring for why that means the header has to be stamped again from
    # inside it.
    app.add_exception_handler(Exception, request_id_exception_handler)

    app.include_router(health.router)
    app.include_router(websites.router)
    app.include_router(runs.router)
    app.include_router(schedules.router)
    return app


app = create_app()
