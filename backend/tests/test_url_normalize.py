"""Tests for app.features.websites.internals.url_normalize.

Pure-function tests, no database and no HTTP: this module is the one place the "what counts
as the same website" rule is written down, and it is the crawler's future dependency as much
as it is `POST /websites`'s. `tests/test_websites_api.py` covers the same function reached
through the API, including the ticket's literal acceptance criterion.
"""

import pytest

from app.features.websites.internals.url_normalize import (
    MAX_URL_LENGTH,
    UrlValidationError,
    normalize_url,
)


@pytest.mark.parametrize(
    ("submitted", "expected_origin"),
    [
        # The acceptance criterion, verbatim.
        ("https://Example.com:443/path/", "https://example.com"),
        # Host case folding — the host is case-insensitive, the path is not.
        ("https://EXAMPLE.COM", "https://example.com"),
        ("HTTPS://Example.Com/Path", "https://example.com"),
        # Default ports are redundant and must not make two spellings different websites.
        ("https://example.com:443", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        # A non-default port IS part of the identity — :8443 is a different service.
        ("https://example.com:8443", "https://example.com:8443"),
        ("http://example.com:8080/x", "http://example.com:8080"),
        # An origin has no path, with or without a trailing slash.
        ("https://example.com/", "https://example.com"),
        ("https://example.com/a/b/c/", "https://example.com"),
        # Query and fragment are per-request, never part of the site's identity.
        ("https://example.com/search?q=1", "https://example.com"),
        ("https://example.com/docs#section", "https://example.com"),
        ("https://example.com/?utm_source=x&utm_campaign=y#top", "https://example.com"),
        # The scheme is part of the origin: http and https are two different origins.
        ("http://example.com", "http://example.com"),
        # Surrounding whitespace is a paste artifact, not input.
        ("  https://example.com/  ", "https://example.com"),
        # Subdomains are distinct hosts, and so is the explicit-root trailing dot form.
        ("https://docs.example.com", "https://docs.example.com"),
        ("https://example.com.", "https://example.com."),
        # An IP literal is a legitimate crawl target.
        ("http://127.0.0.1:8000/docs", "http://127.0.0.1:8000"),
        ("http://[::1]:8000/docs", "http://[::1]:8000"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
        # An internationalized host is stored the way DNS and the crawler will use it.
        ("https://ünïcode.example", "https://xn--ncode-cta3g.example"),
    ],
)
def test_origin_normalization(submitted: str, expected_origin: str) -> None:
    assert normalize_url(submitted).origin == expected_origin


def test_www_is_kept_because_it_can_serve_different_content() -> None:
    """`www.example.com` and `example.com` are NOT folded together.

    The single most tempting "improvement" to this module, and the one that would silently
    generate one site's llms.txt from another site's pages. See the module docstring in
    url_normalize.py.
    """
    assert normalize_url("https://www.example.com").origin == "https://www.example.com"
    assert normalize_url("https://example.com").origin == "https://example.com"
    assert (
        normalize_url("https://www.example.com").origin
        != normalize_url("https://example.com").origin
    )


def test_the_submitted_url_is_preserved_verbatim_apart_from_trimming() -> None:
    """`url` keeps the path, query, and fragment that `origin` drops.

    This pairing is the point of the whole module: `origin` is what deduplicates, `url` is
    what the user sees echoed back and what makes a surprising dedupe explainable.
    """
    normalized = normalize_url("  https://Example.com:443/docs?q=1#top  ")
    assert normalized.url == "https://Example.com:443/docs?q=1#top"
    assert normalized.origin == "https://example.com"


@pytest.mark.parametrize(
    "submitted",
    [
        pytest.param("ftp://example.com", id="ftp-scheme"),
        pytest.param("file:///etc/passwd", id="file-scheme"),
        pytest.param("javascript:alert(1)", id="javascript-scheme"),
        pytest.param("data:text/plain,hello", id="data-scheme"),
        pytest.param("example.com", id="bare-host-not-absolute"),
        pytest.param("example.com/path", id="relative-with-path"),
        pytest.param("/websites/1", id="absolute-path-only"),
        pytest.param("//example.com", id="protocol-relative"),
        pytest.param("not a url at all", id="garbage"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("https://", id="scheme-with-no-host"),
        pytest.param("https:///path", id="empty-host-with-path"),
        pytest.param("https://exa mple.com", id="space-in-host"),
        pytest.param("https://exa\nmple.com", id="newline-in-host"),
        pytest.param("https://example.com:https", id="non-numeric-port"),
        pytest.param("https://example.com:99999", id="port-out-of-range"),
        pytest.param("https://-example.com", id="host-label-starts-with-hyphen"),
        pytest.param("https://example..com", id="empty-host-label"),
        pytest.param("https://exam_ple.com", id="underscore-in-host"),
    ],
)
def test_invalid_urls_are_rejected(submitted: str) -> None:
    with pytest.raises(UrlValidationError):
        normalize_url(submitted)


@pytest.mark.parametrize(
    "submitted",
    [
        "https://user:password@example.com",
        "https://user@example.com",
        "http://admin:hunter2@example.com/dashboard",
    ],
)
def test_credentials_in_the_url_are_rejected_not_stripped(submitted: str) -> None:
    """A credentialed URL is a secret (ARCHITECTURE.md §9.1) and this one would be persisted.

    Stripping the credentials and accepting the rest would store a sanitized copy while the
    user believed their credentials were in use — and would have quietly accepted a password
    into a column that is returned by an endpoint every signed-in user can read.
    """
    with pytest.raises(UrlValidationError) as raised:
        normalize_url(submitted)

    # The rejection must not echo the credential back in its message.
    assert "password" not in str(raised.value).replace("user:password", "")
    assert "hunter2" not in str(raised.value)


def test_a_url_at_the_length_limit_is_accepted_and_one_over_is_not() -> None:
    prefix = "https://example.com/"
    at_limit = prefix + "a" * (MAX_URL_LENGTH - len(prefix))
    assert len(at_limit) == MAX_URL_LENGTH
    assert normalize_url(at_limit).origin == "https://example.com"

    with pytest.raises(UrlValidationError):
        normalize_url(at_limit + "a")


def test_a_host_longer_than_dns_allows_is_rejected() -> None:
    """253 characters is the maximum presentation-format DNS name; longer cannot resolve."""
    long_host = ".".join(["a" * 63] * 4)  # 4 * 63 + 3 dots = 255
    with pytest.raises(UrlValidationError):
        normalize_url(f"https://{long_host}")


def test_an_invalid_ipv6_literal_is_rejected() -> None:
    with pytest.raises(UrlValidationError):
        normalize_url("https://[2001:db8::1::2]")


def test_the_error_is_a_valueerror_so_pydantic_renders_it_as_a_422() -> None:
    """The contract between this module and `schemas.CreateWebsiteRequest`.

    A pydantic `field_validator` converts a `ValueError` into FastAPI's native 422. If this
    exception ever stopped being a `ValueError`, every malformed URL would become a 500 and
    the only thing that would notice is this assertion.
    """
    assert issubclass(UrlValidationError, ValueError)
