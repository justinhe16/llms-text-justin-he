"""Redis access for the ARQ job queue: one settings translator, one pool factory.

The queue counterpart to `app.infrastructure.db` (ARCHITECTURE.md §3.5). `pool.py` turns
`REDIS_URL` into an `arq.connections.RedisSettings` and owns the process-wide enqueue pool
that `app.main`'s lifespan opens.

**Only the API uses the pool in this package.** The worker does not: `arq.worker.Worker`
opens its own Redis connection from the same `RedisSettings` and hands it to job functions
as `ctx["redis"]`. Both therefore go through `redis_settings_from_url()`, which is the one
place in the codebase that decides whether a connection is TLS.
"""
