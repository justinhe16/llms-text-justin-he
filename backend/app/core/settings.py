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

    # Sitemap discovery (app.features.crawl.internals.sitemap), which fills the crawl
    # frontier before the loop above runs. Both caps bound what ONE run may spend on
    # discovery, and hitting either is a SUCCESS in the same sense the caps above are: the
    # crawl proceeds with whatever URLs were found, and a site with no sitemap at all still
    # produces a successful single-page run.
    #
    # `crawl_sitemap_max_documents` counts every sitemap-document fetch ATTEMPT — the two
    # well-known probes, every `Sitemap:` target robots.txt declares, and every child of a
    # sitemap index — regardless of whether it 404s, is refused by the SSRF guard, or
    # parses. Counting only successes would let a hostile robots.txt listing ten thousand
    # dead `Sitemap:` lines cost ten thousand requests. robots.txt itself is not counted:
    # it is not a sitemap document and is fetched at most once. Five leaves three documents
    # for a site that falls through both probes to robots.txt.
    crawl_sitemap_max_documents: int = Field(default=5, ge=1)

    # A defensive cap on how many URLs one run's discovery will accumulate, not a tuning
    # knob: the byte budget usually binds first (discovery may spend at most
    # SITEMAP_BYTE_SHARE of crawl_max_bytes, and 12.5 MiB of sitemap XML holds far fewer
    # than 50,000 entries). It exists so that a pathological document cannot turn into an
    # unbounded list in memory even if that arithmetic ever changes.
    crawl_sitemap_max_urls: int = Field(default=50_000, ge=1)

    # Model-assisted per-page summarization (app.features.crawl.internals.enrich), the layer
    # ARCHITECTURE.md §11 describes as sitting ABOVE `internals/llms_txt.py` rather than
    # inside it. Everything below is read by the worker only — the API never touches these.
    #
    # **PER-194 split what this flag means into two levels, and this is the top one.**
    # `crawl_enrich_with_llm` no longer decides, by itself, whether any given website's runs
    # enrich — `websites.enrich_with_llm` (per-website, owner-settable, default `False`) is
    # the other half, and a run enriches only when BOTH are true
    # (`CrawlService.execute_run`'s gate). What this flag alone answers is narrower but still
    # load-bearing: IS model-assisted summarization available in this deployment at all. A
    # website can have `enrich_with_llm = True` in a deployment where this flag is off — that
    # is the ordinary state of "an owner opted in, but nobody has turned enrichment on for
    # this installation yet" — and that run completes normally, falls back to extracted
    # metadata, and records `enrich_unavailable_reason = "deployment_disabled"`
    # (`internals/run_stats.py`'s version-8 paragraph).
    #
    # `crawl_enrich_with_llm` defaults OFF, and OFF is still load-bearing here, not merely
    # cautious, and for the same reason as before PER-194: with the flag off, nothing in this
    # feature constructs an `AsyncAnthropic` client at all (`app/worker/settings.py`'s
    # `open_worker_resources` checks this same flag before calling `build_anthropic_client`),
    # REGARDLESS of how many websites have opted in — so a run's artifact is byte-identical to
    # what the deterministic path already produced for every website in the deployment, and
    # this suite — and CI — never needs a live `ANTHROPIC_API_KEY` to go green. Flip it on and
    # every website that has independently opted in starts paying for a model call per page on
    # its next run; that is a cost decision an operator makes deliberately, not a default this
    # codebase should make for them, and it is still a single deployment-wide switch — there is
    # no per-website override of THIS half of the gate (§11: no per-run override either).
    #
    # Summarization is ALL-OR-NOTHING for a run, by design, and there is deliberately no
    # third "gap-filling" mode that mixes model-written and extracted metadata based on which
    # calls happened to succeed. An index whose entries came from two different authors, with
    # no way for a reader to tell which is which, is worse than either alone: a reader of
    # `llms.txt` cannot tell "the model wrote this" from "trafilatura wrote this" by looking
    # at it, so a run that silently blends the two teaches nobody anything about which pages
    # to trust more. `internals/enrich.py`'s `enrich_pages` reports every page it could not
    # summarize, and `CrawlService.execute_run` falls back to the FULL deterministic artifact
    # rather than a partially-enriched one — see that method for exactly where the line is
    # drawn. That "no gap-filling" rule is about a single run's OWN pages; it says nothing
    # about the per-website opt-in this ticket adds, which is a different axis entirely — a
    # deployment enriching some websites and not others is the intended product, not the
    # blending this paragraph rules out.
    crawl_enrich_with_llm: bool = False

    # The Anthropic API key. Read only when `crawl_enrich_with_llm` is `True` — see
    # `validate_required_secrets` below, which is why this is safe to default to `""` rather
    # than being declared required unconditionally the way `supabase_secret_key` is. Never
    # logged, interpolated into a message, or `repr`'d anywhere this feature touches it
    # (ARCHITECTURE.md §9.4) — `anthropic_client.py`'s own docstring restates this at the one
    # call site that actually reads it.
    anthropic_api_key: str = ""

    # How many `messages.create` calls `internals/enrich.py`'s `enrich_pages` may have in
    # flight at once, per run — mirrors `crawl_concurrency`'s shape for the page fetch loop,
    # but as its own field: the crawl's own concurrency is bounded by what this process's
    # `httpx.AsyncClient` connection pool and the target site's politeness can tolerate,
    # while this one is bounded by what the Anthropic account's own rate limit allows, and
    # the two have no reason to share a number. Defaulted to 10 to match Firecrawl's own
    # batch size for the equivalent step — a number this ticket did not need to reinvent —
    # rather than to any measurement of this codebase's own account limits.
    crawl_enrich_concurrency: int = Field(default=10, ge=1)

    # How many characters of a page's extracted `markdown` are sent to the model, cut AFTER
    # stripping leading/trailing whitespace so a page that is mostly boilerplate at the edges
    # does not spend its whole budget on nothing (`internals/enrich.py`'s `enrich_pages`).
    # This is where this feature's entire cost lives, and the arithmetic is worth stating
    # rather than assuming: `claude-haiku-4-5` is priced at roughly $1 per million input
    # tokens, and 4,000 characters of English prose is on the order of 1,000 tokens — so a
    # 100-page run spends on the order of 100,000 input tokens, a few cents, before the (far
    # smaller) `MAX_TOKENS`-bounded output cost is added on top. Raising this raises that
    # number roughly linearly; it does not change what gets summarized, since a page's title
    # and the gist of its description are almost always decided well within the first few
    # thousand characters of real prose.
    crawl_enrich_max_chars: int = Field(default=4000, ge=1)

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
        # A deliberately obvious `if`, not a ternary folded into the tuple above. Every other
        # variable in `required` is unconditional, but demanding `ANTHROPIC_API_KEY` with
        # `crawl_enrich_with_llm` off would refuse to boot a perfectly correct deployment —
        # including this process's own tests and CI, where the flag is always off and the
        # key is never set. Appended LAST, which keeps `required`'s existing property of
        # matching `backend/.env.example`'s order: this is a later, optional section of that
        # file, added after every unconditional variable above it.
        if self.crawl_enrich_with_llm:
            required = (*required, ("ANTHROPIC_API_KEY", self.anthropic_api_key))
        missing = [name for name, value in required if not value.strip()]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in backend/.env for local development, or with `fly secrets "
                "set` for a deployed environment. See backend/.env.example."
            )


settings = Settings()
settings.validate_required_secrets()
