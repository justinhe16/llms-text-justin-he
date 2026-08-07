"""The bounded crawl loop: caps, concurrency, and the one deadline everything else answers
to.

`crawl_site()` is what turns one `fetch_page()` call (`internals/fetcher.py`) into a run: it
fetches the seed, then a caller-supplied frontier, under six hard caps read from `Settings`
(`app.core.settings` — `crawl_max_pages`, `crawl_max_wall_clock_s`, `crawl_max_bytes`,
`crawl_request_timeout_s`, `crawl_concurrency`, `crawl_politeness_delay_ms`). **Hitting a cap
is a success, not a failure**: the crawl returns whatever pages it collected before the cap
tripped, and `CrawlResult.stats["cap_hit"]` names which one (or `None`, if it finished on its
own). Only the SEED failing to fetch at all is a failure — `CrawlResult.seed_error` is what
`app.features.crawl.service.CrawlService.execute_run` checks to decide that.

**The frontier is a parameter, not something this module discovers.** `extra_urls` is
whatever the caller already knows about beyond the seed — today, always empty, because link
extraction does not exist yet (ARCHITECTURE.md §3.4, CLAUDE.md #9). That is deliberate: it is
what makes the page cap testable without a link extractor, and it is exactly the seam the
real frontier-discovery pipeline will plug into later. This module does not parse a single
byte of any page's content to find one.

**Two independent timeout mechanisms, not one.** A monotonic deadline (`clock() +
limits.max_wall_clock_s`) is checked before every non-seed fetch is even attempted — cheap,
and it is what lets the run stop between requests without waiting for one to time out. The
whole crawl additionally runs inside `async with asyncio.timeout(limits.max_wall_clock_s)`,
which is the backstop for the case the first mechanism cannot cover: a single connection that
is already in flight and hangs past its own per-request timeout. Without the second
mechanism, one stuck socket could keep a run alive well past its wall-clock budget; the
per-fetch check alone only ever runs *between* fetches.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.settings import Settings
from app.features.crawl.internals.fetcher import ByteBudget, ByteBudgetExceededError, fetch_page
from app.features.crawl.internals.ssrf import Resolver
from app.features.crawl.schemas import CrawledPage


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    """The crawl's six hard caps, read once from `Settings` at the top of a run.

    Plain `Settings` fields, not module constants — unlike `app/worker/settings.py`'s
    `POLL_DELAY_SECONDS`/`MAX_JOBS`, which are deliberately kept off `Settings` for
    arq-specific reasons documented there. There is no equivalent reason to hide these.
    """

    max_pages: int
    """The crawl fetches at most this many pages, seed included."""

    max_wall_clock_s: float
    """This run's own wall-clock budget. See the module docstring for why it is enforced
    twice — a pre-fetch check plus an outer `asyncio.timeout` — and
    `Settings.crawl_max_wall_clock_s` for why, inside a deployed worker,
    `WorkerSettings.job_timeout` usually lands first anyway."""

    max_bytes: int
    """The run-wide response-body budget, shared by every page via one `ByteBudget`
    (`internals/fetcher.py`)."""

    request_timeout_s: float
    """Not read by this module. Already baked into the shared `httpx.AsyncClient` by
    `build_crawl_client` (`app/features/crawl/http_client.py`) — `fetch_page` passes no
    per-call `timeout=` of its own. Carried here anyway so `CrawlLimits` is the one place
    that names all six caps `Settings` declares, for a caller inspecting one crawl's
    configuration as a whole."""

    concurrency: int
    """How many frontier fetches may be in flight at once, via `asyncio.Semaphore`."""

    politeness_delay_ms: int
    """Minimum gap between the START of one frontier request and the next, across the
    whole run — see `_PolitenessGate` below."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "CrawlLimits":
        """Build the six caps for one run from the process-wide `Settings`."""
        return cls(
            max_pages=settings.crawl_max_pages,
            max_wall_clock_s=settings.crawl_max_wall_clock_s,
            max_bytes=settings.crawl_max_bytes,
            request_timeout_s=settings.crawl_request_timeout_s,
            concurrency=settings.crawl_concurrency,
            politeness_delay_ms=settings.crawl_politeness_delay_ms,
        )


@dataclass(slots=True)
class CrawlResult:
    """What one `crawl_site()` call hands back — always, whether a cap tripped or not."""

    pages: list[CrawledPage]
    """Every page fetched successfully, seed first, then the frontier in whatever order
    concurrent fetches happened to finish. `internals/llms_txt.py`'s `generate_llms_txt`
    sorts before it derives anything from this, precisely because that order is not
    deterministic."""

    stats: dict[str, Any]
    """The exact shape `runs.stats` stores: `pages_crawled`, `pages_failed`,
    `bytes_fetched`, `duration_ms`, `cap_hit`, and `pages_empty_content`. `pages_crawled` is
    the key name the websites feature's reader already reads out of this column — see
    `app.features.websites.internals.websites_reader` — so it is spelled exactly that way
    here, not `len(pages)` renamed to something more natural in isolation.

    `pages_empty_content` is counted here, alongside the other five, rather than in
    `internals/run_stats.py` — see that module's own docstring for the line it draws between
    the crawl loop's concerns and persistence-shaped ones. How many of the pages THIS LOOP
    fetched came back with no extractable content is a fact about the fetch, not about how
    the result gets stored, so it belongs on this dict the same way `pages_failed` and
    `bytes_fetched` do."""

    cap_hit: str | None
    """`"pages"`, `"bytes"`, `"wall_clock"`, or `None` if the crawl exhausted its frontier
    (or failed on the seed) before any cap bound it. Duplicated onto `stats["cap_hit"]` —
    both exist because callers that only care about the artifact still want this without
    reaching into a dict, and `runs.stats` needs the dict shape regardless."""

    seed_error: Exception | None
    """Set only when the seed itself could not be fetched — the one failure mode that
    makes the whole run a failure rather than a capped success, because there is nothing to
    build an artifact from. `CrawlService.execute_run` maps this to a sanitized message; it
    is never shown to a caller as-is."""


class _PolitenessGate:
    """Serializes frontier request STARTS to at least `delay_s` apart, across the whole run.

    One `asyncio.Lock` plus a monotonic "next allowed start" timestamp. Held for the entire
    wait — including the `asyncio.sleep` — rather than released early, which is what makes
    "at least `delay_s` apart" true of the order callers actually reach the lock in, not
    just true on average: a caller computes its own start time from whatever
    `_next_allowed_start` currently says, sleeps until it, publishes the next one, and only
    then lets the next waiter in.
    """

    def __init__(self, delay_s: float, clock: Callable[[], float]) -> None:
        self._delay_s = delay_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._next_allowed_start: float | None = None

    async def wait_for_turn(self) -> None:
        """Block until this caller may start its request, then reserve the next slot."""
        async with self._lock:
            now = self._clock()
            start_at = (
                now if self._next_allowed_start is None else max(now, self._next_allowed_start)
            )
            wait = start_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed_start = start_at + self._delay_s


async def crawl_site(
    client: httpx.AsyncClient,
    seed_url: str,
    *,
    limits: CrawlLimits,
    extra_urls: Sequence[str] = (),
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CrawlResult:
    """Crawl `seed_url` plus `extra_urls`, under `limits`, and return whatever resulted.

    Fetches the seed first and alone. If it fails, this returns immediately with
    `seed_error` set and an empty `pages` — there is nothing else to attempt. Otherwise the
    rest of `extra_urls` (today, always empty — see the module docstring) is fetched with
    bounded concurrency, a politeness gate on request starts, a page cap, and a run-wide
    byte cap; a non-seed page failing for any reason (an `httpx` error, `SsrfBlockedError`
    on a redirect, a malformed response) is logged at WARNING, counted in
    `stats["pages_failed"]`, and never fails the run.

    Args:
        client: The shared crawl client (`build_crawl_client`). Every fetch goes through it.
        seed_url: The one URL this run is guaranteed to attempt.
        limits: The six caps for this run — see `CrawlLimits`.
        extra_urls: The rest of the frontier, already known to the caller. Truncated to
            `limits.max_pages - 1` up front, and re-checked against the live page count
            before each fetch (the frontier becomes dynamic once link extraction lands).
        resolver: Forwarded to every `fetch_page` call. Tests inject a fake one; production
            code never does.
        clock: The monotonic clock the wall-clock deadline and the politeness gate are
            measured against. Defaults to `time.monotonic`; tests inject a fake one to make
            the wall-clock cap deterministic without a real sleep.

    Returns:
        A `CrawlResult`. `asyncio.CancelledError` — arq's own job timeout or a SIGTERM —
        is deliberately allowed to propagate unchanged; catching it here would hide a
        worker shutdown from the reaper that is meant to notice it later.
    """
    budget = ByteBudget(limits.max_bytes)
    pages: list[CrawledPage] = []
    pages_failed = 0
    cap_hit: str | None = None
    seed_error: Exception | None = None

    start = clock()
    deadline = start + limits.max_wall_clock_s
    gate = _PolitenessGate(limits.politeness_delay_ms / 1000, clock)
    semaphore = asyncio.Semaphore(limits.concurrency)

    def _mark_cap_hit(hit: str) -> None:
        """Set `cap_hit` to `hit` and log it — but only the first caller to arrive.

        Called from every site below that can trip a cap, three of which run inside
        concurrent `fetch_frontier_url` tasks. The `if cap_hit is not None: return` guard at
        the top of that function only stops a task that starts AFTER a cap has already
        fired; it says nothing about two tasks reaching the same cap in the same moment,
        which without this helper would log that cap twice.

        What makes the check-and-set here safe is that nothing between them awaits, so the
        event loop cannot hand control to another task in the middle of it. The pair is
        atomic in practice even though the five call sites are not synchronized with each
        other in any other way: exactly one INFO line per run, whichever of them wins.
        """
        nonlocal cap_hit
        if cap_hit is None:
            cap_hit = hit
            logger.info(
                "crawl: hit the %s cap", hit, extra={"cap_hit": hit, "pages_crawled": len(pages)}
            )

    async def fetch_frontier_url(url: str) -> None:
        nonlocal pages_failed
        async with semaphore:
            if cap_hit is not None:
                return
            if len(pages) >= limits.max_pages:
                _mark_cap_hit("pages")
                return
            if clock() >= deadline:
                _mark_cap_hit("wall_clock")
                return
            await gate.wait_for_turn()
            try:
                page = await fetch_page(client, url, budget=budget, resolver=resolver)
            except ByteBudgetExceededError:
                _mark_cap_hit("bytes")
                return
            except Exception:
                logger.warning("crawl: frontier fetch failed for %s", url, exc_info=True)
                pages_failed += 1
                return
            pages.append(page)

    try:
        async with asyncio.timeout(limits.max_wall_clock_s):
            try:
                seed_page = await fetch_page(client, seed_url, budget=budget, resolver=resolver)
            except Exception as exc:
                seed_error = exc
            else:
                pages.append(seed_page)

                # Truncated up front — but `cap_hit` is deliberately NOT set here yet.
                # Setting it before the gather below would make every truncated task's own
                # `if cap_hit is not None: return` check fire immediately, since that same
                # flag is what marks "stop early" — the truncated frontier would never run
                # at all. Whether truncation happened is remembered separately
                # (`frontier_was_truncated`) and only turned into `cap_hit` once the
                # truncated batch has actually been attempted, below.
                frontier = list(extra_urls)
                allowed = max(0, limits.max_pages - 1)
                frontier_was_truncated = len(frontier) > allowed
                frontier = frontier[:allowed]

                if frontier:
                    await asyncio.gather(*(fetch_frontier_url(url) for url in frontier))

                if frontier_was_truncated and cap_hit is None:
                    _mark_cap_hit("pages")
    except TimeoutError:
        _mark_cap_hit("wall_clock")

    # A run that collected NOTHING is a failed run, not a capped success — and the outer
    # `asyncio.timeout` is the one path that can produce that shape without the seed's own
    # `except` clause having set `seed_error`. It happens when the seed neither succeeds nor
    # fails: a server that accepts the connection and trickles bytes forever keeps each
    # individual socket operation inside the client's per-request timeout, so httpx never
    # raises, and the whole crawl is cut off from the outside instead. Without this, such a
    # run reports `seed_error is None` with an empty `pages`, and `CrawlService` faithfully
    # generates an artifact out of no pages at all and calls it a success.
    #
    # `pages` being empty is exactly equivalent to "the seed never landed": every later
    # append happens after a successful seed fetch, so there is no other way to get here
    # with nothing collected.
    if seed_error is None and not pages:
        seed_error = TimeoutError(
            "the seed URL did not complete within the crawl's wall-clock budget"
        )

    stats = {
        "pages_crawled": len(pages),
        "pages_failed": pages_failed,
        "bytes_fetched": budget.used,
        "duration_ms": int((clock() - start) * 1000),
        "cap_hit": cap_hit,
        # A fact about the pages THIS LOOP fetched, not a persistence-shaped concern —
        # `internals/run_stats.py`'s module docstring draws that line for `links_emitted` and
        # `version`, and how many fetched pages carried no extractable content belongs on this
        # side of it. Counting it here, rather than downstream in `build_run_stats`, also
        # means the seed-failure path in `service.py`
        # (`build_run_stats(result.stats, links_emitted=links_emitted)`) gets this key for
        # free, with no extra plumbing — the persisted shape stays uniform across a
        # successful run's stats and a failed one's partial stats.
        "pages_empty_content": sum(1 for page in pages if page.is_empty),
    }
    return CrawlResult(pages=pages, stats=stats, cap_hit=cap_hit, seed_error=seed_error)
