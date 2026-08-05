"""Shared FastAPI dependencies.

`Annotated` aliases live here so that route handlers read as `settings: SettingsDep`
instead of repeating `Depends(...)` wiring at every call site. The authenticated-user
dependency joins this file in its own ticket.
"""

from typing import Annotated

from asyncpg import Pool
from fastapi import Depends

from app.core.settings import Settings, settings
from app.infrastructure.db.pool import get_pool


def get_settings() -> Settings:
    """Return the process-wide settings.

    Reads are routed through a dependency rather than importing the module-level
    singleton directly so that a test can substitute a different configuration with
    `app.dependency_overrides[get_settings]`.
    """
    return settings


def get_db_pool() -> Pool:
    """Return the process-wide database pool opened by `app.main`'s lifespan.

    Routed through a dependency, like `get_settings` above, so a test can substitute a
    different pool (or a fake) with `app.dependency_overrides[get_db_pool]` instead of
    reaching into the `app.infrastructure.db.pool` module singleton directly.
    """
    return get_pool()


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbPool = Annotated[Pool, Depends(get_db_pool)]
