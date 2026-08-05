"""Shared FastAPI dependencies.

`Annotated` aliases live here so that route handlers read as `settings: SettingsDep`
instead of repeating `Depends(...)` wiring at every call site. `CurrentUser`,
`CurrentUserId`, and `OptionalUser` are defined in `app.core.auth.dependencies` (the JWKS
verification lives there, alongside `app.core.auth.jwks`) and re-exported below so a route
handler imports every dependency from this one module rather than reaching into `app.core`
directly. `app.core` must not import from `app.api` (ARCHITECTURE.md §3.1) — this file
importing *from* `app.core.auth` is the allowed direction.

**This module stays generic.** Settings, the pool, and the current user are needed by every
feature. A *feature's* service provider is not: it lives beside that feature's routes (see
`get_website_service` in `app.api.routers.websites`), so this file never accumulates an
import of every feature in the codebase.
"""

from typing import Annotated

from arq.connections import ArqRedis
from asyncpg import Pool
from fastapi import Depends

from app.core.auth.dependencies import CurrentUser, CurrentUserId, OptionalUser
from app.core.settings import Settings, settings
from app.infrastructure.db.pool import get_pool
from app.infrastructure.queue.pool import get_queue_pool


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


def get_arq_pool() -> ArqRedis:
    """Return the process-wide ARQ Redis pool opened by `app.main`'s lifespan.

    **The API enqueues; it never consumes.** The worker is the only consumer, and it opens
    its own connection (`app.infrastructure.queue.pool` explains why they are separate).

    Raises:
        RuntimeError: if the pool is not open. Unlike `get_db_pool`, that is a **reachable
            production state, not a bug**: the lifespan deliberately boots the API when
            Redis is unreachable so a queue outage does not also take down reads. A route
            that enqueues therefore has to decide what to do about it — the first one to
            exist should catch this and answer `503`, which is why this returns a
            non-optional pool and makes the failure loud rather than handing back `None`
            for every caller to forget to check.
    """
    return get_queue_pool()


def get_optional_arq_pool() -> ArqRedis | None:
    """Return the ARQ Redis pool, or `None` when the lifespan could not open one.

    Exists for `GET /health` alone, and mirrors `OptionalUser`'s reason for existing: the
    health endpoint has to keep answering `200` while reporting that a component is
    unavailable, and it cannot do that if resolving its own dependency raises. Every other
    caller wants `get_arq_pool` above.
    """
    try:
        return get_queue_pool()
    except RuntimeError:
        return None


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbPool = Annotated[Pool, Depends(get_db_pool)]
ArqPool = Annotated[ArqRedis, Depends(get_arq_pool)]
OptionalArqPool = Annotated[ArqRedis | None, Depends(get_optional_arq_pool)]

# Names re-exported for route handlers. Listed explicitly because CurrentUser,
# CurrentUserId, and OptionalUser are imported here purely to be re-exported, and an unused
# import is a lint error without this declaration. Not re-exporting get_current_user_id /
# get_current_user_uuid / get_optional_user_id: tests and dependency overrides key off
# get_jwks_cache instead, so there is exactly one import path for each name.
#
# CurrentUserId is the alias routes should reach for. It is CurrentUser's `sub` parsed to a
# UUID, which is the type every owner column in this schema decodes to — see
# `get_current_user_uuid` for why comparing the raw string would 403 the actual owner.
__all__ = [
    "ArqPool",
    "CurrentUser",
    "CurrentUserId",
    "DbPool",
    "OptionalArqPool",
    "OptionalUser",
    "SettingsDep",
    "get_arq_pool",
    "get_db_pool",
    "get_optional_arq_pool",
    "get_settings",
]
