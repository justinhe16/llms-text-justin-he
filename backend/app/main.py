"""FastAPI application factory.

`app.core.settings` is imported at module scope, which runs configuration validation
before the application object exists. A container that boots and then fails every request
is worse than one that refuses to boot.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import health
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

    Opens the process-wide Postgres pool via `open_pool()` on startup and closes it via
    `close_pool()` on shutdown — the same factory (`app.infrastructure.db.pool`) a future
    ARQ worker startup hook will call, so the API and the worker build their pools
    identically. `close_pool()` awaits `pool.close()` rather than dropping the reference,
    so in-flight connections finish and are released instead of being torn down mid-query.
    The Redis connection is opened and closed here by its own infrastructure ticket.
    """
    logger.info("Starting llms-text API (environment=%s)", settings.environment)
    await open_pool(settings)
    yield
    logger.info("Shutting down llms-text API")
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
    return app


app = create_app()
