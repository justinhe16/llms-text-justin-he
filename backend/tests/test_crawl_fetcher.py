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
from pathlib import Path

import httpx
import pytest

from app.features.crawl.internals.fetcher import (
    MAX_REDIRECTS,
    ByteBudget,
    ByteBudgetExceededError,
    FetchError,
    _looks_like_html,
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

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    """See `tests/test_crawl_extract.py`'s helper of the same name — identical contract."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


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


# --- PER-177: extraction wired into fetch_page --------------------------------------------


async def test_an_html_response_is_extracted_into_title_description_and_markdown() -> None:
    """The happy path: a real documentation-generator fixture, served with a genuine
    `text/html` `Content-Type`, comes back with everything `internals/extract.py` can find on
    it — this is the end-to-end proof that `fetch_page` actually calls the extractor, not
    just that the extractor works in isolation (`tests/test_crawl_extract.py` already covers
    that)."""
    fixture = _load_fixture("docusaurus_page.html")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=fixture)

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000_000)

    async with _build_client(handler) as client:
        page = await fetch_page(
            client, "https://public.test/docs/config", budget=budget, resolver=resolver
        )

    assert page.title == "Configuration | Acme Docs"
    assert page.description == (
        "How to configure the Acme CLI, including every supported field and its default."
    )
    assert "# Configuration" in page.markdown
    assert page.is_empty is False
    # `content` stays the raw body regardless of what extraction made of it — the two fields
    # are never the same string once extraction has run.
    assert page.content == fixture


async def test_a_javascript_shell_is_empty_but_keeps_its_title() -> None:
    """The [Empty] case, driven through `fetch_page` rather than `extract_content` directly.

    `markdown` is empty and `is_empty` is set, exactly as `tests/test_crawl_extract.py`
    already asserts for this fixture in isolation. The title is asserted here too, and it is
    NOT `None` — a JavaScript shell is real HTML with a real `<title>`, and `fetch_page`
    deliberately does not null it just because the page carries no body. See this module's
    own comment, right where `extracted.title` is copied onto the returned `CrawledPage`, for
    why: nulling it would be branching on `is_empty`, which ARCHITECTURE.md §3.4 forbids.
    """
    fixture = _load_fixture("js_shell_page.html")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=fixture)

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000_000)

    async with _build_client(handler) as client:
        page = await fetch_page(client, "https://public.test/", budget=budget, resolver=resolver)

    assert page.is_empty is True
    assert page.markdown == ""
    assert page.title == "Acme Console", "the title must survive even though the page is empty"
    assert page.description == "The Acme Console."


async def test_a_non_html_response_is_a_successful_fetch_with_nothing_extracted() -> None:
    """A JSON API spec, served with an explicit non-HTML `Content-Type`, is not a failed
    fetch — it is a page with nothing for `internals/extract.py` to have found, and the
    extractor is never even called (see `_looks_like_html`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openapi": "3.1.0", "info": {"title": "Acme API"}})

    resolver = _fake_resolver({"public.test": [PUBLIC_IP]})
    budget = ByteBudget(max_bytes=1_000_000)

    async with _build_client(handler) as client:
        page = await fetch_page(
            client, "https://public.test/openapi.json", budget=budget, resolver=resolver
        )

    assert page.status == 200
    assert page.title is None
    assert page.description is None
    assert page.markdown == ""
    assert page.is_empty is True


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        # Declared HTML, in the shapes real servers actually send it.
        ("text/html", True),
        ("text/html; charset=utf-8", True),
        ("TEXT/HTML; CHARSET=UTF-8", True),
        ("  text/html  ", True),
        ("application/xhtml+xml", True),
        ("application/xhtml+xml; charset=utf-8", True),
        # An explicit non-HTML declaration is a real assertion, and is honored.
        ("application/json", False),
        ("application/pdf", False),
        ("text/plain; charset=utf-8", False),
        ("text/xml", False),
        ("image/png", False),
        # No media type declared at all — the permissive path.
        (None, True),
        ("", True),
        ("   ", True),
        ("; charset=utf-8", True),
        # Malformed but non-empty is NOT "no declaration": the server said something, and
        # nothing it said was `text/html`. See `_looks_like_html`'s docstring on why this is
        # narrower than "unparseable".
        ("garbage", False),
    ],
)
def test_looks_like_html_classifies_content_types(content_type: str | None, expected: bool) -> None:
    """Every branch of `_looks_like_html`, asserted directly rather than through `fetch_page`.

    The permissive-on-no-declaration rows are the ones worth the parametrization: they are a
    deliberate design decision (see the function's own docstring — an absent `Content-Type` is
    not an assertion that the body is not HTML), and a future edit that "tightened" them into
    returning `False` would silently start counting real HTML pages as empty in
    `runs.stats["pages_empty_content"]`, which is exactly the measurement that counter exists
    to keep honest.
    """
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = httpx.Response(200, headers=headers, content=b"<html></html>")

    assert _looks_like_html(response) is expected
