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

This module now has one opinion about a page's content, and one only: whether the response
looks like HTML, and if so what `internals/extract.py` makes of it — a title, a description,
a markdown body, and whether that body counts as empty. That parse happens here, inline with
the one fetch this module already makes, rather than as a second pass over `CrawledPage`
later, so a page is only ever decoded and read once. This module still has no opinion about
concurrency, deadlines, or the page cap — those remain the crawl loop's job
(`internals/crawler.py`). It fetches exactly one URL, following its own redirects, and
returns exactly one `CrawledPage` or raises.
"""

import logging
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.features.crawl.internals.extract import ExtractedContent, extract_content
from app.features.crawl.internals.ssrf import Resolver, ValidatedTarget, validate_url
from app.features.crawl.schemas import CrawledPage


logger = logging.getLogger(__name__)

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


_HTML_MEDIA_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})


def _looks_like_html(response: httpx.Response) -> bool:
    """Is it worth handing this response's body to `extract_content`?

    Reads `Content-Type`'s media type only — everything before the first `;`, stripped and
    lowercased, so `text/html; charset=utf-8` and `TEXT/HTML` both match `text/html` — and
    returns `True` for that and `application/xhtml+xml`, `False` for any other explicit media
    type (`application/json`, `application/pdf`, `text/plain`, ...), and, deliberately,
    **`True`** when the header is missing or carries nothing this function can parse as a
    media type at all.

    That last case is a considered choice, not an oversight, and the conservative-sounding
    alternative — treat "no header" as "not HTML" — was rejected because it is actually the
    less accurate rule, not merely the more cautious one. `extract_content` is contractually
    never-raises (`internals/extract.py`): fed a non-HTML body, it returns its empty result,
    which is exactly what the conservative rule would have produced anyway, just reached by
    measurement instead of assumption. Fed a genuine HTML body from a server that simply
    omitted the header — not a hypothetical; plenty of misconfigured origins do this — the
    conservative rule would instead skip extraction entirely and hand back
    `title=None, markdown=""`, indistinguishable in `runs.stats["pages_empty_content"]` from an
    actual JavaScript shell. That is a false positive in the exact counter this ticket adds to
    measure how often headless rendering would matter (`internals/crawler.py`,
    `internals/run_stats.py`) — silently corrupting the measurement to save a parse that was
    going to cost almost nothing anyway. So the permissive rule is never LESS accurate than
    the conservative one; its only cost is one wasted `extract_content` call on a body that
    turns out not to be markup. An absent `Content-Type` is not an assertion that the body is
    not HTML, and this function does not treat it as one. An EXPLICIT non-HTML media type is a
    real assertion and is still honored — this gate is about avoiding wasted parses on
    responses that have already said what they are, not a safety boundary; `extract_content`
    already has to tolerate arbitrary attacker-supplied bytes regardless of what this function
    decides.
    """
    content_type = response.headers.get("content-type")
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type:
        return True
    return media_type in _HTML_MEDIA_TYPES


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
        host-based URL — never the IP address that was actually dialed. `title`,
        `description`, `markdown`, and `is_empty` come from `internals/extract.py`'s
        `extract_content` when the response's `Content-Type` looks like HTML (see
        `_looks_like_html`); for anything else those four are, respectively, `None`, `None`,
        `""`, and `True`, and the extractor is never called. Either way this is a
        SUCCESSFUL fetch — a page that fails to extract, or was never HTML in the first
        place, is not a failed fetch, only a page with nothing to show for the parse.

    Raises:
        SsrfBlockedError: `url`, or a `Location` it redirected to, failed validation.
        FetchError: a redirect had no `Location`, the chain exceeded `MAX_REDIRECTS`, or a
            redirect would have downgraded `https` to `http`.
        ByteBudgetExceededError: the response body exceeded the shared byte budget.
        httpx.HTTPError: a network-level failure — timeout, connection error, TLS error —
            propagates unchanged; this function does not catch or reclassify it.
    """
    current_url = url

    # `hop` doubles as "how many redirects have already been followed" for the DEBUG lines
    # below: it is 0 on the first attempt, so a response that is not a redirect logs
    # `redirects=0` without a separate counter to keep in sync with the loop.
    for hop in range(MAX_REDIRECTS + 1):
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
                # `%s` args, not an f-string: at INFO (the deployed default) this line is
                # never formatted at all, and building the string up front would spend the
                # cost on every hop of every fetch regardless of whether anything reads it.
                logger.debug(
                    "fetch: redirect %s -> %s (%d)",
                    target.url,
                    next_url,
                    response.status_code,
                    extra={
                        "from_url": target.url,
                        "to_url": next_url,
                        "status": response.status_code,
                    },
                )
                current_url = next_url
                continue

            content, content_bytes = await _read_body_within_budget(response, budget)

            if _looks_like_html(response):
                extracted = extract_content(content, url=target.url)
            else:
                # Not HTML, by its own declared `Content-Type` — skip the parse rather than
                # hand `extract_content` a JSON or PDF body it would return this exact shape
                # for anyway (see `_looks_like_html`'s docstring for why an ABSENT header does
                # NOT take this branch). Built directly rather than imported from
                # `extract.py`'s private `_EMPTY` constant, which is that module's own
                # implementation detail, not a shape this one should reach across the module
                # boundary for.
                extracted = ExtractedContent(
                    title=None, description=None, markdown="", is_empty=True
                )

            # `title` is DELIBERATELY never nulled just because `extracted.is_empty` is
            # `True`, even though a literal reading of this ticket's own acceptance criteria
            # would null it. A JavaScript shell is real HTML with a real `<title>`, and
            # `extract_content` already keeps it on purpose — see that function's own comment,
            # and see `test_crawl_extract.py`'s
            # `test_a_javascript_shell_is_empty_and_keeps_the_metadata_it_does_have`, an
            # already-passing PER-173 test that asserts exactly this. ARCHITECTURE.md §3.4 is
            # explicit that `is_empty` is instrumentation, never a branch — "Nothing branches
            # on it" — and nulling `title` BECAUSE the page is empty would be exactly the
            # branch that sentence forbids. Per CLAUDE.md, ARCHITECTURE.md wins over a ticket
            # that contradicts it, so `extracted.title` survives here unconditionally.
            #
            # One DEBUG line per fetch outcome, never the body OR the extracted text: both
            # `content` and `markdown` are a page's actual prose and have no business in
            # `fly logs`, so only byte counts and flags are here.
            logger.debug(
                "fetch: %s -> %d (%d bytes, %d redirect(s), empty=%s)",
                target.url,
                response.status_code,
                content_bytes,
                hop,
                extracted.is_empty,
                extra={
                    "url": target.url,
                    "status": response.status_code,
                    "bytes": content_bytes,
                    "redirects": hop,
                    "is_empty": extracted.is_empty,
                },
            )
            return CrawledPage(
                url=target.url,
                status=response.status_code,
                title=extracted.title,
                content=content,
                fetched_at=datetime.now(UTC),
                content_bytes=content_bytes,
                description=extracted.description,
                markdown=extracted.markdown,
                is_empty=extracted.is_empty,
            )

    raise FetchError(f"exceeded {MAX_REDIRECTS} redirects")
