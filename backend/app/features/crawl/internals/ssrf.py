"""SSRF protection — the one gate every crawl fetch target passes through before a socket
opens.

**What this defends.** The crawler's whole job is to make outbound HTTP requests to URLs it
did not choose — a user-submitted seed, and (in a later phase) a `Location` header a remote
server sent back. Both are attacker-influenced input in a system that runs on a Fly machine
sitting next to Postgres, Redis, and a cloud metadata endpoint that will hand out credentials
to whoever asks it nicely. `validate_url()` is the only function in this feature allowed to
decide "yes, connect to this" — nothing downstream re-derives that answer, and nothing
upstream is trusted to have already made it safe.

**Check-then-connect has a race, and this module does not pretend otherwise.** Validating a
hostname and then handing that same hostname to an HTTP client invites a second DNS lookup
between the check and the connect (DNS rebinding) — a public address at check time, a
private one moments later. `validate_url()` closes that gap by resolving *once* and handing
back the resolved address itself: `ValidatedTarget.connect_url` names the IP, not the host,
so there is no second lookup left to race. The other half of this defense lives in
`internals/fetcher.py`, which must actually dial `connect_url` — and must still present the
original hostname as the `Host` header and the TLS SNI name, or the far end cannot serve the
right site and its certificate cannot be verified against the name the crawl operator
believes it is fetching. Handing the hostname back to httpx after validating it — "connect
to `host`, we already checked it's fine" — is exactly the mistake this module exists to
prevent; it is not a smaller version of the fix, it is the bug.

**Order of checks**, each one specific enough that a caller (and a test) can tell which rule
fired:

1. Parse the URL. A malformed URL is rejected outright.
2. Scheme must be `http` or `https`. Everything else — `file:`, `gopher:`, `ftp:`,
   `redis:`, `data:`, no scheme at all (`//host/…`, a bare `host/…`) — is a way to reach
   something that is not a web server, or not a server at all.
3. No userinfo in the authority (`user:pass@host`). A crawl target never legitimately
   carries credentials, and `http://trusted.example.com@evil.test/` is the classic
   "looks like the trusted host if you stop reading too early" bypass.
4. The host must be non-empty once a single trailing dot is stripped (`localhost.` and
   `localhost` name the same thing to a resolver, and must be treated as the same input
   here too).
5. The port — explicit, or the scheme's default — must be in `allowed_ports`
   (`{80, 443}` unless a caller opts into more, which no call site does today).
6. Resolve the host to one or more IP addresses. An IP literal in the URL is used directly,
   skipping DNS; the resolver is asked for anything else.
7. **Classify every resolved address, and reject the whole URL if even one of them is
   disallowed.** A hostname with one public and one private A record is an attacker
   controlling which answer a retry gets, not a misconfiguration to route around by
   picking the address that happens to be safe.
8. Connect to the first allowed address. Everything else about the original request —
   scheme, host, port, path, query — survives into `ValidatedTarget`; only the fragment is
   dropped, because it is never sent to a server.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import Final
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

# The parameter exists so a future ticket can opt a specific call site into an explicit
# high port. No call site opts in today — every crawl fetch, seed or redirect, is checked
# against exactly this set.
ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})

# Known cloud instance-metadata endpoints: infrastructure that will hand out credentials to
# whatever process asks, with no authentication beyond "the request came from this host".
# Every one of these is already refused by one of the named-property checks below, and they
# are listed here anyway, first, on purpose: relying on that agreement would leave "this is
# a metadata endpoint, specifically" invisible in a diff, and it would make the protection
# an accident of how `ipaddress` happens to classify these particular addresses rather than
# a decision this module made.
_METADATA_ADDRESSES: Final[frozenset[IPv4Address | IPv6Address]] = frozenset(
    {
        IPv4Address("169.254.169.254"),  # AWS, GCP, Azure instance metadata
        IPv4Address("100.100.100.200"),  # Alibaba Cloud metadata
        IPv4Address("192.0.0.192"),  # Oracle Cloud metadata
        IPv6Address("fd00:ec2::254"),  # AWS IMDSv6
    }
)

# RFC 6052's well-known NAT64 prefix: `64:ff9b::<ipv4>` is how an IPv6-only host reaches an
# IPv4 destination through a translator, which makes `64:ff9b::7f00:1` a spelling of
# 127.0.0.1 that contains no IPv4 address a reader would recognize. Decoded explicitly by
# `_embedded_ipv4_addresses` below rather than left to `is_reserved` — which does currently
# catch this whole prefix, via `::/8`, and which is not a property that says anything about
# NAT64. See that function for why an incidental rejection is not good enough here.
_NAT64_WELL_KNOWN: Final = IPv6Network("64:ff9b::/96")


class SsrfBlockedError(Exception):
    """A URL was refused before any socket was opened.

    Raised by `validate_url()` below, for anything from an unparseable URL to a hostname
    that resolves to an address the crawler must never connect to. `reason` is always safe
    to show the caller: it describes the URL's own scheme, host, port, or a resolved
    address — never anything about this process's internals — so a service catching this
    (a later phase of this ticket) may record it verbatim as a run's error.
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        """The URL — the original fetch target, or a redirect's `Location` — that was
        refused."""
        self.reason = reason
        """A human-readable reason naming which check failed and why."""
        super().__init__(reason)


def _refused(url: str, reason: str) -> SsrfBlockedError:
    """Record the refusal and return the error to raise — `raise _refused(url, reason)`.

    A factory rather than logging inside `SsrfBlockedError.__init__`, and rather than a
    `logger.warning` at each of `validate_url`'s ten refusal sites. The constructor is the
    wrong place because an exception object has to stay inert: a test that builds one to
    assert against, or any code that constructs one without raising it, would otherwise
    emit a security warning about a refusal that never happened. Ten call sites is the
    wrong place because it is ten chances to forget. This is the one place every refusal
    goes through that is still an *action* rather than a value.

    Returning the error instead of raising it keeps `raise ... from None` expressible at
    the call sites that need it — four of them do, to keep a platform resolver's or
    `urlsplit`'s own message out of the chain.

    **An SSRF refusal is a security event and belongs in `fly logs` on its own**, not only
    as a sanitized string inside a later `runs.error`. `reason` sometimes names the
    resolved address that caused the rejection, and that is deliberate: it is the crawl
    TARGET's address, never this process's own infrastructure, and it is exactly what
    distinguishes "a hostname legitimately moved" from "something is DNS-rebinding at us".
    `SsrfBlockedError` already commits to `reason` being safe to show a signed-in user
    verbatim (see its docstring), so logging it is a strictly smaller disclosure.

    `run_id` is not passed and does not need to be: this only ever runs inside
    `crawl_task`'s `run_id_context` (`app.core.logging`), so the line is already correlated
    to the run whose fetch was refused.
    """
    logger.warning("crawl: refused a URL (%s)", reason, extra={"url": url, "reason": reason})
    return SsrfBlockedError(url, reason)


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """Everything `internals/fetcher.py` needs to dial the address this module validated,
    instead of the hostname a second DNS lookup could quietly repoint. See the module
    docstring's "check-then-connect" section for why that distinction is the entire point
    of this dataclass.
    """

    url: str
    """The URL that was validated, exactly as given — host-based, never the IP one. A
    relative redirect must be resolved with `urljoin(target.url, location)` against THIS,
    never against `connect_url`, or the resolved link would inherit the IP as its base and
    the next hop would carry the wrong `Host` header."""

    scheme: str
    """Lower-cased `"http"` or `"https"`."""

    host: str
    """The original hostname — lower-cased, one trailing dot stripped. This is what goes in
    the `Host` header and the TLS SNI extension, never `ip`, so the far end sees the name it
    actually serves and, for https, so certificate verification checks the name a human
    would recognize rather than a bare address."""

    port: int
    """The port that was checked: explicit in the URL, or the scheme's default."""

    ip: str
    """The single resolved (or literal) address that was validated, unbracketed. What
    `fetcher.py` must actually connect to."""

    host_header: str
    """`<host, bracketed if IPv6>`, plus `":{port}"` when `port` is not `scheme`'s default —
    the exact value `fetcher.py` sends as the `Host` header. RFC 7230 §5.4 is
    `Host = uri-host [ ":" port ]`, and `uri-host` for an IPv6 address is an `IP-literal`,
    brackets included — so the brackets are required in BOTH branches, not only the one
    that appends a port."""

    connect_url: str
    """`scheme://<ip, bracketed if IPv6>[:port]/path?query` — what httpx is actually told
    to dial. Deliberately never a hostname: handing httpx a name here would return DNS
    resolution to httpx and undo everything this module checked."""


# A resolver maps (host, port) to the addresses to try, in order. `validate_url()` accepts
# one so tests can inject a fake mapping instead of touching real DNS (see
# tests/test_ssrf.py) — nothing in production code passes one; every real call falls
# through to `default_resolver` below.
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


async def default_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve `host` with the running event loop's real DNS, de-duplicated, order kept.

    `type=socket.SOCK_STREAM` asks the OS resolver for one entry per address rather than
    one per (socket type, protocol) pair for the *same* address, which is otherwise the
    redundant thing `getaddrinfo` hands back for a plain TCP connect.
    """
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: dict[str, None] = {}
    for result in results:
        # `sockaddr[0]` is typed `str | int` in typeshed (the `int` half covers exotic
        # address families like AF_PACKET, which SOCK_STREAM never returns) — `str(...)`
        # is a real normalization for both actual cases, not a cast around one.
        seen.setdefault(str(result[4][0]), None)
    return list(seen)


def _classify(address: IPv4Address | IPv6Address) -> str | None:
    """Return why `address` must not be connected to, or `None` if it may be.

    `not address.is_global` is the primary check — it is the only one of these that
    catches `100.64.0.0/10` (CGNAT), which `is_private` does not (`is_global` and
    `is_private` are BOTH `False` there; see `ipaddress.IPv6Address.is_global`'s own
    docstring for that exact carve-out). The named properties below are checked
    explicitly anyway, even though `is_global` subsumes most of what they catch, for two
    reasons stated here rather than left implicit: the ticket asks for them by name, and a
    future Python release changing the definition of one of them should not silently open
    a hole this function used to close. `is_multicast` is the property that most needs to
    stay explicit: `224.0.0.1.is_global` is `True` (multicast has its own global/local
    split, unrelated to unicast routability), so multicast addresses would sail through an
    `is_global`-only check.
    """
    if address in _METADATA_ADDRESSES:
        return f"{address} is a cloud metadata address"
    if not address.is_global:
        return f"{address} is not a globally routable address"
    if address.is_private:
        return f"{address} is a private address"
    if address.is_loopback:
        return f"{address} is a loopback address"
    if address.is_link_local:
        return f"{address} is a link-local address"
    if address.is_multicast:
        return f"{address} is a multicast address"
    if address.is_reserved:
        return f"{address} is a reserved address"
    if address.is_unspecified:
        return f"{address} is an unspecified address"
    # DEPRECATED IPv6 SITE-LOCAL (fec0::/10), AND THE ONE GAP THIS FUNCTION HAD.
    #
    # Measured on CPython 3.12, the version this project targets, `fec0::1` answers every
    # check above like this:
    #
    #     is_global    True    is_private     False   is_loopback    False
    #     is_reserved  False   is_link_local  False   is_multicast   False
    #     is_unspecified False                        is_site_local  True
    #
    # So this range is not merely *missed* by `is_global` — it is affirmatively reported as
    # GLOBAL, and every other named check above passes it too. `is_site_local` is the only
    # property in the standard library that identifies it, which makes this line the entire
    # defense against `fec0::/10` rather than one more redundant layer over `is_global`.
    # RFC 3879 deprecated the range in 2004, but "deprecated" describes what new deployments
    # should do — not what a resolver will hand back, or what an internal network still
    # answers on.
    if isinstance(address, IPv6Address) and address.is_site_local:
        return f"{address} is a deprecated site-local address"
    return None


def _embedded_ipv4_addresses(address: IPv6Address) -> list[IPv4Address]:
    """Every IPv4 address `address` encodes, under any of the three encodings that matter.

    Teredo yields two — a server and a client — because a Teredo address is fully attacker
    controlled and encodes both. This does not assume any of the four delegate to the
    embedded address's own classification the way `IPv6Address.is_global` already does for
    `ipv4_mapped` on recent Pythons (verified: it does, on the interpreter this project
    pins) — `_reject_reason` below re-classifies every one of these explicitly, so this
    function's answer is what matters, not whichever stdlib version happens to be running.

    The same argument is why NAT64 is decoded here even though `64:ff9b::/96` is, today,
    already rejected as `is_reserved` (it falls inside `::/8`). That rejection is a
    coincidence of prefix arithmetic that has nothing to do with NAT64: it would disappear
    the day the well-known prefix moved or `ipaddress` narrowed `::/8`, and until then it
    reports "reserved address" for what is really "127.0.0.1 with extra steps". A guard
    whose correctness a reader cannot verify from the code in front of them is one refactor
    away from being wrong, so the translation is stated rather than inherited.
    """
    embedded: list[IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in _NAT64_WELL_KNOWN:
        # The low 32 bits of an RFC 6052 /96-prefixed address are the IPv4 address itself.
        embedded.append(IPv4Address(int(address) & 0xFFFFFFFF))
    return embedded


def _reject_reason(address: IPv4Address | IPv6Address) -> str | None:
    """Return why `address` must not be connected to, or `None` if it may be.

    Classifies `address` itself, then — for an IPv6 address — every IPv4 address it embeds
    (IPv4-mapped, 6to4, Teredo, NAT64), so that an address which looks superficially global
    at the IPv6 level cannot smuggle a private, loopback, or metadata IPv4 address past this
    check inside one of those encodings. See `_embedded_ipv4_addresses` for why this does
    not trust `IPv6Address.is_private` alone to already cover them.
    """
    reason = _classify(address)
    if reason is not None:
        return reason
    if isinstance(address, IPv6Address):
        for embedded in _embedded_ipv4_addresses(address):
            embedded_reason = _classify(embedded)
            if embedded_reason is not None:
                return f"{address} embeds {embedded}, which {embedded_reason}"
    return None


def _parse_ip_literal(host: str) -> IPv4Address | IPv6Address | None:
    """Return `host` as an `ip_address`, or `None` if it is a name rather than a literal."""
    try:
        return ip_address(host)
    except ValueError:
        return None


async def validate_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    allowed_ports: frozenset[int] = ALLOWED_PORTS,
) -> ValidatedTarget:
    """Validate `url` as a safe crawl target, or raise `SsrfBlockedError`.

    See the module docstring for the full, numbered order of checks and the reasoning
    behind each one. `resolver` defaults to `default_resolver`; every test in
    `tests/test_ssrf.py` injects a fake one instead, which is the only reason this
    parameter exists at all (see `Resolver`'s docstring).

    Args:
        url: The fetch target — a crawl seed, or a redirect's `Location`, already resolved
            against its base if it was relative.
        resolver: Injected for tests. Production code never passes one.
        allowed_ports: Ports a validated target may use. No call site overrides the
            default `ALLOWED_PORTS` today; the parameter exists for a future one that does.

    Returns:
        A `ValidatedTarget` naming the exact address `internals/fetcher.py` must dial.

    Raises:
        SsrfBlockedError: `url` is malformed, uses a disallowed scheme or port, carries
            credentials, has no host, or resolves to (or embeds) an address that is not
            safe to connect to.
    """
    resolve = resolver if resolver is not None else default_resolver

    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise _refused(url, f"URL could not be parsed: {error}") from None

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise _refused(url, f"scheme {parts.scheme!r} is not http or https")

    if "@" in parts.netloc:
        raise _refused(url, "URL must not contain credentials (a user:password@ prefix)")

    host = parts.hostname or ""
    if host.endswith("."):
        host = host[:-1]
    host = host.lower()
    if not host:
        raise _refused(url, "URL must include a host")

    try:
        port = parts.port
    except ValueError as error:
        raise _refused(url, f"URL port is invalid: {error}") from None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if port not in allowed_ports:
        raise _refused(url, f"port {port} is not allowed")

    literal = _parse_ip_literal(host)
    if literal is not None:
        addresses: list[IPv4Address | IPv6Address] = [literal]
    else:
        try:
            resolved = await resolve(host, port)
        # socket.gaierror is a subclass of OSError; catching OSError covers both it and
        # any other resolver failure (e.g. a fake resolver raising a plain OSError in
        # tests) with one branch.
        except OSError:
            # The resolver's own message is deliberately NOT interpolated. Every
            # `SsrfBlockedError.reason` is treated as safe to display verbatim — it is
            # written straight into `runs.error`, which every signed-in user can read
            # (ARCHITECTURE.md §4.1) — and that guarantee holds only while every reason is
            # a string this module composed itself. A `socket.gaierror`'s text comes from
            # the platform resolver, so it is the one value here that this module does not
            # control the content of. It is generic library text in practice; excluding it
            # costs nothing and keeps "safe verbatim" true by construction rather than by
            # inspection of whatever libc happens to say.
            raise _refused(url, f"DNS resolution for {host!r} failed") from None
        if not resolved:
            raise _refused(url, f"DNS resolution for {host!r} returned no addresses")
        addresses = []
        for candidate in resolved:
            try:
                addresses.append(ip_address(candidate))
            except ValueError as error:
                raise _refused(
                    url, f"resolver returned an unparseable address {candidate!r}: {error}"
                ) from None

    # Classify EVERY resolved address before connecting to any of them — see the module
    # docstring's point 7. A single disallowed address anywhere in the list blocks the
    # whole URL; this is not "filter the bad ones and connect to a good one".
    for address in addresses:
        reason = _reject_reason(address)
        if reason is not None:
            raise _refused(url, reason)

    chosen = addresses[0]
    default_port = _DEFAULT_PORTS[scheme]

    # `urlsplit().hostname` strips the brackets off an IPv6 literal, and `uri-host` in RFC
    # 7230 §5.4's `Host = uri-host [ ":" port ]` is an `IP-literal` for an IPv6 address —
    # brackets included — so they go back on for BOTH branches below, not just the one that
    # appends a port.
    #
    # The predicate is the parsed `literal`, and specifically NOT `chosen`: this header
    # carries the NAME while `connect_url` below carries the ADDRESS, so a hostname with
    # only an AAAA record is exactly where the two part company — it must stay unbracketed
    # here while `connect_url` brackets what it dials. `literal` is `None` on every DNS
    # path, so that falls out for free. (A `":" in host` test would be equivalent for names
    # — `urlsplit` has already taken the port off and userinfo is rejected above — so it is
    # the AAAA case, not name safety, that decides this.) `internals/url_ranking.py`'s
    # `_authority()` is the same concern in the sibling module, kept separate on purpose
    # (ARCHITECTURE.md §3.1).
    header_host = f"[{host}]" if isinstance(literal, IPv6Address) else host
    host_header = header_host if port == default_port else f"{header_host}:{port}"

    netloc = f"[{chosen}]" if isinstance(chosen, IPv6Address) else str(chosen)
    if port != default_port:
        netloc = f"{netloc}:{port}"
    connect_url = urlunsplit((scheme, netloc, parts.path, parts.query, ""))

    return ValidatedTarget(
        url=url,
        scheme=scheme,
        host=host,
        port=port,
        ip=str(chosen),
        host_header=host_header,
        connect_url=connect_url,
    )
