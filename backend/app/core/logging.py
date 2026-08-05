"""Logging configuration, shared by both processes that run from the backend image.

It lives in `app.core` rather than in `app.main` because **the ARQ worker never imports
`app.main`**, and a worker with no logging configuration is close to a worker with no
logging: `arq`'s CLI calls `dictConfig` with a config that names only the `arq` logger
(`arq.logs.default_log_config`), so everything this codebase logs under `app.*` falls
through to a root logger with no handlers. Python's `lastResort` handler then prints
WARNING and above with no timestamp, no logger name, and no level — and silently drops
every INFO line, including the worker's own "ready" message and `LOG_LEVEL` entirely.

Nothing here may log or format a configuration *value* — see
[ARCHITECTURE.md §9.4](../../../ARCHITECTURE.md#94-never-echo-or-log-a-secret).
"""

import logging

from app.core.settings import settings


def configure_logging() -> None:
    """Send `app.*` logs to stderr at `LOG_LEVEL`. Idempotent.

    `basicConfig` is a no-op once the root logger has handlers, which is what makes this
    safe to call from both `app.main.create_app()` and the worker's settings module
    without the two fighting over the root logger.
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )
