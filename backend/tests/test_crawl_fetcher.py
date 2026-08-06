"""Tests for `app.features.crawl.internals.fetcher` — the one bounded, redirect-following,
SSRF-checked GET.

Every client here is built with `transport=httpx.MockTransport(...)`, per this suite's
autouse `_forbid_real_network` fixture (tests/conftest.py), and every fetch injects a fake
`Resolver` instead of touching real DNS. That combination is what turns "no socket was
opened for the second hop" from an assumption into something this suite can actually
measure: the assertion is a call count on the transport handler, not an absence of a log
line.
"""

from collections.abc import AsyncIterator, Callable, Sequence

import httpx
import pytest

from app.features.crawl.internals.fetcher import (
    MAX_REDIRECTS,
    ByteBudget,
    ByteBudgetExceededError,
    FetchError,
    fetch_page,
)
from app.features.crawl.internals.ssrf import Resolver, SsrfBlockedError


def _fake_resolver(mapping: dict[str, Sequence[str]]) -> Resolver:
    """See tests/test_ssrf.py's helper of the same name — identical contract."""

    async def resolve(host: str, port: int) -> Sequence[str]:
        return mapping.get(host, [])

    return resolve


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """An `AsyncClient` backed by nothing but `httpx.MockTransport(handler)`, per this
    suite's autouse `_forbid_real_network` fixture (tests/conftest.py)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


PUBLIC_IP = "8.8.8.8"


async def test_connects_to_the_validated_ip_and_preserves_host_and_sni() -> None:
    """The TOCTOU defense's other half, verified on the request that actually reaches the
    transport: httpx dials the IP `validate_url` picked, but the far end still sees the
    original hostname in both the `Host` header and the TLS SNI extension."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        page = await fetch_page(client, "https://public.test/", budget=budget, resolver=resolver)

    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == PUBLIC_IP
    assert request.headers["Host"] == "public.test"
    assert request.extensions.get("sni_hostname") == "public.test"
    assert page.url == "https://public.test/"
    assert page.status == 200
    assert page.content == "ok"
    assert page.title is None


async def test_non_default_port_is_included_in_the_host_header() -> None:
    """`:443` on `http` is allowed but is not `http`'s default port."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        await fetch_page(client, "http://public.test:443/", budget=budget, resolver=resolver)

    assert captured[0].headers["Host"] == "public.test:443"
    assert captured[0].url.host == PUBLIC_IP
    assert captured[0].url.port == 443


async def test_redirect_to_a_private_address_is_rejected_before_the_second_request() -> None:
    """The first request reaches the transport; the second is refused before a socket
    would ever open for it."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(302, headers={"Location": "http://internal.test/"})

    resolver = _fake_resolver({"public.test": [PUBLIC_IP], "internal.test": ["10.0.0.5"]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        with pytest.raises(SsrfBlockedError):
            await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)

    assert call_count == 1


def _redirect_chain_handler(num_redirects: int) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that redirects to itself `num_redirects` times, then serves a 200.

    All `num_redirects` redirects target the same URL — the loop in `fetch_page` counts
    HOPS, not distinct hosts, so a self-redirect exercises the hop limit identically to a
    chain of `num_redirects` distinct URLs while needing only one resolver entry.
    """
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] <= num_redirects:
            return httpx.Response(302, headers={"Location": "http://public.test/"})
        return httpx.Response(200, text="done")

    return handler


async def test_a_chain_of_five_hops_succeeds() -> None:
    assert MAX_REDIRECTS == 5
    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(_redirect_chain_handler(5)) as client:
        page = await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)

    assert page.status == 200
    assert page.content == "done"


async def test_a_chain_of_six_hops_exceeds_the_limit() -> None:
    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(_redirect_chain_handler(6)) as client:
        with pytest.raises(FetchError):
            await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)


async def test_https_to_http_downgrade_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://public.test/"})

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        with pytest.raises(FetchError):
            await fetch_page(client, "https://public.test/", budget=budget, resolver=resolver)


async def test_http_to_https_upgrade_is_allowed() -> None:
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(302, headers={"Location": "https://public.test/"})
        return httpx.Response(200, text="secure")

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        page = await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)

    assert page.content == "secure"
    assert page.url == "https://public.test/"


async def test_a_relative_redirect_resolves_against_the_host_based_url() -> None:
    """A relative `Location` must resolve against the original hostname, never the IP
    address `fetch_page` actually dialed — otherwise the next hop would rewrite its base
    onto the IP and lose the ability to send the right `Host` header."""
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(302, headers={"Location": "/next"})
        return httpx.Response(200, text="ok")

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        page = await fetch_page(
            client, "http://public.test/start", budget=budget, resolver=resolver
        )

    assert page.url == "http://public.test/next"


async def test_a_redirect_with_no_location_header_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        with pytest.raises(FetchError):
            await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)


async def test_byte_budget_aborts_mid_stream_without_pulling_every_chunk() -> None:
    """The body is an async generator that counts the chunks it has yielded, so this test
    can assert the crawl stopped mid-stream rather than merely "raised after reading
    everything" — the "aborted, not buffered" criterion the byte cap exists to satisfy."""
    yielded = 0
    total_chunks = 20
    chunk = b"x" * 100  # 2,000 bytes if every chunk were pulled

    async def body() -> AsyncIterator[bytes]:
        nonlocal yielded
        for _ in range(total_chunks):
            yielded += 1
            yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=250)  # crosses the cap partway through the 20 chunks

    async with _build_client(handler) as client:
        with pytest.raises(ByteBudgetExceededError):
            await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)

    assert yielded < total_chunks


async def test_a_lying_content_length_is_still_stopped_by_the_streamed_counter() -> None:
    """`Content-Length` says 10 bytes; the real body is far larger. `fetch_page` must never
    have trusted the header — the streamed counter is what stops it."""
    real_body = b"z" * 200_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "10"}, content=real_body)

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000)

    async with _build_client(handler) as client:
        with pytest.raises(ByteBudgetExceededError):
            await fetch_page(client, "http://public.test/", budget=budget, resolver=resolver)
