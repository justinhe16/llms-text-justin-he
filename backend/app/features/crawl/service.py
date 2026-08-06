"""`CrawlService.execute_run` — what `app.worker.jobs.crawl_task` calls, and the only place
in this feature that calls another feature's service.

**This feature owns no table**, so it reads and writes `runs` and `websites` through
`RunService` and `WebsiteService`, never through a reader or writer of its own
(ARCHITECTURE.md §3.1: "if feature A needs data owned by feature B, it calls B's service").
`build_crawl_service` below constructs both from the pool the worker hands it, the same way
`app.api.routers.runs.get_run_service` builds a `WebsiteService` alongside a `RunService` for
the API.

**The network call happens outside every transaction** (ARCHITECTURE.md §5.1). `crawl_site`
runs between the atomic claim (`RunService.claim_for_processing`, its own short transaction)
and whatever happens after it (`RunService.record_failure`, a second, independent
transaction) — never inside either one. `CrawlService` itself opens no transaction at all;
both of the ones this method touches belong to `RunService`.

**A successful run is deliberately left `processing`.** Persisting `llms_txt` and flipping
the row to `completed` needs a `Storage` upload and a third write this ticket does not add —
that is the next ticket's job. Returning a `CrawlOutcome` without writing it anywhere is the
honest state of "the crawl half of this pipeline works; the persistence half does not exist
yet," not an oversight.
"""

import logging
from typing import Final
from uuid import UUID

import httpx
from asyncpg import Pool
from fastapi import HTTPException

from app.core.settings import Settings
from app.features.crawl.internals.crawler import CrawlLimits, crawl_site
from app.features.crawl.internals.fetcher import ByteBudgetExceededError, FetchError
from app.features.crawl.internals.llms_txt import generate_llms_txt
from app.features.crawl.internals.ssrf import SsrfBlockedError
from app.features.crawl.schemas import CrawlOutcome
from app.features.runs.service import RunService
from app.features.websites.service import WebsiteService


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
    """
    if isinstance(exc, SsrfBlockedError):
        message = str(exc)
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
        run_service: RunService,
        website_service: WebsiteService,
        settings: Settings,
    ) -> None:
        self._client = client
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
            A `CrawlOutcome` if the seed was fetched and an artifact was generated. The
            underlying `runs` row is left `processing` even on success — see the module
            docstring.
        """
        try:
            run = await self._runs.get_run(run_id)
        except HTTPException:
            logger.warning("crawl: run %s no longer exists; skipping", run_id)
            return None

        if run.status != "pending":
            # The readable guard. `claim_for_processing` below is the one that is actually
            # correct under concurrent delivery — this one only makes the common case fail
            # fast and say why in the log.
            logger.info("crawl: run %s is %r, not pending; skipping", run_id, run.status)
            return None

        claimed = await self._runs.claim_for_processing(run_id)
        if not claimed:
            logger.info("crawl: run %s lost the claim race to another worker; skipping", run_id)
            return None

        try:
            website = await self._websites.get_website(run.website_id)

            # The network call, deliberately outside any transaction — see the module
            # docstring's second paragraph.
            result = await crawl_site(
                self._client, website.url, limits=CrawlLimits.from_settings(self._settings)
            )

            if result.seed_error is not None:
                message = _safe_error_message(result.seed_error)
                logger.warning(
                    "crawl: run %s could not fetch its seed URL (%s)",
                    run_id,
                    message,
                    exc_info=result.seed_error,
                )
                await self._runs.record_failure(run_id, message)
                return None

            llms_txt = generate_llms_txt(result.pages)
        except Exception as exc:
            logger.error("crawl: run %s failed unexpectedly", run_id, exc_info=True)
            await self._runs.record_failure(run_id, _safe_error_message(exc))
            return None

        logger.info(
            "crawl: run %s fetched %d page(s) (cap_hit=%s); left `processing` for the "
            "persistence ticket",
            run_id,
            len(result.pages),
            result.stats.get("cap_hit"),
        )
        return CrawlOutcome(llms_txt=llms_txt, stats=result.stats)


def build_crawl_service(pool: Pool, client: httpx.AsyncClient, settings: Settings) -> CrawlService:
    """Build a `CrawlService` for one job, from the resources `app.worker.jobs.crawl_task`
    already has on `ctx`: the process-wide pool and the shared crawl `httpx.AsyncClient`.

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
    return CrawlService(client, run_service, website_service, settings)
