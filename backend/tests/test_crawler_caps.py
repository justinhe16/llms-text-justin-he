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

from app.features.crawl.internals.crawler import CrawlLimits, crawl_site
from app.features.crawl.internals.ssrf import Resolver


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
        # gate itself: `_PolitenessGate` only ever sleeps to reach or exceed its target.
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
