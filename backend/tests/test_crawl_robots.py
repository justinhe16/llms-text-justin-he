"""Tests for `app.features.crawl.internals.robots` — parsing `robots.txt` beyond its
`Sitemap:` lines.

No database, no network, no clock: `parse_robots` and `effective_crawl_delay_ms` are pure,
exactly like `tests/test_url_ranking.py`'s subject. Every test here calls the module directly
with hand-built `robots.txt` bodies; `tests/test_crawl_sitemap.py` pins the wiring (the one
fetch, the shared response, the politeness gate) and `tests/test_run_persistence.py` pins the
end-to-end behaviour.
"""

import inspect
import time
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
        # The plain-prefix shape: `/private` must anchor at position 0, not merely appear
        # somewhere in the target -- `/public/private` is a genuinely different path.
        ("/private", "https://docs.test/private", False),
        ("/private", "https://docs.test/public/private", True),
        # The same anchoring, with a `*` in the pattern: `_matches`'s FIRST segment is still
        # `str.startswith` at position 0, even though every later segment is only `str.find`
        # from wherever the previous one ended. "/private/" appears in the second URL -- just
        # not at the start -- and must not match.
        ("/private/*.pdf", "https://docs.test/private/guide.pdf", False),
        ("/private/*.pdf", "https://docs.test/public/private/guide.pdf", True),
    ],
)
def test_disallow_is_a_prefix_match_anchored_at_position_zero_not_a_substring_match(
    pattern: str, url: str, expected: bool
) -> None:
    """Robots.txt paths are PREFIX matches anchored at the start of the target, never a
    substring test -- `Disallow: /private` must not block `/public/private`. `_matches` gets
    this right by checking its first segment with `target.startswith(first)`; mutating that
    check to `first in target` would let all four cases above match (nothing after it corrects
    for the wrong start position), so this is what pins the check to `startswith` rather than
    membership."""
    text = f"User-agent: *\nDisallow: {pattern}\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed(url) is expected


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


def test_dollar_anchor_accepts_a_zero_width_wildcard_at_the_match_boundary() -> None:
    """Pins `_matches`'s anchored branch at its exact boundary. `start` is where the final
    `$`-anchored segment would have to begin; `pos` is where the previous segment's match
    ended; the guard is `start < pos`. `start == pos` is a LEGITIMATE match -- the `*` between
    the two segments consumed zero characters -- and the guard must let it through; mutating
    `<` to `<=` would turn this exact case into a false rejection.

    Pattern `/aaa*a$` (segments `("/aaa", "a")`, anchored) against target `/aaaa`: the first
    segment matches `/aaa` at position 0-4, so `pos == 4`; the anchored last segment `a` must
    then start at `len(target) - 1 == 4`, i.e. `start == pos == 4` -- the `*` contributed
    nothing between them. Its near-neighbour, target `/aaa` (one character shorter), is where
    the segments genuinely WOULD have to overlap: the anchored `a` would have to start at
    position 3, one character before the first segment's own match ends, and must not match
    either way -- this is not the boundary the mutant moves.
    """
    text = "User-agent: *\nDisallow: /aaa*a$\n"
    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed("https://docs.test/aaaa") is False  # start == pos: a real match
    assert rules.is_allowed("https://docs.test/aaa") is True  # start == pos - 1: a real overlap


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


# --- Glob edge cases --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        # Leading "*" — an empty first segment, matched trivially at position 0.
        ("*.pdf", "https://docs.test/guide.pdf", False),
        ("*.pdf", "https://docs.test/guide.txt", True),
        # Trailing "*" — an empty last segment; unanchored, so it degrades to a plain prefix
        # match identical to the star-free pattern.
        ("/docs*", "https://docs.test/docs/page", False),
        ("/docs*", "https://docs.test/doc", True),
        # Consecutive "**" — the empty middle segment does no work, so "**" behaves exactly
        # like a single "*".
        ("/a**b", "https://docs.test/axyzb", False),
        ("/a**b", "https://docs.test/ab", False),
        # The pattern "*" alone — matches every target, no matter its shape.
        ("*", "https://docs.test/anything/at/all", False),
        # The pattern "/" alone — no wildcard at all, an ordinary prefix match against every
        # path (the same case `test_disallow_slash_allows_nothing` pins end to end).
        ("/", "https://docs.test/", False),
        ("/", "https://docs.test/anything", False),
        # "$" appearing anywhere other than the final character is a literal, per the de facto
        # standard — this pattern is NOT end-anchored.
        ("/a$b", "https://docs.test/a$b", False),
        ("/a$b", "https://docs.test/a", True),
    ],
)
def test_glob_edge_cases(pattern: str, url: str, expected: bool) -> None:
    text = f"User-agent: *\nDisallow: {pattern}\n"

    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)

    assert rules.is_allowed(url) is expected


# --- ReDoS resistance ---------------------------------------------------------------------------


def test_a_hostile_many_wildcard_pattern_returns_promptly() -> None:
    """The exact reproduction from the code-review finding this test exists to pin down: a
    regex-based translation of this pattern (`".*".join(re.escape(seg) for seg in
    pattern.split("*"))`, what `_compile` built before this ticket) is catastrophically
    backtracking against a target that satisfies every wildcard's prefix but never reaches the
    trailing literal — `is_allowed` runs synchronously, inline, on the worker's single event
    loop (module docstring's "wildcards" section), so a hang here is not merely slow, it
    freezes every OTHER crawl sharing that worker process. The bound below is generous — the
    greedy glob matcher this ticket replaced it with returns in microseconds — but real: it is
    what makes this test fail loudly if an exponential matcher ever comes back.
    """
    pattern = "/" + "a*" * 25 + "Z"
    text = f"User-agent: *\nDisallow: {pattern}\n"
    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)
    url = "http://example.com/" + "a" * 60  # matches every "a*" prefix, never reaches "Z"

    start = time.monotonic()
    result = rules.is_allowed(url)
    elapsed = time.monotonic() - start

    assert result is True  # the trailing "Z" is never found -> not disallowed
    assert elapsed < 0.5, f"is_allowed took {elapsed:.3f}s — the matcher is backtracking again"


def test_a_hostile_dollar_anchored_pattern_that_cannot_match_returns_promptly() -> None:
    """Same adversarial shape, `$`-anchored — `_matches`'s anchored branch is a different code
    path (module docstring) from its unanchored one and needs its own coverage: a target that
    satisfies every `a*` prefix but can never satisfy the anchor, because it does not end in
    `Z` at all."""
    pattern = "/" + "a*" * 25 + "Z$"
    text = f"User-agent: *\nDisallow: {pattern}\n"
    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)
    url = "http://example.com/" + "a" * 60  # ends in "a", never "Z"

    start = time.monotonic()
    result = rules.is_allowed(url)
    elapsed = time.monotonic() - start

    assert result is True
    assert elapsed < 0.5, f"is_allowed took {elapsed:.3f}s — the matcher is backtracking again"


def test_many_consecutive_wildcards_against_a_long_target_return_promptly() -> None:
    """`****...*` collapses to a single `*` (the "glob edge cases" section above) -- but a
    pattern of bare consecutive wildcards with nothing else is not, on its own, a ReDoS shape:
    matching fifty empty segments in a row against a target that satisfies every one of them
    (what an earlier version of this test did, with an unanchored `/` + `"*" * 50`) is cheap
    under EITHER matcher, old or new, because nothing ever forces a search for a split point
    that does not exist -- the match just succeeds. What makes many consecutive wildcards
    genuinely hostile is pairing them with a `$`-anchored literal the target can never reach:
    under the OLD regex-based `_compile` this ticket replaced (`".*".join(re.escape(p) for p
    in body.split("*"))` -- module docstring's "wildcards" section), fifty consecutive `.*`
    groups failing to find a trailing "Z" is exactly the catastrophic-backtracking shape, and
    hangs for seconds against a target only a few thousand characters long (confirmed against a
    scratch reconstruction of that old translation: this exact pattern/target pair had still
    not returned after a 5-second budget). The new segment-based `_matches` walks the same
    input in one linear pass instead, and this pins that it keeps doing so.
    """
    pattern = "/" + "*" * 50 + "Z$"
    text = f"User-agent: *\nDisallow: {pattern}\n"
    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)
    url = "http://example.com/" + "a" * 5000  # satisfies every "*", never reaches the "Z"

    start = time.monotonic()
    result = rules.is_allowed(url)
    elapsed = time.monotonic() - start

    assert result is True  # the trailing "Z" is never found -> not disallowed
    assert elapsed < 0.5, f"is_allowed took {elapsed:.3f}s -- the matcher is backtracking again"


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
