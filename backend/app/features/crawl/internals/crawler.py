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
whatever the caller already knows about beyond the seed — since PER-176, that is
`internals/url_ranking.py`'s `select_urls` output over whatever `internals/sitemap.py`'s
`discover_sitemap_urls` found, wired together one layer up in `service.py`. `extra_urls`
staying a plain parameter is what let the page cap be tested without either pipeline existing
yet, and it is exactly the seam PER-176's discovery-and-selection pipeline plugged into.

**PER-178 made that frontier dynamic, and did it without moving discovery in here.** A site
with no sitemap has nothing for `extra_urls` to carry, and the only place its links exist is
in the seed page's own HTML — which does not exist until this function has already fetched it.
So `crawl_site` grew a second, optional way to be handed a frontier: `frontier_from_seed`, a
caller-supplied function invoked once, on the fetched seed page, when and only when
`extra_urls` is empty. **This module still does not discover anything itself**: it does not
parse a single byte of any page's content to find a link, and it does not know or care that
the function it calls does. Both frontiers land in the same list, under the same truncation,
the same page cap, and the same politeness gate; the difference is only WHEN the caller gets
to compute one. What remains genuinely out of scope here, and is not a thing this seam can
grow into by accident, is recursion: `frontier_from_seed` is called exactly once per run, on
the seed alone, and the pages fetched from the frontier are never handed back to it (see
`crawl_site`'s own parameter docstring, and `app/features/crawl/internals/links.py`).

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
    (`internals/fetcher.py`) — **unless the caller passes its own `budget` to `crawl_site`**,
    in which case this field is never consulted at all. `service.py` always does: it builds
    one `ByteBudget(limits.max_bytes)` before sitemap discovery runs and hands the same
    object to both `discover_sitemap_urls` and `crawl_site`, so the run-wide cap this field
    names is enforced either way — just not by reading `max_bytes` a second time here. See
    `crawl_site`'s `budget` parameter for the full reasoning."""

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
    frontier_from_seed: Callable[[CrawledPage], Sequence[str]] | None = None,
    budget: ByteBudget | None = None,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CrawlResult:
    """Crawl `seed_url` plus `extra_urls`, under `limits`, and return whatever resulted.

    Fetches the seed first and alone. If it fails, this returns immediately with
    `seed_error` set and an empty `pages` — there is nothing else to attempt. Otherwise the
    rest of `extra_urls` is fetched with bounded concurrency, a politeness gate on request
    starts, a page cap, and a run-wide byte cap; a non-seed page failing for any reason (an
    `httpx` error, `SsrfBlockedError` on a redirect, a malformed response) is logged at
    WARNING, counted in `stats["pages_failed"]`, and never fails the run.

    Args:
        client: The shared crawl client (`build_crawl_client`). Every fetch goes through it.
        seed_url: The one URL this run is guaranteed to attempt.
        limits: The six caps for this run — see `CrawlLimits`.
        extra_urls: The rest of the frontier, already known to the caller BEFORE the seed is
            fetched — since PER-176, `service.py`'s
            `select_urls(discover_sitemap_urls(...))` pipeline. Truncated to
            `limits.max_pages - 1` up front, and re-checked against the live page count
            before each fetch. Empty is an ordinary value, not a degenerate one: it is what a
            site with no sitemap produces, and it is the condition `frontier_from_seed` below
            answers.
        frontier_from_seed: How to derive a frontier from the SEED PAGE ITSELF, for the
            caller that could not know one up front. Called at most once per run — after the
            seed fetch succeeds, and only when `extra_urls` is empty — with the fetched
            `CrawledPage`, and whatever sequence it returns becomes the frontier, truncated
            and capped exactly as `extra_urls` would have been. `service.py` passes
            `internals/links.py`'s `extract_links` fed through the same `select_urls` the
            sitemap path uses (PER-178); every other caller, and every caller that already
            has a sitemap-derived frontier, leaves it `None`.

            **Three properties of this parameter are the whole depth-1 rule**, and none of
            them is enforced by the function it is given: it is called exactly ONCE, it is
            called with the SEED page only, and its result is fetched but never fed back into
            it. There is no frontier queue, no visited set, and no depth counter anywhere in
            this module, because with one extraction per run there is no second level for any
            of them to bound. `tests/test_crawler_caps.py` pins the "second level is never
            reached" half of that directly, with a callback that would happily return more
            links if it were ever called a second time.

            **Two obligations on whatever is passed here, neither of them enforced by this
            module.** First, it MUST NOT RAISE. It is called inside this function's own
            `asyncio.timeout` block but outside every `except` clause that could absorb it,
            so an exception escaping it would leave `crawl_site` entirely and reach
            `CrawlService.execute_run`'s `except Exception`, which turns anything it catches
            into a FAILED run — a site whose markup could not be read would fail rather than
            producing the single-page run it deserves. `service.py`'s `_select_seed_links`
            discharges this with a `try/except` of its own, exactly as
            `discover_sitemap_urls` does for the sitemap path; it is not defended a second
            time here, because a bare `except` around this call would also swallow real bugs
            in a caller's own code and report them as "this site had no links".

            Second, it is SYNCHRONOUS, and that is load-bearing in two directions. A function
            that cannot `await` cannot make an HTTP request, which is what makes "the fallback
            costs no extra fetch" a property of the type rather than a rule to remember. The
            flip side is that it cannot be interrupted: it runs to completion on the event
            loop, so `limits.max_wall_clock_s` cannot cut a pathological parse short, and the
            time it takes is time no other job in this worker process gets. That is the same
            property `internals/fetcher.py` already has for the `extract_content` call it
            makes per page, and it is bounded the same way — by `crawl_max_bytes` on the body
            being parsed, and by `links.MAX_LINKS` on what comes out.

            Skipped entirely when `extra_urls` is non-empty, so a run cannot end up with a
            frontier assembled from two sources: a sitemap is a site operator's own statement
            about which pages matter, and links scraped off one page do not get a vote
            alongside it. Never called at all when the seed fetch fails — there is no page to
            derive anything from, and the run is already a failure.
        budget: A caller-supplied `ByteBudget` to spend from, instead of a fresh one built
            from `limits.max_bytes`. Both caller-supplied production values, alongside
            `extra_urls` — `resolver`/`clock` below are the test-injection pair instead.
            `service.py` always passes one: it builds a single `ByteBudget(limits.max_bytes)`
            BEFORE calling `crawl_site` at all, so that `internals/sitemap.py`'s
            `discover_sitemap_urls` can spend from it first and this run-wide cap is honored
            across both phases rather than resetting between them. **When a budget is
            injected, `limits.max_bytes` is never consulted** — see `CrawlLimits.max_bytes`'s
            own docstring. `stats["bytes_fetched"]` (== `budget.used`) then includes whatever
            the caller already spent before this function was ever called, which is correct
            — `bytes_fetched` and `cap_hit == "bytes"` must always agree on the same counter
            (`internals/sitemap.py`'s module docstring makes the same argument in more
            detail) — but it does mean `bytes_fetched / pages_crawled` is no longer a page's
            average size once any pre-crawl discovery ran. Defaults to
            `ByteBudget(limits.max_bytes)`, matching every call site before this parameter
            existed.
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
    budget = budget if budget is not None else ByteBudget(limits.max_bytes)
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

                # The frontier, from whichever of the two sources supplied one. The
                # `if not frontier` guard is what keeps them mutually exclusive rather than
                # additive — see `frontier_from_seed`'s own docstring — and it is also why
                # `frontier_from_seed` cannot be reached at all on the seed-failure path
                # above: this whole block is the seed fetch's `else`.
                frontier = list(extra_urls)
                if not frontier and frontier_from_seed is not None:
                    frontier = list(frontier_from_seed(seed_page))

                # Truncated up front — but `cap_hit` is deliberately NOT set here yet.
                # Setting it before the gather below would make every truncated task's own
                # `if cap_hit is not None: return` check fire immediately, since that same
                # flag is what marks "stop early" — the truncated frontier would never run
                # at all. Whether truncation happened is remembered separately
                # (`frontier_was_truncated`) and only turned into `cap_hit` once the
                # truncated batch has actually been attempted, below.
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
