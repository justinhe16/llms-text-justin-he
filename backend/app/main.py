"""FastAPI application factory.

`app.core.settings` is imported at module scope, which runs configuration validation
before the application object exists. A container that boots and then fails every request
is worse than one that refuses to boot.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import health, websites
from app.core.auth.jwks import close_jwks_cache, create_jwks_client, open_jwks_cache
from app.core.settings import settings
from app.infrastructure.db.pool import close_pool, open_pool


logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure root logging from settings.

    Log the environment, never the settings object: it holds credentials, and this
    repository and its logs are public (ARCHITECTURE.md §9.4).
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown.

    Opens the process-wide Postgres pool via `open_pool()` and the process-wide JWKS cache
    via `open_jwks_cache()` on startup, and closes both on shutdown — the same factories
    (`app.infrastructure.db.pool`, `app.core.auth.jwks`) a future ARQ worker startup hook
    will call, so the API and the worker build them identically. `close_pool()` awaits
    `pool.close()` rather than dropping the reference, so in-flight connections finish and
    are released instead of being torn down mid-query. The Redis connection is opened and
    closed here by its own infrastructure ticket.
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

    yield

    logger.info("Shutting down llms-text API")
    await close_jwks_cache()
    await jwks_client.aclose()
    await close_pool()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    _configure_logging()
    app = FastAPI(
        title="llms-text API",
        description="Backend for llms-text: website registration, crawl runs, llms.txt generation",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(websites.router)
    return app


app = create_app()
