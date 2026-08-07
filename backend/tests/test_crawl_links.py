"""Tests for `app.features.crawl.internals.links.extract_links` — the depth-1 fallback's
pure half.

This file is about ONE document in and a list of URLs out: what counts as a link, how each
href shape resolves, and what is filtered. The three criteria that are about the *pipeline*
rather than the parser live where the pipeline does — `tests/test_crawler_caps.py` pins that
the second level is never reached and that the seed is fetched exactly once, and
`tests/test_run_persistence.py` pins that a site with a sitemap never reaches this module at
all while a site without one does, ranked and capped like any other frontier.

Nothing here touches the network, so no `httpx.MockTransport` and no fake `Resolver` appear
below: `extract_links` is pure, and the autouse `_forbid_real_network` fixture
(tests/conftest.py) has nothing to stop.
"""

from pathlib import Path

from app.features.crawl.internals.links import MAX_LINKS, extract_links


_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    """See `tests/test_crawl_extract.py`'s helper of the same name — identical contract."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_a_seed_page_fixture_yields_its_same_origin_links_deduped_and_deterministic() -> None:
    """The happy path, against real generator markup rather than a hand-built string.

    `docusaurus_page.html` is the same fixture `tests/test_crawl_extract.py` extracts prose
    from, and it carries exactly the mix a documentation seed page does: a navbar and sidebar
    of root-relative links, two on-page anchors, an "Edit this page" link to GitHub, and a
    community link to Discord. The two off-origin links and the two fragment-only ones are
    gone; every same-origin link survives, once each, in document order.
    """
    html = _load_fixture("docusaurus_page.html")

    links = extract_links(html, base_url="https://docs.acme.test/docs/config")

    assert links == [
        "https://docs.acme.test/",
        "https://docs.acme.test/docs/intro",
        "https://docs.acme.test/blog",
        "https://docs.acme.test/docs/config",
        "https://docs.acme.test/docs/deploy",
        "https://docs.acme.test/docs/troubleshooting",
        "https://docs.acme.test/privacy",
        "https://docs.acme.test/terms",
    ]
    # Deterministic in the strong sense the module promises: a second call over the same
    # inputs is not merely equal-as-a-set, it is the identical list.
    assert extract_links(html, base_url="https://docs.acme.test/docs/config") == links


def test_every_href_shape_resolves_against_the_base_url() -> None:
    """Criterion 4, one assertion per shape the ticket names.

    The base URL deliberately has a path (`/docs/`) and a scheme (`https`) that both matter:
    `./guide` resolves relative to the directory, `/guide` discards it, `//host/guide`
    discards it and keeps the scheme, and an absolute URL discards both.
    """
    html = """
    <a href="./guide">relative</a>
    <a href="guide-bare">relative, no leading dot</a>
    <a href="../top">parent-relative</a>
    <a href="/guide">root-relative</a>
    <a href="//docs.acme.test/proto">protocol-relative</a>
    <a href="https://docs.acme.test/absolute">absolute</a>
    """

    links = extract_links(html, base_url="https://docs.acme.test/docs/")

    assert links == [
        "https://docs.acme.test/docs/guide",
        "https://docs.acme.test/docs/guide-bare",
        "https://docs.acme.test/top",
        "https://docs.acme.test/guide",
        "https://docs.acme.test/proto",
        "https://docs.acme.test/absolute",
    ]


def test_a_protocol_relative_href_inherits_the_base_urls_scheme_and_can_go_off_origin() -> None:
    """`//host/path` is same-origin only when `host` is the base's own — the scheme it
    inherits is never enough on its own. Split out from the shape test above because the two
    halves of protocol-relative resolution (inherits the scheme, keeps its own host) are
    easy to conflate and only one of them is about origin."""
    html = """
    <a href="//docs.acme.test/kept">same host</a>
    <a href="//cdn.acme.test/dropped">different host</a>
    """

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/kept"
    ]


def test_off_origin_and_non_navigational_hrefs_are_all_excluded() -> None:
    """Criterion 3, naming every excluded shape individually.

    `mailto:`, `tel:`, `javascript:` and `data:` are dropped by the same-origin rule rather
    than by a scheme deny-list (see the module docstring) — this test does not care which
    mechanism refuses them, only that all four are gone, which is what keeps that
    implementation choice free to change.
    """
    html = """
    <a href="/kept">the only survivor</a>
    <a href="https://other.test/off-origin">another site</a>
    <a href="http://docs.acme.test/downgraded">same host, different scheme</a>
    <a href="https://docs.acme.test:8443/other-port">same host, different port</a>
    <a href="mailto:support@acme.test">mailto</a>
    <a href="tel:+15550100">tel</a>
    <a href="javascript:void(0)">javascript</a>
    <a href="data:text/html,<b>hi</b>">data</a>
    <a href="#install">fragment only</a>
    <a href="">empty</a>
    <a>no href attribute at all</a>
    """

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/kept"
    ]


def test_fragments_are_stripped_before_deduping() -> None:
    """A fragment is resolved by the client and never sent to the server, so three spellings
    of one page are one frontier entry — and the stripped form is what comes back, not the
    first spelling seen."""
    html = """
    <a href="/docs/guide#install">with a fragment</a>
    <a href="/docs/guide">without</a>
    <a href="/docs/guide#configure">a different fragment</a>
    <a href="/docs/guide?v=2#install">a fragment behind a query</a>
    """

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/docs/guide",
        "https://docs.acme.test/docs/guide?v=2",
    ]


def test_a_seed_page_with_no_links_yields_an_empty_list() -> None:
    """Criterion 7's first half — the second half (that such a run still completes as a
    successful single-page run) is `tests/test_run_persistence.py`'s
    `test_a_site_whose_seed_page_has_no_links_still_completes_a_single_page_run`."""
    assert (
        extract_links(
            "<html><body><h1>No links here</h1></body></html>", base_url="https://a.test/"
        )
        == []
    )


def test_a_body_that_is_not_html_at_all_yields_an_empty_list_rather_than_raising() -> None:
    """`extract_links` is contractually never-raises, the same as `extract_content`: it is fed
    whatever the seed URL served, which is not always markup."""
    assert extract_links("", base_url="https://a.test/") == []
    assert extract_links("   \n  ", base_url="https://a.test/") == []
    assert extract_links('{"openapi": "3.1.0", "paths": {}}', base_url="https://a.test/") == []
    assert extract_links("\x00\x01\x02 not markup", base_url="https://a.test/") == []


def test_an_xhtml_page_declaring_its_own_encoding_is_still_parsed() -> None:
    """The regression this module's `str`-to-bytes step exists to prevent.

    `lxml.html.document_fromstring` raises `ValueError: Unicode strings with encoding
    declaration are not supported` when handed a `str` beginning with an XML declaration —
    and an XHTML documentation page is exactly that. Parsing bytes under an explicit UTF-8
    parser is what makes this page's links reachable instead of silently `[]`.
    """
    html = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<a href="/docs/xhtml">a link</a>'
        "</body></html>"
    )

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/docs/xhtml"
    ]


def test_a_stale_meta_charset_does_not_re_decode_an_already_decoded_body() -> None:
    """`internals/fetcher.py` decoded this body once already, per the response's own
    `Content-Type`; a `<meta charset>` disagreeing with that must not get a second vote.
    Without the explicit UTF-8 parser, lxml would honour the meta declaration and mangle the
    non-ASCII path below."""
    html = (
        '<html><head><meta charset="iso-8859-1"></head>'
        '<body><a href="/docs/café">accented</a></body></html>'
    )

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/docs/café"
    ]


def test_malformed_markup_still_yields_the_links_it_does_contain() -> None:
    """lxml's HTML parser is a recovering one, which is the whole reason this module uses it
    rather than an XML parser: unclosed tags, an unquoted attribute and a stray `<` are what
    real pages are made of, and none of them may cost a link."""
    html = '<html><body><a href="/one">one<div><a href=/two>two</a><p>3 < 4'

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/one",
        "https://docs.acme.test/two",
    ]


def test_a_link_written_inside_a_script_body_is_not_a_link() -> None:
    """`<script>` content is CDATA, not markup — a router table or an analytics snippet that
    happens to contain an `<a href>` string contributes nothing to the frontier."""
    html = """
    <script>var nav = '<a href="/fabricated">x</a>';</script>
    <a href="/real">real</a>
    """

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/real"
    ]


def test_surrounding_whitespace_in_an_href_is_ignored() -> None:
    """HTML attribute values routinely carry the newline and indentation of the template that
    wrote them."""
    html = '<a href="\n   /docs/guide   \n">padded</a>'

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/docs/guide"
    ]


def test_a_declared_base_href_is_what_relative_links_resolve_against() -> None:
    """A document's `<base href>` is the base URL a browser resolves its relatives against,
    so ignoring it would mis-resolve every relative link on a page that declares one — here,
    onto `/guide` instead of `/docs/v2/guide`."""
    html = """
    <html><head><base href="/docs/v2/"></head><body>
    <a href="guide">relative to the declared base</a>
    <a href="/absolute">root-relative, unaffected by the base</a>
    </body></html>
    """

    assert extract_links(html, base_url="https://docs.acme.test/whatever/page") == [
        "https://docs.acme.test/docs/v2/guide",
        "https://docs.acme.test/absolute",
    ]


def test_an_off_origin_base_href_cannot_nominate_another_sites_pages() -> None:
    """The other half of the `<base href>` pair: the declared base decides RESOLUTION, the
    page's own URL decides ORIGIN. A page declaring someone else's site as its base resolves
    its relatives onto that site — which is what a browser would agree they point at — and
    every one of them is then dropped, because this run's frontier is this site's pages."""
    html = """
    <html><head><base href="https://evil.test/"></head><body>
    <a href="phishing">relative, resolved onto the declared base</a>
    <a href="https://docs.acme.test/legit">absolute, still this site's own</a>
    </body></html>
    """

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/legit"
    ]


def test_only_the_first_declared_base_href_counts() -> None:
    """HTML defines the document base URL as the first `<base>` element carrying an `href`;
    a second one is inert."""
    html = """
    <html><head><base href="/first/"><base href="/second/"></head><body>
    <a href="guide">g</a>
    </body></html>
    """

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/first/guide"
    ]


def test_an_unparseable_base_url_fails_closed() -> None:
    """Mirrors `select_urls`'s own unparseable-seed rule: an origin that cannot be determined
    is one nothing can safely be compared against, so nothing is extracted rather than
    everything being treated as same-origin by default. Defensive rather than expected — by
    the time this runs, `crawl_site` has already fetched `base_url` successfully."""
    html = '<a href="/docs/guide">a perfectly good link</a>'

    assert extract_links(html, base_url="not a url") == []
    assert extract_links(html, base_url="ftp://docs.acme.test/") == []
    assert extract_links(html, base_url="https://[not-an-ipv6-literal/") == []


def test_the_link_ceiling_bounds_what_one_page_can_contribute() -> None:
    """`MAX_LINKS` is a defensive ceiling, not a tuning knob: a 50 MiB body of nothing but
    `<a href>` elements must not become millions of candidates for `select_urls` to normalize
    and score. Counted AFTER deduping, which is why the 100 repeats of `/dupe` below cost
    nothing against it."""
    unique = "".join(f'<a href="/p/{i}">{i}</a>' for i in range(MAX_LINKS + 50))
    repeated = '<a href="/dupe">d</a>' * 100

    links = extract_links(repeated + unique, base_url="https://docs.acme.test/")

    assert len(links) == MAX_LINKS
    assert links[0] == "https://docs.acme.test/dupe"
    assert links[1] == "https://docs.acme.test/p/0"


def test_the_host_comparison_is_case_insensitive_but_the_path_is_left_alone() -> None:
    """Origin equality lowercases the host (a host name is case-insensitive), so a shouted
    URL is still same-origin — while the path keeps its case, because a path is NOT
    case-insensitive and rewriting one would be normalization this module does not do.
    (`urlsplit`/`urlunsplit` lowercase the scheme on the way through; that is their
    round-trip, not a rule of this module's own.) `select_urls` lowercases the host again on
    its way to a canonical form, which is exactly the division of labour intended: doing half
    of normalization here would leave two modules with an opinion about what a URL is."""
    html = '<a href="HTTPS://DOCS.ACME.TEST/Docs/Guide">shouting</a>'

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://DOCS.ACME.TEST/Docs/Guide"
    ]


def test_a_newline_or_tab_inside_an_href_is_removed_rather_than_kept() -> None:
    """A template that wraps a long href across two lines writes the newline INTO the
    attribute value, where `.strip()` cannot reach it. `urlsplit` removes ASCII tab, CR and
    LF anywhere in a URL — the same rule a browser applies — and `_resolve` round-trips every
    candidate through it, so this is already handled. Pinned because it is not obvious from
    reading `_resolve`, and a well-meaning refactor that stopped round-tripping through
    `urlsplit`/`urlunsplit` would put a literal newline into a URL the crawler then tries to
    fetch."""
    html = '<a href="/docs/gu\nide">wrapped</a><a href="/ok\tay">tabbed</a>'

    assert extract_links(html, base_url="https://docs.acme.test/") == [
        "https://docs.acme.test/docs/guide",
        "https://docs.acme.test/okay",
    ]
