"""`CrawlService.execute_run` — what `app.worker.jobs.crawl_task` calls, and the only place
in this feature that calls another feature's service.

**This feature owns no table**, so it reads and writes `runs` and `websites` through
`RunService` and `WebsiteService`, never through a reader or writer of its own
(ARCHITECTURE.md §3.1: "if feature A needs data owned by feature B, it calls B's service").
`build_crawl_service` below constructs both from the pool the worker hands it, the same way
`app.api.routers.runs.get_run_service` builds a `WebsiteService` alongside a `RunService` for
the API.

**The network calls happen outside every transaction** (ARCHITECTURE.md §5.1). `crawl_site`
and the Storage upload both run between the atomic claim (`RunService.claim_for_processing`,
its own short transaction) and whatever happens after them (`RunService.record_success` or
`RunService.record_failure`, a second, independent transaction) — never inside either one.
`CrawlService` itself opens no transaction at all; every one this method touches belongs to
`RunService`.

**A successful run uploads its payload, then writes the row — never the other order.** Once
`internals/llms_txt.py` has produced both artifacts — the `llms.txt` index and its
`llms-full.txt` expansion, along with the two counts `runs.stats` records about them — this
method gzips the fetched pages as JSONL (`internals/payload.py`), uploads that to Supabase
Storage, and only THEN opens the short transaction that writes `llms_txt`, `llms_full_txt`,
`storage_path`, `stats`, and flips the row to `completed` (`RunService.record_success`).
Artifact generation is pure and happens entirely before the upload, so it neither extends the
time a transaction is open nor depends on one. That order, and not its reverse, is what
ARCHITECTURE.md §5.1 exists to require: uploading first means the worst case of a
mid-pipeline failure is an
orphaned Storage object costing a fraction of a cent, while writing the row first would risk
a `completed` run whose `storage_path` points at an object that was never actually written —
a 404 in the UI on a run the database swears succeeded. Nothing about this method holds a
database transaction open while the upload is in flight; `RunService.record_success` opens
its own transaction only after `await self._storage.upload(...)` has already returned.

**PER-180 added a THIRD network call to that same no-transaction window: flag-gated,
model-assisted enrichment (`internals/enrich.py`'s `enrich_pages`), between artifact
generation and the Storage upload.** It is the one call in this window that CANNOT fail the
run — unlike the Storage upload, whose failure is `StorageUploadError` and ends the attempt
one way or another, a bad or unreachable model call degrades silently to the deterministic
artifact `internals/llms_txt.py` has always produced. See `execute_run`'s own comment at the
call site for exactly where it sits and why nowhere else would do.

**PER-194 turned that flag-gate into a TWO-LEVEL one, and taught this method to say why an
enrichment request went unfulfilled.** `Settings.crawl_enrich_with_llm` used to be the whole
gate; now it is one of two conditions `execute_run` reads, the other being the run's own
website's `enrich_with_llm` column — a run enriches only when BOTH are true. The gate is
still `if enrich_requested:` first, so a website that never opted in still runs the identical
no-op branch it always did, and the deployment flag being off is still what keeps this
codebase's test suite and CI from ever needing a live `ANTHROPIC_API_KEY`, exactly as before.
What is new is that a website CAN ask for enrichment and not get it — the deployment flag is
off, no `AsyncAnthropic` client was built at worker boot, or the pass ran and produced nothing
usable — and every one of those three cases still completes the run, falls back to extracted
metadata, and records ONE of `"deployment_disabled"` / `"no_api_key"` / `"api_error"` in
`runs.stats["enrich_unavailable_reason"]`, alongside `enrich_requested`/`enrich_applied`
(`internals/run_stats.py`'s version-8 paragraph has the full decision table). Deciding the
reason is entirely this method's job — `internals/enrich.py` itself gets zero edits for this
ticket, which is the concrete meaning of "do not widen it".

**PER-191: `robots.txt` is obeyed, and this method owns the one shared `PolitenessGate`
(`internals/fetcher.py`) both discovery and the crawl loop wait on.** `execute_run` builds one
gate BEFORE calling `discover_sitemap_urls` — at `Settings.crawl_politeness_delay_ms`, the
operator-configured floor — hands it to that call, then WIDENS the SAME gate, once, to
`internals/robots.py`'s `effective_crawl_delay_ms(configured_ms, discovery.robots
.crawl_delay_s)` before handing it to `crawl_site`. That single widening call is the entire
implementation of "a site's `Crawl-delay` slows the CRAWL phase, never discovery's own
remaining fetches" (`internals/robots.py`'s module docstring works through why the split has
to be this way). The seed's own disallow check is symmetric with the pipeline this method
already runs: `discover_sitemap_urls` returns `DiscoveryResult.robots`, this method threads it
into `select_urls(..., robots=...)` for frontier filtering and into `crawl_site(...,
is_allowed=robots.is_allowed)` for the seed — one parsed `RobotsRules`, two consumers, neither
of which re-fetches or re-parses anything.

**PER-193 added a FOURTH call to that same window: `_build_index_diff`'s read of the previous
completed run, via `RunService.get_previous_completed_index`.** It is a database read rather
than an external HTTP call, but the rule it sits under is the same one — no transaction may be
open while it runs, and it runs strictly after `generate_llms_txt` has produced this run's own
index and strictly before the Storage upload. Like enrichment, it CANNOT fail the run:
`_build_index_diff` catches every `Exception` the read raises and degrades to `index_diff =
None`, on the same reasoning enrichment's own belt-and-braces catch gives — a diff is derived
bookkeeping, and the artifact is the product.

**PER-194 retrofits that diff onto a world where the artifact's title and description can be
rewritten independently of the URLs it lists.** `index_diff`'s `pages_changed`/`changed_sample`
are renamed to `metadata_changed`/`metadata_changed_sample`, because title and description are
the only thing enrichment ever touches, and this method now also computes a body-fingerprint
side-channel — `internals/index_diff.py`'s `build_content_hashes(result.pages)`, called
`content_hashes` here, HASHED FROM `result.pages`, NEVER `artifact_pages` — and passes both it
and this run's own `enrich_applied` into `_build_index_diff`, which threads the previous run's
own two fields (read alongside `urls_discovered` out of the same `runs.stats` `_build_
index_diff` already fetches) into `internals/index_diff.py`'s `build_index_diff`. When an
enrichment mode change is detected, only `metadata_changed` is marked not-comparable; every
other signal — added, removed, `urls_discovered_delta`, the new `content_changed`,
`sections_delta`, `selection_churn` — is unaffected and reports normally, because none of them
depends on what a bullet's title or description says.
"""

import asyncio
import logging
from dataclasses import replace
from typing import Any, Final, Literal
from uuid import UUID

import httpx
from anthropic import AsyncAnthropic
from asyncpg import Pool
from fastapi import HTTPException

from app.core.settings import Settings
from app.features.crawl.internals.crawler import (
    CrawlLimits,
    CrawlResult,
    RobotsDisallowedError,
    crawl_site,
)
from app.features.crawl.internals.enrich import apply_summaries, enrich_pages
from app.features.crawl.internals.fetcher import (
    ByteBudget,
    ByteBudgetExceededError,
    FetchError,
    PolitenessGate,
)
from app.features.crawl.internals.index_diff import (
    PreviousIndex,
    build_content_hashes,
    build_index_diff,
)
from app.features.crawl.internals.links import extract_links
from app.features.crawl.internals.llms_txt import (
    count_full_txt_truncations,
    count_indexed_pages,
    generate_llms_full_txt,
    generate_llms_txt,
)
from app.features.crawl.internals.payload import (
    PAYLOAD_CONTENT_TYPE,
    payload_object_path,
    serialize_payload,
)
from app.features.crawl.internals.robots import ALLOW_ALL, RobotsRules, effective_crawl_delay_ms
from app.features.crawl.internals.run_stats import build_run_stats
from app.features.crawl.internals.sitemap import DiscoverySource, discover_sitemap_urls
from app.features.crawl.internals.ssrf import SsrfBlockedError
from app.features.crawl.internals.url_ranking import DiscoveredUrl, SelectionResult, select_urls
from app.features.crawl.schemas import CrawledPage, CrawlOutcome
from app.features.runs.schemas import RunDetailResponse
from app.features.runs.service import RunService
from app.features.websites.service import WebsiteService
from app.infrastructure.storage.supabase_storage import StorageUploadError, SupabaseStorage


logger = logging.getLogger(__name__)

RunDiscoverySource = DiscoverySource | Literal["links"]
"""Where one run's frontier came from — `internals/sitemap.py`'s four entry points, plus the
one this service adds on top of them.

`"links"` is not a fifth SITEMAP entry point and deliberately does not live in that module's
own `DiscoverySource`: nothing in `internals/sitemap.py` can produce it, and widening that
type would make every `match` over it in that module have to answer for a value it never
emits. This service is the one place that runs both discovery paths and therefore the one
place that can name all five outcomes, so the union is declared here.

`internals/run_stats.py`'s `build_run_stats` takes this as a plain `str`, on purpose and for
a reason its own docstring gives — it is a pure module and may not import an I/O one. That
makes this alias the only type-checked spelling of the vocabulary, which is exactly why it is
a `Literal` union rather than a second `str`: a typo in `"sitemap_index"` or `"links"` is
caught here or nowhere."""

# `runs.error` is readable by every signed-in user (ARCHITECTURE.md §4.1) and rendered in
# the UI. 500 characters is generous for any of the fixed strings below — including
# SsrfBlockedError's own message, the one case that is not a fixed string — while still
# bounding what a future exception type could accidentally leak if someone adds one to
# `_safe_error_message` without thinking about its `str()`.
_MAX_ERROR_LENGTH: Final = 500

# What `runs.error` says when a run is cancelled on its LAST attempt — arq's `job_timeout`
# fired, or a deploy's SIGTERM landed after `job_completion_wait` expired, and there is no
# budget left to try again. Deliberately says what happened rather than blaming the site: the
# site may have been perfectly fine, and telling a user their crawl timed out when in fact we
# redeployed underneath it would be a lie they could act on.
_CANCELLED_MESSAGE: Final = "The run was interrupted before it could finish."


class TransientCrawlError(Exception):
    """Raised by `CrawlService.execute_run` when this attempt failed transiently and the run
    has budget left — the run is ALREADY back in `pending` by the time this leaves the
    service, and the only thing left to arrange is a redelivery.

    **`app.worker.jobs.crawl_task` translates this into `arq.worker.Retry`, and that
    translation is the job's, not this service's.** Deciding *whether* an attempt deserves
    another go is a fact about crawling; deciding *how long to wait* and *how to tell the
    queue* are facts about arq, and `app/features/` may not import `app/worker/`
    (ARCHITECTURE.md §3.3). The exception is the seam between the two, exactly as a router
    turning a service's return value into a status code is.

    This is the inverse of the shape PER-166's ticket describes ("raise a distinct exception
    type for permanent failures"), and deliberately so: arq 0.28 does **not** retry on an
    arbitrary exception — `Worker.run_job` retries only on `Retry`, `RetryJob`, or
    `CancelledError`, and treats everything else as a terminal job failure. So in this
    library a permanent failure is the one that needs no exception at all, and a retryable
    one is the case that has to say so out loud.
    """

    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        # The run's `runs.attempts` value for the attempt that just failed. Carried on the
        # exception so `crawl_task` can size the backoff from the RUN's counter rather than
        # from arq's `ctx["job_try"]`: the reaper re-enqueues runs under fresh job ids, which
        # resets `job_try` to 1 and would restart the backoff ladder from ten seconds on a
        # run that has already been failing for an hour.
        self.attempts = attempts


# Exception types a second attempt could plausibly answer differently. Anything not on this
# list is permanent — see `_is_retryable`.
_RETRYABLE_EXCEPTIONS: Final = (
    # Both spellings of "too slow", for the reason `_safe_error_message` gives below: httpx
    # times out one request, `crawl_site`'s outer `asyncio.timeout` times out the whole run
    # and raises the builtin.
    httpx.TimeoutException,
    TimeoutError,
    # `ConnectError` is also what a DNS resolution failure surfaces as (httpx wraps
    # `socket.gaierror`), which the ticket names separately but which has no separate type.
    # `TransportError` is its parent and catches the rest of the transport layer — read
    # errors, protocol errors, and the TLS failures httpx has no dedicated class for.
    httpx.TransportError,
    # Supabase Storage being briefly unavailable says nothing about whether this site can be
    # crawled. The crawl itself already succeeded when this fires.
    StorageUploadError,
)


def _is_retryable(exc: Exception) -> bool:
    """Would waiting and trying again plausibly produce a different outcome?

    **Retryable** is the narrow, enumerated set above: the network was uncooperative, or
    Storage was. **Permanent** is everything else, and the defaulting direction is the whole
    point of the function. Three of the failures this crawler can actually produce are
    permanent by construction and would be pure latency to retry:

    * `SsrfBlockedError` — the URL resolves somewhere it is not allowed to. It will resolve
      there again in ten seconds, and again in sixty. The ticket's headline example.
    * `FetchError` — a redirect loop, or a redirect this crawler refuses to follow. A
      property of the site's configuration, not of this moment.
    * `ByteBudgetExceededError` — the site is bigger than `crawl_max_bytes`. It will still be.
    * `RobotsDisallowedError` (PER-191) — the site's own `robots.txt` disallows the seed URL.
      It will still be disallowed in sixty seconds; nothing about waiting changes what an
      operator's own policy file says.

    An exception this function has never heard of is treated as permanent too. That is the
    conservative choice in the direction that matters: an unrecognized exception is far more
    likely to be a bug in this codebase than a blip on the network, and retrying a bug three
    times over five minutes delays an honest `failed` row by five minutes to reach the
    identical outcome. The cost of getting it wrong in this direction is a run that fails
    once when it might have succeeded on a second try; the cost of the other direction is
    every bug in the crawl path becoming a five-minute-slow bug.

    **A 4xx or 5xx from the target site is not on either list, because it is not an exception
    here at all.** `internals/fetcher.py` returns a `CrawledPage` carrying `status` rather
    than raising for a non-2xx response, so "404 on the seed URL" and "5xx from the target
    site" — which the ticket classifies as permanent and retryable respectively — currently
    reach this service as a *successful* crawl of a page. Turning them into failures at all
    is a crawler-pipeline decision, and that milestone is not designed (CLAUDE.md #9,
    ARCHITECTURE.md §3.4). When it is, the classification belongs here, in this function.
    """
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def _attempt_message(message: str, attempts: int) -> str:
    """Prefix a terminal failure with the attempt count, once there is more than one.

    "Failed after 3 attempts: The site took too long to respond." reads very differently from
    the same sentence on its own, and that difference is the point — it is what lets the UI,
    and whoever is reading the row, tell a site that is genuinely down apart from a one-off
    blip. A first-and-only attempt gets no prefix: "Failed after 1 attempts" would be noise
    on every permanent failure, which is most of them.

    Truncated to `_MAX_ERROR_LENGTH` after the prefix is applied, never before, so the bound
    on what reaches `runs.error` holds for the string actually stored.
    """
    if attempts <= 1:
        return message[:_MAX_ERROR_LENGTH]
    return f"Failed after {attempts} attempts: {message}"[:_MAX_ERROR_LENGTH]


def _safe_error_message(exc: Exception) -> str:
    """Map `exc`'s TYPE to a fixed, safe-to-display string. Never `str(exc)`.

    An `httpx` exception's own message routinely carries internal hostnames, resolved IP
    addresses, and socket-level detail — exactly what a crawl target's operator, or a
    curious signed-in user, should never read back out of `runs.error`. `SsrfBlockedError`
    is the one exception whose own message is safe verbatim: it describes the URL the run
    itself is crawling, a fact its owner already knows, never anything about this process.

    `StorageUploadError` gets its own fixed string, same as every other branch below except
    `SsrfBlockedError` — `SupabaseStorage.upload`'s own message already excludes the service
    key and the request URL (see that module's docstring), but "already safe" is not the bar
    this function holds every other exception type to, and there is no reason to make an
    exception for this one. It is checked before the `httpx` branches for readability only:
    `StorageUploadError` is this codebase's own type, never an `httpx` one, so there is no
    overlap between it and the `isinstance` checks that follow.
    """
    if isinstance(exc, SsrfBlockedError):
        message = str(exc)
    elif isinstance(exc, StorageUploadError):
        message = "Could not store this run's output."
    elif isinstance(exc, RobotsDisallowedError):
        message = "This site's robots.txt disallows crawling this URL."
    elif isinstance(exc, httpx.TimeoutException | TimeoutError):
        # Both spellings, because two different layers time a crawl out and they do not
        # share a base class: httpx raises `httpx.TimeoutException` when one request exceeds
        # the client's per-request timeout, while `crawl_site`'s outer `asyncio.timeout`
        # produces the builtin `TimeoutError` when the whole run exceeds its wall-clock
        # budget with the seed still in flight.
        message = "The site took too long to respond."
    elif isinstance(exc, httpx.ConnectError):
        message = "Could not connect to the site."
    elif isinstance(exc, httpx.TransportError):
        # Everything else httpx raises at the transport layer, TLS failures included: httpx
        # has no dedicated TLS exception, so those surface as either ConnectError (caught
        # above) or a plainer TransportError depending on where the handshake failed.
        message = "A network error occurred while fetching the site."
    elif isinstance(exc, FetchError):
        message = "The site sent too many redirects, or an invalid one."
    elif isinstance(exc, ByteBudgetExceededError):
        message = "The site's response was too large."
    else:
        message = "An unexpected error occurred while crawling the site."
    return message[:_MAX_ERROR_LENGTH]


class CrawlService:
    """Business logic for one crawl run. Constructed once per job by `build_crawl_service`."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        storage: SupabaseStorage,
        run_service: RunService,
        website_service: WebsiteService,
        settings: Settings,
        *,
        anthropic_client: AsyncAnthropic | None = None,
    ) -> None:
        self._client = client
        self._storage = storage
        self._runs = run_service
        self._websites = website_service
        self._settings = settings
        # `None` when `settings.crawl_enrich_with_llm` is off, or in every test that builds a
        # `CrawlService` without one — `execute_run`'s enrichment guard checks both the flag
        # and this attribute before ever calling `enrich_pages`, so a service built with no
        # client behaves exactly like one built with the flag off, regardless of which the
        # caller forgot.
        self._anthropic = anthropic_client

    async def execute_run(self, run_id: UUID, *, max_attempts: int) -> CrawlOutcome | None:
        """Crawl the website behind `run_id`, and return what it produced — or `None`.

        `None` covers every path that is not "a crawl actually ran and produced an
        outcome": the run no longer exists, it was not `pending`, another worker already
        claimed it, or something failed terminally. A worker's job function
        (`app.worker.jobs.crawl_task`) never raises an `HTTPException` at arq, so every one
        of those paths is logged here and turned into a plain return instead.

        **Two exceptions now leave this method, and neither is an application error.**
        PER-166 amended the older "a worker must not raise at arq" contract, which was
        written when a raise had nowhere useful to go:

        * `TransientCrawlError` — a transient failure with attempts left on the run's budget. The run
          is already back in `pending` before this is raised; `crawl_task` turns it into
          `arq.worker.Retry`, which is the only way to ask arq 0.28 for a redelivery.
        * `asyncio.CancelledError` — arq's `job_timeout`, or a deploy's SIGTERM landing after
          `job_completion_wait` expired. **This is now caught, acted on, and re-raised**,
          where it used to propagate untouched. Catching it does not race the signal the way
          the old docstring feared: the recovery write is one short, guarded statement, and
          if it does not land the stuck-run reaper still sweeps the row minutes later.

          **The two causes are NOT redelivered the same way, and the difference is arq's, not
          this method's.** `arq.worker.Worker.run_job` runs the job as
          `await asyncio.wait_for(task, timeout_s)`, and it retries only on `Retry`,
          `RetryJob`, and `CancelledError`:

          - **SIGTERM** cancels `run_job`'s own frame, so arq sees `CancelledError`, logs
            "cancelled, will be run again", and re-queues the job under the same id. The run
            is `pending` and the redelivery is immediate — which is the difference between a
            deploy costing a user five minutes and costing them nothing they can see.
          - **`job_timeout`** cancels the INNER task, and `asyncio.wait_for` converts that
            into `TimeoutError` at its own call site once the coroutine finishes unwinding.
            `TimeoutError` is not in arq's retry set, so arq records the job as failed and
            does NOT redeliver it. The run is still left correctly `pending` by the recovery
            below — what picks it up is the reaper's ORPHAN sweep, within
            `REAPER_INTERVAL_MINUTES`, not arq.

          Either way the database ends in a state something acts on, which is the property
          that matters; the second path is simply slower. It is also the rarer one by
          construction — `job_timeout` (600s) sits above the crawl's own 300s cap, so a
          normal slow crawl fails through `TransientCrawlError` long before arq's timeout is
          reachable at all. `tests/test_crawl_retry.py` pins arq's actual behaviour here
          against a real `Worker` rather than assuming it.

        Args:
            run_id: The run to crawl.
            max_attempts: The retry budget, `MAX_ATTEMPTS` from `app/worker/policy.py`,
                passed in by the job. Not imported: nothing under `app/features/` may import
                `app/worker/` (ARCHITECTURE.md §3.3), and a test wants to set it to 1.

        Returns:
            A `CrawlOutcome` if the seed was fetched, an artifact was generated, it was
            uploaded to Storage, and the `runs` row was written `completed` — by the time
            this returns non-`None`, all of that has already happened (see the module
            docstring). `None` covers every other path, including a Storage upload or a
            database write that failed terminally after a successful crawl.

        Raises:
            TransientCrawlError: transient failure, attempts remaining, run already returned to
                `pending`.
            asyncio.CancelledError: re-raised after the run has been handed back or failed.
        """
        try:
            run = await self._runs.get_run(run_id)
        except HTTPException:
            # `run_id` is deliberately not interpolated here, or in any log line below it in
            # this method: `crawl_task` binds it with `run_id_context` around this entire
            # call, so `app.core.logging.JsonFormatter` already attaches it to every line —
            # repeating it in the message text would just be `jq`-unfriendly duplication.
            #
            # Permanent, and not merely by classification: there is no row left to record a
            # failure against, which is why this returns before anything is claimed.
            logger.warning("crawl: run no longer exists; skipping")
            return None

        if run.status != "pending":
            # The readable guard. `claim_for_processing` below is the one that is actually
            # correct under concurrent delivery — this one only makes the common case fail
            # fast and say why in the log. `status` is a structured value, so it goes in
            # `extra` rather than only inside the message.
            logger.info("crawl: run is not pending; skipping", extra={"status": run.status})
            return None

        # THE ATTEMPT NUMBER COMES FROM THE DATABASE, not from arq's `ctx["job_try"]`. The
        # claim increments `runs.attempts` inside the same atomic UPDATE that wins the race
        # (`RunsWriter._CLAIM_PENDING`), so this number is the run's own, is visible to the
        # reaper running in another process, and cannot be inflated by a job that lost the
        # race. See `MAX_ATTEMPTS` in app/worker/policy.py for the full argument.
        attempts = await self._runs.claim_for_processing(run_id)
        if attempts is None:
            logger.info("crawl: lost the claim race to another worker; skipping")
            return None

        # Hoisted above the `try` so the `except` below can build a partial `stats` dict from
        # whatever this run actually managed before it failed, rather than reporting nothing
        # at all. `result` stays `None` for a failure that never got as far as `crawl_site`
        # returning (e.g. `get_website` raising); `links_emitted` and `full_txt_truncated`
        # stay 0 for a seed failure, where `result.stats["pages_crawled"]` is 0 anyway, and
        # for any failure that happened before the artifacts were generated. They are hoisted
        # rather than defaulted inside `build_run_stats` so that a partial-failure row carries
        # the same KEYS as a success row — the shape `runs.stats` stores must not depend on
        # how far a run got (`internals/run_stats.py`, `RUN_STATS_VERSION`).
        result: CrawlResult | None = None
        links_emitted = 0
        full_txt_truncated = 0
        # Same reasoning again, for the four PER-180 counters: they stay 0 on every path that
        # never reached enrichment — a seed failure, a run with the flag off, a run where
        # `self._anthropic` is `None` — for exactly the reason `links_emitted` and
        # `full_txt_truncated` above stay 0 on those same paths. The shape `runs.stats`
        # stores must not depend on how far a run got, and that now includes whether it got
        # as far as calling a model.
        pages_enriched = 0
        enrich_failures = 0
        enrich_input_tokens = 0
        enrich_output_tokens = 0
        # Same hoisting reasoning again, for the two PER-193 keys: `llms_txt_bytes` stays 0
        # and `index_diff` stays `None` on every path that never generated an index — a seed
        # failure, or any failure before `generate_llms_txt` ran — for the identical reason
        # `links_emitted` does. The shape `runs.stats` stores must not depend on how far a
        # run got, and that now includes whether it got as far as diffing its own index.
        llms_txt_bytes = 0
        index_diff: dict[str, Any] | None = None
        # PER-194's own hoisted locals, same reasoning: `enrich_requested` is set the moment
        # the website is fetched (below) and stays whatever it was on every later path;
        # `enrich_applied` and `enrich_unavailable_reason` stay at their "never asked, never
        # ran, nothing to report" defaults on every path that never reached the enrichment
        # gate; `content_hashes` stays `{}` on every path that never fetched a page.
        enrich_requested = False
        enrich_applied = False
        enrich_unavailable_reason: str | None = None
        content_hashes: dict[str, str] = {}
        outcome: CrawlOutcome | None = None
        # Same hoisting reasoning as the three locals above: a seed failure never reaches the
        # `discover_sitemap_urls`/`select_urls` call below, so `build_run_stats` in that path
        # needs values to report rather than a `NameError`. "None" / 0 / 0 is exactly what a
        # run that discovered nothing (or never got to discover) looks like — and it is also
        # what a run whose seed failed reports for the LINK fallback below, which never runs
        # at all without a fetched seed page to read.
        discovery_source: RunDiscoverySource = "none"
        urls_discovered = 0
        urls_selected = 0
        # PER-191's own hoisted locals, for the same reason as the three block above: a seed
        # failure — including one CAUSED by this ticket's own disallowed-seed check — still
        # needs a shape `build_run_stats` can report. `crawl_delay_ms` defaults to the
        # operator's own configured floor, which is exactly the answer for a run that never
        # got as far as reading a `robots.txt` at all.
        urls_robots_disallowed = 0
        crawl_delay_ms = self._settings.crawl_politeness_delay_ms
        robots: RobotsRules = ALLOW_ALL
        # Set by the failure paths below when the attempt should be retried, and raised
        # AFTER the `try` rather than from inside it. Raising `TransientCrawlError` from within the
        # `try` would be caught by this method's own `except Exception` and turned into a
        # terminal failure — the retry would quietly become the very thing it exists to
        # avoid.
        pending_retry: TransientCrawlError | None = None
        try:
            website = await self._websites.get_website(run.website_id)
            # THE OWNER'S INTENT, read the moment it is known. `enrich_requested` never
            # changes again after this line, on any path — a run's gate decision is a fact
            # about the website AT THE START of this attempt, not something that could shift
            # underneath it if an owner toggled the setting mid-run (PER-194).
            enrich_requested = website.enrich_with_llm

            # Hoisted so the "crawl starting" line below and the `crawl_site` call right
            # after it are guaranteed to describe the same caps — building `CrawlLimits`
            # twice here would risk the log claiming one set of numbers while the run
            # actually executed under another.
            limits = CrawlLimits.from_settings(self._settings)
            logger.info(
                "crawl: starting",
                extra={
                    "url": website.url,
                    "max_pages": limits.max_pages,
                    "max_bytes": limits.max_bytes,
                    "max_wall_clock_s": limits.max_wall_clock_s,
                    "attempt": attempts,
                    "max_attempts": max_attempts,
                },
            )

            # THREE network calls now, all outside every transaction (ARCHITECTURE.md §5.1).
            # Discovery never fails a run: `discover_sitemap_urls` returns an empty result
            # for a 404, malformed XML, an SSRF refusal, or an exhausted byte share, and logs
            # the reason at WARNING — see that module's own docstring. `budget` is built
            # BEFORE discovery runs and handed to both `discover_sitemap_urls` and
            # `crawl_site`, so the two share one run-wide byte cap rather than each getting its
            # own (`crawl_site`'s `budget` parameter docstring has the other half of this).
            # `gate` is the run's one shared `PolitenessGate` (PER-191) — built here, at the
            # operator-configured floor, handed to discovery first, then WIDENED (never
            # replaced) to this run's own `Crawl-delay`-aware delay before `crawl_site` gets
            # it. See the module docstring's PER-191 paragraph for the shape this makes.
            budget = ByteBudget(limits.max_bytes)
            gate = PolitenessGate(self._settings.crawl_politeness_delay_ms / 1000)
            discovery = await discover_sitemap_urls(
                self._client, website.origin, budget=budget, settings=self._settings, gate=gate
            )
            robots = discovery.robots
            crawl_delay_ms = effective_crawl_delay_ms(
                self._settings.crawl_politeness_delay_ms, robots.crawl_delay_s
            )
            if crawl_delay_ms != self._settings.crawl_politeness_delay_ms:
                logger.info(
                    "crawl: robots.txt requested a longer delay than configured; widening "
                    "the politeness gate for the crawl phase",
                    extra={
                        "configured_delay_ms": self._settings.crawl_politeness_delay_ms,
                        "crawl_delay_ms": crawl_delay_ms,
                    },
                )
            # Widened once, here — BETWEEN discovery and the crawl loop, never while either is
            # in flight (`PolitenessGate.widen_to`'s own docstring). `limits` is rebuilt with
            # the same value so `CrawlLimits.politeness_delay_ms` and the gate actually in
            # force never disagree about what this run's crawl phase used.
            gate.widen_to(crawl_delay_ms / 1000)
            limits = replace(limits, politeness_delay_ms=crawl_delay_ms)

            selection = select_urls(
                discovery.urls, seed_url=website.url, limit=limits.max_pages - 1, robots=robots
            )
            discovery_source = discovery.source
            urls_discovered = len(discovery.urls)
            urls_selected = len(selection.selected)
            urls_robots_disallowed = selection.dropped.get("robots_disallowed", 0)

            # THE DEPTH-1 LINK FALLBACK (PER-178), and its one trigger condition.
            #
            # A site with no sitemap has no frontier to hand `crawl_site` up front, and the
            # only place its links exist is in the seed page's own HTML — which does not
            # exist until `crawl_site` has fetched it. So the fallback is passed IN, as a
            # synchronous callback `crawl_site` invokes once on the fetched seed page. Sync,
            # not async, deliberately: a function that cannot `await` cannot make an HTTP
            # request, so "this costs no extra fetch" is a property of the type rather than
            # a rule someone has to remember (the ticket's [Cost] criterion).
            #
            # `not discovery.urls` — not `not selection.selected` — is the condition, and the
            # difference is the whole "sitemap wins" ordering. A sitemap that lists only
            # `/tag/` pages leaves `selection.selected` empty while `discovery.urls` is not;
            # that site HAS a sitemap and its operator's answer to "which pages matter" was
            # "these, and ranking dropped them all". Falling back to scraped links there
            # would quietly overrule a statement the site actually made.
            link_selection: SelectionResult | None = None

            def frontier_from_seed(seed_page: CrawledPage) -> list[str]:
                """Rank the seed page's own links into a frontier. Called at most once, by
                `crawl_site`, and only after the seed fetch has already succeeded."""
                nonlocal link_selection
                link_selection = self._select_seed_links(
                    seed_page, limit=limits.max_pages - 1, robots=robots
                )
                return link_selection.selected

            result = await crawl_site(
                self._client,
                website.url,
                limits=limits,
                extra_urls=selection.selected,
                frontier_from_seed=None if discovery.urls else frontier_from_seed,
                budget=budget,
                gate=gate,
                is_allowed=robots.is_allowed,
            )

            if link_selection is not None and link_selection.discovered_count > 0:
                # The same rule `internals/sitemap.py` applies to its own four entry points:
                # a source is named only when it actually YIELDED at least one candidate, and
                # a path that ran and found nothing reports `"none"`. That keeps one reading
                # true across all five values — `discovery_source == "none"` means this run
                # found no frontier anywhere, rather than meaning "no sitemap specifically" —
                # and it is why the check is on `discovered_count` (what extraction found)
                # rather than on `selected` (what ranking then kept), exactly as a sitemap
                # whose every URL is dropped by ranking still reports `"sitemap"`.
                discovery_source = "links"
                urls_discovered = link_selection.discovered_count
                urls_selected = len(link_selection.selected)
                urls_robots_disallowed = link_selection.dropped.get("robots_disallowed", 0)

            if result.seed_error is not None:
                # WARNING, not ERROR, and a message of its own: a site that is down is an
                # ordinary outcome of crawling the internet, not an incident. It goes through
                # the same `_finish_failed_attempt` as an unexpected exception because the
                # retry decision is identical — only the log line differs. A disallowed seed
                # (PER-191) gets its OWN line, without `exc_info` — there is no traceback
                # worth a stack, just a site's own policy file saying no.
                if isinstance(result.seed_error, RobotsDisallowedError):
                    logger.warning(
                        "crawl: robots.txt disallows the seed URL; not crawling",
                        extra={"url": website.url},
                    )
                else:
                    logger.warning(
                        "crawl: could not fetch its seed URL (%s)",
                        _safe_error_message(result.seed_error),
                        exc_info=result.seed_error,
                    )
                pending_retry = await self._finish_failed_attempt(
                    run_id,
                    result.seed_error,
                    attempts=attempts,
                    max_attempts=max_attempts,
                    stats=build_run_stats(
                        result.stats,
                        links_emitted=links_emitted,
                        full_txt_truncated=full_txt_truncated,
                        discovery_source=discovery_source,
                        urls_discovered=urls_discovered,
                        urls_selected=urls_selected,
                        urls_robots_disallowed=urls_robots_disallowed,
                        crawl_delay_ms=crawl_delay_ms,
                        pages_enriched=pages_enriched,
                        enrich_failures=enrich_failures,
                        enrich_input_tokens=enrich_input_tokens,
                        enrich_output_tokens=enrich_output_tokens,
                        llms_txt_bytes=llms_txt_bytes,
                        index_diff=index_diff,
                        enrich_requested=enrich_requested,
                        enrich_applied=enrich_applied,
                        enrich_unavailable_reason=enrich_unavailable_reason,
                        content_hashes=content_hashes,
                    ),
                )
            else:
                # ENRICHMENT, FIRST THING IN THIS BRANCH, BEFORE `generate_llms_txt`. This is
                # the only place it can sit: `generate_llms_txt`/`generate_llms_full_txt` read
                # `title`/`description` straight off each `CrawledPage` (`internals/llms_txt.py`),
                # so a summary applied after either had already run would never reach the
                # artifact it was supposed to improve. ARCHITECTURE.md §11 requires this to be
                # a layer ABOVE `internals/llms_txt.py`, never inside it — `artifact_pages`
                # below, not `result.pages` itself, is what carries that layering: the seam's
                # own module stays completely unaware a model was ever involved. And exactly
                # like the upload two dozen lines down, NO TRANSACTION IS OPEN HERE and none
                # may be opened around it (ARCHITECTURE.md §5.1) — this call sits in the same
                # network-calls-outside-every-transaction window the Storage upload already
                # occupies, between the atomic claim and `RunService.record_success`.
                #
                # THE TWO-LEVEL GATE (PER-194). `enrich_requested` alone used to be enough to
                # decide whether to call the model — now it is only the OWNER'S half of the
                # question, and `if enrich_requested:` is what makes the deployment flag off
                # (or the website never having opted in) byte-identical to today: with
                # `enrich_requested` `False`, nothing below this line runs, `artifact_pages`
                # stays `result.pages`, and every counter below stays at its hoisted default —
                # the identical no-op this branch was before this ticket existed. Only once a
                # website HAS asked does this branch look at whether the deployment can
                # actually provide it, and record which of the three reasons it could not, if
                # any.
                artifact_pages = result.pages
                if enrich_requested:
                    if not self._settings.crawl_enrich_with_llm:
                        enrich_unavailable_reason = "deployment_disabled"
                    elif self._anthropic is None:
                        enrich_unavailable_reason = "no_api_key"
                    else:
                        try:
                            enrichment = await enrich_pages(
                                self._anthropic, result.pages, settings=self._settings
                            )
                        except Exception:
                            # BELT-AND-BRACES. `enrich_pages` itself never raises for an API
                            # reason (its own module docstring), so reaching this branch means
                            # a bug in `internals/enrich.py` rather than an ordinary model
                            # failure — and this codebase's rule is still "a model can never
                            # fail a run" (ARCHITECTURE.md §11), so that has to be true
                            # structurally, not merely true as long as that module stays
                            # bug-free. `artifact_pages` is left at `result.pages`, exactly the
                            # fallback a per-page failure inside `enrich_pages` already
                            # produces, so this branch and that one are indistinguishable to
                            # everything downstream of it.
                            #
                            # `asyncio.CancelledError` is a `BaseException`, not an
                            # `Exception`, and is therefore DELIBERATELY NOT caught by this
                            # clause — arq's job timeout and a deploy's SIGTERM still reach
                            # this method's own `except asyncio.CancelledError` handler
                            # unchanged, exactly as they would if this call were not here at
                            # all.
                            logger.error("crawl: enrichment failed unexpectedly", exc_info=True)
                            enrich_unavailable_reason = "api_error"
                        else:
                            artifact_pages = apply_summaries(result.pages, enrichment.summaries)
                            pages_enriched = len(enrichment.summaries)
                            enrich_failures = enrichment.failures
                            enrich_input_tokens = enrichment.input_tokens
                            enrich_output_tokens = enrichment.output_tokens
                            enrich_applied = pages_enriched > 0
                            # A run that requested enrichment, ran the pass cleanly, and had
                            # NOTHING to send — every page's markdown was empty after
                            # truncation — is `enrich_applied=False, reason=None`: nothing
                            # failed, there was simply nothing to summarize. Only a request
                            # that was attempted and produced no usable summary earns
                            # `"api_error"` here.
                            if not enrich_applied and enrich_failures > 0:
                                enrich_unavailable_reason = "api_error"
                            logger.info(
                                "crawl: enrichment complete",
                                extra={
                                    "pages_enriched": pages_enriched,
                                    "enrich_failures": enrich_failures,
                                    "enrich_input_tokens": enrich_input_tokens,
                                    "enrich_output_tokens": enrich_output_tokens,
                                    "enrich_applied": enrich_applied,
                                    "enrich_unavailable_reason": enrich_unavailable_reason,
                                },
                            )

                # All four calls are pure and none touches the network, so generating the
                # artifacts here — before the upload, and long before any transaction — costs
                # the run nothing it was not already going to spend. `links_emitted` is asked
                # of the artifact rather than derived from `len(result.pages)`, which is what
                # it used to be: the index omits pages with no extractable content, and the
                # only component that knows how many it omitted is the one that built it
                # (`internals/llms_txt.py`, and `RUN_STATS_VERSION` 3 for what that changed).
                # Reads `artifact_pages`, not `result.pages` — see the enrichment comment
                # above for why the two can differ, and `serialize_payload` a few lines down
                # for why the PAYLOAD deliberately does not follow this same substitution.
                llms_txt = generate_llms_txt(artifact_pages)

                # THE DIFF READ. No transaction is open here, and none may be opened around
                # it — see the module docstring's "PER-193 added a FOURTH call" paragraph.
                # Sits between generating this run's own index and everything downstream of
                # it (the full-text expansion, the payload, the upload): the diff needs only
                # `llms_txt` and `urls_discovered`, both already in hand, so there is no
                # reason to delay it behind work it does not depend on.
                llms_txt_bytes = len(llms_txt.encode())
                # `result.pages`, never `artifact_pages` — enrichment rewrites title and
                # description and never markdown, so the two produce identical hashes here,
                # but reading the original, pre-enrichment list is what makes "the body
                # fingerprint is mode-independent" a property of this code rather than a
                # coincidence a future refactor could quietly break (`build_content_hashes`'s
                # own docstring makes the same point from the other side).
                content_hashes = build_content_hashes(result.pages)
                index_diff = await self._build_index_diff(
                    run,
                    run_id,
                    llms_txt,
                    urls_discovered,
                    content_hashes=content_hashes,
                    enrich_applied=enrich_applied,
                )

                llms_full_txt = generate_llms_full_txt(artifact_pages)
                links_emitted = count_indexed_pages(artifact_pages)
                full_txt_truncated = count_full_txt_truncations(artifact_pages)

                # `result.pages` — the ORIGINAL, extracted pages, never `artifact_pages` — is
                # deliberate and asymmetric with the four calls just above it. The payload is
                # this run's archival record, "meant to outlive today's extractor"
                # (`CrawledPage.markdown`'s own docstring) — a future re-extraction pass reads
                # it to reproduce what THIS extractor produced, and overwriting an archived
                # extracted title with a model-written one would destroy that record
                # permanently and conflate "what this feature extracted" with "what a model
                # wrote about it," a distinction nothing downstream of Storage could ever
                # recover once it was gone.
                payload = serialize_payload(result.pages)
                object_path = payload_object_path(run.website_id, run_id)

                # THE NETWORK CALL. No transaction is open here, and none may be opened
                # around it (ARCHITECTURE.md §5.1). Upload first: an orphaned object costs a
                # fraction of a cent, while a committed row pointing at an object that does
                # not exist is a 404 in the UI on a run the database swears succeeded.
                storage_path = await self._storage.upload(
                    object_path, payload, content_type=PAYLOAD_CONTENT_TYPE
                )
                # `len(payload)` is a number already sitting in hand, not a re-derivation —
                # this stays cheap. The payload itself is never logged: it is every fetched
                # page's content, gzip-compressed, and a page's content has no business
                # appearing in `fly logs`, which every signed-in user's crawl targets share.
                logger.info(
                    "crawl: uploaded payload to storage",
                    extra={"bytes": len(payload), "storage_path": storage_path},
                )

                stats = build_run_stats(
                    result.stats,
                    links_emitted=links_emitted,
                    full_txt_truncated=full_txt_truncated,
                    discovery_source=discovery_source,
                    urls_discovered=urls_discovered,
                    urls_selected=urls_selected,
                    urls_robots_disallowed=urls_robots_disallowed,
                    crawl_delay_ms=crawl_delay_ms,
                    pages_enriched=pages_enriched,
                    enrich_failures=enrich_failures,
                    enrich_input_tokens=enrich_input_tokens,
                    enrich_output_tokens=enrich_output_tokens,
                    llms_txt_bytes=llms_txt_bytes,
                    index_diff=index_diff,
                    enrich_requested=enrich_requested,
                    enrich_applied=enrich_applied,
                    enrich_unavailable_reason=enrich_unavailable_reason,
                    content_hashes=content_hashes,
                )
                await self._runs.record_success(
                    run_id,
                    llms_txt=llms_txt,
                    llms_full_txt=llms_full_txt,
                    storage_path=storage_path,
                    stats=stats,
                )

                # `result.stats` already has exactly the six keys `runs.stats` wants named
                # (`pages_crawled`, `pages_failed`, `bytes_fetched`, `duration_ms`, `cap_hit`,
                # `pages_empty_content`) — spread rather than restated by hand, so this line
                # can never drift from the shape `internals/crawler.py` actually produces.
                # `storage_path` is the one field that is not already on it.
                logger.info(
                    "crawl: completed", extra={**result.stats, "storage_path": storage_path}
                )
                outcome = CrawlOutcome(
                    llms_txt=llms_txt,
                    llms_full_txt=llms_full_txt,
                    stats=stats,
                    storage_path=storage_path,
                )
        except asyncio.CancelledError:
            # arq's `job_timeout`, or SIGTERM after the drain budget expired. Handled and
            # RE-RAISED — see the method docstring. `_recover_cancelled_attempt` never
            # raises, so nothing here can mask the cancellation or replace it with a
            # different exception on the way out.
            await self._recover_cancelled_attempt(
                run_id, attempts=attempts, max_attempts=max_attempts
            )
            raise
        except Exception as exc:
            # `exc_info=True` stays: `fly logs` is the only log surface this system has and
            # there is no error-tracking service on either side of it holding a second copy
            # (`app.core.logging`'s own module docstring), so the full traceback this one
            # line carries is the only record of the failure that will ever exist.
            logger.error("crawl: failed unexpectedly", exc_info=True)
            partial_stats = (
                build_run_stats(
                    result.stats,
                    links_emitted=links_emitted,
                    full_txt_truncated=full_txt_truncated,
                    discovery_source=discovery_source,
                    urls_discovered=urls_discovered,
                    urls_selected=urls_selected,
                    urls_robots_disallowed=urls_robots_disallowed,
                    crawl_delay_ms=crawl_delay_ms,
                    pages_enriched=pages_enriched,
                    enrich_failures=enrich_failures,
                    enrich_input_tokens=enrich_input_tokens,
                    enrich_output_tokens=enrich_output_tokens,
                    llms_txt_bytes=llms_txt_bytes,
                    index_diff=index_diff,
                    enrich_requested=enrich_requested,
                    enrich_applied=enrich_applied,
                    enrich_unavailable_reason=enrich_unavailable_reason,
                    content_hashes=content_hashes,
                )
                if result is not None
                else None
            )
            pending_retry = await self._finish_failed_attempt(
                run_id, exc, attempts=attempts, max_attempts=max_attempts, stats=partial_stats
            )

        if pending_retry is not None:
            raise pending_retry
        return outcome

    async def _build_index_diff(
        self,
        run: RunDetailResponse,
        run_id: UUID,
        llms_txt: str,
        urls_discovered: int,
        *,
        content_hashes: dict[str, str],
        enrich_applied: bool,
    ) -> dict[str, Any] | None:
        """This run's `index_diff` block, or `None` if computing it failed.

        Reads the previous completed run via `RunService.get_previous_completed_index` and
        feeds both indexes to `internals/index_diff.py`'s `build_index_diff` — the pure half
        of this work, which never raises on its own. **This method's entire body is the read,
        wrapped in one `try`/`except Exception`.** A diff is derived bookkeeping computed
        AFTER this run's own artifact already exists; failing an otherwise-successful crawl
        because a bookkeeping read against `runs` hit a transient database blip would be
        exactly the wrong trade — the identical argument `execute_run`'s enrichment
        belt-and-braces catch makes for a bad model call, in the module docstring's own
        words. `asyncio.CancelledError` is a `BaseException`, not an `Exception`, and is
        therefore deliberately NOT caught here either — arq's job timeout and a deploy's
        SIGTERM still reach `execute_run`'s own `except asyncio.CancelledError` handler
        unchanged, exactly as they would if this method did not exist.

        Called once, from the success branch, strictly after this run's own `llms_txt` exists
        and strictly before the Storage upload — see the call site's comment and the module
        docstring's "PER-193 added a FOURTH call" paragraph for why no transaction may be
        open here.

        Args:
            run: This run's own `RunDetailResponse`, already fetched at the top of
                `execute_run` — reused for its `website_id` and `started_at` rather than
                fetched again.
            run_id: This run's own id — the tie-break half of the keyset pair
                `get_previous_completed_index` compares against.
            llms_txt: This run's own freshly generated index.
            urls_discovered: This run's own `runs.stats["urls_discovered"]` value.
            content_hashes: This run's own `build_content_hashes(result.pages)` (PER-194) —
                fed to `build_index_diff` as `current_content_hashes`.
            enrich_applied: This run's own `enrich_applied` (PER-194) — fed to
                `build_index_diff` as `current_enrich_applied`, to decide whether this
                comparison spans an enrichment mode change.

        Returns:
            `build_index_diff`'s result, or `None` on any exception from the read.
        """
        try:
            previous, previous_run_completed = await self._runs.get_previous_completed_index(
                run.website_id, before_started_at=run.started_at, before_run_id=run_id
            )
        except Exception:
            logger.warning(
                "crawl: could not read the previous completed run's index; recording no "
                "index_diff for this run",
                exc_info=True,
            )
            return None

        previous_index = (
            None
            if previous is None
            else PreviousIndex(
                run_id=str(previous.run_id),
                completed_at=(
                    previous.completed_at.isoformat() if previous.completed_at is not None else None
                ),
                llms_txt=previous.llms_txt,
                urls_discovered=previous.urls_discovered,
                enrich_applied=previous.enrich_applied,
                content_hashes=previous.content_hashes,
            )
        )
        return build_index_diff(
            current_llms_txt=llms_txt,
            current_urls_discovered=urls_discovered,
            current_enrich_applied=enrich_applied,
            current_content_hashes=content_hashes,
            previous=previous_index,
            previous_run_completed=previous_run_completed,
        )

    def _select_seed_links(
        self, seed_page: CrawledPage, *, limit: int, robots: RobotsRules | None = None
    ) -> SelectionResult:
        """Turn the SEED page's own links into a ranked frontier — the depth-1 fallback for a
        site with no sitemap (PER-178).

        Pure and synchronous: `internals/links.py`'s `extract_links` parses HTML that is
        already in hand (`CrawledPage.content`, the body `crawl_site` just fetched) and
        `select_urls` ranks it, so this makes no request of any kind. It is called once per
        run, on the seed page alone, by `crawl_site` via `frontier_from_seed`; the pages
        fetched from the frontier it returns are never passed back to it, which is the entire
        depth-1 rule.

        **It does re-parse the seed's HTML, and that is the one real cost here.** trafilatura
        already parsed this same body once, inside `fetch_page`, to produce
        `CrawledPage.markdown` — but what that produces is extracted PROSE, with the navigation
        carrying most of a documentation site's links deliberately stripped out, so there is no
        parsed artifact on `CrawledPage` this could read instead. The cost is one extra lxml
        parse, of one page, on the exception path only (a site with a sitemap never reaches
        this method), bounded by the same `crawl_max_bytes` that bounds the body itself.
        Threading a parsed tree out of `extract_content` to avoid it would widen that module's
        return type for one caller on one page — a worse trade than the parse. It is
        `duration_ms`, not `bytes_fetched` or a request count, that would show this if it ever
        matters.

        **Every candidate carries `source="links"`, `lastmod=None`, `priority=None`** — a
        page's own markup declares neither of the two signals a sitemap does. That is not a
        degraded input to `select_urls`: that module ranks a candidate with no metadata on
        path shape alone (`SITEMAP_DEFAULT_PRIORITY` centers `priority`, and `_recency_term`
        is neutral without a `lastmod`), which is exactly the case PER-172 built it to handle.

        `seed_page.url` is used as the ranking seed, not `website.url`: it is the final,
        post-redirect URL the links were actually found on and resolved against, so it is the
        origin `select_urls`'s `"off_origin"` rule must compare them to. Using the configured
        URL instead would drop a redirecting site's entire frontier.

        **Never raises**, matching `discover_sitemap_urls`'s own contract and for the same
        reason: this runs inside `execute_run`'s `try`, whose `except Exception` turns
        anything that escapes into a failed run. A site whose HTML this codebase cannot cope
        with should produce a single-page run, exactly as a site with no sitemap does —
        "hitting a cap is a success, not a failure" (ARCHITECTURE.md §3.4), one layer up.

        `robots` (PER-191) is forwarded straight into `select_urls`, unchanged — this method
        parses no `robots.txt` itself, and never re-fetches or re-derives the rules
        `execute_run` already read once from `DiscoveryResult.robots`.

        Returns:
            The `SelectionResult` — carried out whole rather than reduced to its `selected`
            list, because `runs.stats` records both how many links were found
            (`urls_discovered`) and how many survived ranking (`urls_selected`), and the
            ratio between them is the tunable thing (`internals/run_stats.py`).
        """
        try:
            urls = extract_links(seed_page.content, base_url=seed_page.url)
            selection = select_urls(
                [
                    DiscoveredUrl(url=url, lastmod=None, priority=None, source="links")
                    for url in urls
                ],
                seed_url=seed_page.url,
                limit=limit,
                robots=robots,
            )
        except Exception:
            logger.warning(
                "crawl: link extraction failed; continuing on the seed alone", exc_info=True
            )
            return SelectionResult(selected=[], discovered_count=0, dropped={})

        logger.info(
            "crawl: no sitemap; filled the frontier from the seed page's links",
            extra={
                "urls_discovered": selection.discovered_count,
                "urls_selected": len(selection.selected),
                "dropped": selection.dropped,
            },
        )
        return selection

    async def _finish_failed_attempt(
        self,
        run_id: UUID,
        exc: Exception,
        *,
        attempts: int,
        max_attempts: int,
        stats: dict[str, Any] | None,
    ) -> TransientCrawlError | None:
        """Record a failed attempt, and say whether it should be retried.

        The single decision point for every non-cancellation failure in `execute_run`, so
        that the seed-failure path and the unexpected-exception path cannot drift apart in
        how they classify, how they count, or what they leave in the database.

        Returns:
            A `TransientCrawlError` for the caller to raise once it is safely outside its own
            `except Exception` — the run has been handed back to `pending` and wants
            redelivering. `None` when the failure was terminal and `runs` now says so.
        """
        message = _safe_error_message(exc)
        retryable = _is_retryable(exc)

        if retryable and attempts < max_attempts:
            # Back to `pending` FIRST, then ask for a redelivery. The order matters: the
            # redelivered job's `claim_pending` matches only a `pending` row, so a retry
            # requested against a row still marked `processing` would arrive, find nothing to
            # claim, log "not pending; skipping", and quietly burn the attempt.
            await self._runs.release_claim(run_id)
            logger.warning(
                "crawl: attempt %d of %d failed transiently (%s); returning the run to the "
                "queue for another attempt",
                attempts,
                max_attempts,
                message,
                extra={"attempt": attempts, "max_attempts": max_attempts, "retryable": True},
            )
            return TransientCrawlError(message, attempts)

        # Terminal: either the failure is permanent, or the budget is gone. `stats` is
        # whatever partial numbers this attempt produced, threaded through unchanged —
        # `RunsWriter.mark_processing_failed` COALESCEs `None` against what is already
        # stored rather than blanking it.
        await self._runs.record_failure(run_id, _attempt_message(message, attempts), stats)
        # ERROR, and this is the line PER-166 asks for. A terminal failure IS the incident
        # record, because there is no error-tracking service holding a second copy
        # (ARCHITECTURE.md §3.8) — and it is an ERROR rather than a WARNING precisely because
        # nothing else will ever say this run stopped for good. `run_id` is already bound as
        # a correlation id by `crawl_task`, so it is not repeated in the message text.
        logger.error(
            "crawl: run failed terminally after %d attempt(s) (%s)",
            attempts,
            message,
            extra={"attempt": attempts, "max_attempts": max_attempts, "retryable": retryable},
        )
        return None

    async def _recover_cancelled_attempt(
        self, run_id: UUID, *, attempts: int, max_attempts: int
    ) -> None:
        """Leave a cancelled run in a state something can act on. **Never raises.**

        Called from `execute_run`'s `except asyncio.CancelledError` handler, on the way back
        out. A cancellation is not a failure of the site being crawled — it is this process
        being told to stop — so a run with budget left goes back to `pending` rather than to
        `failed`, and arq's own retry-on-cancellation puts the job back on the queue behind
        it. A run with no budget left is failed here rather than five minutes later by the
        reaper, which is the "terminal state is written even on an arq timeout" half of the
        ticket.

        **Every exception is swallowed, deliberately.** This runs inside a coroutine that has
        already been cancelled, on its way to re-raising; an exception escaping here would
        replace the `CancelledError` with something arq treats completely differently — a
        cancellation is re-queued, an ordinary exception is not. And the failure mode of
        swallowing is already covered: a run left `processing` because this write did not
        land is exactly what the stuck-run reaper sweeps, the same backstop
        `RunService.record_failure`'s own unguarded write has always relied on.
        """
        try:
            if attempts >= max_attempts:
                await self._runs.record_failure(
                    run_id, _attempt_message(_CANCELLED_MESSAGE, attempts)
                )
                logger.error(
                    "crawl: cancelled on attempt %d of %d with no budget left; recorded a "
                    "terminal failure",
                    attempts,
                    max_attempts,
                    extra={"attempt": attempts, "max_attempts": max_attempts},
                )
            else:
                await self._runs.release_claim(run_id)
                logger.warning(
                    "crawl: cancelled on attempt %d of %d (a deploy, or arq's job timeout); "
                    "returned the run to the queue",
                    attempts,
                    max_attempts,
                    extra={"attempt": attempts, "max_attempts": max_attempts},
                )
        except Exception:
            logger.error(
                "crawl: could not record the outcome of a cancelled run; leaving it for the "
                "stuck-run reaper to sweep",
                exc_info=True,
            )


def build_crawl_service(
    pool: Pool,
    client: httpx.AsyncClient,
    storage: SupabaseStorage,
    settings: Settings,
    *,
    anthropic_client: AsyncAnthropic | None = None,
) -> CrawlService:
    """Build a `CrawlService` for one job, from the resources `app.worker.jobs.crawl_task`
    already has on `ctx`: the process-wide pool, the shared crawl `httpx.AsyncClient`, the
    shared `SupabaseStorage` client, and — only when the flag is on — the shared
    `AsyncAnthropic` client.

    Constructs its own `WebsiteService` and `RunService` rather than importing either
    feature's router-level provider function — those are wired for FastAPI's dependency
    injection, which does not exist inside an arq job. Mirrors
    `app.api.routers.runs.get_run_service`, which builds the same trio for the same reason,
    including handing `RunService` the same `Settings` object: it takes one so that its
    per-user run caps are configurable and testable without mutating the module singleton,
    and a worker-built instance must not quietly diverge from a request-built one.

    `anthropic_client` is keyword-only AND optional, deliberately, rather than matching
    `client`/`storage`'s required, positional shape: the crawl `httpx.AsyncClient` and the
    Storage client exist in every worker process regardless of configuration, but the
    Anthropic client exists only when `settings.crawl_enrich_with_llm` is on
    (`app/worker/settings.py`'s `open_worker_resources`). Making it required would force
    every caller — including every test that builds a `CrawlService` without enrichment in
    mind — to pass `None` explicitly for a feature they are not exercising; making it
    optional-and-keyword-only instead makes "absent" a first-class, ordinary state rather
    than something that reads as a misconfiguration at every call site that predates PER-180.
    """
    website_service = WebsiteService(pool)
    run_service = RunService(pool, website_service, settings)
    return CrawlService(
        client, storage, run_service, website_service, settings, anthropic_client=anthropic_client
    )
