"""Tests for `app.features.crawl.internals.sitemap` — sitemap discovery.

Hermeticity contract, same as every other suite in this feature: every `httpx.AsyncClient`
here is built with `transport=httpx.MockTransport(...)`, enforced by this suite's autouse
`_forbid_real_network` fixture (`tests/conftest.py`), and every call into `discover_sitemap_
urls` injects a fake `Resolver` instead of touching real DNS. NO NETWORK IN CI — a test that
slips a real `httpx.AsyncHTTPTransport` fails loudly rather than silently reaching out.

The central measurement instrument, `_Site` below, is modelled on `conftest.py`'s
`_JwksServer`: it records `requested_paths`, appended inside the `MockTransport` handler on
every request regardless of what it returns, which is what makes "was a socket ever opened
for this URL" a fact this suite can assert rather than infer. Its DNS-side counterpart is
`_resolver`'s own `resolved_hosts` list. Both are what make this file's SSRF tests
(`test_a_credentialed_same_origin_sitemap_target_is_refused_by_the_ssrf_guard` and its two
siblings) prove the guard actually RAN, not merely that nothing crashed — see their own
docstrings for the reasoning `internals/sitemap.py`'s module docstring's "same-origin,
enforced twice" section sets up.

**`fetch_page` dials the validated IP, never the hostname** (`internals/fetcher.py`'s own
docstring), so `request.url.host` inside a `MockTransport` handler is always the resolved
address, not `docs.test`. `tests/test_crawler_caps.py`'s `_host()` helper works around this by
reading the `Host` header instead, for suites juggling several simultaneous hostnames — this
file never needs to, because every test here drives exactly one origin (`ORIGIN`, below) at a
time, and `internals/ssrf.py`'s `connect_url` construction preserves the original request
PATH untouched, so `request.url.path` is exactly what `sitemap.py` asked for regardless of
which host or IP it was for.
"""

import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import _JsonLogCapture

from app.core.settings import Settings
from app.features.crawl.internals import sitemap
from app.features.crawl.internals.fetcher import ByteBudget
from app.features.crawl.internals.sitemap import (
    SITEMAP_BYTE_SHARE,
    DiscoveryResult,
    discover_sitemap_urls,
)
from app.features.crawl.internals.ssrf import Resolver


ORIGIN = "https://docs.test"
OFF_ORIGIN = "https://cdn.other.test"

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    """See `tests/test_crawl_extract.py`'s helper of the same name — identical contract."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _settings(
    *,
    crawl_max_bytes: int = 52_428_800,
    crawl_sitemap_max_documents: int = 5,
    crawl_sitemap_max_urls: int = 50_000,
) -> Settings:
    """Build a `Settings` from explicit values only — the same "ignore the ambient
    environment" contract `tests/test_settings.py`'s own `_settings` helper documents."""
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost:5432/llms_text",
        redis_url="redis://localhost:6379/0",
        supabase_url="https://example.supabase.co",
        supabase_secret_key="placeholder",
        crawl_max_bytes=crawl_max_bytes,
        crawl_sitemap_max_documents=crawl_sitemap_max_documents,
        crawl_sitemap_max_urls=crawl_sitemap_max_urls,
    )


def _urlset_with_one_url(loc: str) -> str:
    """A minimal, namespaced one-`<url>` `<urlset>` body. `loc` is used verbatim, so callers
    control the scheme, host, and path themselves."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{loc}</loc></url></urlset>"
    )


def _index_with_children(locs: Sequence[str]) -> str:
    """A minimal, namespaced `<sitemapindex>` body with one `<sitemap><loc>` per entry in
    `locs`, in order. `locs` are used verbatim, exactly like `_urlset_with_one_url` above."""
    children = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{children}'
        "</sitemapindex>"
    )


class _Site:
    """A fake origin: registered `path -> (status, body, content_type)` responses, plus
    `requested_paths` — the socket-opening half of this file's "did the guard actually run"
    measurement (see the module docstring). Every request lands here regardless of what it
    returns, including a 404 for a path nothing registered.
    """

    def __init__(self, *, default_status: int = 404) -> None:
        self._responses: dict[str, tuple[int, str, dict[str, str]]] = {}
        self._default_status = default_status
        self.requested_paths: list[str] = []

    def add(
        self, path: str, status: int, body: str = "", *, content_type: str | None = None
    ) -> None:
        headers = {"Content-Type": content_type} if content_type else {}
        self._responses[path] = (status, body, headers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requested_paths.append(request.url.path)
        status, body, headers = self._responses.get(
            request.url.path, (self._default_status, "", {})
        )
        return httpx.Response(status, text=body, headers=headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), follow_redirects=False
        )


def _resolver(
    mapping: dict[str, Sequence[str]] | None = None, *, default: str = "8.8.8.8"
) -> tuple[Resolver, list[str]]:
    """A `Resolver` that answers from `mapping` (falling back to `default` for anything
    else), paired with `resolved_hosts` — the DNS-side half of this file's "did the guard
    actually run" measurement, alongside `_Site.requested_paths`' socket-opening half.
    """
    resolved_hosts: list[str] = []
    known = mapping or {}

    async def resolve(host: str, port: int) -> Sequence[str]:
        resolved_hosts.append(host)
        return known.get(host, [default])

    return resolve, resolved_hosts


# -----------------------------------------------------------------------------------------
# 1. A plain sitemap: lastmod/priority parsed, changefreq ignored.
# -----------------------------------------------------------------------------------------


async def test_a_plain_sitemap_yields_urls_with_lastmod_and_priority_parsed() -> None:
    site = _Site()
    site.add("/sitemap.xml", 200, _load_fixture("sitemap.xml"), content_type="application/xml")
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "sitemap"
    assert [u.url for u in result.urls] == [
        "https://docs.test/",
        "https://docs.test/docs/getting-started",
        "https://docs.test/docs/configuration",
        "https://docs.test/pricing",
        "https://docs.test:443/docs/ports",
    ]
    getting_started = result.urls[1]
    assert getting_started.lastmod == datetime(2025, 3, 10, 9, 15, tzinfo=UTC)
    assert getting_started.priority == 0.9

    bare = result.urls[3]
    assert bare.url == "https://docs.test/pricing"
    assert bare.lastmod is None
    assert bare.priority is None

    assert all(u.source == "sitemap" for u in result.urls)


async def test_changefreq_is_ignored() -> None:
    """The one entry carrying `<changefreq>` (the fixture's root `/`) still parses its
    `<loc>`/`<lastmod>`/`<priority>` normally — `DiscoveredUrl` has no field for changefreq
    at all, so the only way this rule could be violated is the unrecognized element breaking
    the parse of the sibling fields it sits beside."""
    site = _Site()
    site.add("/sitemap.xml", 200, _load_fixture("sitemap.xml"), content_type="application/xml")
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    root = next(u for u in result.urls if u.url == "https://docs.test/")
    assert root.lastmod == datetime(2025, 3, 14)
    assert root.priority == 1.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025-03-14", datetime(2025, 3, 14)),
        ("2025-03-10T09:15:00Z", datetime(2025, 3, 10, 9, 15, tzinfo=UTC)),
        (
            "2025-02-01T00:00:00+02:00",
            datetime(2025, 2, 1, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        ),
        ("2025-03", None),
        ("", None),
        ("garbage", None),
        (None, None),
    ],
)
def test_lastmod_formats_parse(text: str | None, expected: datetime | None) -> None:
    assert sitemap._parse_lastmod(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.0", 0.0),
        ("1.0", 1.0),
        ("0.9", 0.9),
        ("1.5", None),
        ("-1", None),
        ("high", None),
        ("", None),
        (None, None),
    ],
)
def test_priority_outside_zero_to_one_is_discarded(
    text: str | None, expected: float | None
) -> None:
    assert sitemap._parse_priority(text) == expected


# -----------------------------------------------------------------------------------------
# 2. Sitemap indices: one level of recursion, merged, entry point named honestly.
# -----------------------------------------------------------------------------------------


async def test_a_sitemap_index_is_recursed_one_level_and_child_urls_are_merged() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add(
        "/sitemap_index.xml",
        200,
        _load_fixture("sitemap_index.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/docs.xml",
        200,
        _load_fixture("sitemap_child_docs.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/guides.xml",
        200,
        _load_fixture("sitemap_child_guides.xml"),
        content_type="application/xml",
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "sitemap_index"
    assert [u.url for u in result.urls] == [
        "https://docs.test/docs/one",
        "https://docs.test/docs/two",
        "https://docs.test/docs/three",
        "https://docs.test/guides/one",
        "https://docs.test/guides/two",
    ]
    assert site.requested_paths == [
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemaps/docs.xml",
        "/sitemaps/guides.xml",
    ]


async def test_a_sitemap_index_served_at_the_well_known_sitemap_path_still_reports_sitemap() -> (
    None
):
    """The SAME index body as the test above, served at `/sitemap.xml` directly instead of
    `/sitemap_index.xml` — children still merge, but `source` is `"sitemap"`, not
    `"sitemap_index"`, because `discovery_source` names the ENTRY POINT, not the document's
    own root element (module docstring)."""
    site = _Site()
    site.add(
        "/sitemap.xml", 200, _load_fixture("sitemap_index.xml"), content_type="application/xml"
    )
    site.add(
        "/sitemaps/docs.xml",
        200,
        _load_fixture("sitemap_child_docs.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/guides.xml",
        200,
        _load_fixture("sitemap_child_guides.xml"),
        content_type="application/xml",
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "sitemap"
    assert len(result.urls) == 5


# -----------------------------------------------------------------------------------------
# 3. robots.txt: used only when neither well-known path exists, and only `Sitemap:` is read.
# -----------------------------------------------------------------------------------------


async def test_robots_sitemap_directive_is_used_when_neither_well_known_path_exists() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, _load_fixture("robots.txt"))
    site.add(
        "/sitemaps/docs.xml",
        200,
        _load_fixture("sitemap_child_docs.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/guides.xml",
        200,
        _load_fixture("sitemap_child_guides.xml"),
        content_type="application/xml",
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "robots"
    assert len(result.urls) == 5
    assert site.requested_paths.count("/robots.txt") == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sitemap: https://docs.test/a.xml", ["https://docs.test/a.xml"]),
        ("SITEMAP: https://docs.test/a.xml", ["https://docs.test/a.xml"]),
        ("sitemap: https://docs.test/a.xml", ["https://docs.test/a.xml"]),
        ("   Sitemap: https://docs.test/a.xml", ["https://docs.test/a.xml"]),
        ("# a comment\nSitemap: https://docs.test/a.xml", ["https://docs.test/a.xml"]),
        ("Sitemap: https://docs.test/a.xml   # trailing comment", ["https://docs.test/a.xml"]),
        ("Sitemap: /relative.xml", ["https://docs.test/relative.xml"]),
        (
            "Sitemap: https://docs.test/a.xml\nSitemap: https://docs.test/a.xml",
            ["https://docs.test/a.xml"],
        ),
        (
            "Sitemap: https://docs.test/a.xml\nsitemap: https://docs.test/b.xml",
            ["https://docs.test/a.xml", "https://docs.test/b.xml"],
        ),
    ],
)
def test_robots_directive_parsing(text: str, expected: list[str]) -> None:
    assert sitemap._robots_sitemap_urls(text, origin=ORIGIN) == expected


async def test_robots_disallow_and_crawl_delay_are_ignored() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add(
        "/robots.txt",
        200,
        "User-agent: *\nDisallow: /\nAllow:\nCrawl-delay: 10\nSitemap: /docs.xml\n",
    )
    site.add(
        "/docs.xml", 200, _load_fixture("sitemap_child_docs.xml"), content_type="application/xml"
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "robots"
    assert [u.url for u in result.urls] == [
        "https://docs.test/docs/one",
        "https://docs.test/docs/two",
        "https://docs.test/docs/three",
    ]


# -----------------------------------------------------------------------------------------
# 4. Same-origin, enforced twice: dropped from the return value, and never even fetched.
# -----------------------------------------------------------------------------------------


async def test_off_origin_urls_in_a_sitemap_are_dropped() -> None:
    site = _Site()
    site.add("/sitemap.xml", 200, _load_fixture("sitemap.xml"), content_type="application/xml")
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    urls = [u.url for u in result.urls]
    assert "https://cdn.other.test/assets/logo.svg" not in urls
    assert "not a url" not in urls
    assert "https://docs.test/" in urls
    assert "https://docs.test/docs/getting-started" in urls
    assert "https://docs.test/docs/configuration" in urls
    assert "https://docs.test/pricing" in urls


async def test_the_same_origin_check_collapses_the_schemes_default_port() -> None:
    site = _Site()
    site.add("/sitemap.xml", 200, _load_fixture("sitemap.xml"), content_type="application/xml")
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert "https://docs.test:443/docs/ports" in [u.url for u in result.urls]


async def test_an_off_origin_child_sitemap_is_never_fetched() -> None:
    site = _Site()
    site.add(
        "/sitemap.xml", 200, _load_fixture("sitemap_index.xml"), content_type="application/xml"
    )
    site.add(
        "/sitemaps/docs.xml",
        200,
        _load_fixture("sitemap_child_docs.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/guides.xml",
        200,
        _load_fixture("sitemap_child_guides.xml"),
        content_type="application/xml",
    )
    resolver, resolved_hosts = _resolver()

    async with site.client() as client:
        await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert "cdn.other.test" not in resolved_hosts
    assert "/sitemaps/assets.xml" not in site.requested_paths


async def test_an_off_origin_robots_sitemap_target_is_never_fetched() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, _load_fixture("robots.txt"))
    site.add(
        "/sitemaps/docs.xml",
        200,
        _load_fixture("sitemap_child_docs.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/guides.xml",
        200,
        _load_fixture("sitemap_child_guides.xml"),
        content_type="application/xml",
    )
    resolver, resolved_hosts = _resolver()

    async with site.client() as client:
        await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert "cdn.other.test" not in resolved_hosts
    assert "/sitemaps/assets.xml" not in site.requested_paths


# -----------------------------------------------------------------------------------------
# 5. Depth stops at one level, and the caps (documents, URLs).
# -----------------------------------------------------------------------------------------


async def test_a_grandchild_index_is_fetched_but_not_recursed_into() -> None:
    """The first child is ITSELF an index — the same body as the top-level one, reused
    deliberately (fixtures section, `PLAN-PER-176.md`) to prove `allow_index=False` stops
    depth-1 recursion rather than merely "this particular document happened to have no
    children." Its own children (docs.xml again, guides.xml, the off-origin asset) are never
    independently requested."""
    site = _Site()
    site.add(
        "/sitemap.xml", 200, _load_fixture("sitemap_index.xml"), content_type="application/xml"
    )
    site.add(
        "/sitemaps/docs.xml",
        200,
        _load_fixture("sitemap_index.xml"),
        content_type="application/xml",
    )
    site.add(
        "/sitemaps/guides.xml",
        200,
        _load_fixture("sitemap_child_guides.xml"),
        content_type="application/xml",
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert [u.url for u in result.urls] == [
        "https://docs.test/guides/one",
        "https://docs.test/guides/two",
    ]
    assert site.requested_paths == ["/sitemap.xml", "/sitemaps/docs.xml", "/sitemaps/guides.xml"]


async def test_no_more_than_crawl_sitemap_max_documents_are_fetched() -> None:
    child_locs = [f"{ORIGIN}/children/{i}.xml" for i in range(6)]

    site = _Site()
    site.add("/sitemap.xml", 200, _index_with_children(child_locs), content_type="application/xml")
    for i, _loc in enumerate(child_locs):
        site.add(
            f"/children/{i}.xml",
            200,
            _urlset_with_one_url(f"{ORIGIN}/page-{i}"),
            content_type="application/xml",
        )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client,
            ORIGIN,
            budget=ByteBudget(10_000_000),
            settings=_settings(crawl_sitemap_max_documents=3),
            resolver=resolver,
        )

    # index + 2 children — a cap is a SUCCESS, not an empty result.
    assert len(site.requested_paths) == 3
    assert result.urls != []


async def test_a_404_probe_counts_against_the_document_cap() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, "Sitemap: /real.xml\n")
    site.add(
        "/real.xml", 200, _urlset_with_one_url(f"{ORIGIN}/page"), content_type="application/xml"
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client,
            ORIGIN,
            budget=ByteBudget(10_000_000),
            settings=_settings(crawl_sitemap_max_documents=2),
            resolver=resolver,
        )

    assert result.source == "none"
    assert "/real.xml" not in site.requested_paths


async def test_robots_txt_itself_does_not_count_against_the_document_cap() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, "Sitemap: /real.xml\n")
    site.add(
        "/real.xml", 200, _urlset_with_one_url(f"{ORIGIN}/page"), content_type="application/xml"
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client,
            ORIGIN,
            budget=ByteBudget(10_000_000),
            settings=_settings(crawl_sitemap_max_documents=3),
            resolver=resolver,
        )

    assert result.source == "robots"
    assert "/real.xml" in site.requested_paths


async def test_the_url_cap_truncates_without_failing() -> None:
    site = _Site()
    site.add("/sitemap.xml", 200, _load_fixture("sitemap.xml"), content_type="application/xml")
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client,
            ORIGIN,
            budget=ByteBudget(10_000_000),
            settings=_settings(crawl_sitemap_max_urls=2),
            resolver=resolver,
        )

    assert result.source == "sitemap"
    assert len(result.urls) == 2


# -----------------------------------------------------------------------------------------
# 6. SSRF: the guard actually ran, not merely "nothing crashed".
#
# A naive "an internal-looking Sitemap: target is refused" test proves nothing here: the
# same-origin filter above already refuses an off-origin target BEFORE `fetch_page` is ever
# called, so the SSRF guard would never be entered and a test built that way would pass
# identically against a `discover_sitemap_urls` with the guard deleted. Every test below is
# built so the LOCAL same-origin check passes and only `internals/ssrf.py`'s guard can say
# no — see `internals/sitemap.py`'s module docstring for why that check exists at all.
# -----------------------------------------------------------------------------------------


async def test_a_credentialed_same_origin_sitemap_target_is_refused_by_the_ssrf_guard(
    json_logs: _JsonLogCapture,
) -> None:
    """`robots.txt` declares a `Sitemap:` target carrying credentials
    (`http://user:pass@docs.test/...`) — `urlsplit(...).hostname` strips the userinfo, so
    `_origin_key` reports the SAME origin as the site (asserted explicitly below, so the
    reader can see the local same-origin check let it through) and only
    `internals/ssrf.py`'s check 3 ("URL must not contain credentials") refuses it.

    Assertion (a) is "the target's own path never reaches the transport", not "exactly one
    request was made": `discover_sitemap_urls` always tries both well-known probes before
    ever falling back to `robots.txt` (discovery order, module docstring), so those two 404s
    are expected here and orthogonal to what this test is proving.
    """
    origin = "http://docs.test"
    target = "http://user:pass@docs.test/credentialed-target.xml"
    assert sitemap._origin_key(target) == sitemap._origin_key(origin)

    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, f"Sitemap: {target}\n")
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, origin, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
    assert "/credentialed-target.xml" not in site.requested_paths

    warnings = [
        r for r in json_logs.at("app.features.crawl.internals.ssrf") if r["level"] == "WARNING"
    ]
    assert any("credentials" in r["reason"] for r in warnings)


async def test_a_sitemap_target_whose_host_resolves_privately_is_refused_before_a_socket_opens(
    json_logs: _JsonLogCapture,
) -> None:
    """A STATEFUL, rebinding-shaped resolver: `docs.test` answers PUBLICLY for the two
    well-known probes and the `robots.txt` fetch itself, then PRIVATELY the moment the
    same-origin, robots-declared target is looked up — so only the guard, not the same-origin
    pre-filter, can refuse it. Three independent measurements prove the guard actually ran for
    the target: (a) the resolver was asked about `docs.test` a FOURTH time — proving DNS was
    genuinely consulted for the target, which is what tells "refused" apart from "never
    attempted"; (b) the target's own path never reaches the transport; (c) a WARNING from
    `internals.ssrf` names the address that was refused. `10.0.0.5` is refused as "not a
    globally routable address" rather than "a private address" — `ssrf._classify` checks
    `not address.is_global` before `is_private`, and every RFC 1918 address answers `False`
    to `is_global` too, so the broader check always fires first; see that function's own
    docstring for why the narrower, named checks stay explicit anyway.
    """
    origin = "http://docs.test"
    target_path = "/private-target.xml"
    answers = iter(["8.8.8.8", "8.8.8.8", "8.8.8.8", "10.0.0.5"])
    resolved_hosts: list[str] = []

    async def resolver(host: str, port: int) -> Sequence[str]:
        resolved_hosts.append(host)
        return [next(answers)]

    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, f"Sitemap: http://docs.test{target_path}\n")

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, origin, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
    assert resolved_hosts == ["docs.test", "docs.test", "docs.test", "docs.test"]
    assert target_path not in site.requested_paths

    warnings = [
        r for r in json_logs.at("app.features.crawl.internals.ssrf") if r["level"] == "WARNING"
    ]
    assert any(
        "10.0.0.5" in r["reason"] and "not a globally routable" in r["reason"] for r in warnings
    )


async def test_the_same_robots_sitemap_target_is_fetched_when_it_resolves_publicly() -> None:
    """Falsifiability check for the two refusal tests above: byte-for-byte the same origin,
    `robots.txt` body, and target path, but the resolver answers PUBLICLY throughout — the
    target IS fetched and its URL comes back. Without this test, both refusal tests would
    pass identically against a `discover_sitemap_urls` that always returned `[]`.
    """
    origin = "http://docs.test"
    target_path = "/private-target.xml"
    resolver, _resolved_hosts = _resolver()  # every host resolves publicly, every call

    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, f"Sitemap: http://docs.test{target_path}\n")
    site.add(
        target_path,
        200,
        _urlset_with_one_url("http://docs.test/page"),
        content_type="application/xml",
    )

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, origin, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "robots"
    assert [u.url for u in result.urls] == ["http://docs.test/page"]
    assert target_path in site.requested_paths


async def test_an_ssrf_refused_sitemap_target_still_returns_a_result() -> None:
    """Not fatal: with `docs.test` resolving privately throughout (so every fetch this run
    could make is refused), `discover_sitemap_urls` still returns an ordinary empty result —
    the same shape a missing sitemap produces — rather than letting `SsrfBlockedError`
    propagate out of it."""
    origin = "http://docs.test"
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, "Sitemap: http://docs.test/private.xml\n")
    resolver, _resolved_hosts = _resolver({"docs.test": ["10.0.0.5"]})

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, origin, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")


# -----------------------------------------------------------------------------------------
# 7. The byte share: charged honestly, and bounded.
# -----------------------------------------------------------------------------------------


async def test_sitemap_bytes_are_charged_against_the_run_byte_budget() -> None:
    sitemap_body = _load_fixture("sitemap.xml")
    sitemap_bytes = sitemap_body.encode("utf-8")

    site = _Site()
    site.add("/sitemap.xml", 200, sitemap_body, content_type="application/xml")
    resolver, _resolved_hosts = _resolver()

    budget = ByteBudget(1_000_000)
    async with site.client() as client:
        await discover_sitemap_urls(
            client, ORIGIN, budget=budget, settings=_settings(), resolver=resolver
        )

    assert budget.used == len(sitemap_bytes)


async def test_discovery_cannot_spend_more_than_its_share_of_the_run_budget() -> None:
    settings = _settings(crawl_max_bytes=10_000)  # share = 2,500 bytes

    async def huge_body() -> AsyncIterator[bytes]:
        # Streamed in many small chunks, which is the shape a real transport produces. The
        # complementary case — a whole oversized body arriving as ONE chunk, which is what
        # `httpx.MockTransport` does with a plain `content=` response — is covered by
        # `tests/test_crawler_caps.py::test_a_huge_sitemap_does_not_starve_the_page_crawl`.
        # `_DiscoveryBudget` has to hold the share in both shapes, and it holds them by
        # different halves of `take()`: here it stops partway through the stream, there it
        # refuses the single chunk before the run's budget is charged at all.
        for _ in range(200):
            yield b"a" * 256  # 51,200 bytes total — far more than the 2,500-byte share

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200, content=huge_body(), headers={"Content-Type": "application/xml"}
            )
        return httpx.Response(404)

    resolver, _resolved_hosts = _resolver()
    budget = ByteBudget(settings.crawl_max_bytes)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=budget, settings=settings, resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
    # Pinned against the SHARE itself, not a loose `crawl_max_bytes // 2`: the share is the
    # number `_DiscoveryBudget` actually enforces, and asserting the looser bound would stay
    # green under a charge-ordering regression that let discovery spend up to half the run.
    assert budget.used <= int(settings.crawl_max_bytes * SITEMAP_BYTE_SHARE)


async def test_exhausting_the_share_mid_index_keeps_the_children_already_parsed() -> None:
    settings = _settings(crawl_max_bytes=1_000_000)  # share = 250,000 bytes
    child1_loc = f"{ORIGIN}/children/child1.xml"
    child2_loc = f"{ORIGIN}/children/child2.xml"
    index_body = _index_with_children([child1_loc, child2_loc])
    child1_body = _urlset_with_one_url(f"{ORIGIN}/page-1")

    async def huge_child2() -> AsyncIterator[bytes]:
        for _ in range(2000):
            yield b"b" * 1000  # 2,000,000 bytes total — far more than the remaining share

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(404)
        if request.url.path == "/sitemap_index.xml":
            return httpx.Response(200, text=index_body, headers={"Content-Type": "application/xml"})
        if request.url.path == "/children/child1.xml":
            return httpx.Response(
                200, text=child1_body, headers={"Content-Type": "application/xml"}
            )
        if request.url.path == "/children/child2.xml":
            return httpx.Response(
                200, content=huge_child2(), headers={"Content-Type": "application/xml"}
            )
        return httpx.Response(404)

    resolver, _resolved_hosts = _resolver()
    budget = ByteBudget(settings.crawl_max_bytes)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=budget, settings=settings, resolver=resolver
        )

    assert result.source == "sitemap_index"
    assert [u.url for u in result.urls] == [f"{ORIGIN}/page-1"]


# -----------------------------------------------------------------------------------------
# 8. No sitemap anywhere: a clean, unremarkable "none".
# -----------------------------------------------------------------------------------------


async def test_a_site_with_no_sitemap_and_no_robots_returns_none() -> None:
    site = _Site()
    site.add("/sitemap.xml", 404)
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 404)
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
    assert len(site.requested_paths) == 3


# -----------------------------------------------------------------------------------------
# 9. Hostile or merely wrong documents: refused or skipped, never raised.
# -----------------------------------------------------------------------------------------


async def test_malformed_xml_returns_no_urls_instead_of_raising(json_logs: _JsonLogCapture) -> None:
    site = _Site()
    site.add(
        "/sitemap.xml", 200, _load_fixture("sitemap_malformed.xml"), content_type="application/xml"
    )
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 404)
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
    warnings = [
        r for r in json_logs.at("app.features.crawl.internals.sitemap") if r["level"] == "WARNING"
    ]
    assert warnings


async def test_a_document_declaring_a_doctype_is_refused_before_parsing(
    json_logs: _JsonLogCapture,
) -> None:
    site = _Site()
    site.add(
        "/sitemap.xml",
        200,
        _load_fixture("sitemap_billion_laughs.xml"),
        content_type="application/xml",
    )
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 404)
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
    warnings = [
        r for r in json_logs.at("app.features.crawl.internals.sitemap") if r["level"] == "WARNING"
    ]
    assert any("DOCTYPE" in r["message"] for r in warnings)


def test_the_stdlib_parser_would_have_expanded_the_billion_laughs_fixture() -> None:
    """Characterization, not a test of this module's own behaviour. Parses the checked-in
    bomb fixture with BARE `xml.etree.ElementTree.fromstring` — no DOCTYPE guard at all — to
    prove it really is one. Measured on this project's pinned interpreter (CPython 3.12.13,
    expat 2.8.1): 4 levels of entity nesting expand to exactly 100,000 characters, safely
    under expat's own amplification-activation threshold (8 MiB) — see
    `internals/sitemap.py`'s module docstring for why this project does not rely on that
    threshold firing anyway. `_parse_document`'s DOCTYPE refusal, pinned by the test above,
    is what actually protects `discover_sitemap_urls`; this test only pins down what it is
    refusing.
    """
    text = _load_fixture("sitemap_billion_laughs.xml")
    root = ET.fromstring(text)
    loc_text = root[0][0].text
    assert loc_text is not None
    assert len(loc_text) == 100_000


async def test_an_html_error_page_served_at_the_sitemap_path_does_not_stop_discovery() -> None:
    site = _Site()
    site.add(
        "/sitemap.xml",
        200,
        "<!DOCTYPE html><html><body>404 Not Found</body></html>",
        content_type="text/html",
    )
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 200, f"Sitemap: {ORIGIN}/real.xml\n")
    site.add(
        "/real.xml", 200, _urlset_with_one_url(f"{ORIGIN}/page"), content_type="application/xml"
    )
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result.source == "robots"
    assert [u.url for u in result.urls] == [f"{ORIGIN}/page"]


async def test_a_non_2xx_response_carrying_a_valid_sitemap_is_not_parsed() -> None:
    site = _Site()
    site.add(
        "/sitemap.xml",
        404,
        _urlset_with_one_url(f"{ORIGIN}/should-not-appear"),
        content_type="application/xml",
    )
    site.add("/sitemap_index.xml", 404)
    site.add("/robots.txt", 404)
    resolver, _resolved_hosts = _resolver()

    async with site.client() as client:
        result = await discover_sitemap_urls(
            client, ORIGIN, budget=ByteBudget(10_000_000), settings=_settings(), resolver=resolver
        )

    assert result == DiscoveryResult([], "none")
