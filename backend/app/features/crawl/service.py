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
`generate_llms_txt` has produced an artifact, this method gzips the fetched pages as JSONL
(`internals/payload.py`), uploads that to Supabase Storage, and only THEN opens the short
transaction that writes `llms_txt`, `storage_path`, `stats`, and flips the row to `completed`
(`RunService.record_success`). That order, and not its reverse, is what ARCHITECTURE.md §5.1
exists to require: uploading first means the worst case of a mid-pipeline failure is an
orphaned Storage object costing a fraction of a cent, while writing the row first would risk
a `completed` run whose `storage_path` points at an object that was never actually written —
a 404 in the UI on a run the database swears succeeded. Nothing about this method holds a
database transaction open while the upload is in flight; `RunService.record_success` opens
its own transaction only after `await self._storage.upload(...)` has already returned.
"""

import logging
from typing import Final
from uuid import UUID

import httpx
from asyncpg import Pool
from fastapi import HTTPException

from app.core.settings import Settings
from app.features.crawl.internals.crawler import CrawlLimits, CrawlResult, crawl_site
from app.features.crawl.internals.fetcher import ByteBudgetExceededError, FetchError
from app.features.crawl.internals.llms_txt import generate_llms_txt
from app.features.crawl.internals.payload import (
    PAYLOAD_CONTENT_TYPE,
    payload_object_path,
    serialize_payload,
)
from app.features.crawl.internals.run_stats import build_run_stats
from app.features.crawl.internals.ssrf import SsrfBlockedError
from app.features.crawl.schemas import CrawlOutcome
from app.features.runs.service import RunService
from app.features.websites.service import WebsiteService
from app.infrastructure.storage.supabase_storage import StorageUploadError, SupabaseStorage


logger = logging.getLogger(__name__)

# `runs.error` is readable by every signed-in user (ARCHITECTURE.md §4.1) and rendered in
# the UI. 500 characters is generous for any of the fixed strings below — including
# SsrfBlockedError's own message, the one case that is not a fixed string — while still
# bounding what a future exception type could accidentally leak if someone adds one to
# `_safe_error_message` without thinking about its `str()`.
_MAX_ERROR_LENGTH: Final = 500


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
    ) -> None:
        self._client = client
        self._storage = storage
        self._runs = run_service
        self._websites = website_service
        self._settings = settings

    async def execute_run(self, run_id: UUID) -> CrawlOutcome | None:
        """Crawl the website behind `run_id`, and return what it produced — or `None`.

        `None` covers every path that is not "a crawl actually ran and produced an
        outcome": the run no longer exists, it was not `pending`, another worker already
        claimed it, or something failed. A worker's job function
        (`app.worker.jobs.crawl_task`) never raises an `HTTPException` at arq, so every one
        of those paths is logged here and turned into a plain return instead.

        Deliberately does not catch `asyncio.CancelledError` — arq's own job timeout or a
        SIGTERM delivers exactly that, and letting it propagate is what leaves the run in
        `processing` for the stuck-run reaper (a later, reliability ticket) to find, rather
        than this method racing that same signal to record a failure of its own.

        Returns:
            A `CrawlOutcome` if the seed was fetched, an artifact was generated, it was
            uploaded to Storage, and the `runs` row was written `completed` — by the time
            this returns non-`None`, all of that has already happened (see the module
            docstring). `None` covers every other path, including a Storage upload or a
            database write that failed after a successful crawl: those are recorded as a
            `failed` run (see the partial-stats handling below), never left `processing`.
        """
        try:
            run = await self._runs.get_run(run_id)
        except HTTPException:
            # `run_id` is deliberately not interpolated here, or in any log line below it in
            # this method: `crawl_task` binds it with `run_id_context` around this entire
            # call, so `app.core.logging.JsonFormatter` already attaches it to every line —
            # repeating it in the message text would just be `jq`-unfriendly duplication.
            logger.warning("crawl: run no longer exists; skipping")
            return None

        if run.status != "pending":
            # The readable guard. `claim_for_processing` below is the one that is actually
            # correct under concurrent delivery — this one only makes the common case fail
            # fast and say why in the log. `status` is a structured value, so it goes in
            # `extra` rather than only inside the message.
            logger.info("crawl: run is not pending; skipping", extra={"status": run.status})
            return None

        claimed = await self._runs.claim_for_processing(run_id)
        if not claimed:
            logger.info("crawl: lost the claim race to another worker; skipping")
            return None

        # Hoisted above the `try` so the `except` below can build a partial `stats` dict from
        # whatever this run actually managed before it failed, rather than reporting nothing
        # at all. `result` stays `None` for a failure that never got as far as `crawl_site`
        # returning (e.g. `get_website` raising); `links_emitted` stays 0 for a seed failure,
        # where `result.stats["pages_crawled"]` is 0 anyway.
        result: CrawlResult | None = None
        links_emitted = 0
        try:
            website = await self._websites.get_website(run.website_id)

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
                },
            )

            # The network call, deliberately outside any transaction — see the module
            # docstring's second paragraph.
            result = await crawl_site(self._client, website.url, limits=limits)

            if result.seed_error is not None:
                message = _safe_error_message(result.seed_error)
                logger.warning(
                    "crawl: could not fetch its seed URL (%s)",
                    message,
                    exc_info=result.seed_error,
                )
                await self._runs.record_failure(
                    run_id, message, build_run_stats(result.stats, links_emitted=links_emitted)
                )
                return None

            llms_txt = generate_llms_txt(result.pages)
            links_emitted = len(result.pages)

            payload = serialize_payload(result.pages)
            object_path = payload_object_path(run.website_id, run_id)

            # THE NETWORK CALL. No transaction is open here, and none may be opened around
            # it (ARCHITECTURE.md §5.1). Upload first: an orphaned object costs a fraction
            # of a cent, while a committed row pointing at an object that does not exist is
            # a 404 in the UI on a run the database says succeeded.
            storage_path = await self._storage.upload(
                object_path, payload, content_type=PAYLOAD_CONTENT_TYPE
            )
            # `len(payload)` is a number already sitting in hand, not a re-derivation — this
            # stays cheap. The payload itself is never logged: it is every fetched page's
            # content, gzip-compressed, and a page's content has no business appearing in
            # `fly logs`, which every signed-in user's crawl targets share.
            logger.info(
                "crawl: uploaded payload to storage",
                extra={"bytes": len(payload), "storage_path": storage_path},
            )

            stats = build_run_stats(result.stats, links_emitted=links_emitted)
            await self._runs.record_success(
                run_id, llms_txt=llms_txt, storage_path=storage_path, stats=stats
            )
        except Exception as exc:
            # `exc_info=True` stays: `fly logs` is the only log surface this system has and
            # there is no error-tracking service on either side of it holding a second copy
            # (`app.core.logging`'s own module docstring), so the full traceback this one
            # line carries is the only record of the failure that will ever exist.
            logger.error("crawl: failed unexpectedly", exc_info=True)
            partial_stats = (
                build_run_stats(result.stats, links_emitted=links_emitted)
                if result is not None
                else None
            )
            await self._runs.record_failure(run_id, _safe_error_message(exc), partial_stats)
            return None

        # `result.stats` already has exactly the five keys `runs.stats` wants named
        # (`pages_crawled`, `pages_failed`, `bytes_fetched`, `duration_ms`, `cap_hit`) —
        # spread rather than restated by hand, so this line can never drift from the shape
        # `internals/crawler.py` actually produces. `storage_path` is the one field that
        # isn't already on it.
        logger.info("crawl: completed", extra={**result.stats, "storage_path": storage_path})
        return CrawlOutcome(llms_txt=llms_txt, stats=stats, storage_path=storage_path)


def build_crawl_service(
    pool: Pool, client: httpx.AsyncClient, storage: SupabaseStorage, settings: Settings
) -> CrawlService:
    """Build a `CrawlService` for one job, from the resources `app.worker.jobs.crawl_task`
    already has on `ctx`: the process-wide pool, the shared crawl `httpx.AsyncClient`, and
    the shared `SupabaseStorage` client.

    Constructs its own `WebsiteService` and `RunService` rather than importing either
    feature's router-level provider function — those are wired for FastAPI's dependency
    injection, which does not exist inside an arq job. Mirrors
    `app.api.routers.runs.get_run_service`, which builds the same trio for the same reason,
    including handing `RunService` the same `Settings` object: it takes one so that its
    per-user run caps are configurable and testable without mutating the module singleton,
    and a worker-built instance must not quietly diverge from a request-built one.
    """
    website_service = WebsiteService(pool)
    run_service = RunService(pool, website_service, settings)
    return CrawlService(client, storage, run_service, website_service, settings)
