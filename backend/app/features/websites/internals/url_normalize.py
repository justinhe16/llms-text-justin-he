"""URL validation and origin normalization — the one implementation, deliberately shared.

This module lives in `websites/internals/` because `websites` is the feature that needs it
first, but it is written to be the ONLY place this logic ever exists. The crawler
milestone (ARCHITECTURE.md §3.4) has to resolve relative links against a page's origin,
which is the same computation; two implementations of "what is this URL's origin" would
diverge the first time one of them learned about default ports and the other did not. If a
second feature needs it, move this module to a shared location and update both imports —
do not copy it.

**What normalization is for.** `websites` has a `UNIQUE (user_id, origin)` constraint
(db/schema.prisma), so `origin` is the dedupe key: `https://Example.com:443/pricing?ref=x`
and `https://example.com/` are the same site and must produce the same `origin`. `url`
keeps whatever the user actually typed, because it is what gets shown back to them and
what makes "why did it dedupe against that?" answerable.

**Deliberately pure.** No I/O, no DNS lookup, no HTTP request, no database access. This
function cannot tell you whether a host exists — only whether the string is a well-formed
absolute `http`/`https` URL and what its origin is. Reachability is the crawler's problem.

**The normalization rules**, and the reasoning behind each:

| Input | Origin | Why |
| --- | --- | --- |
| `https://EXAMPLE.com` | `https://example.com` | Host is case-insensitive (RFC 3986 §3.2.2) |
| `https://example.com:443` | `https://example.com` | Default port for the scheme is redundant |
| `http://example.com:80` | `http://example.com` | Same, for `http` |
| `https://example.com:8443` | `https://example.com:8443` | A non-default port is part of the identity |
| `https://example.com/a/b/` | `https://example.com` | An origin has no path — trailing slash included |
| `https://example.com?q=1#f` | `https://example.com` | Query and fragment are per-request, not per-site |
| `https://www.example.com` | `https://www.example.com` | **`www.` is KEPT** — see below |

**Why `www.` is kept.** It is tempting to strip it so that `www.example.com` and
`example.com` dedupe together. That is wrong here: the two names can, and often do, serve
different content, and this product's entire output is a crawl of whatever a host actually
returns. Folding them would silently generate one site's `llms.txt` from another site's
pages. The cost of keeping them separate is a user occasionally adding both; the cost of
folding them is a wrong artifact that looks right.

**What is rejected**, all with `UrlValidationError` (which the request schema turns into a
`422`, see `app.features.websites.schemas`):

- Anything that is not an absolute `http`/`https` URL — `ftp://`, `file://`, `javascript:`,
  a bare `example.com`, `/relative/path`, `//protocol-relative`, or plain garbage.
- **Credentials in the URL** (`https://user:pass@example.com`). Rejected rather than
  stripped: a credentialed URL is a secret (ARCHITECTURE.md §9.1), and this one would be
  persisted in `websites.url`, echoed back in API responses, and eventually handed to the
  crawler. Silently stripping it would store the sanitized copy and leave the user thinking
  their credentials were being used; rejecting says what happened.
- Anything longer than `MAX_URL_LENGTH`.
- A malformed host: whitespace, control characters, an empty label, an out-of-range port.
"""

import re
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv6Address
from typing import Final
from urllib.parse import urlsplit


# 2048 characters. Not a standard — there isn't one for URL length — but it is the de-facto
# ceiling every browser and proxy has settled around, and it is far past anything a human
# types into "add a website". The cap exists so that a pathological input is rejected by a
# cheap `len()` before it reaches the database, not because 2049 characters is meaningfully
# different from 2048.
MAX_URL_LENGTH: Final = 2048

# 253 is the maximum length of a DNS name in presentation format (RFC 1035 §2.3.4's 255
# octets, minus the length byte and the root label). A host longer than this cannot resolve,
# so accepting it would only defer the failure to the crawler.
_MAX_HOST_LENGTH: Final = 253

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

# One DNS label: alphanumerics and hyphens, not starting or ending with a hyphen, 63
# characters or fewer. `_HOSTNAME_PATTERN` is one or more of those joined by dots, with an
# optional trailing dot (the explicit-root form, `example.com.`, which is legal and which
# `urlsplit` preserves). This also accepts an IPv4 literal, since `1.2.3.4` is four labels
# that happen to be numeric — deliberate: an IP address is a legitimate crawl target.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_PATTERN: Final = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")

# An IPv6 literal as `urlsplit` reports it — brackets already stripped. Kept deliberately
# loose (hex groups, colons, and the IPv4-mapped tail) rather than trying to express the
# full RFC 4291 grammar in a regex: `ipaddress.IPv6Address` does the real validation in
# `_validate_host`, and this is only the cheap "does this even look like an IPv6 address"
# check that routes a host to it.
_IPV6_SHAPE: Final = re.compile(r"^[0-9a-f:.]+$")


class UrlValidationError(ValueError):
    """A URL that cannot be accepted, carrying a message safe to show the caller.

    Subclasses `ValueError` on purpose: a pydantic `field_validator` turns a `ValueError`
    into FastAPI's native `422` response with the message attached, so the request schema
    needs no `except` clause and no second copy of these rules (see
    `app.features.websites.schemas.CreateWebsiteRequest`).

    Every message below describes the *input*, never anything about this system's internals,
    and never echoes a credential — the credentials case says that credentials were present,
    not what they were.
    """


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """The two values `websites` stores for one submitted URL.

    Frozen because normalization is a pure function of its input, and a caller mutating one
    half of this pair after the fact would break the invariant that `origin` is derived from
    `url`.
    """

    url: str
    """The caller's input with surrounding whitespace removed, and otherwise verbatim —
    path, query, and fragment all preserved. Stored in `websites.url` for display."""

    origin: str
    """Lowercased scheme and host, plus the port only when it is not the scheme's default.
    No path, no query, no fragment, no trailing slash. Stored in `websites.origin`, which is
    half of the `UNIQUE (user_id, origin)` dedupe key."""


def _reject(message: str) -> UrlValidationError:
    """Build the one exception type this module raises.

    Returned rather than raised so call sites read `raise _reject(...)`, which keeps the
    `raise` visible at the point control leaves — a helper that raises internally makes
    every call site look like it might fall through.
    """
    return UrlValidationError(message)


def _validate_host(host: str) -> str:
    """Return `host` in its canonical ASCII form, or raise.

    `urlsplit` has already lowercased the host and stripped the brackets from an IPv6
    literal, so this only has to answer "is this a plausible host at all?".

    A non-ASCII host is IDNA-encoded to punycode (`ünïcode.example` ->
    `xn--ncode-cta3g.example`) rather than rejected, because the encoded form is what DNS,
    the crawler, and every other consumer will actually use — storing the unicode spelling
    would mean every downstream user has to remember to encode it, and the ones that forget
    would silently miss the cache/dedupe.
    """
    if not host:
        raise _reject("URL must include a host, for example https://example.com")

    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as error:
            message = f"URL host is not a valid internationalized domain name: {error}"
            raise _reject(message) from None

    if len(host) > _MAX_HOST_LENGTH:
        raise _reject(f"URL host must be at most {_MAX_HOST_LENGTH} characters")

    if _IPV6_SHAPE.match(host) and ":" in host:
        try:
            IPv6Address(host)
        except AddressValueError as error:
            raise _reject(f"URL host is not a valid IPv6 address: {error}") from None
        return host

    if not _HOSTNAME_PATTERN.match(host):
        raise _reject(
            "URL host is not a valid hostname — it may contain only letters, digits, "
            "hyphens, and dots"
        )
    return host


def normalize_url(raw: str) -> NormalizedUrl:
    """Validate `raw` as an absolute `http`/`https` URL and derive its origin.

    See the module docstring for the full rule table and the reasoning behind each rule.

    Args:
        raw: The URL exactly as the user submitted it.

    Returns:
        A `NormalizedUrl` pairing the trimmed input with its canonical origin.

    Raises:
        UrlValidationError: If `raw` is not an absolute `http`/`https` URL, carries
            credentials, is longer than `MAX_URL_LENGTH`, or has a malformed host or port.
    """
    candidate = raw.strip()

    if not candidate:
        raise _reject("URL must not be empty")

    if len(candidate) > MAX_URL_LENGTH:
        raise _reject(f"URL must be at most {MAX_URL_LENGTH} characters")

    # Checked before `urlsplit`, because `urlsplit` silently DELETES tab, newline, and
    # carriage return anywhere in the input (WHATWG-compatible behaviour, Python 3.6.14+).
    # Without this, `"https://exa\nmple.com"` would normalize to `https://example.com` — a
    # different site than the one the string names, arrived at by deletion.
    if any(character.isspace() or ord(character) < 0x20 for character in candidate):
        raise _reject("URL must not contain spaces, tabs, newlines, or control characters")

    try:
        parts = urlsplit(candidate)
    except ValueError as error:
        raise _reject(f"URL could not be parsed: {error}") from None

    if parts.scheme not in _ALLOWED_SCHEMES:
        # Covers three distinct inputs with one message, on purpose: an unsupported scheme
        # (`ftp://`), and both flavours of not-absolute (`example.com` and `/path`, which
        # `urlsplit` reports with an empty scheme). All three are answered by the same
        # instruction, so splitting them into three messages would only make the caller
        # read more to learn the same thing.
        raise _reject("URL must be an absolute http:// or https:// URL")

    if "@" in parts.netloc:
        # Never echo the netloc back — it is the part that would contain the password.
        raise _reject("URL must not contain credentials (a user:password@ prefix)")

    try:
        port = parts.port
    except ValueError:
        # `SplitResult.port` raises rather than returning None for a port that is
        # non-numeric or outside 0-65535.
        raise _reject("URL port must be a number between 0 and 65535") from None

    host = _validate_host(parts.hostname or "")

    # Re-bracket an IPv6 literal: `urlsplit` strips the brackets from `hostname`, but they
    # are required in the authority component, so `[::1]:8080` must be rebuilt as such.
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        authority = f"{authority}:{port}"

    return NormalizedUrl(url=candidate, origin=f"{parts.scheme}://{authority}")
