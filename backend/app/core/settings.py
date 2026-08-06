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

    # THERE IS NO ERROR-TRACKING SETTING HERE, AND THERE IS NOT MEANT TO BE. `sentry_dsn`
    # was scaffolded in PER-142 for an integration that is not happening: this system's
    # entire observability story is JSON logs on stdout plus correlation ids, read with
    # `fly logs | jq` (app/core/logging.py, ARCHITECTURE.md §3.8). Adding Sentry or an
    # equivalent is its own ticket and its own decision, not a field somebody re-adds
    # because a later ticket's prose mentions one.

    # Pool sizing, kept deliberately small: Fly machines are small, Supabase connection
    # limits are real, and the API and the ARQ worker draw connections from the same
    # budget (both processes construct this same Settings class — see the module
    # docstring). A pool that is too large on either process starves the other, or trips
    # Supabase's own connection cap. See app/infrastructure/db/pool.py for the factory
    # that reads these.
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_command_timeout: float = 30.0

    # Crawl hard caps (app.features.crawl). Hitting one of these is a SUCCESS, not a
    # failure: the crawl returns whatever pages it collected before the cap tripped, and
    # `runs.stats` records which cap (if any) stopped it. These are ordinary `Settings`
    # fields, unlike `worker/settings.py`'s POLL_DELAY_SECONDS/MAX_JOBS, which are
    # deliberately kept off `Settings` for arq-specific reasons documented there — there is
    # no equivalent reason to hide these, and everything else the crawler needs to know is
    # configured the same way.
    crawl_max_pages: int = Field(default=100, ge=1)

    # The crawl's OWN wall-clock budget, and — since PER-166 — the number that actually
    # lands first in a deployed worker. `job_timeout` (app/worker/policy.py) sits at 600s,
    # twice this, so the application-level cap is what ends a long crawl: `crawl_site`'s own
    # `asyncio.timeout` fires, the run records a clean `failed` with a sanitized message, and
    # `runs.stats` says which cap stopped it.
    #
    # **An earlier revision of this comment said the opposite, and told you not to fix it.**
    # `job_timeout` was 180s — BELOW this cap — so arq cancelled every long crawl before its
    # own budget expired, and the note here said not to raise it because it was ordered
    # against `job_completion_wait` (200s) and fly.toml's `kill_timeout` (240s). That
    # ordering constraint turned out to be the thing that was wrong, not the timeout; see
    # `JOB_COMPLETION_WAIT_SECONDS` in app/worker/policy.py for why it was unsatisfiable
    # alongside a crawl cap this size, and tests/test_worker_settings.py for the two
    # assertions that replaced it.
    #
    # arq's timeout is now the outer backstop for a genuinely wedged job. When it does fire,
    # `CancelledError` is caught by `CrawlService.execute_run`, which hands the run back to
    # `pending` (or fails it, if the retry budget is spent) and re-raises — and the stuck-run
    # reaper sweeps the row if even that write does not land.
    crawl_max_wall_clock_s: float = Field(default=300.0, gt=0)

    crawl_max_bytes: int = Field(default=52_428_800, ge=1)  # 50 MiB across the whole run
    crawl_request_timeout_s: float = Field(default=10.0, gt=0)
    crawl_concurrency: int = Field(default=5, ge=1)
    crawl_politeness_delay_ms: int = Field(default=200, ge=0)

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

    # Where a completed run's gzip-compressed JSONL payload is uploaded
    # (app.infrastructure.storage.supabase_storage, read by the worker only — the API never
    # touches Storage). Not a secret, so it is not in validate_required_secrets() below:
    # a wrong bucket name fails loudly the first time an upload 404s, which is close enough
    # to "fails at boot" for a value that is otherwise just a path prefix.
    supabase_storage_bucket: str = Field(default="crawl-payloads", min_length=1)

    # The Storage upload's own timeout — deliberately NOT `crawl_request_timeout_s`, and
    # deliberately below `JOB_TIMEOUT_SECONDS` (600s, app/worker/policy.py). A crawl fetch is
    # one page; an upload can be a multi-megabyte compressed payload, so it gets its own,
    # larger budget. Sitting below the job timeout is what decides which of two things
    # happens to a hung upload: under it, `SupabaseStorage.upload` raises its own
    # `StorageUploadError` first, which `CrawlService` classifies as RETRYABLE — the run goes
    # back to `pending` for another attempt, or ends `failed` if the budget is spent. Above
    # it, arq's job timeout fires first instead and delivers a `CancelledError`, which is a
    # coarser signal: it says the job stopped, not what stopped it, so the run is handed back
    # or failed without a message that names Storage at all.
    #
    # 120s was chosen against the old 180s job timeout, and PER-166's raise to 600s only
    # widens the margin — it is left alone deliberately rather than scaled up with it,
    # because the number a real upload needs did not change.
    storage_upload_timeout_s: float = Field(default=120.0, gt=0)

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
