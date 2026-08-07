"""Tests for `app.features.runs.internals.artifact_filename`.

Pure-function tests, no database and no HTTP — in the spirit of `tests/test_run_limits.py`
and `tests/test_url_normalize.py`: this module is the ONE place that decides both the
download filename `GET /runs/{id}/llms.txt` / `GET /runs/{id}/llms-full.txt` offer and the
`404` detail `RunService._missing_artifact_detail` builds when there is nothing to download.

`_PARITY_CASES` below is a transcription of the inputs and expected outputs run against BOTH
`artifact_filename` (this module) and `llmsTxtFilename` (`frontend/lib/crawls/run-display.ts:
79`) by hand when this landed — see `artifact_filename.py`'s own module docstring for why a
shared, documented table plus two pointers is what stands in for a shared executable fixture
in a repo with no vitest/jest. Any future change to either function must be re-checked
against this table by hand, and both source files carry a pointer to it.
"""

import re

import pytest

from app.features.runs.internals.artifact_filename import (
    ArtifactKind,
    artifact_filename,
    artifact_name,
)


# Both `ArtifactKind` values, spelled out once with an explicit annotation so a `for kind in
# _KINDS` loop below infers `kind: ArtifactKind` rather than the widened `str` a bare tuple
# literal would give it — the same "no `Any`, no cast" bar the rest of this diff holds to.
_KINDS: tuple[ArtifactKind, ArtifactKind] = ("index", "full")

# (origin, expected "index"-kind filename). The "full"-kind expectation for each row is
# derived mechanically in the parametrized tests below, never duplicated here — see
# `test_the_full_expansion_uses_the_same_slug_with_a_llms_full_stem`.
_PARITY_CASES = [
    pytest.param("https://example.com", "llms-example-com.txt", id="plain-https"),
    pytest.param("http://example.com", "llms-example-com.txt", id="plain-http"),
    pytest.param("https://example.com:8443", "llms-example-com-8443.txt", id="https-with-port"),
    pytest.param(
        "https://sub.domain.example.co.uk",
        "llms-sub-domain-example-co-uk.txt",
        id="multi-label-subdomain",
    ),
    pytest.param("HTTPS://EXAMPLE.COM", "llms-example-com.txt", id="uppercase-scheme-and-host"),
    pytest.param("example.com", "llms-example-com.txt", id="no-scheme-at-all"),
    pytest.param("svn+ssh://example.com", "llms-example-com.txt", id="non-http-scheme"),
    pytest.param("//example.com", "llms-example-com.txt", id="protocol-relative"),
    pytest.param("https://example.com/", "llms-example-com.txt", id="trailing-slash"),
    pytest.param("ftp://192.168.0.1:9000", "llms-192-168-0-1-9000.txt", id="ip-host-with-port"),
    pytest.param(
        "https://xn--r8jz45g.com", "llms-xn-r8jz45g-com.txt", id="punycode-run-of-hyphens"
    ),
    pytest.param("https://例え.com", "llms-com.txt", id="non-ascii-host-label-dropped"),
    pytest.param("https://", "llms-site.txt", id="scheme-only"),
    pytest.param("---", "llms-site.txt", id="hyphens-only"),
    pytest.param("", "llms-site.txt", id="empty-string"),
    pytest.param(
        "https://EXAMPLE.com:8443", "llms-example-com-8443.txt", id="mixed-case-with-port"
    ),
]


# -----------------------------------------------------------------------------------------
# The parity table — verified by hand against the TypeScript original
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("origin", "expected"), _PARITY_CASES)
def test_the_parity_table_matches_the_typescript_original(origin: str, expected: str) -> None:
    assert artifact_filename(origin, "index") == expected


@pytest.mark.parametrize(("origin", "expected"), _PARITY_CASES)
def test_the_full_expansion_uses_the_same_slug_with_a_llms_full_stem(
    origin: str, expected: str
) -> None:
    """`"full"` is the same slug behind a different stem — never a second, independently
    derived filename that could drift from the `"index"` one."""
    assert artifact_filename(origin, "full") == expected.replace("llms-", "llms-full-", 1)


# -----------------------------------------------------------------------------------------
# The fallback slug
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin",),
    [
        pytest.param("https://", id="scheme-only"),
        pytest.param("---", id="hyphens-only"),
        pytest.param("", id="empty-string"),
        pytest.param("://", id="bare-separator"),
    ],
)
def test_an_origin_that_sanitizes_to_nothing_falls_back_to_site(origin: str) -> None:
    assert artifact_filename(origin, "index") == "llms-site.txt"


# -----------------------------------------------------------------------------------------
# The `+` quantifier — a run of unusable characters collapses to ONE hyphen
# -----------------------------------------------------------------------------------------


def test_a_run_of_unusable_characters_collapses_to_one_hyphen() -> None:
    """`xn--r8jz45g.com` has two consecutive hyphens after its scheme is stripped; dropping
    the `+` quantifier in `_NON_ALPHANUMERIC` would turn that into three (`xn---r8jz45g-com`)
    instead of collapsing the run to one — this is the single most likely way to reimplement
    the sanitizer wrong, so it gets its own test rather than relying on the parity table
    alone to catch it."""
    assert artifact_filename("https://xn--r8jz45g.com", "index") == "llms-xn-r8jz45g-com.txt"


# -----------------------------------------------------------------------------------------
# The header-injection guarantee
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("origin", "expected"), _PARITY_CASES)
def test_the_result_contains_only_characters_a_header_cannot_be_injected_through(
    origin: str, expected: str
) -> None:
    """`_artifact_response` (`app.api.routers.runs`) interpolates this value into a quoted
    `Content-Disposition` header with no escaping — safe only because the result can never
    contain a quote, a semicolon, a CR, or an LF. Asserted over the whole parity table, which
    establishes the guarantee for every input the two implementations are pinned against;
    `test_a_hostile_origin_cannot_break_out_of_the_header` below establishes it for inputs
    chosen to attack the header itself."""
    for kind in _KINDS:
        result = artifact_filename(origin, kind)
        assert re.fullmatch(r"[a-z0-9-]+\.txt", result), result


# Origins written to break out of `attachment; filename="..."` rather than to exercise the
# sanitizer's normal shape. None of these can reach the header intact, but that is a property
# worth asserting against inputs that actually try: the table above is a *parity* fixture and
# every row of it is a plausible origin, so on its own it demonstrates the guarantee only for
# values that were never hostile to begin with.
#
# `websites.origin` is a normalized scheme + host and cannot realistically hold a newline
# today. That is a fact about a different module (`websites/internals/url_normalize.py`), and
# this function's safety must not depend on it — the whole reason the filename is sanitized
# here is so the header stays safe no matter what a `websites` row comes to contain.
_HOSTILE_ORIGINS = [
    pytest.param('https://example.com"; filename="evil.sh', id="quote-and-semicolon"),
    pytest.param("https://example.com\r\nSet-Cookie: session=stolen", id="crlf-header-split"),
    pytest.param("https://example.com\nX-Injected: yes", id="bare-lf"),
    pytest.param("https://example.com\r", id="bare-cr"),
    pytest.param("../../../../etc/passwd", id="path-traversal"),
    pytest.param("https://example.com/../../secret.txt", id="traversal-after-host"),
    pytest.param("https://example.com\x00.txt", id="nul-byte"),
    pytest.param("https://example.com; rm -rf /", id="shell-metacharacters"),
]


@pytest.mark.parametrize("origin", _HOSTILE_ORIGINS)
def test_a_hostile_origin_cannot_break_out_of_the_header(origin: str) -> None:
    """The security claim in `artifact_filename`'s docstring, asserted against inputs written
    to defeat it rather than against plausible origins.

    Checks the guarantee two ways, because the second does not follow from the first by
    inspection: the result matches `[a-z0-9-]+\\.txt`, AND the header `_artifact_response`
    would actually build stays one line naming one filename — two quotes, one semicolon, no
    control characters.
    """
    for kind in _KINDS:
        result = artifact_filename(origin, kind)

        assert re.fullmatch(r"[a-z0-9-]+\.txt", result), result

        header = f'attachment; filename="{result}"'
        assert header.count('"') == 2, header
        assert header.count(";") == 1, header
        assert "\r" not in header
        assert "\n" not in header
        assert "\x00" not in header


# -----------------------------------------------------------------------------------------
# artifact_name and artifact_filename agree on which artifact is which
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("kind",), [pytest.param("index"), pytest.param("full")])
def test_artifact_name_and_artifact_filename_name_the_same_artifact(kind: ArtifactKind) -> None:
    """`RunService._missing_artifact_detail` is built from `artifact_name`, and the download
    itself is named by `artifact_filename` — this is the test that fails if the two ever name
    two different artifacts for the same `kind`."""
    stem = artifact_name(kind).removesuffix(".txt")
    assert artifact_filename("https://example.com", kind).startswith(f"{stem}-")
