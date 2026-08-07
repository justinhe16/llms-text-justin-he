"""The adversarial suite for `app.features.crawl.internals.ssrf` — the module every other
part of the crawl feature trusts to have already said no.

No test here touches real DNS or a real socket. An IP literal in a URL needs neither; every
other case injects a fake `Resolver` (`_fake_resolver` below) instead of `default_resolver`,
which is the only reason `validate_url` accepts one at all (see that parameter's docstring).
That makes this suite exactly as hermetic as the rest of this project's, and it is *stronger*
than "no real request was made" for the cases that matter most here: "no DNS lookup this
suite did not script happened at all."
"""

import socket
from collections.abc import Sequence

import pytest

from app.features.crawl.internals.ssrf import (
    ALLOWED_PORTS,
    Resolver,
    SsrfBlockedError,
    validate_url,
)


def _fake_resolver(mapping: dict[str, Sequence[str]]) -> Resolver:
    """Build a `Resolver` that answers only for the hostnames named in `mapping`.

    A host outside `mapping` resolves to no addresses, which `validate_url` already treats
    as blocked (see the "resolver returns nothing" case below) — so a test only has to name
    the hostnames its own assertions care about.
    """

    async def resolve(host: str, port: int) -> Sequence[str]:
        return mapping.get(host, [])

    return resolve


PUBLIC_V4 = "8.8.8.8"
PUBLIC_V6 = "2001:4860:4860::8888"


def test_default_allowed_ports_are_exactly_the_web_ports() -> None:
    """The default `allowed_ports` a caller gets without opting into anything else."""
    assert ALLOWED_PORTS == frozenset({80, 443})


# --- IP literals named directly by the ticket, and their siblings ---------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
    ],
)
async def test_named_ip_literals_are_blocked(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        await validate_url(url)


async def test_localhost_is_blocked() -> None:
    """Not a literal — exercises the DNS path via a fake resolver, like a real crawl
    fetching `localhost` would go through the OS resolver rather than a literal-IP
    shortcut."""
    with pytest.raises(SsrfBlockedError):
        await validate_url(
            "http://localhost/", resolver=_fake_resolver({"localhost": ["127.0.0.1"]})
        )


@pytest.mark.parametrize("host", ["2130706433", "0177.0.0.1", "0x7f000001"])
async def test_alternate_encodings_of_loopback_are_blocked(host: str) -> None:
    """Decimal, octal, and hex loopback spellings.

    `ipaddress.ip_address` accepts none of these as a literal (only strict dotted-decimal
    is), so each one goes through DNS rather than the literal-IP shortcut. A real OS
    resolver on both glibc and macOS expands every one of these to `127.0.0.1`; the fake
    resolver below reproduces exactly that outcome so the assertion — "blocked" — holds
    independent of what the machine actually running this suite would do with them.
    """
    resolver = _fake_resolver({host: ["127.0.0.1"]})
    with pytest.raises(SsrfBlockedError):
        await validate_url(f"http://{host}/", resolver=resolver)


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[2002:7f00:1::]/",  # 6to4, embedding 127.0.0.1
        "http://[fd00::1]/",  # unique-local
        "http://[::]/",  # unspecified
        "http://[fd00:ec2::254]/",  # AWS IMDSv6
        # DEPRECATED SITE-LOCAL (RFC 3879). This one is a regression test for a real hole,
        # not a hypothetical: `fec0::1` answers False to is_global, is_private, is_loopback,
        # is_link_local, is_multicast, is_reserved AND is_unspecified, so the guard let it
        # straight through until `is_site_local` was added. Deprecated in 2004 says what new
        # deployments should do; it says nothing about what an internal network still
        # answers on.
        "http://[fec0::1]/",
        "http://[fec0::a9fe:a9fe]/",
        # NAT64 (RFC 6052): `64:ff9b::<ipv4>` is how an IPv6-only host reaches an IPv4
        # destination, so this is 127.0.0.1 written with no recognizable IPv4 address in it.
        "http://[64:ff9b::7f00:1]/",
        "http://[64:ff9b::a9fe:a9fe]/",  # ... and the AWS metadata endpoint, likewise
        # IPv4-compatible IPv6 (`::<ipv4>`), the other deprecated way to spell 127.0.0.1.
        "http://[::7f00:1]/",
        "http://[ff02::1]/",  # link-local all-nodes multicast: is_global is True for this
    ],
)
async def test_ipv6_forms_of_private_targets_are_blocked(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        await validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0/",
        "http://100.64.0.1/",  # CGNAT — not caught by is_private, only by is_global
        "http://224.0.0.1/",  # multicast — is_global is True here, only is_multicast catches it
        "http://240.0.0.1/",  # reserved
        "http://255.255.255.255/",  # broadcast
        "http://100.100.100.200/",  # Alibaba Cloud metadata
        "http://192.0.0.192/",  # Oracle Cloud metadata
    ],
)
async def test_other_non_public_ipv4_targets_are_blocked(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        await validate_url(url)


# --- The cases a hostname-string check would pass, and only a resolved-IP check catches ---


async def test_a_public_looking_hostname_resolving_to_loopback_is_blocked() -> None:
    """The `localtest.me` case: nothing about the hostname string looks unsafe."""
    resolver = _fake_resolver({"localtest.me": ["127.0.0.1"]})
    with pytest.raises(SsrfBlockedError):
        await validate_url("http://localtest.me/", resolver=resolver)


async def test_a_hostname_resolving_to_both_public_and_private_is_blocked() -> None:
    """One good address and one bad one blocks the whole URL — never "use the good one"."""
    resolver = _fake_resolver({"mixed.test": [PUBLIC_V4, "10.0.0.1"]})
    with pytest.raises(SsrfBlockedError):
        await validate_url("http://mixed.test/", resolver=resolver)


async def test_a_resolver_that_raises_is_blocked() -> None:
    async def resolver(host: str, port: int) -> Sequence[str]:
        raise socket.gaierror("simulated DNS failure")

    with pytest.raises(SsrfBlockedError):
        await validate_url("http://nowhere.test/", resolver=resolver)


async def test_a_resolver_returning_no_addresses_is_blocked() -> None:
    with pytest.raises(SsrfBlockedError):
        await validate_url("http://nowhere.test/", resolver=_fake_resolver({}))


# --- Schemes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://public.test/",
        "ftp://public.test/",
        "redis://public.test:6379/",
        "//evil.test/",
    ],
)
async def test_disallowed_schemes_are_blocked(url: str) -> None:
    """None of these reach DNS: the scheme check runs before resolution."""
    with pytest.raises(SsrfBlockedError):
        await validate_url(url)


# --- Ports --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://public.test:6379/", "http://public.test:22/", "http://public.test:8080/"],
)
async def test_disallowed_ports_on_an_otherwise_public_host_are_blocked(url: str) -> None:
    resolver = _fake_resolver({"public.test": [PUBLIC_V4]})
    with pytest.raises(SsrfBlockedError):
        await validate_url(url, resolver=resolver)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.test:80/",
        "https://public.test:443/",
        "http://public.test/",
        "https://public.test/",
    ],
)
async def test_allowed_ports_and_implicit_defaults_are_accepted(url: str) -> None:
    resolver = _fake_resolver({"public.test": [PUBLIC_V4]})
    target = await validate_url(url, resolver=resolver)
    assert target.ip == PUBLIC_V4


# --- Userinfo -------------------------------------------------------------------------


async def test_userinfo_before_an_ip_literal_is_blocked() -> None:
    with pytest.raises(SsrfBlockedError):
        await validate_url("http://public.test@127.0.0.1/")


async def test_userinfo_before_a_public_host_is_blocked() -> None:
    """Never reaches DNS: the userinfo check runs before resolution."""
    with pytest.raises(SsrfBlockedError):
        await validate_url("http://user:pw@public.test/")


# --- Trailing dot ------------------------------------------------------------------------


async def test_trailing_dot_on_localhost_is_blocked() -> None:
    resolver = _fake_resolver({"localhost": ["127.0.0.1"]})
    with pytest.raises(SsrfBlockedError):
        await validate_url("http://localhost./", resolver=resolver)


# --- Allowed control cases — the guard is not "reject everything" ----------------------


async def test_a_public_ipv4_literal_is_allowed() -> None:
    target = await validate_url(f"http://{PUBLIC_V4}/")
    assert target.ip == PUBLIC_V4
    assert target.connect_url == f"http://{PUBLIC_V4}/"


async def test_a_public_ipv6_literal_is_allowed() -> None:
    target = await validate_url(f"http://[{PUBLIC_V6}]/")
    assert target.ip == PUBLIC_V6
    assert target.connect_url == f"http://[{PUBLIC_V6}]/"


async def test_a_zone_id_on_a_private_ipv6_literal_does_not_smuggle_it_past_the_guard() -> None:
    """A scoped-address suffix must not change how the base 128 bits are classified.

    `ipaddress` parses `fe80::1%eth0` as a link-local address with a `scope_id`, and the
    interesting question is whether attaching a zone makes the guard reason about a
    different value than the address itself. It must not: the zone is routing metadata, not
    part of the address.
    """
    for url in ("http://[fe80::1%25eth0]/", "http://[fec0::1%25eth0]/", "http://[::1%25lo0]/"):
        with pytest.raises(SsrfBlockedError):
            await validate_url(url)


async def test_a_zone_id_on_a_public_ipv6_literal_survives_into_a_well_formed_connect_url() -> None:
    """The allowed half of the case above, and the reason it is worth pinning.

    `connect_url` is assembled by string interpolation into `[...]`, so an address whose
    text contains a `%` is exactly the shape that would produce a malformed URL if the
    brackets were ever dropped or the zone escaped them. `ip_address()` rejects `/`, `@`
    and `]` inside a zone id, so there is no way to break out of the brackets — this test
    exists so that stays true, and so the zoned form is known to round-trip rather than
    merely assumed to.

    Note the `%25` that survives into the parsed address rather than becoming a bare `%`.
    RFC 6874 says a zone id is percent-ENCODED inside a URI (`%25eth0` on the wire meaning
    `%eth0`), and `urlsplit().hostname` does not percent-decode, so what reaches
    `ip_address()` is the literal text `...%25eth0` and the resulting `scope_id` is the
    slightly surprising `"25eth0"`. Harmless — the zone is never used for anything, and only
    the base 128 bits are ever classified — but asserted rather than tidied away, because
    "decode the host first" is precisely the change that must never be made here: it is what
    would turn a percent-encoded loopback (`http://%31%32%37.0.0.1/`) back into an address,
    and this guard's safety rests on the host text never being decoded before it is
    classified.
    """
    target = await validate_url(f"http://[{PUBLIC_V6}%25eth0]/")

    assert target.ip == f"{PUBLIC_V6}%25eth0"
    assert target.connect_url == f"http://[{PUBLIC_V6}%25eth0]/"
    assert target.host_header == f"[{PUBLIC_V6}%25eth0]"


async def test_an_ipv6_literal_is_bracketed_in_the_host_header_on_a_non_default_port() -> None:
    """RFC 7230 §5.4 is `Host = uri-host [ ":" port ]`, and `uri-host` for an IPv6 address
    is an `IP-literal` — brackets included. `urlsplit().hostname` strips them, so the header
    has to put them back: without the brackets, `<address>` glued to `:443` is just one more
    colon in a string of them, and no server can tell where the address ends.
    """
    target = await validate_url(f"http://[{PUBLIC_V6}]:443/docs")
    assert target.host_header == f"[{PUBLIC_V6}]:443"
    assert target.connect_url == f"http://[{PUBLIC_V6}]:443/docs"


async def test_an_ipv6_literal_is_bracketed_in_the_host_header_on_the_default_port() -> None:
    """The branch a fix that only touches the `f"{host}:{port}"` half would miss.

    There is no port to disambiguate here, but `uri-host` is still `IP-literal`, so a bare
    `Host: 2001:4860:4860::8888` is malformed on its own and not merely ambiguous.
    """
    target = await validate_url(f"https://[{PUBLIC_V6}]/docs")
    assert target.host_header == f"[{PUBLIC_V6}]"
    assert target.connect_url == f"https://[{PUBLIC_V6}]/docs"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/docs", "example.test"),
        ("http://example.test:443/docs", "example.test:443"),
        (f"https://{PUBLIC_V4}/docs", PUBLIC_V4),
        (f"http://{PUBLIC_V4}:443/docs", f"{PUBLIC_V4}:443"),
    ],
)
async def test_non_ipv6_hosts_are_never_bracketed_in_the_host_header(
    url: str, expected: str
) -> None:
    """The other side of the case above: bracketing keys off the parsed address family, not
    off a colon in the string, so neither a name nor an IPv4 literal picks up brackets from
    the `:port` that follows it."""
    resolver = _fake_resolver({"example.test": [PUBLIC_V4]})
    target = await validate_url(url, resolver=resolver)
    assert target.host_header == expected


async def test_explicit_default_https_port_is_allowed_and_omitted_from_the_host_header() -> None:
    resolver = _fake_resolver({"public.test": [PUBLIC_V4]})
    target = await validate_url("https://public.test:443/", resolver=resolver)
    assert target.host_header == "public.test"


async def test_a_non_default_port_is_included_in_the_host_header() -> None:
    """`:443` on `http` is allowed (443 is in `ALLOWED_PORTS`) but is not `http`'s default,
    so it must appear in the `Host` header — the header a server needs to route the
    request correctly."""
    resolver = _fake_resolver({"public.test": [PUBLIC_V4]})
    target = await validate_url("http://public.test:443/", resolver=resolver)
    assert target.host_header == "public.test:443"
    assert target.connect_url == f"http://{PUBLIC_V4}:443/"


async def test_path_and_query_are_preserved_into_connect_url() -> None:
    resolver = _fake_resolver({"public.test": [PUBLIC_V4]})
    target = await validate_url("http://public.test/a/b?x=1&y=2", resolver=resolver)
    assert target.connect_url == f"http://{PUBLIC_V4}/a/b?x=1&y=2"


async def test_fragment_is_dropped_from_connect_url() -> None:
    resolver = _fake_resolver({"public.test": [PUBLIC_V4]})
    target = await validate_url("http://public.test/page#section", resolver=resolver)
    assert target.connect_url == f"http://{PUBLIC_V4}/page"
