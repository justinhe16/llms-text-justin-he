"""One bounded, redirect-following, SSRF-checked GET.

`fetch_page()` is the only place in this feature that makes an HTTP request. Everything
that makes a single "fetch this URL" safe and boundable lives here:

* **Every hop is re-validated.** The seed URL and every `Location` a redirect points at go
  through `internals/ssrf.py`'s `validate_url()` before a request is made — not just the
  first one. A redirect is a URL exactly as attacker-influenced as the seed; skipping
  re-validation on hop two would make hop one's check theater.
* **httpx dials the address `validate_url()` picked, not the hostname.** See `ssrf.py`'s
  module docstring for why a second DNS lookup between check and connect is the whole
  vulnerability this ticket closes, and why that means this module must build its request
  around `target.connect_url` — never `target.host` — while still sending `target.host` as
  the `Host` header and, for `https`, as the TLS SNI name.
* **The body is a stream, never a buffer.** `response.aiter_bytes()` is read chunk by
  chunk, and each chunk is charged against a caller-supplied `ByteBudget` *before* it is
  kept — a response is never pulled entirely into memory before its size is known, so a
  hostile multi-gigabyte body is stopped as it arrives, not after.

This module has no opinion about pages, concurrency, deadlines, or the page cap — those are
the crawl loop's job (a later phase of this ticket). It fetches exactly one URL, following
its own redirects, and returns exactly one `CrawledPage` or raises.
"""

from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.features.crawl.internals.ssrf import Resolver, ValidatedTarget, validate_url
from app.features.crawl.schemas import CrawledPage


MAX_REDIRECTS = 5
"""How many redirect hops one fetch will follow before giving up. `fetch_page` makes at
most `MAX_REDIRECTS + 1` requests total — the redirects, plus the one response that is
finally not a redirect — so a chain of exactly `MAX_REDIRECTS` hops still succeeds and one
hop more does not."""

_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """A fetch failed for a reason specific to this module: a redirect with no
    `Location`, a redirect chain longer than `MAX_REDIRECTS`, or a redirect that would
    downgrade `https` to `http`.

    Deliberately distinct from `SsrfBlockedError` (raised by `validate_url` and left to
    propagate through this module unchanged) and from whatever `httpx` itself raises for a
    timeout, a connection failure, or a TLS error — a caller that wants to tell "this
    module's own bookkeeping refused the fetch" apart from "the network refused it" can
    already do so by exception type, so this class does not need to wrap or reclassify
    either of those.
    """


class ByteBudgetExceededError(Exception):
    """Raised by `ByteBudget.take()` when accepting more bytes would exceed the cap.

    Deliberately its own type rather than a generic `RuntimeError`: the crawl loop (a later
    phase of this ticket) needs to tell "this run hit its byte cap" apart from every other
    way a fetch can fail, because the former stops the whole run (`cap_hit="bytes"`) while
    the latter only fails one page.
    """


class ByteBudget:
    """A byte counter shared across every fetch in one crawl run.

    One instance is created per run and handed to every `fetch_page` call for that run —
    concurrent fetches (the crawl loop's job, a later phase) share the same object, so the
    cap is enforced across the *run*, not reset per page. Defined here, in the fetcher
    rather than the crawl loop, because charging a chunk against the budget has to happen
    inside the streaming loop below, at the point where a chunk exists and before it is
    kept — nowhere else sees bytes early enough to enforce a run-wide cap while streaming
    rather than after buffering.

    Not guarded by an `asyncio.Lock`: `take()` does no `await`, so two concurrent fetches
    cannot interleave *inside* one call to it — ordinary Python attribute mutation is
    already atomic with respect to the event loop as long as nothing inside the critical
    section yields.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._used = 0

    @property
    def used(self) -> int:
        """Bytes accepted so far, across every fetch that shares this budget."""
        return self._used

    def take(self, num_bytes: int) -> None:
        """Charge `num_bytes` against the budget, or raise if that would exceed it.

        Call this BEFORE keeping a chunk, never after — see `fetch_page`'s streaming loop.
        A chunk that would tip the budget over is never appended to the page's content:
        the partial body collected so far is discarded, not returned truncated, because a
        truncated HTML/text body is not a meaningfully "successful, but short" fetch.
        """
        if self._used + num_bytes > self._max_bytes:
            raise ByteBudgetExceededError(
                f"crawl byte budget exceeded: {self._used + num_bytes} > {self._max_bytes}"
            )
        self._used += num_bytes


async def _read_body_within_budget(response: httpx.Response, budget: ByteBudget) -> tuple[str, int]:
    """Stream `response`'s body, charging every chunk against `budget` before keeping it.

    Never calls `response.aread()`, `.content`, or `.text` — every one of those buffers the
    whole body before this function would get a chance to stop it, which is exactly the
    "buffered before the cap was consulted" failure the byte cap exists to prevent. Never
    trusts `Content-Length` either: it is advisory, a server can lie about it or omit it
    entirely, and the streamed counter here is correct regardless of what the header says.

    Returns:
        The decoded body, and the raw byte count actually charged against `budget` for this
        response — the sum of `len(chunk)` over every chunk kept, before decoding. That
        second number is `CrawledPage.content_bytes`'s source of truth: it is what came off
        the wire, which `len(decoded.encode())` cannot reconstruct once `errors="replace"`
        has had a chance to change the length (see `content_bytes`'s own docstring).
    """
    chunks: list[bytes] = []
    raw_bytes = 0
    async for chunk in response.aiter_bytes():
        budget.take(len(chunk))
        raw_bytes += len(chunk)
        chunks.append(chunk)
    encoding = response.encoding or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace"), raw_bytes


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    budget: ByteBudget,
    resolver: Resolver | None = None,
) -> CrawledPage:
    """Fetch `url`, following its own redirects by hand, and return the final page.

    `client` is expected to be one built by `build_crawl_client` — in particular,
    `follow_redirects=False` and a configured per-request timeout — so this function passes
    no `timeout=` of its own and relies entirely on the client's own configuration; there is
    exactly one place a crawl's request timeout is decided, and it is not here.

    Args:
        client: The shared crawl client. Every request this function makes goes through it.
        url: The URL to fetch — a crawl seed, or (in a later phase) a frontier entry.
        budget: The run-wide `ByteBudget` every response body is charged against while
            streaming. Shared across concurrent fetches by the caller.
        resolver: Forwarded to `validate_url` on every hop. Tests inject a fake one; nothing
            in production code passes one.

    Returns:
        The final, non-redirect response as a `CrawledPage`, with `url` set to the final,
        host-based URL — never the IP address that was actually dialed.

    Raises:
        SsrfBlockedError: `url`, or a `Location` it redirected to, failed validation.
        FetchError: a redirect had no `Location`, the chain exceeded `MAX_REDIRECTS`, or a
            redirect would have downgraded `https` to `http`.
        ByteBudgetExceededError: the response body exceeded the shared byte budget.
        httpx.HTTPError: a network-level failure — timeout, connection error, TLS error —
            propagates unchanged; this function does not catch or reclassify it.
    """
    current_url = url

    for _hop in range(MAX_REDIRECTS + 1):
        target: ValidatedTarget = await validate_url(current_url, resolver=resolver)

        extensions: dict[str, str] = {}
        if target.scheme == "https":
            extensions["sni_hostname"] = target.host

        async with client.stream(
            "GET",
            target.connect_url,
            headers={"Host": target.host_header},
            extensions=extensions,
            follow_redirects=False,
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                # The body of a redirect is never read: exiting this `async with` block
                # closes the response without a single call to `.aiter_bytes()`.
                location = response.headers.get("Location")
                if not location:
                    raise FetchError(
                        f"redirect response {response.status_code} had no Location header"
                    )
                next_url = urljoin(target.url, location)
                if target.scheme == "https" and urlsplit(next_url).scheme.lower() == "http":
                    raise FetchError("refusing to follow a redirect from https to http")
                current_url = next_url
                continue

            content, content_bytes = await _read_body_within_budget(response, budget)
            return CrawledPage(
                url=target.url,
                status=response.status_code,
                title=None,
                content=content,
                fetched_at=datetime.now(UTC),
                content_bytes=content_bytes,
            )

    raise FetchError(f"exceeded {MAX_REDIRECTS} redirects")
