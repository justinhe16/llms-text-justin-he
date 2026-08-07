"""The single `AsyncAnthropic` client every enrichment request is issued through.

Built once per worker process (`app/worker/settings.py`'s `open_worker_resources`, guarded
on `settings.crawl_enrich_with_llm`) and threaded through `internals/enrich.py`'s
`enrich_pages`, the same way the process shares one `httpx.AsyncClient` for crawl fetches
rather than opening a connection per job.

Lives at the top of the feature, not in `internals/`, because `app/worker/settings.py`
constructs it and `internals/` is private to this feature (ARCHITECTURE.md §3.1) — a module
another feature's setup code has to import cannot also be one only this feature may import.
Sibling of `http_client.py`, and deliberately the same shape.
"""

from typing import Final

from anthropic import AsyncAnthropic

from app.core.settings import Settings


ANTHROPIC_REQUEST_TIMEOUT_S: Final = 15.0
"""The per-request budget for one `messages.create` call, in seconds.

**Not the SDK's own default, which is unusable here.** `AsyncAnthropic`'s default request
timeout is ten minutes — sized for a caller who has nothing else running and can afford to
wait, which is not this process. Enrichment is a phase inside `CrawlService.execute_run`,
which itself runs after `crawl_site`'s own 300-second wall-clock cap
(`Settings.crawl_max_wall_clock_s`) and before the Storage upload's own 120-second budget
(`Settings.storage_upload_timeout_s`) — all three sit inside arq's outer 600-second
`job_timeout` (`app/worker/policy.py`). A ten-minute per-request timeout would let one stuck
`messages.create` call outlive the entire job on its own, taking the crawl's already-fetched
pages down with it. 15 seconds is generous for a `MAX_TOKENS=100` completion over at most
`crawl_enrich_max_chars` characters of input, and it is a per-request bound only —
`ENRICH_WALL_CLOCK_S` (`internals/enrich.py`) is the separate, whole-phase bound that keeps
a run of many slow-but-individually-successful requests from eating the job budget instead.

Declared here, alongside the client that is timed with it, rather than as a fourth
`Settings` field — this is a property of how patient this codebase is willing to be with
Anthropic's API, not a knob an operator should be tuning per environment. Precedent:
`internals/sitemap.py`'s `SITEMAP_BYTE_SHARE`."""


def build_anthropic_client(settings: Settings) -> AsyncAnthropic:
    """Build the shared `AsyncAnthropic` client `internals/enrich.py`'s `enrich_pages` calls
    through.

    Called ONLY when `settings.crawl_enrich_with_llm` is `True` — see
    `app/worker/settings.py`'s `open_worker_resources`, which guards this call on that same
    flag rather than building the client unconditionally the way `build_crawl_client` and
    `build_storage_client` are. With the flag off, `settings.anthropic_api_key` may be empty
    (`validate_required_secrets` only demands it when the flag is on), and constructing a
    client from an empty key would be pointless work behind a feature nothing will ever use.

    Constructing this client opens no socket and makes no network call — the same property
    `build_crawl_client`'s own docstring documents for the crawl's `httpx.AsyncClient` — so
    `on_startup` cannot fail on it, and the guard above is about avoiding a useless client
    when the flag is off, not about avoiding a startup failure when it is on.

    **`max_retries` is left at the SDK's own default of 2, not overridden here.** Retry
    LADDERS of this codebase's own design are out of scope for this ticket (CLAUDE.md #9's
    "real crawling logic is out of scope" reasoning extends to this layer above it); the
    SDK's built-in retry on a 429 or a transient 5xx is behaviour the pinned dependency
    already ships, bounded by `ANTHROPIC_REQUEST_TIMEOUT_S` per attempt and by
    `ENRICH_WALL_CLOCK_S` for the phase as a whole, and a rate limit that clears on a quick
    retry is exactly the case enrichment should quietly survive rather than counting as a
    per-page failure. A reviewer who disagrees can flip this to `max_retries=0` in one line;
    it is called out here so that disagreement is easy to find and act on.

    The key is read from `settings.anthropic_api_key` and handed straight to the SDK
    constructor — never logged, never interpolated into a message, never `repr`'d anywhere
    in this module or its caller (ARCHITECTURE.md §9.4). `AsyncAnthropic` itself is the only
    thing that ever sees the raw value after this call returns.
    """
    return AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=ANTHROPIC_REQUEST_TIMEOUT_S)
