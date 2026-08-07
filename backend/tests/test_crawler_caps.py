"""Tests for `app.features.crawl.internals.crawler.crawl_site` — the bounded crawl loop.

Every client here is built with `transport=httpx.MockTransport(...)`, per this suite's
autouse `_forbid_real_network` fixture (tests/conftest.py), and every crawl injects a fake
`Resolver` instead of touching real DNS — see `tests/test_ssrf.py`'s helper of the same
name.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import httpx

from app.core.settings import Settings
from app.features.crawl.internals.crawler import CrawlLimits, RobotsDisallowedError, crawl_site
from app.features.crawl.internals.fetcher import ByteBudget, PolitenessGate
from app.features.crawl.internals.links import extract_links
from app.features.crawl.internals.sitemap import SITEMAP_BYTE_SHARE, discover_sitemap_urls
from app.features.crawl.internals.ssrf import Resolver
from app.features.crawl.schemas import CrawledPage


_SyncHandler = Callable[[httpx.Request], httpx.Response]
_AsyncHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


PUBLIC_IP = "8.8.8.8"

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    """See `tests/test_crawl_extract.py`'s helper of the same name — identical contract."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _fake_resolver(mapping: dict[str, Sequence[str]] | None = None) -> Resolver:
    """Resolve any host not named in `mapping` to `PUBLIC_IP`.

    Unlike `tests/test_ssrf.py` and `tests/test_crawl_fetcher.py`'s helper of the same
    name — which treats an unmapped host as "resolves to nothing" (the case those suites
    want to test) — this one defaults to a public address, because every test in this file
    is about the crawl LOOP, not about SSRF, and a crawl over several distinct hostnames
    should not have to name every one of them just to keep `validate_url` happy.
    """
    known = mapping or {}

    async def resolve(host: str, port: int) -> Sequence[str]:
        return known.get(host, [PUBLIC_IP])

    return resolve


def _limits(
    *,
    max_pages: int = 100,
    max_wall_clock_s: float = 300.0,
    max_bytes: int = 50_000_000,
    request_timeout_s: float = 10.0,
    concurrency: int = 5,
    politeness_delay_ms: int = 0,
) -> CrawlLimits:
    """`CrawlLimits` with generous defaults, overridden by whichever cap a test cares about."""
    return CrawlLimits(
        max_pages=max_pages,
        max_wall_clock_s=max_wall_clock_s,
        max_bytes=max_bytes,
        request_timeout_s=request_timeout_s,
        concurrency=concurrency,
        politeness_delay_ms=politeness_delay_ms,
    )


def _client(handler: _SyncHandler | _AsyncHandler) -> httpx.AsyncClient:
    """An `AsyncClient` backed by nothing but `httpx.MockTransport(handler)`."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _host(request: httpx.Request) -> str:
    """The original hostname a request was addressed to — the `Host` header, not
    `request.url.host`, which is the validated IP `internals/ssrf.py` dialed."""
    return request.headers["Host"]


async def test_page_cap_truncates_the_frontier_and_is_a_success_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    seed = "http://seed.test/"
    extra = [f"http://f{i}.test/" for i in range(4)]  # seed + 4 = a 5-URL frontier

    async with _client(handler) as client:
        result = await crawl_site(
            client, seed, limits=_limits(max_pages=3), extra_urls=extra, resolver=_fake_resolver()
        )

    assert len(result.pages) == 3
    assert result.stats["pages_crawled"] == 3
    assert result.cap_hit == "pages"
    assert result.stats["cap_hit"] == "pages"
    assert result.seed_error is None


async def test_wall_clock_cap_stops_the_crawl_and_keeps_pages_collected_so_far() -> None:
    """The manual, pre-fetch deadline check — not the real `asyncio.timeout` backstop — is
    what trips here: an injected clock jumps far past the deadline on its second call, so
    the crawl stops deterministically without depending on real elapsed time anywhere near
    its (tiny) wall-clock budget. The handler still `sleep`s, for realism, but the cap is
    tripped by the clock, not by that sleep actually exhausting the budget."""
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1_000.0

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(200, text="ok")

    seed = "http://seed.test/"
    extra = [f"http://f{i}.test/" for i in range(3)]

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            seed,
            limits=_limits(max_wall_clock_s=1.0),
            extra_urls=extra,
            resolver=_fake_resolver(),
            clock=clock,
        )

    assert result.cap_hit == "wall_clock"
    assert result.stats["cap_hit"] == "wall_clock"
    assert len(result.pages) == 1
    assert result.pages[0].url == seed
    assert result.seed_error is None


async def test_byte_cap_stops_at_a_later_page_not_just_within_one() -> None:
    body = b"x" * 600

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    seed = "http://seed.test/"
    extra = ["http://f0.test/"]

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            seed,
            limits=_limits(max_bytes=1000),
            extra_urls=extra,
            resolver=_fake_resolver(),
        )

    assert result.cap_hit == "bytes"
    assert result.stats["cap_hit"] == "bytes"
    assert len(result.pages) == 1
    assert result.pages[0].url == seed
    assert result.stats["bytes_fetched"] == 600


async def test_concurrency_never_exceeds_the_configured_limit() -> None:
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, text="ok")

    seed = "http://seed.test/"
    extra = [f"http://f{i}.test/" for i in range(6)]

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            seed,
            limits=_limits(concurrency=2),
            extra_urls=extra,
            resolver=_fake_resolver(),
        )

    assert max_in_flight == 2
    assert result.seed_error is None


async def test_politeness_delay_spaces_out_frontier_request_starts() -> None:
    start_times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start_times.append(time.monotonic())
        return httpx.Response(200, text="ok")

    seed = "http://seed.test/"
    extra = [f"http://f{i}.test/" for i in range(3)]

    async with _client(handler) as client:
        await crawl_site(
            client,
            seed,
            limits=_limits(politeness_delay_ms=50, concurrency=3),
            extra_urls=extra,
            resolver=_fake_resolver(),
        )

    # The seed's own request is fetched before the politeness gate is ever consulted; only
    # the gaps between the GATED frontier requests are this test's concern.
    frontier_starts = start_times[1:]
    assert len(frontier_starts) == 3
    for earlier, later in zip(frontier_starts, frontier_starts[1:], strict=False):
        # A small tolerance below 50ms guards against scheduler jitter, not against the
        # gate itself: `PolitenessGate` only ever sleeps to reach or exceed its target.
        assert later - earlier >= 0.045


async def test_seed_failure_sets_seed_error_and_yields_no_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    async with _client(handler) as client:
        result = await crawl_site(
            client, "http://seed.test/", limits=_limits(), resolver=_fake_resolver()
        )

    assert result.pages == []
    assert result.seed_error is not None
    assert isinstance(result.seed_error, httpx.ConnectError)
    assert result.cap_hit is None


async def test_a_non_seed_page_failing_lands_in_pages_failed_and_does_not_fail_the_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "seed" in _host(request):
            return httpx.Response(200, text="ok")
        raise httpx.ConnectError("simulated connection failure", request=request)

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "http://seed.test/",
            limits=_limits(),
            extra_urls=["http://broken.test/"],
            resolver=_fake_resolver(),
        )

    assert result.seed_error is None
    assert len(result.pages) == 1
    assert result.pages[0].url == "http://seed.test/"
    assert result.stats["pages_failed"] == 1
    assert result.cap_hit is None


async def test_pages_empty_content_counts_pages_the_extractor_found_nothing_on() -> None:
    """`stats["pages_empty_content"]` (PER-177) is a fact about the pages THIS LOOP
    fetched — see `CrawlResult.stats`'s own docstring for why it lives here rather than in
    `internals/run_stats.py`. The seed here carries real documentation prose; the one
    frontier page is a JavaScript shell that `internals/extract.py` finds nothing on — so of
    the two pages this crawl collects, exactly one should count."""
    docusaurus = _load_fixture("docusaurus_page.html")
    js_shell = _load_fixture("js_shell_page.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if "seed" in _host(request):
            return httpx.Response(200, html=docusaurus)
        return httpx.Response(200, html=js_shell)

    seed = "http://seed.test/"
    extra = ["http://shell.test/"]

    async with _client(handler) as client:
        result = await crawl_site(
            client, seed, limits=_limits(), extra_urls=extra, resolver=_fake_resolver()
        )

    assert len(result.pages) == 2
    assert result.stats["pages_empty_content"] == 1
    # The seed's own page is not the empty one — pin down WHICH page counts, not just how many.
    seed_page = next(page for page in result.pages if page.url == seed)
    shell_page = next(page for page in result.pages if page.url != seed)
    assert seed_page.is_empty is False
    assert shell_page.is_empty is True


async def test_a_seed_that_hangs_past_the_wall_clock_budget_is_a_seed_failure() -> None:
    """A crawl that collected NOTHING must report a seed failure, never a capped success.

    The slowloris shape, and the one path that can reach the end of `crawl_site` with an
    empty `pages` and no `seed_error` set by the seed's own `except` clause: the server
    accepts the connection and then neither responds nor drops it, so every individual
    socket operation stays inside the client's per-request timeout, httpx never raises, and
    the outer `asyncio.timeout` cuts the whole run off from the outside instead.

    Without the "collected nothing means the seed never landed" rule this asserts,
    `CrawlService.execute_run` sees `seed_error is None`, hands an empty page list to
    `generate_llms_txt`, and records a perfectly cheerful run whose artifact describes no
    pages at all — a red-looking outage reported as a green one.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)  # never completes within the budget below
        raise AssertionError("unreachable: the wall-clock backstop must cut this off first")

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "http://seed.test/",
            limits=_limits(max_wall_clock_s=0.05),
            resolver=_fake_resolver(),
        )

    assert result.pages == []
    assert result.cap_hit == "wall_clock"
    assert isinstance(result.seed_error, TimeoutError)


def _settings(*, crawl_max_bytes: int) -> Settings:
    """Build a `Settings` from explicit values only — the same "ignore the ambient
    environment" contract `tests/test_settings.py`'s own `_settings` helper documents — so
    nothing this file asserts on can be perturbed by a developer's own `backend/.env`. Takes
    only the one field this file's tests ever need to override; every other field keeps
    `Settings`'s own default."""
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost:5432/llms_text",
        redis_url="redis://localhost:6379/0",
        supabase_url="https://example.supabase.co",
        supabase_secret_key="placeholder",
        crawl_max_bytes=crawl_max_bytes,
    )


async def test_a_caller_supplied_budget_is_used_instead_of_limits_max_bytes() -> None:
    """`crawl_site`'s `budget` parameter, not `limits.max_bytes`, is what actually binds when
    both are given — and the caller's PRIOR spend on that object is included in
    `stats["bytes_fetched"]`, which is what proves the injected object is the one this
    function spends from, rather than a fresh `ByteBudget(limits.max_bytes)` built inside
    it. `limits.max_bytes` here is deliberately generous enough that it would never itself
    bind at the byte count this test reaches — only the caller's smaller, pre-spent `budget`
    can explain the result.
    """
    body = b"x" * 200

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    budget = ByteBudget(1000)
    budget.take(500)

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "http://seed.test/",
            limits=_limits(max_bytes=50_000_000),
            budget=budget,
            resolver=_fake_resolver(),
        )

    assert result.stats["bytes_fetched"] == 700
    assert budget.used == 700


async def test_a_huge_sitemap_does_not_starve_the_page_crawl() -> None:
    """The ticket's own "a 40 MB sitemap must not leave the page crawl with nothing"
    scenario, driven for real across both modules: `discover_sitemap_urls`
    (`internals/sitemap.py`) spends most of a small, shared `ByteBudget` on an oversized
    `/sitemap.xml` — 80% of `crawl_max_bytes` — and the SAME budget object is then handed to
    `crawl_site` for the seed fetch, exactly as `service.py` wires the two together.

    Without `internals/sitemap.py`'s `_DiscoveryBudget` giving discovery its own, tighter
    share of the run's budget, discovery would spend directly from the full shared budget,
    the oversized sitemap would consume nearly all of it, and the seed fetch below would
    raise `ByteBudgetExceededError` — turning a site with a huge sitemap and a perfectly
    reachable seed into a FAILED run (`crawler.py` catches that into `seed_error`, and
    `CrawlService` turns a `seed_error` into a failed run).

    **The seed page here is deliberately 300 KB, not two bytes, and that is what gives this
    test its teeth.** The oversized sitemap is 800 KB of a 1 MB run budget, so an
    implementation that let discovery spend from the full shared budget would leave only
    ~200 KB — enough for a token-sized seed, and NOT enough for this one. A 2-byte seed would
    survive that broken implementation and the test would pass while the bug it is named
    after was live. Sizing the seed above the leftover is what makes "the page crawl was not
    starved" an assertion rather than a hope.

    Two independent things are pinned below, and both go red under a regression:
    `budget.used` staying inside discovery's share catches the charge-ordering bug in
    `_DiscoveryBudget.take` directly, and `seed_error is None` catches its consequence.
    """
    settings = _settings(crawl_max_bytes=1_000_000)
    huge_sitemap = b"a" * int(settings.crawl_max_bytes * 0.8)
    # Larger than what a starved run would have left over (1 MB - 800 KB), so the seed fetch
    # can only succeed if discovery genuinely stayed inside its own share.
    seed_body = "s" * 300_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, content=huge_sitemap)
        # PER-191: `discover_sitemap_urls` now fetches `/robots.txt` unconditionally, before
        # the sitemap probes. Without this branch it would fall through to the `seed_body`
        # default below and serve 300 KB at `/robots.txt` too — tripping the 12.5% discovery
        # byte share on ROBOTS alone, before `/sitemap.xml` is ever reached, and silently
        # gutting what this test is actually pinning (that the oversized SITEMAP does not
        # starve the seed fetch that follows it).
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=seed_body)

    budget = ByteBudget(settings.crawl_max_bytes)

    async with _client(handler) as client:
        discovery = await discover_sitemap_urls(
            client,
            "http://seed.test",
            budget=budget,
            settings=settings,
            resolver=_fake_resolver(),
        )
        spent_by_discovery = budget.used
        result = await crawl_site(
            client,
            "http://seed.test/",
            limits=_limits(max_bytes=settings.crawl_max_bytes),
            budget=budget,
            resolver=_fake_resolver(),
        )

    # Discovery harvested nothing (the fixture is `"a" * N`, not valid XML) and — the point
    # of this test — never charged the run for more than its share, even though the document
    # it tried to read arrived as one chunk far larger than that share.
    assert discovery.urls == []
    assert spent_by_discovery <= int(settings.crawl_max_bytes * SITEMAP_BYTE_SHARE)

    # The consequence: the seed still had the budget it needed.
    assert result.seed_error is None
    assert len(result.pages) == 1
    assert result.pages[0].url == "http://seed.test/"
    assert result.pages[0].content_bytes == len(seed_body)


# -----------------------------------------------------------------------------------------
# PER-178: `frontier_from_seed`, the depth-1 link-extraction fallback's half of `crawl_site`.
# `tests/test_crawl_links.py` pins the parser these tests feed in; `tests/test_run_persistence
# .py` pins the end-to-end wiring in `CrawlService`. What is pinned HERE is the contract
# between the two: when the callback is called, how many times, with what, and what the loop
# does with what it returns.
# -----------------------------------------------------------------------------------------


def _seed_links(page: CrawledPage) -> list[str]:
    """The real production callback, minus the ranking step `service.py` adds around it —
    `internals/links.py`'s `extract_links` over the page `crawl_site` just fetched. Used
    rather than a stub list so that "the second level is never reached" is a fact about the
    real parser, not about a fixture that declined to return anything."""
    return extract_links(page.content, base_url=page.url)


def _page(*hrefs: str) -> str:
    """An HTML page linking to each of `hrefs` — the shape a documentation site's navigation
    has, reduced to the one element this fallback reads."""
    links = "".join(f'<a href="{href}">{href}</a>' for href in hrefs)
    return f"<html><body>{links}</body></html>"


async def test_frontier_from_seed_fills_the_frontier_when_no_extra_urls_were_supplied() -> None:
    """The fallback's happy path at the loop level: nothing was known up front, the seed's
    own links become the frontier, and those pages are fetched like any other."""
    seed_html = _page("/docs/intro", "/docs/config", "https://elsewhere.test/off-origin")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, html=seed_html)
        return httpx.Response(200, html=_page())

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            frontier_from_seed=_seed_links,
            resolver=_fake_resolver(),
        )

    assert result.seed_error is None
    assert sorted(page.url for page in result.pages) == [
        "https://seed.test/",
        "https://seed.test/docs/config",
        "https://seed.test/docs/intro",
    ]


async def test_a_link_found_on_a_frontier_page_is_never_extracted_or_followed() -> None:
    """**The [Depth] criterion.** Depth 1 means the SEED page's links, and nothing else.

    Every page this crawl serves — the seed and both frontier pages — links to a distinct
    second-level URL. If anything ever handed a fetched frontier page back to the extractor,
    `/level-2-from-*` would be requested and would appear in `pages`; the assertions below go
    red on the first such request rather than on a downstream symptom of it.

    The callback is also counted: depth 1 is not "the second level is filtered out later", it
    is "the second level is never looked for", which is only true if `frontier_from_seed` is
    invoked exactly once per run, on the seed alone.
    """
    seen_by_callback: list[str] = []
    requested: list[str] = []

    def counting_callback(page: CrawledPage) -> list[str]:
        seen_by_callback.append(page.url)
        return _seed_links(page)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested.append(path)
        if path == "/":
            return httpx.Response(200, html=_page("/first", "/second"))
        if path == "/first":
            return httpx.Response(200, html=_page("/level-2-from-first"))
        if path == "/second":
            return httpx.Response(200, html=_page("/level-2-from-second"))
        raise AssertionError(f"a second-level URL was fetched: {path}")

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            frontier_from_seed=counting_callback,
            resolver=_fake_resolver(),
        )

    assert seen_by_callback == ["https://seed.test/"], "the callback ran once, on the seed only"
    assert sorted(requested) == ["/", "/first", "/second"]
    assert sorted(page.url for page in result.pages) == [
        "https://seed.test/",
        "https://seed.test/first",
        "https://seed.test/second",
    ]
    assert result.stats["pages_crawled"] == 3
    assert result.stats["pages_failed"] == 0


async def test_the_seed_is_fetched_exactly_once_when_the_link_fallback_runs() -> None:
    """**The [Cost] criterion.** The seed page's HTML is already in hand — `crawl_site`
    fetched it, and `CrawledPage.content` holds the body — so filling the frontier from it
    must cost zero additional requests.

    Counted per path against the mock transport, which is the only way to actually prove it:
    a run that re-fetched the seed to read its links would still produce exactly the same
    `pages`, the same `stats["pages_crawled"]`, and the same artifact, and would differ ONLY
    in the request count. `bytes_fetched` is asserted alongside it as the second, independent
    witness — a second seed fetch would charge its body to the shared budget twice.
    """
    seed_html = _page("/one", "/two")
    requests_per_path: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests_per_path[path] = requests_per_path.get(path, 0) + 1
        return httpx.Response(200, html=seed_html if path == "/" else _page())

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            frontier_from_seed=_seed_links,
            resolver=_fake_resolver(),
        )

    assert requests_per_path == {"/": 1, "/one": 1, "/two": 1}
    assert result.stats["bytes_fetched"] == len(seed_html) + 2 * len(_page())


async def test_a_frontier_supplied_up_front_wins_and_the_callback_is_never_called() -> None:
    """The fallback is a fallback. When the caller already knows a frontier — `service.py`
    passing `select_urls(discover_sitemap_urls(...))` — the seed's own links are not consulted
    at all, and the two sources are never merged into one frontier."""
    called = False

    def callback(page: CrawledPage) -> list[str]:
        nonlocal called
        called = True
        return ["https://seed.test/from-the-page"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/from-the-page":
            raise AssertionError("the seed's own links must not be fetched when a frontier exists")
        return httpx.Response(200, html=_page("/from-the-page"))

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            extra_urls=["https://seed.test/from-the-sitemap"],
            frontier_from_seed=callback,
            resolver=_fake_resolver(),
        )

    assert called is False
    assert sorted(page.url for page in result.pages) == [
        "https://seed.test/",
        "https://seed.test/from-the-sitemap",
    ]


async def test_the_callback_is_never_called_when_the_seed_fetch_fails() -> None:
    """There is no page to read links off, and the run is already a failure. This is also
    what lets `CrawlService`'s hoisted `discovery_source`/`urls_discovered`/`urls_selected`
    defaults survive unchanged into a seed-failure row without extra plumbing."""
    called = False

    def callback(page: CrawledPage) -> list[str]:
        nonlocal called
        called = True
        return []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            frontier_from_seed=callback,
            resolver=_fake_resolver(),
        )

    assert called is False
    assert result.pages == []
    assert isinstance(result.seed_error, httpx.ConnectError)


async def test_a_link_derived_frontier_is_bound_by_the_same_page_cap() -> None:
    """A frontier that came from a page gets no special treatment: the same
    `limits.max_pages` truncation, and the same `cap_hit == "pages"` that
    `test_page_cap_truncates_the_frontier_and_is_a_success_not_an_error` pins for a
    caller-supplied one."""
    seed_html = _page(*(f"/p{i}" for i in range(10)))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=seed_html if request.url.path == "/" else _page())

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(max_pages=3),
            frontier_from_seed=_seed_links,
            resolver=_fake_resolver(),
        )

    assert len(result.pages) == 3
    assert result.cap_hit == "pages"
    assert result.stats["cap_hit"] == "pages"
    assert result.seed_error is None


async def test_a_seed_page_with_no_links_is_a_successful_single_page_run() -> None:
    """The [Edge] criterion's loop half: no links, no frontier, no failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><body><h1>Nothing to see</h1></body></html>")

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            frontier_from_seed=_seed_links,
            resolver=_fake_resolver(),
        )

    assert result.seed_error is None
    assert result.cap_hit is None
    assert [page.url for page in result.pages] == ["https://seed.test/"]


# -----------------------------------------------------------------------------------------
# PER-191: `is_allowed`, the seed-only robots.txt check, and the caller-supplied `gate`.
# -----------------------------------------------------------------------------------------


async def test_a_disallowed_seed_is_never_fetched_and_becomes_a_seed_error() -> None:
    """[Disallow — seed]. `is_allowed` refusing the seed means no request is ever made — the
    handler raises on ANY request, so a passing test proves the socket was never opened, not
    merely that the fetch happened to fail some other way."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"unreachable: a disallowed seed must never be fetched ({request.url})"
        )

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            is_allowed=lambda url: False,
            resolver=_fake_resolver(),
        )

    assert result.pages == []
    assert isinstance(result.seed_error, RobotsDisallowedError)
    assert result.cap_hit is None


async def test_an_allowed_seed_is_fetched_normally_with_the_same_predicate_shape() -> None:
    """Falsifiability partner: the identical predicate shape, answering `True` this time —
    the seed is fetched exactly as it would be with no `is_allowed` at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        result = await crawl_site(
            client,
            "https://seed.test/",
            limits=_limits(),
            is_allowed=lambda url: True,
            resolver=_fake_resolver(),
        )

    assert result.seed_error is None
    assert [page.url for page in result.pages] == ["https://seed.test/"]


async def test_an_injected_gate_is_used_instead_of_limits_politeness_delay() -> None:
    """Mirrors `test_a_caller_supplied_budget_is_used_instead_of_limits_max_bytes`: a
    caller-supplied `PolitenessGate`, built at a delay `limits.politeness_delay_ms` does not
    itself carry, is what actually binds — `limits`' own (zero) delay would space out nothing,
    so a gap this large can only be explained by the injected gate."""
    start_times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start_times.append(time.monotonic())
        return httpx.Response(200, text="ok")

    seed = "http://seed.test/"
    extra = [f"http://f{i}.test/" for i in range(2)]
    gate = PolitenessGate(0.05)

    async with _client(handler) as client:
        await crawl_site(
            client,
            seed,
            limits=_limits(politeness_delay_ms=0, concurrency=2),
            extra_urls=extra,
            gate=gate,
            resolver=_fake_resolver(),
        )

    frontier_starts = start_times[1:]
    assert len(frontier_starts) == 2
    assert frontier_starts[1] - frontier_starts[0] >= 0.045


async def test_widening_a_gate_between_phases_applies_to_the_frontier() -> None:
    """`PolitenessGate.widen_to`, called BEFORE `crawl_site` — exactly the shape `service.py`
    uses once `internals/robots.py`'s `effective_crawl_delay_ms` has been computed from a run's
    one `robots.txt` fetch."""
    start_times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start_times.append(time.monotonic())
        return httpx.Response(200, text="ok")

    seed = "http://seed.test/"
    extra = [f"http://f{i}.test/" for i in range(2)]
    gate = PolitenessGate(0.0)
    gate.widen_to(0.05)

    async with _client(handler) as client:
        await crawl_site(
            client,
            seed,
            limits=_limits(politeness_delay_ms=0, concurrency=2),
            extra_urls=extra,
            gate=gate,
            resolver=_fake_resolver(),
        )

    frontier_starts = start_times[1:]
    assert len(frontier_starts) == 2
    assert frontier_starts[1] - frontier_starts[0] >= 0.045
