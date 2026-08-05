"""The asyncpg connection pool: one factory, one process-wide singleton.

**Why `DATABASE_URL` must be the Supabase session pooler on port 5432, not the
transaction pooler on port 6543.** Supavisor's transaction mode multiplexes many client
connections onto a smaller number of shared Postgres connections, handing a connection
back to the pool as soon as a transaction ends. asyncpg caches prepared statements on the
physical connection it thinks it owns; under transaction mode that connection can be
handed to a different client between statements, so the cache goes stale and queries fail
intermittently with `prepared statement "__asyncpg_stmt_x__" does not exist` — an error
that only shows up under concurrency, which makes it miserable to reproduce locally.
Session mode gives each client a dedicated Postgres connection for the life of that
connection, which is what asyncpg's statement cache assumes. `create_pool()` below
detects a 6543 URL and disables the cache rather than trusting every caller to remember
this, but the fix that actually matters is using the right URL in the first place.

**Why not Supabase's direct connection (`db.<ref>.supabase.co:5432`) instead of a
pooler.** It is IPv6-only, and GitHub-hosted Actions runners are IPv4-only — a migration
or CI job that dials it directly simply cannot connect. Supavisor's session pooler is
IPv4-reachable, so it is used everywhere, not just from Fly.

`create_pool()` is a pure factory with no module state: it is the function both
`app.main`'s lifespan and a future ARQ worker startup hook call to obtain a pool. Most of
this process's code should not call it directly — use `open_pool()` once at startup and
`get_pool()` everywhere else, which is what makes `app/api/deps.py` able to hand routers a
single shared pool via dependency injection.
"""

import asyncio
import logging
from urllib.parse import urlsplit

import asyncpg
from asyncpg import Pool

from app.core.settings import Settings


logger = logging.getLogger(__name__)

# Supabase's Supavisor pooler in transaction mode listens on this port. See the module
# docstring for why a pool built against it needs `statement_cache_size=0`.
_TRANSACTION_POOLER_PORT = 6543

# How long a graceful `pool.close()` gets before shutdown stops waiting and terminates the
# pool outright. asyncpg's own `Pool.close()` documentation advises wrapping it in a
# timeout, and this service has a specific reason to: a connection can be stuck against an
# unresponsive Postgres (see the abandoned health-check probe in api/routers/health.py),
# and `close()` waits for every holder to be released. Without a bound, one hung
# connection turns SIGTERM into "wait for Fly's kill grace period", which is the abrupt
# teardown a graceful close exists to avoid.
_CLOSE_TIMEOUT_SECONDS = 5.0

# The process-wide pool. Private: read it through get_pool(), never this name directly,
# so every caller goes through the "has it been opened yet?" check.
_pool: Pool | None = None


def _targets_transaction_pooler(database_url: str) -> bool:
    """Return whether `database_url` points at port 6543, the transaction pooler.

    Parses the URL and compares the port rather than scanning for the substring
    `":6543"`, so a password or database name that happens to contain that text cannot
    produce a false positive. `urlsplit` raises `ValueError` on a malformed URL (for
    example, a non-numeric port); if that happens, this falls back to the safe default of
    `False` — treating the URL as session-mode rather than guessing — since a malformed
    URL will fail to connect on its own and surface its own, clearer error.
    """
    try:
        port = urlsplit(database_url).port
    except ValueError:
        return False
    return port == _TRANSACTION_POOLER_PORT


async def create_pool(settings: Settings) -> Pool:
    """Create a new asyncpg pool from `settings`. Called once per process.

    This is a pure factory — it takes settings in and returns a pool, with no reference
    to the module-level singleton below. That is what lets both `app.main`'s lifespan
    (via `open_pool()`) and a future ARQ worker startup hook build a pool the same way
    without importing FastAPI-specific plumbing.
    """
    if _targets_transaction_pooler(settings.database_url):
        # Never interpolate the URL (or any part of it) into this message — it can carry
        # credentials, and this repository and its logs are public (ARCHITECTURE.md §9.4).
        logger.warning(
            "DATABASE_URL targets the Supabase transaction pooler (port 6543). "
            "Transaction mode multiplexes connections per-transaction, which breaks "
            "asyncpg's prepared-statement cache and causes intermittent "
            '"prepared statement does not exist" errors under concurrency. Disabling '
            "the statement cache to compensate — switch to the session pooler on port "
            "5432 to fix this properly."
        )
        # A separate, explicit-kwargs call rather than building a `**kwargs` dict: an
        # unpacked dict defeats asyncpg's overload resolution (mypy cannot tell which of
        # `create_pool`'s overloads a dynamically-typed `**dict[str, int]` is meant to
        # satisfy), where two literal calls type-check cleanly.
        return await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            command_timeout=settings.db_command_timeout,
            statement_cache_size=0,
        )

    return await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=settings.db_command_timeout,
    )


async def open_pool(settings: Settings) -> Pool:
    """Create the process-wide pool and store it for `get_pool()` to return.

    Called exactly once, from `app.main`'s lifespan on startup (and, in the future, from
    the ARQ worker's startup hook).

    Raises:
        RuntimeError: if a pool is already open. Overwriting `_pool` would leak the first
            one — its connections are never closed and stay checked out against Postgres
            until the process dies — and the leak would be invisible. This is the failure
            mode of a worker entrypoint copy-pasted from `app.main` that ends up calling
            this twice, so it fails loudly on the first run instead.
    """
    global _pool
    if _pool is not None:
        raise RuntimeError(
            "The database pool is already open. open_pool() is called once per process "
            "(see app.main.lifespan); call close_pool() before opening another."
        )
    _pool = await create_pool(settings)
    return _pool


def get_pool() -> Pool:
    """Return the process-wide pool opened by `open_pool()`.

    Raises:
        RuntimeError: if called before `open_pool()` has run — a clear failure at the
            call site instead of an `AttributeError` on `None` deep inside a repository.
    """
    if _pool is None:
        raise RuntimeError(
            "The database pool has not been opened yet. Call open_pool(settings) during "
            "application startup (see app.main.lifespan) before get_pool() is used."
        )
    return _pool


async def close_pool() -> None:
    """Close the process-wide pool and clear it. Safe to call more than once.

    Awaits `pool.close()` rather than dropping the reference, so in-flight connections
    finish their work and are released gracefully instead of being torn down mid-query —
    the difference between a clean shutdown and "connection was closed" noise in the logs.

    That graceful close is bounded by `_CLOSE_TIMEOUT_SECONDS`, because it waits for every
    connection to be released and a connection stuck against an unresponsive Postgres
    would never be. On expiry the pool is terminated instead: a shutdown that always ends
    is worth more than the last few connections closing politely, and terminating is what
    `close()` would do internally on error anyway.
    """
    global _pool
    if _pool is None:
        return

    pool, _pool = _pool, None
    try:
        await asyncio.wait_for(pool.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Database pool did not close within %.1fs — terminating it. A connection was "
            "most likely still waiting on an unresponsive database.",
            _CLOSE_TIMEOUT_SECONDS,
        )
        pool.terminate()
