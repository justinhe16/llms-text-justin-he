"""Tests for `app.features.crawl.internals.robots` — parsing `robots.txt` beyond its
`Sitemap:` lines.

No database, no network, no clock: `parse_robots` and `effective_crawl_delay_ms` are pure,
exactly like `tests/test_url_ranking.py`'s subject. Every test here calls the module directly
with hand-built `robots.txt` bodies; `tests/test_crawl_sitemap.py` pins the wiring (the one
fetch, the shared response, the politeness gate) and `tests/test_run_persistence.py` pins the
end-to-end behaviour.
"""

import inspect
from pathlib import Path

import pytest

from app.features.crawl.http_client import CRAWL_USER_AGENT
from app.features.crawl.internals import robots
from app.features.crawl.internals.robots import (
    ALLOW_ALL,
    MAX_RULES,
    effective_crawl_delay_ms,
    parse_robots,
)


_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    """See `tests/test_crawl_extract.py`'s helper of the same name — identical contract."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


# --- Purity ---------------------------------------------------------------------------------


def test_module_is_pure_no_async_no_io_no_clock() -> None:
    """Source-level assertion, deliberately — the same technique
    `tests/test_url_ranking.py`'s test of the same name uses, and for the same reason: a
    behavioural test cannot distinguish "doesn't need the clock for this input" from "cannot
    read the clock at all."""
    source = inspect.getsource(robots)

    assert "async " not in source
    assert "await " not in source
    assert "httpx" not in source
    assert "requests" not in source
    assert "socket" not in source
    assert "open(" not in source
    assert "Path(" not in source
    assert "datetime.now(" not in source
    assert ".utcnow(" not in source
    assert "time.time(" not in source
    assert "time.monotonic(" not in source
    assert "import time" not in source
    assert "app.core.settings" not in source


# --- Group selection --------------------------------------------------------------------------


def test_a_named_group_wins_and_the_star_group_is_never_merged_in() -> None:
    """[Group isolation]. `/a` is disallowed by the named group; `/b` is disallowed only by
    the `*` group. If the two were merged, `/b` would be disallowed too — it is not."""
    text = "User-agent: *\nDisallow: /b\n\nUser-agent: llms-text-bot\nDisallow: /a\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/a") is False
    assert rules.is_allowed("https://docs.test/b") is True


def test_the_star_group_is_used_when_no_named_group_matches() -> None:
    text = "User-agent: *\nDisallow: /private\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/private") is False


def test_no_matching_group_at_all_allows_everything() -> None:
    text = "User-agent: googlebot\nDisallow: /\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules == ALLOW_ALL
    assert rules.is_allowed("https://docs.test/anything") is True


def test_consecutive_user_agent_lines_form_one_group() -> None:
    """The ordinary multi-agent shape: two `User-agent` lines with no rule between them share
    whatever rules follow."""
    text = "User-agent: googlebot\nUser-agent: llms-text-bot\nDisallow: /shared\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/shared") is False


def test_two_blocks_naming_the_same_agent_are_merged_not_isolated() -> None:
    """Distinct from group ISOLATION (the `*`-vs-named test above): two `User-agent:
    llms-text-bot` blocks in one file are the same group, written twice, and their rules
    combine in file order — this is standard, and not what the isolation rule forbids."""
    text = "User-agent: llms-text-bot\nDisallow: /a\n\nUser-agent: llms-text-bot\nDisallow: /b\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/a") is False
    assert rules.is_allowed("https://docs.test/b") is False


def test_a_sitemap_directive_between_groups_does_not_split_the_group() -> None:
    """`Sitemap:` is group-independent — module docstring — so a real file that puts one
    between two `User-agent` lines still forms a single group out of them."""
    text = "User-agent: llms-text-bot\nSitemap: https://docs.test/sitemap.xml\nDisallow: /a\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/a") is False


# --- Matching: longest match, ties, wildcards -------------------------------------------------


def test_longest_matching_rule_wins() -> None:
    text = "User-agent: *\nDisallow: /docs\nAllow: /docs/public\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/docs/public/page") is True
    assert rules.is_allowed("https://docs.test/docs/private") is False


def test_an_exact_length_tie_resolves_to_allow() -> None:
    text = "User-agent: *\nDisallow: /docs\nAllow: /docs\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/docs") is True


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("/*.pdf$", "https://docs.test/guide.pdf", False),
        ("/*.pdf$", "https://docs.test/guide.pdf.html", True),
        ("/a/*/b", "https://docs.test/a/x/b", False),
        ("/a/*/b", "https://docs.test/a/x/y/b", False),
        ("/a/*/b", "https://docs.test/a/b", True),
        ("/x$", "https://docs.test/x", False),
        ("/x$", "https://docs.test/xy", True),
    ],
)
def test_star_and_dollar_wildcards(pattern: str, url: str, expected: bool) -> None:
    text = f"User-agent: *\nDisallow: {pattern}\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed(url) is expected


def test_an_empty_disallow_allows_everything() -> None:
    text = "User-agent: *\nDisallow:\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.disallow == ()
    assert rules.is_allowed("https://docs.test/anything") is True


def test_disallow_slash_allows_nothing() -> None:
    text = "User-agent: *\nDisallow: /\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/") is False
    assert rules.is_allowed("https://docs.test/anything") is False


# --- Percent-encoding --------------------------------------------------------------------------


def test_percent_encoding_compares_equal_in_both_directions() -> None:
    rules_encoded_url = parse_robots(
        "User-agent: *\nDisallow: /private\n", user_agent=CRAWL_USER_AGENT
    )
    assert rules_encoded_url.is_allowed("https://docs.test/%70rivate") is False

    rules_encoded_rule = parse_robots(
        "User-agent: *\nDisallow: /%70rivate\n", user_agent=CRAWL_USER_AGENT
    )
    assert rules_encoded_rule.is_allowed("https://docs.test/private") is False


def test_percent_2f_is_not_decoded_on_either_side() -> None:
    """`%2F` is preserved, never decoded — a `Disallow` written with a literal slash does NOT
    match a URL carrying an encoded one, because decoding it would change the path's
    structure."""
    rules = parse_robots("User-agent: *\nDisallow: /a/b\n", user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/a%2Fb") is True


def test_a_multi_byte_percent_sequence_decodes_as_one_run() -> None:
    """`%C3%A9` is `é` in UTF-8 — decoded together in one `unquote()` call so it round-trips
    correctly instead of becoming two replacement characters, and therefore compares equal to
    its literal spelling."""
    rules = parse_robots("User-agent: *\nDisallow: /caf%C3%A9\n", user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/café") is False


# --- User-agent token derivation ----------------------------------------------------------------


def test_the_user_agent_token_is_derived_from_the_full_header_value() -> None:
    text = "User-agent: llms-text-bot\nDisallow: /private\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/private") is False


# --- Failure is open ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<!DOCTYPE html><html><body>404 Not Found</body></html>",
        "\x00\x01\x02 binary junk \xff not a robots file at all",
        "",
    ],
)
def test_a_malformed_or_html_body_allows_everything_without_raising(text: str) -> None:
    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules == ALLOW_ALL


# --- Crawl-delay parsing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10", 10.0),
        ("0.5", 0.5),
        ("-1", None),
        ("nan", None),
        ("abc", None),
        ("", None),
    ],
)
def test_crawl_delay_parsing(value: str, expected: float | None) -> None:
    text = f"User-agent: *\nCrawl-delay: {value}\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.crawl_delay_s == expected


def test_crawl_delay_is_none_when_not_declared() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /private\n", user_agent=CRAWL_USER_AGENT)

    assert rules.crawl_delay_s is None


def test_the_last_valid_crawl_delay_wins() -> None:
    text = "User-agent: *\nCrawl-delay: 5\nCrawl-delay: 9\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.crawl_delay_s == 9.0


def test_an_invalid_crawl_delay_does_not_overwrite_a_valid_earlier_one() -> None:
    text = "User-agent: *\nCrawl-delay: 5\nCrawl-delay: abc\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.crawl_delay_s == 5.0


# --- effective_crawl_delay_ms ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured_ms", "crawl_delay_s", "expected"),
    [
        (200, 0.1, 200),  # below configured -> unchanged
        (200, 5.0, 5000),  # above configured -> widened
        (200, 3600.0, 10_000),  # far above the ceiling -> clamped
        (500, None, 500),  # no Crawl-delay published -> configured value
    ],
)
def test_effective_crawl_delay_ms(
    configured_ms: int, crawl_delay_s: float | None, expected: int
) -> None:
    assert effective_crawl_delay_ms(configured_ms, crawl_delay_s) == expected


# --- Defensive ceiling -------------------------------------------------------------------------


def test_a_rule_count_beyond_max_rules_is_truncated() -> None:
    lines = ["User-agent: *"] + [f"Disallow: /path-{i}" for i in range(MAX_RULES + 500)]
    text = "\n".join(lines) + "\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert len(rules.disallow) == MAX_RULES
    assert rules.disallow[0] == "/path-0"


# --- Fixture -------------------------------------------------------------------------------


def test_the_realistic_fixture_carries_a_named_group_wildcards_and_a_crawl_delay() -> None:
    text = _load_fixture("robots_groups.txt")

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    # The llms-text-bot group wins outright — the `*` group's own `/private/` rule never
    # applies, and its own admin rules (Disallow with an Allow carve-out, and a `$`-anchored
    # wildcard) do.
    assert rules.is_allowed("https://docs.test/private/") is True
    assert rules.is_allowed("https://docs.test/admin/settings") is False
    assert rules.is_allowed("https://docs.test/admin/public/page") is True
    assert rules.is_allowed("https://docs.test/guide.pdf") is False
    assert rules.crawl_delay_s == 5.0
