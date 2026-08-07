"""Depth-1 link extraction: the crawl frontier's fallback for a site with no sitemap.

**One page in, a list of URLs out — and that is the whole feature.** `extract_links` reads the
`<a href>` values out of ONE document's HTML (the seed page's, and only the seed page's),
resolves them against that page's own URL, keeps the same-origin ones, and hands them back
deduped. `CrawlService.execute_run` runs it only when `internals/sitemap.py`'s
`discover_sitemap_urls` came back with nothing, feeds the result through the SAME
`internals/url_ranking.py` `select_urls` call the sitemap path uses, and the SAME
`crawl_max_pages` cap bounds what is actually fetched. Every documentation generator this
crawler targets — Mintlify, Docusaurus, VitePress, Nextra — ships a sitemap, so this is the
exception path, not the common one.

**Depth 1, and depth 1 is a property of the CALLER, not of this function.** This module is
handed one document and returns URLs; it has no idea whether that document was a seed or a
frontier page, and it never fetches anything, so it could not recurse if it wanted to. What
makes the depth rule true is that `service.py` calls it exactly once per run, on the seed page
alone, and the URLs it returns go straight into `crawl_site`'s frontier — whose own pages are
never handed back here. There is deliberately no frontier queue, no cycle detection, no
visited set, and no depth counter anywhere in this feature: with exactly one extraction per
run there is no second level for any of them to bound. A site that needs more than the seed's
own links plus ranking gets a worse `llms.txt`, which is an accepted v1 outcome
(ARCHITECTURE.md §11).

**This costs no extra request.** `crawl_site` already fetched the seed, and
`CrawledPage.content` holds that response's raw decoded body — the HTML this function parses.
Nothing in this module opens a socket, reads a clock, or touches the database; it is pure and
deterministic in the same category as `internals/extract.py`, `internals/url_ranking.py`,
`internals/payload.py`, and `internals/run_stats.py`. `tests/test_crawl_links.py` pins the
"no second seed fetch" property with a request counter rather than leaving it to this
paragraph.

**lxml, because trafilatura already brought it.** `internals/extract.py` runs trafilatura over
every fetched HTML page, and trafilatura parses with lxml — which `requirements.txt` already
pins on its own line, at first-order status, precisely because it decides what comes out of a
page. So the parser here is one this image already loads, not a second HTML dependency
(BeautifulSoup, selectolax) added to read one attribute. This is not in tension with
`internals/sitemap.py` rejecting lxml for its own job: that module parses XML, where the
stdlib's `ElementTree` is a genuine alternative and `lxml.etree.fromstring` needs an
encode-plus-parser dance to accept a document carrying an encoding declaration. There is no
stdlib equivalent for real-world, malformed HTML — `html.parser` gives you tag events, not a
tree — and the encode dance is exactly what `_hrefs` below does deliberately, for the same
reason.

**Every href is parsed, then judged; nothing is judged by string prefix.** `mailto:`, `tel:`,
`javascript:`, and `data:` are not enumerated in a deny-list here. They are dropped by the
same-origin rule, which requires a candidate to parse to the same `(scheme, host, port)` as
`base_url` — and `http`/`https` is the only scheme that can. A deny-list would have to keep up
with `sms:`, `ftp:`, `webcal:`, `intent:`, `blob:`, and whatever a browser learns next; the
origin comparison is closed by construction and needs no maintenance. Fragment-only hrefs
(`#install`) ARE special-cased, because they resolve to `base_url` itself rather than to a
different origin, and "the page you are already on" is not a frontier entry.
`tests/test_crawl_links.py` names each of these individually anyway, so the behaviour is
pinned per-scheme regardless of which rule implements it.

**`<base href>` is honoured for RESOLUTION and ignored for ORIGIN, which is not a compromise
but the only correct pair.** A document's declared base is what a browser resolves its
relative hrefs against, so ignoring it would mis-resolve every link on a page that has one.
But the origin a candidate is then compared to is the PAGE's own — `base_url` — never the
declared base's: otherwise a page declaring `<base href="https://evil.test/">` could nominate
an entirely different site's URLs into this run's frontier. With the pair as written, such a
page resolves its relatives onto `evil.test` and every one of them is then dropped as
off-origin, which is both what a browser would agree those links point at and the outcome that
costs this crawler nothing.

**What this module deliberately does not do.** It does not interpret `rel="nofollow"`,
`rel="canonical"`, or any other link relation. It reads `<a href>` and nothing else — not
`<link>`, not `<area>`, not `<iframe src>`, not a `<script>`-built router table. And it
normalizes nothing beyond stripping the fragment: `select_urls` normalizes every candidate it
is given (lowercasing, tracking parameters, trailing slashes), and doing half of that job here
would leave two modules with an opinion about what a URL "is" and no way to tell which one was
consulted.
"""

import logging
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import lxml.html


logger = logging.getLogger(__name__)

MAX_LINKS: Final = 10_000
"""A defensive ceiling on how many links one page may contribute, not a tuning knob — the
same role, and the same reasoning, as `Settings.crawl_sitemap_max_urls`'s own comment gives
for discovery's 50,000. A module constant rather than a `Settings` field for the reason
`internals/sitemap.py`'s `SITEMAP_BYTE_SHARE` states: it is a property of this algorithm's
safety margin, not something a deployment should be tuning.

The arithmetic that makes it generous: `crawl_max_pages` defaults to 100, so the ranked
frontier is cut to 99 entries no matter how many candidates arrive, and a documentation
page's own navigation carries tens of links, not thousands. What this bounds is the one
pathological shape those numbers do not — a 50 MiB body (`crawl_max_bytes`) of nothing but
`<a href>` elements holds millions of them, and normalizing and scoring millions of
candidates in `select_urls` would cost real seconds and real memory on a small Fly machine
to produce the same 99 URLs. Counted AFTER same-origin filtering and deduping, so a page
padded with off-origin or repeated links spends nothing against it."""

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}
"""The same two entries `url_ranking._DEFAULT_PORTS`, `sitemap._DEFAULT_PORTS`, and
`websites/internals/url_normalize._DEFAULT_PORTS` each declare. Reimplemented here rather
than imported, for the reason `internals/sitemap.py`'s own copy documents: `url_ranking`'s
version is reachable only through a `_NormalizedCandidate`, which would drag that module's
tracking-parameter stripping into a comparison that needs none, and `url_normalize` belongs
to a different feature entirely (ARCHITECTURE.md §3.1 forbids importing another feature's
`internals/`)."""


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """`(scheme, host.lower(), port-or-default)` for `url`, or `None` if it is not a usable
    same-origin comparison key: an unparseable URL, a non-http(s) scheme, a hostless URL, or
    a port `urlsplit(...).port` itself raises `ValueError` on.

    Byte-for-byte the same rule as `internals/sitemap.py`'s helper of the same name, and the
    same rule `url_ranking._origin` applies downstream — deliberately, so that a link this
    module keeps is never one `select_urls` would then drop under `"off_origin"`, and vice
    versa. Returning `None` for every non-http(s) scheme is also what silently disposes of
    `mailto:`, `tel:`, `javascript:` and `data:` hrefs (module docstring).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return (scheme, host.lower(), port if port is not None else _DEFAULT_PORTS[scheme])


def _parse(html: str) -> tuple[str | None, list[str]]:
    """The document's declared `<base href>` (or `None`), and every `<a href>` value in
    document order, exactly as the document wrote them — unresolved, unfiltered, and
    including the empty and fragment-only ones.

    Only the FIRST `<base href>` is read, because that is the one HTML gives meaning to: the
    document base URL is defined by the first `<base>` element that has an `href`, and a
    second one is inert.

    **Never raises**, the same contract `internals/extract.py`'s `extract_content` offers and
    for the same reason: this runs on attacker-supplied bytes, once per run, and a document
    that cannot be parsed is a frontier of one page rather than a failed run.

    **Encoded to UTF-8 bytes rather than handed to lxml as a `str`**, which is not incidental:
    `lxml.html.document_fromstring` raises `ValueError: Unicode strings with encoding
    declaration are not supported` for any `str` carrying an XML declaration — and an XHTML
    documentation page beginning `<?xml version="1.0" encoding="utf-8"?>` is exactly that.
    Parsing bytes under an explicit `encoding="utf-8"` parser sidesteps it and simultaneously
    stops a stale `<meta charset>` from re-decoding a body `internals/fetcher.py` has already
    decoded once. The `errors="replace"` on the way in cannot lose anything real:
    `CrawledPage.content` was itself decoded with `errors="replace"`, so every character in it
    is already representable.

    Values are copied through `str(...)` on the way out. lxml's XPath returns "smart strings"
    (`_ElementUnicodeResult`), which are `str` subclasses that keep a reference to the element
    they came from — returning them as-is would keep the whole parsed tree alive for as long
    as any caller held one href.
    """
    try:
        root = lxml.html.document_fromstring(
            html.encode("utf-8", errors="replace"),
            parser=lxml.html.HTMLParser(encoding="utf-8"),
        )
        base_values = root.xpath("//base/@href")
        href_values = root.xpath("//a/@href")
    except Exception:
        # Broad on purpose, exactly as `extract_content` is: lxml's recovering HTML parser
        # raises `ParserError` for an empty document, `ValueError` for several malformed
        # inputs, and enumerating the rest would be a list that goes stale the first time
        # lxml is upgraded. The contract is "never raises", and catching everything is the
        # only way to actually offer it.
        logger.warning("links: could not parse the seed page's HTML", exc_info=True)
        return None, []
    declared_bases = [str(value) for value in base_values if isinstance(value, str)]
    hrefs = [str(value) for value in href_values if isinstance(value, str)]
    return (declared_bases[0] if declared_bases else None), hrefs


def _resolve(href: str, *, base_url: str) -> str | None:
    """Resolve one raw href against `base_url` and strip its fragment, or `None` if it is not
    a candidate at all.

    Handles all four shapes the ticket names, and handles them the same way a browser does,
    because `urljoin` implements RFC 3986's reference resolution: relative (`./guide`,
    `guide`, `../guide`), root-relative (`/guide`), protocol-relative (`//host/guide`, which
    inherits `base_url`'s scheme), and absolute (`https://host/guide`, returned unchanged).

    `None` for an href that is empty, whitespace-only, or fragment-only (`#install`) — all
    three resolve to `base_url` itself, i.e. to the page already being read. Every OTHER way
    an href can fail to be a frontier entry (a `mailto:`, an off-origin link, an unparseable
    string) is left to the same-origin check in `extract_links`, so that this function has one
    job and there is exactly one place a candidate can be judged on its origin.

    The fragment is stripped rather than kept because `https://host/guide#install` and
    `https://host/guide` are the same fetch — a fragment is resolved by the client and never
    sent to the server — so keeping it would put the same page in the frontier twice under two
    spellings. Nothing else about the URL is normalized here (module docstring).
    """
    value = href.strip()
    if not value or value.startswith("#"):
        return None
    try:
        resolved = urljoin(base_url, value)
        parts = urlsplit(resolved)
    except ValueError:
        # `urlsplit` raises on a malformed IPv6 literal, and `urljoin` calls it internally.
        # An href this module cannot parse is one it cannot judge the origin of either, so it
        # is dropped rather than passed downstream on the chance `select_urls` copes.
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def extract_links(html: str, *, base_url: str) -> list[str]:
    """Extract the same-origin links from ONE page's HTML — the seed's, and only the seed's.

    Pure and deterministic: no network, no clock, no I/O. The same `html` and `base_url`
    always produce the identical list, in the identical order, which is what lets the
    frontier this feeds be as reproducible as the sitemap-derived one it stands in for.

    Args:
        html: The page's raw body as text — `CrawledPage.content`, exactly as
            `internals/fetcher.py` decoded it, not pre-cleaned and not pre-validated. Being
            handed something that is not HTML at all is an expected input, not a caller
            error: it yields `[]`.
        base_url: The page's own final, post-redirect URL (`CrawledPage.url`), never the URL
            the run was configured with — a site that redirects `http://` to `https://www.`
            would otherwise have every one of its links resolved and origin-checked against
            an origin it no longer serves. Relative hrefs resolve against it (or against the
            document's own `<base href>`, if it declares one), and its origin — always its
            own, never the declared base's — is what every candidate is compared to.
            Keyword-only so a two-string call site cannot silently transpose its arguments.

    Returns:
        Up to `MAX_LINKS` absolute, same-origin, fragment-free URLs, deduped, in the order
        their `<a>` elements appear in the document. `[]` for a page with no links, HTML that
        cannot be parsed, or a `base_url` this module cannot itself parse — that last case
        fails CLOSED, mirroring `select_urls`'s own unparseable-seed rule, because an origin
        that cannot be determined is one nothing can safely be compared against.

        Every URL is returned as resolved, NOT normalized — `select_urls` owns normalization,
        and duplicates that differ only in case or a trailing slash are merged there rather
        than here (module docstring).
    """
    origin = _origin_key(base_url)
    if origin is None:
        logger.warning("links: given an unparseable base URL %r", base_url)
        return []

    declared_base, hrefs = _parse(html)
    # The document's own `<base href>` decides what relatives resolve AGAINST; `origin`,
    # computed above from the page's real URL, decides what they are compared TO. See the
    # module docstring for why those two must not be the same value. A declared base is
    # itself resolved against the page's URL first, since it is allowed to be relative.
    resolution_base = base_url
    if declared_base is not None:
        try:
            resolution_base = urljoin(base_url, declared_base)
        except ValueError:
            # A declared base this module cannot parse is one it cannot resolve against; the
            # page's own URL is the honest fallback, and it is what a document without a
            # `<base>` would have used anyway.
            logger.warning("links: ignoring an unparseable <base href> on %s", base_url)

    # A dict, not a set: `dict` preserves insertion order, so deduping cannot disturb the
    # document order this function promises. (`select_urls` re-sorts by score anyway, but a
    # function whose output order depends on set iteration is one whose tests pass or fail
    # for reasons unrelated to what they assert.)
    links: dict[str, None] = {}
    for href in hrefs:
        candidate = _resolve(href, base_url=resolution_base)
        if candidate is None or _origin_key(candidate) != origin:
            continue
        links[candidate] = None
        if len(links) >= MAX_LINKS:
            logger.warning(
                "links: stopped at the %d-link ceiling for %s",
                MAX_LINKS,
                base_url,
                extra={"url": base_url, "max_links": MAX_LINKS},
            )
            break

    logger.debug(
        "links: %s -> %d same-origin link(s)",
        base_url,
        len(links),
        extra={"url": base_url, "links": len(links)},
    )
    return list(links)
