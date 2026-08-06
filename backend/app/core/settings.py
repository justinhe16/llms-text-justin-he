"""Application settings.

Configuration is read from the environment, with `.env` support for local development,
and validated **at module import**. That timing is deliberate: importing this module is
the first thing both processes that run from this image do, so a misconfigured container
refuses to boot instead of serving a 500 on the first request that happens to need a
missing value. Validating in the API's app factory instead would leave the ARQ worker
unchecked.

Nothing in this module may print or log a configuration *value* — see
[ARCHITECTURE.md §9.4](../../../ARCHITECTURE.md#94-never-echo-or-log-a-secret). The
validation error below names missing variables and nothing else.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for every process that runs from the backend image.

    Both the FastAPI API and the ARQ worker read this one class. Later tickets append
    fields here rather than introducing a second settings object, so that a single file
    describes everything the service needs to boot.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Constrained to the values the code actually branches on, so that a typo
    # (ENVIRONMENT=prod) fails at construction rather than silently missing every
    # `environment ==` comparison downstream.
    environment: Literal["development", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Required in every environment, but declared with an empty default so that
    # construction always succeeds and validate_required_secrets() can report *all*
    # missing variables at once. Declaring them as required Pydantic fields would abort
    # on the first one, costing an operator one restart per missing variable.
    database_url: str = ""
    redis_url: str = ""
    supabase_url: str = ""
    supabase_secret_key: str = ""

    # Optional: error reporting is disabled when this is unset.
    sentry_dsn: str | None = None

    # Pool sizing, kept deliberately small: Fly machines are small, Supabase connection
    # limits are real, and the API and the ARQ worker draw connections from the same
    # budget (both processes construct this same Settings class — see the module
    # docstring). A pool that is too large on either process starves the other, or trips
    # Supabase's own connection cap. See app/infrastructure/db/pool.py for the factory
    # that reads these.
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_command_timeout: float = 30.0

    # Abuse protection for `POST /websites/{id}/runs` (`app.features.runs.service.
    # RunService.trigger_run`). Both caps are per-USER, not per-website — a user with ten
    # websites gets ten websites' worth of manual triggers, not ten independent budgets —
    # and both are enforced by joining `runs` to `websites` on `websites.user_id` rather
    # than by a column on `runs` itself, which has none.
    #
    # `max_concurrent_runs_per_user` counts only `trigger = 'manual'` runs in `pending` or
    # `processing`, deliberately excluding scheduled ones: a website on an hourly schedule
    # would otherwise hold one of these two slots for a large fraction of every hour,
    # which would look like a bug from the UI ("why can't I trigger a run when nothing
    # I started is running?"). See `internals/runs_reader.py` and `RunService` for the
    # matching asymmetry in the duplicate-run guard, which has no such filter.
    #
    # `max_runs_per_day_per_user` counts every trigger — manual and scheduled alike, since
    # a scheduled crawl costs the same worker time a manual one does — over a ROLLING 24h
    # window ending now, not a calendar day. A calendar day needs a timezone to be
    # anchored to, and this product has never asked a user for one; a rolling window needs
    # nothing but `now()`, which every request already has.
    # Both are `ge=1`, and that constraint is load-bearing rather than tidy-minded. A cap of
    # 0 would mean "no manual runs at all", which nothing in the product wants and which
    # nothing in `_enforce_run_caps` is written to survive: the daily branch derives its
    # reset time from `min(started_at)` over the runs inside the window, and a cap of 0
    # makes that branch reachable with no rows in the window at all, where `min()` is NULL
    # and `None + timedelta` is a `TypeError` — a 500 from the code path whose whole job is
    # to answer 429 politely. Constraining it here turns that misconfiguration into a
    # refusal to boot, which is this module's whole thesis (see the module docstring).
    max_concurrent_runs_per_user: int = Field(default=2, ge=1)
    max_runs_per_day_per_user: int = Field(default=50, ge=1)

    def validate_required_secrets(self) -> None:
        """Fail loudly if any required variable is unset, naming every one of them.

        Raises:
            RuntimeError: names each missing environment variable. Values are never
                included in the message — the repository, its CI logs, and its issue
                tracker are all public.
        """
        # Ordered to match backend/.env.example, so the message reads as a checklist
        # against the file an operator is about to edit.
        required: tuple[tuple[str, str], ...] = (
            ("DATABASE_URL", self.database_url),
            ("REDIS_URL", self.redis_url),
            ("SUPABASE_URL", self.supabase_url),
            ("SUPABASE_SECRET_KEY", self.supabase_secret_key),
        )
        missing = [name for name, value in required if not value.strip()]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in backend/.env for local development, or with `fly secrets "
                "set` for a deployed environment. See backend/.env.example."
            )


settings = Settings()
settings.validate_required_secrets()
