"""The ARQ Redis pool: one settings translator, one factory, one process-wide singleton.

Deliberately shaped like `app.infrastructure.db.pool`, because it solves the same problem
for the other backing service: a pure factory both processes can call, plus an
`open`/`get`/`close` singleton for the one process that needs a long-lived handle.

**Who uses what.** `redis_settings_from_url()` is called by *both* processes — the API to
build its enqueue pool, the worker (via `app.worker.settings`) to tell `arq` where the
queue is. The pool functions below are used by the **API only**: `arq.worker.Worker` opens
its own connection and publishes it to job functions as `ctx["redis"]`, so a worker that
also called `open_queue_pool()` would hold two connections to the same Redis for no reason.

**Why TLS is derived from the URL scheme and never hardcoded.** Production is Upstash over
`rediss://`; local development is the `redis:7-alpine` container in docker-compose.yml over
plain `redis://`. `RedisSettings.from_dsn()` already reads the scheme and sets `ssl`
accordingly, which is why this module wraps it rather than assembling `RedisSettings`
field by field — a hardcoded `ssl=True` would break `make dev`, and a hardcoded
`ssl=False` would silently send the Upstash password in cleartext.
"""

import logging
from urllib.parse import urlsplit

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.settings import Settings


logger = logging.getLogger(__name__)

# The URL scheme that means "this connection is TLS". `RedisSettings.from_dsn` accepts
# `redis`, `rediss` and `unix`; only this one turns encryption on.
_TLS_SCHEME = "rediss"

# The process-wide enqueue pool. Private: read it through get_queue_pool(), never this
# name directly, so every caller goes through the "has it been opened yet?" check.
_queue_pool: ArqRedis | None = None


def redis_settings_from_url(redis_url: str) -> RedisSettings:
    """Translate `REDIS_URL` into the `RedisSettings` both processes connect with.

    `RedisSettings.from_dsn()` does the parsing, including setting `ssl=True` for a
    `rediss://` URL and leaving it off for `redis://` — which is what keeps one code path
    working against both Upstash and the local container.

    This adds exactly one thing on top: **hostname verification, which arq leaves off.**
    `RedisSettings.ssl_check_hostname` defaults to `False`, so out of the box a `rediss://`
    connection validates that the certificate chains to a trusted CA but not that the
    certificate was issued for the host we dialled. That is most of the way to no
    authentication at all on a connection that carries the Redis password, and it is
    switched on here rather than at the call sites so there is no way to get an
    unverified TLS connection out of this codebase.

    It is applied *only* to the TLS case. Setting it on a plain `redis://` connection
    would be meaningless, and asserting the difference is what the local-development test
    in tests/test_worker_settings.py checks.

    Raises:
        RuntimeError: from `from_dsn`, if the scheme is not redis/rediss/unix. Never
            include `redis_url` in an error or log message — it carries the password, and
            this repository and its logs are public (ARCHITECTURE.md §9.4).
    """
    redis_settings = RedisSettings.from_dsn(redis_url)
    if urlsplit(redis_url).scheme == _TLS_SCHEME:
        redis_settings.ssl_check_hostname = True
    return redis_settings


async def create_queue_pool(settings: Settings) -> ArqRedis:
    """Create a new ARQ Redis pool from `settings`. Called once per process.

    A pure factory with no reference to the singleton below, for the same reason
    `app.infrastructure.db.pool.create_pool` is one: it is the seam a test can call
    directly with a throwaway configuration.

    `arq.create_pool` pings before returning and retries up to `RedisSettings.conn_retries`
    (5, one second apart) first, so a pool handed back from here has genuinely connected —
    it is not a lazy handle that fails on first use.
    """
    return await create_pool(redis_settings_from_url(settings.redis_url))


async def open_queue_pool(settings: Settings) -> ArqRedis:
    """Create the process-wide enqueue pool and store it for `get_queue_pool()` to return.

    Called exactly once, from `app.main`'s lifespan on startup. The worker does not call
    this — see the module docstring.

    Raises:
        RuntimeError: if a pool is already open, matching `open_pool()`'s contract in
            `app.infrastructure.db.pool`. Overwriting the reference would leak the first
            pool's connections for the life of the process, invisibly.
    """
    global _queue_pool
    if _queue_pool is not None:
        raise RuntimeError(
            "The queue pool is already open. open_queue_pool() is called once per process "
            "(see app.main.lifespan); call close_queue_pool() before opening another."
        )
    _queue_pool = await create_queue_pool(settings)
    return _queue_pool


def get_queue_pool() -> ArqRedis:
    """Return the process-wide enqueue pool opened by `open_queue_pool()`.

    Raises:
        RuntimeError: if called before `open_queue_pool()` has run, or after Redis failed
            to connect at startup. The API boots without Redis on purpose (see
            `app.main.lifespan`), so this is a reachable state in production rather than a
            programming error, and the message says which of the two it is.
    """
    if _queue_pool is None:
        raise RuntimeError(
            "The queue pool is not open. Either open_queue_pool(settings) has not run yet "
            "(see app.main.lifespan), or it failed to reach Redis at startup and the API "
            "booted without a queue — check GET /health, whose `redis` field reports "
            "exactly that."
        )
    return _queue_pool


async def close_queue_pool() -> None:
    """Close the process-wide enqueue pool and clear it. Safe to call more than once.

    `close_connection_pool=True` closes the underlying connection pool rather than just
    returning this client's connection to it. Without it every deploy would leave the
    sockets of the outgoing process open until Redis timed them out — the leak the ticket
    calls out, and one that compounds because deploys are frequent and Upstash caps
    concurrent connections.
    """
    global _queue_pool
    if _queue_pool is None:
        return

    pool, _queue_pool = _queue_pool, None
    await pool.aclose(close_connection_pool=True)
