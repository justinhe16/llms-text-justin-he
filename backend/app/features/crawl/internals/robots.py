"""Parsing `robots.txt` beyond its `Sitemap:` lines — `Disallow`, `Allow`, and `Crawl-delay`.

Until PER-191, this crawler read exactly one directive out of `robots.txt`: `Sitemap:`
(`internals/sitemap.py`'s `_robots_sitemap_urls`). Everything else — which paths a site's
operator has asked not to be fetched, and how fast they will tolerate being fetched — was
read by nothing and enforced by nothing. This module is the other half: `parse_robots` turns
one `robots.txt` body into a `RobotsRules`, and `RobotsRules.is_allowed` and
`effective_crawl_delay_ms` are what the rest of this feature consults it through.

**Pure, and never raises** — the same contract `internals/links.py`'s `extract_links` and
`internals/extract.py`'s `extract_content` hold, and for the same reason: this runs on
attacker-supplied bytes, and a `robots.txt` this module cannot make sense of is not a reason
to fail a run a user explicitly asked for. `parse_robots`'s entire body sits inside one
`try/except Exception`, logs at WARNING, and returns `ALLOW_ALL` — the same "fail open" rule
`internals/sitemap.py` already applies to a missing or unreadable `robots.txt` file, extended
here to a `robots.txt` this module cannot PARSE: a 404, a timeout, a 500, and an HTML error
page all collapse to the same `ALLOW_ALL` a syntactically-nonsense body does, because none of
them is evidence the site operator meant to restrict anything, and refusing to crawl a site
because ITS robots.txt was momentarily broken would be a worse failure mode than the one
`Disallow` support exists to fix.

**The disallowed-seed decision, and why it belongs to the CALLER, not to this module.** This
module answers "is this one URL allowed?" — `RobotsRules.is_allowed`. What `internals/
crawler.py`'s `crawl_site` does with a `False` answer for the run's SEED is a decision this
module has no way to make honestly, because it never sees a seed URL, only a `robots.txt`
body and a URL to test. The decision `crawler.py` makes (see its own `RobotsDisallowedError`
docstring) is to HONOUR the refusal and fail the run, rather than silently crawling the seed
anyway or silently skipping straight to an empty artifact — a `runs` row a user is watching
should say "this site's robots.txt disallows crawling this URL," not report success on zero
pages, and it should certainly not fetch a URL its own operator asked this crawler not to
fetch. Frontier URLs get no equivalent second check inside `crawl_site` at all: they are
filtered once, upstream, by `internals/url_ranking.py`'s `select_urls` under the
`"robots_disallowed"` rule, which is also where the drop is counted — a second check here
would either duplicate that counter or silently diverge from it.

**`Crawl-delay` applies to the CRAWL phase only, never to discovery's own remaining
fetches — and this is a deliberate, load-bearing split, not an oversight.** A site can publish
`Crawl-delay: 10` and a `sitemap_index.xml` with five children; sitemap discovery
(`internals/sitemap.py`'s `discover_sitemap_urls`) runs BEFORE `crawl_site`'s own
`asyncio.timeout(max_wall_clock_s)` even starts, bounded by nothing tighter than arq's 600s
job timeout, so widening its politeness gate to the site's own requested delay could spend
50+ unbounded seconds before a single page is fetched. So the gate discovery's own fetches
wait on stays at the OPERATOR-CONFIGURED `crawl_politeness_delay_ms` throughout discovery, and
`app.features.crawl.service.CrawlService.execute_run` widens the SAME `PolitenessGate`
(`internals/fetcher.py`) to the site's requested delay exactly once, after discovery has
returned and before `crawl_site`'s own loop begins — see that method, and
`PolitenessGate.widen_to`, for where the one call that matters actually sits. This module only
computes the NUMBER; it has no gate to widen and no opinion about when one should be.

**The 10-second ceiling is a module constant, never a `Settings` field, for the reason
`internals/sitemap.py`'s `SITEMAP_BYTE_SHARE` docstring already gives for its own constant:
this is a property of what this crawler considers a reasonable maximum politeness delay to
honour, not a number a deployment should be tuning.** A `robots.txt` is written by a site
operator who has never heard of THIS crawler's own wall-clock budget, and an unbounded
`Crawl-delay` (some sites genuinely publish `Crawl-delay: 86400`) would starve
`crawl_max_wall_clock_s` on its own, one gated request at a time, long before any of this
feature's other six caps had a chance to bind. The ceiling clamps only the ROBOTS
CONTRIBUTION — `effective_crawl_delay_ms` never lowers the operator's own configured value,
only ever raises it, and only ever by up to `MAX_ROBOTS_CRAWL_DELAY_MS`.

**Group selection: an exact-match, whole-token comparison, and the `*` group is never merged
in when a named group exists.** `robots.txt` groups its directives under one or more `User-agent`
lines, and the de facto standard (there is no single normative spec) is that the MOST
SPECIFIC applicable group wins outright — never blended with the wildcard group, even for a
directive the named group does not itself mention. `parse_robots` derives the product token
this crawler answers to from `user_agent` — `token = user_agent.split("/")[0].split()[0]
.lower()`, which turns `app.features.crawl.http_client.CRAWL_USER_AGENT`
(`"llms-text-bot/0.1 (+https://…)"`) into `"llms-text-bot"` — rather than hardcoding the
string a second time, so "read the constant" and "match it exactly" cannot drift apart. If
any group names that token, EVERY `*` group is discarded outright and only the named group(s)
are consulted; only when no group names the token does this module fall back to `*`; and
when neither exists, the whole file contributes nothing and `ALLOW_ALL` is returned. Merging
multiple groups that each name the SAME agent — two separate `User-agent: llms-text-bot`
blocks appearing in one file — is standard and unrelated to that isolation rule: both are
"the llms-text-bot group," just written in two places, and their rules are combined in file
order exactly as if they were one block.

**Consecutive `User-agent` lines with no rule between them form ONE group** — the ordinary
shape of `User-agent: googlebot\nUser-agent: bingbot\nDisallow: /private/`, which the spec
gives both agents the same rules for. A new group starts only when a `User-agent` line is
seen with no group currently open, or when the CURRENT group has already accumulated a rule
(`Allow`/`Disallow`/`Crawl-delay`) — a rule is what closes a group's agent list. Every other
directive (`Sitemap:`, `Host:`, `Request-rate:`, anything this module does not recognize) is
read by nothing here and, cruicaly, does NOT close the agent block either: `Sitemap:`
routinely sits between two `User-agent` lines in a real file (`tests/fixtures/robots_groups
.txt` has one), and treating it as a rule would incorrectly split what the file intends as one
group into two.

**Wildcards: `*` (any run of characters) and `$` (anchors the match to the end) — matched by a
greedy, non-backtracking glob, never by a regex.** An earlier revision of this module compiled
each pattern into a regex — `.*`.join of the literal runs between `*`s, escaped, anchored at
the start always and at the end only when the pattern itself ends in `$`. That is the
textbook translation, and it is also the textbook way to build a catastrophically backtracking
regex: a pattern with many `*`s tested against a target that satisfies every wildcard's prefix
but never reaches the final literal makes the engine try every split point of every `.*`
against every other before it can report failure, which is exponential in the number of
wildcards. `is_allowed` runs synchronously, inline, on the CRAWL phase's single event loop —
called from `crawler.py`'s seed check and `url_ranking.py`'s per-candidate frontier check —
with no timeout and no thread/process offload, and a hang there cannot be interrupted by
`crawl_site`'s own `asyncio.timeout` or arq's `job_timeout`, both of which only take effect at
the next `await`. With `max_jobs = 2` and no tenant isolation in this codebase (§4 of
`ARCHITECTURE.md`), one user's hostile `robots.txt` would freeze every other user's crawl
sharing that worker process. So this module uses the algorithm Google's own open-sourced
`robots.txt` parser uses instead: `_compile` splits a pattern's body on `*` into a tuple of
literal segments (a `$` at the end sets `anchored` and is stripped first; a `$` anywhere else
is just a literal character, since it has no other meaning in this grammar) and `_matches`
walks them in order — the first segment must match at position 0 (prefix semantics), every
following segment is located with a single `str.find` from wherever the previous one ended,
and, when `anchored`, the final segment must land exactly at the end of the target without
overlapping the previous match. An unanchored pattern succeeds the moment every segment has
been located in order; the rest of the target is free, which is what gives a bare pattern like
`/docs` its "matches this path or anything under it" behaviour. Every step is a fast C-level
`str.startswith`/`str.find`/`str.endswith` call, the work done is linear in
`len(pattern) + len(target)` by construction, and there is no split point to backtrack over —
no input can make this take longer than a linear scan. Compilation is memoized with a
module-level `@lru_cache`, now caching the parsed `(segments, anchored)` pair rather than a
compiled regex — still a pure function of the pattern string, so caching it introduces no
state this module's "no I/O, no clock" contract would be broken by; it exists because the same
handful of patterns from one `robots.txt` are tested against every candidate URL in a run's
frontier.

**Longest match wins, `Allow` wins an exact tie, and "longest" means the length of the RULE
PATTERN — `*` and `$` included — not of what it matched.** That is how the de facto standard
(Google's, and every crawler that follows it) defines specificity, and it is why
`Disallow: /` — the shortest possible disallow rule — loses to any `Allow` at all that also
matches: a one-character pattern cannot out-specify anything longer than itself. `Allow`
winning a genuine tie (`Disallow: /docs` vs `Allow: /docs`, both length 5) is the other half of
the same standard: an operator who bothered to write an `Allow` at the same specificity as a
`Disallow` meant the `Allow` to carry a URL that would otherwise be caught by the broader
rule.

**Percent-encoding: normalized identically on both the rule and the URL side, so `/private`
and `/%70rivate` compare equal — except for `%2F`, `%3F`, `%23`, and `%25`, which are
preserved (upper-cased, never decoded) because decoding any of THOSE would change what the
path actually means:** `%2F` decoded is a literal `/`, which would silently turn one path
segment into two; `%3F` decoded is a literal `?`, which would turn part of a path into a query
string; `%23` decoded is a literal `#`, a fragment delimiter no server ever receives; and
`%25` decoded is a literal `%`, which would make a SECOND decoding pass ambiguous with a
genuinely double-encoded value. Every other percent-token is decoded — a multi-byte UTF-8
sequence like `%C3%A9` is decoded as one RUN of consecutive tokens through a single
`urllib.parse.unquote()` call, not token-by-token, which is what keeps a two-byte character
from becoming two separate replacement characters. `is_allowed` applies the identical
normalization to the URL it is asked about, on the path plus query only — the fragment is
never sent to a server and is dropped before comparison, the same rule `internals/url_ranking
.py`'s own normalization step applies.

**Defensive ceilings, the same role `internals/links.py`'s `MAX_LINKS` plays for a hostile
page — and what they do NOT cover.** `MAX_LINES` bounds how much of a `robots.txt` body this
module will even scan (a hostile or merely enormous file cannot make this module do unbounded
*parsing* work), and `MAX_RULES` bounds how many `Allow`/`Disallow` entries the winning
group(s) contribute regardless of how many the file declares, kept in file order — a thousand
real, distinct path rules is already far beyond any file this crawler will meet in practice.
Neither one bounds the cost of matching a SINGLE pattern against a SINGLE URL — a file with
one line and no lines to spare could still carry a pattern expensive to match if the matcher
itself allowed it. That is why the matcher is linear-time BY CONSTRUCTION rather than merely
bounded from outside: see the "wildcards" section above for the algorithm and why a
regex-based one was replaced rather than capped. `is_allowed` has no timeout of its own and
runs inline on the worker's single event loop, so "some inputs are slow" was never an option
this module could patch around — every input has to be fast.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final
from urllib.parse import unquote, urlsplit


logger = logging.getLogger(__name__)

MAX_ROBOTS_CRAWL_DELAY_MS: Final = 10_000
"""The ceiling `effective_crawl_delay_ms` clamps a site's own `Crawl-delay` to, in
milliseconds — 10 seconds. See the module docstring for why this is a module constant and why
it bounds only the ROBOTS contribution, never the operator's own configured
`crawl_politeness_delay_ms`."""

MAX_RULES: Final = 1_000
"""A defensive ceiling on how many `Allow`/`Disallow` entries the winning group(s) may
contribute — the same role `internals/links.py`'s `MAX_LINKS` plays for a hostile page's link
count. See the module docstring's "defensive ceilings" section."""

MAX_LINES: Final = 50_000
"""A defensive ceiling on how many lines of a `robots.txt` body this module will scan —
bounds the work `parse_robots` will do on a hostile or merely enormous file, the same role
`MAX_LINES` plays for `MAX_RULES` one level up."""

_PRESERVED_TOKENS: Final[frozenset[str]] = frozenset({"%2F", "%3F", "%23", "%25"})
"""Percent-tokens `_normalize_encoding` refuses to decode — see the module docstring's
"percent-encoding" section for why each one would change what the path means if decoded."""

_TOKEN_PATTERN: Final = re.compile(r"%[0-9A-Fa-f]{2}")


def _normalize_encoding(value: str) -> str:
    """Percent-decode `value`, except for `_PRESERVED_TOKENS`, which are kept and
    upper-cased — see the module docstring's "percent-encoding" section.

    Applied identically to a rule's own path (at parse time) and to the URL `is_allowed`
    tests (at match time), which is what makes `/private` and `/%70rivate` compare equal on
    either side. Consecutive decodable tokens are decoded together in one `unquote()` call,
    not one token at a time, so a multi-byte UTF-8 sequence (`%C3%A9`) round-trips correctly
    instead of becoming two separate replacement characters.
    """
    pieces: list[str] = []
    run: list[str] = []
    index = 0
    length = len(value)

    def _flush_run() -> None:
        if run:
            pieces.append(unquote("".join(run)))
            run.clear()

    while index < length:
        if value[index] == "%" and index + 3 <= length and _TOKEN_PATTERN.match(value, index):
            token = value[index : index + 3]
            if token.upper() in _PRESERVED_TOKENS:
                _flush_run()
                pieces.append(token.upper())
            else:
                run.append(token)
            index += 3
        else:
            _flush_run()
            pieces.append(value[index])
            index += 1
    _flush_run()
    return "".join(pieces)


@dataclass(frozen=True, slots=True)
class _CompiledPattern:
    """One `Allow`/`Disallow` pattern, pre-split for `_matches` — private to this module. See
    the module docstring's "wildcards" section for the algorithm this supports."""

    segments: tuple[str, ...]
    """The pattern's body (the `$`, if any, already stripped), split on `*`. Always at least
    one element — a pattern with no `*` at all is a single segment, matched as a plain prefix
    (or, if `anchored`, an exact string)."""

    anchored: bool
    """Whether the original pattern ended in `$` — the final segment in `segments` must then
    land exactly at the end of the target, rather than merely being found somewhere in it."""


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> _CompiledPattern:
    """Split one `Allow`/`Disallow` pattern into `_matches`'s segment form — a trailing `$`
    anchors the match to the end rather than allowing it as an ordinary prefix match; a `$`
    anywhere else is a literal character, per the module docstring's "wildcards" section.

    Memoized: a pure function of `pattern` alone, so caching it adds no state this otherwise
    I/O-free module would not already have — it exists because the same handful of patterns
    from one `robots.txt` are tested against every candidate URL in a run's frontier.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return _CompiledPattern(segments=tuple(body.split("*")), anchored=anchored)


def _matches(compiled: _CompiledPattern, target: str) -> bool:
    """Does `target` match `compiled`? The greedy, non-backtracking glob algorithm the module
    docstring's "wildcards" section describes — no regex, and no case that takes longer than a
    linear scan of `target`.

    The first segment is matched with `str.startswith` at position 0 (prefix semantics); every
    later segment is located with a single `str.find` from wherever the previous one ended. An
    empty segment — from a leading, trailing, or doubled `*` — matches at the current position
    with no work to do, which is exactly right: `**` means the same thing as `*`. When
    `compiled.anchored`, the LAST segment is a special case instead of an ordinary `find`: it
    must end exactly at `len(target)`, and the position it would start at must not fall before
    the previous match, so the match cannot overlap itself.
    """
    segments = compiled.segments
    first = segments[0]
    if not target.startswith(first):
        return False
    if len(segments) == 1:
        # No `*` in the pattern at all — the first segment is also the only one.
        return not compiled.anchored or len(target) == len(first)

    pos = len(first)
    last = len(segments) - 1
    for index in range(1, len(segments)):
        segment = segments[index]
        if index == last and compiled.anchored:
            start = len(target) - len(segment)
            if start < pos or target[start:] != segment:
                return False
            pos = len(target)
        elif segment:
            found = target.find(segment, pos)
            if found == -1:
                return False
            pos = found + len(segment)
        # else: an empty middle/last-unanchored segment needs no work — see docstring.
    return True


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """One site's applicable `robots.txt` rules, already narrowed to the winning group(s) —
    see `parse_robots`. Frozen: a fact about what one `robots.txt` body said, not something a
    caller edits."""

    allow: tuple[str, ...]
    """`Allow` patterns from the winning group(s), in file order, already percent-normalized.
    Never contains an empty pattern — an empty `Allow` value is ignored at parse time (module
    docstring)."""

    disallow: tuple[str, ...]
    """`Disallow` patterns from the winning group(s), in file order, already
    percent-normalized. Never contains an empty pattern — an empty `Disallow` value
    contributes nothing, which is exactly what makes an empty `Disallow` mean "allow
    everything" rather than "disallow the empty string.\""""

    crawl_delay_s: float | None
    """The LAST valid `Crawl-delay` value the winning group(s) declared, in seconds — `None`
    when none did, or when every declared value was unparseable, negative, `nan`, or `inf`.
    See `effective_crawl_delay_ms` for how this combines with the operator's own configured
    delay."""

    def is_allowed(self, url: str) -> bool:
        """Is `url` allowed by these rules?

        Fails OPEN in every direction this module cannot resolve: no `disallow` patterns at
        all (the common case — most sites publish none) returns `True` without even parsing
        `url`, and a `url` this module cannot itself parse (`urlsplit` raising `ValueError`)
        also returns `True` rather than being treated as disallowed by default.

        Matches against the URL's PATH plus its QUERY STRING (`?` and everything after,
        joined with no separator changes) — never the fragment, which a server never
        receives — normalized through the same `_normalize_encoding` a rule's own pattern was
        normalized through at parse time. An empty path (`urlsplit("https://host").path ==
        ""`) is treated as `/`, matching what a server actually receives for a bare origin
        request.

        Longest matching pattern wins; `Allow` wins an exact-length tie. See the module
        docstring's "longest match" section for why length is measured on the PATTERN,
        wildcards and anchor included, not on what it matched.
        """
        if not self.disallow:
            return True
        try:
            parts = urlsplit(url)
        except ValueError:
            return True
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        target = _normalize_encoding(target)

        disallow_lengths = [
            len(pattern) for pattern in self.disallow if _matches(_compile(pattern), target)
        ]
        if not disallow_lengths:
            return True
        best_disallow = max(disallow_lengths)

        allow_lengths = [
            len(pattern) for pattern in self.allow if _matches(_compile(pattern), target)
        ]
        best_allow = max(allow_lengths) if allow_lengths else -1
        return best_allow >= best_disallow


ALLOW_ALL: Final[RobotsRules] = RobotsRules(allow=(), disallow=(), crawl_delay_s=None)
"""What every failure mode this module can produce degrades to — a missing `robots.txt`, one
that failed to fetch, one this module could not parse, and a `robots.txt` that genuinely
declares no group this crawler's product token or `*` matches. See the module docstring's
"fail open" paragraph."""


@dataclass(slots=True)
class _Group:
    """One `User-agent` block, accumulated while scanning — private to `parse_robots`. See the
    module docstring's "group accumulation" section for the two-state machine this supports."""

    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)
    """`(field, value)` pairs — `field` is `"allow"`, `"disallow"`, or `"crawl-delay"` — in the
    order they appeared in the file."""

    has_rule: bool = False
    """Set the moment a rule is appended. A `User-agent` line seen while this is `True` starts
    a NEW group rather than extending this one — see the module docstring."""


def _product_token(user_agent: str) -> str:
    """The product token this crawler answers to, derived from a full `User-Agent` header
    value: everything before the first `/`, then everything before the first run of
    whitespace, lowercased. `CRAWL_USER_AGENT` (`app.features.crawl.http_client`) —
    `"llms-text-bot/0.1 (+https://…)"` — yields `"llms-text-bot"`. `""` for an empty or
    all-whitespace `user_agent`, which matches no group's agent list (a bare `""` is never a
    line a real `robots.txt` would declare)."""
    head = user_agent.split("/", 1)[0].split()
    return head[0].lower() if head else ""


def _normalize_rule_value(value: str) -> str | None:
    """Normalize one `Allow`/`Disallow` value, or `None` if it contributes nothing: an empty
    value (module docstring — this is what makes an empty `Disallow` mean "allow
    everything"), or one starting with neither `/` nor `*` (not a path pattern this module
    can compare a URL's path against)."""
    if not value:
        return None
    if not (value.startswith("/") or value.startswith("*")):
        return None
    return _normalize_encoding(value)


def _parse_crawl_delay(value: str) -> float | None:
    """Parse a `Crawl-delay` value, or `None` for anything unusable: not a float, negative,
    `nan`, or `inf` — none of those name a real number of seconds to wait."""
    try:
        parsed = float(value)
    except ValueError:
        return None
    if math.isnan(parsed) or math.isinf(parsed) or parsed < 0:
        return None
    return parsed


def _parse_robots(text: str, *, user_agent: str) -> RobotsRules:
    """The body of `parse_robots`, allowed to raise — every caller goes through the public
    wrapper's `try/except` instead. Split out so the "never raises" contract lives in exactly
    one place rather than being re-asserted at every early return below.
    """
    token = _product_token(user_agent)

    groups: list[_Group] = []
    current: _Group | None = None
    for raw_line in text.splitlines()[:MAX_LINES]:
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_raw, _, value_raw = line.partition(":")
        field_name = field_raw.strip().lower()
        value = value_raw.strip()

        if field_name == "user-agent":
            if current is None or current.has_rule:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
        elif field_name in ("allow", "disallow", "crawl-delay"):
            if current is None:
                # A rule with no `User-agent` above it belongs to no group — a malformed
                # file, not a crash.
                continue
            current.rules.append((field_name, value))
            current.has_rule = True
        # Every other directive (`Sitemap:`, `Host:`, anything unrecognized) is read by
        # nothing here and deliberately does NOT close the current group — module docstring.

    winning = [group for group in groups if token and token in group.agents]
    if not winning:
        winning = [group for group in groups if "*" in group.agents]
    if not winning:
        return ALLOW_ALL

    allow: list[str] = []
    disallow: list[str] = []
    crawl_delay_s: float | None = None
    kept = 0
    for group in winning:
        for field_name, value in group.rules:
            if kept >= MAX_RULES:
                break
            if field_name == "disallow":
                normalized = _normalize_rule_value(value)
                if normalized is None:
                    continue
                disallow.append(normalized)
                kept += 1
            elif field_name == "allow":
                normalized = _normalize_rule_value(value)
                if normalized is None:
                    continue
                allow.append(normalized)
                kept += 1
            else:  # "crawl-delay"
                parsed = _parse_crawl_delay(value)
                if parsed is not None:
                    crawl_delay_s = parsed
        if kept >= MAX_RULES:
            break

    return RobotsRules(allow=tuple(allow), disallow=tuple(disallow), crawl_delay_s=crawl_delay_s)


def parse_robots(text: str, *, user_agent: str) -> RobotsRules:
    """Parse `text` — one `robots.txt` body — into the rules that apply to `user_agent`.

    **Never raises.** A `robots.txt` this module cannot make sense of is not a reason to fail
    a run; see the module docstring's "fail open" paragraph. Matches
    `internals/links.py`'s `extract_links` and `internals/extract.py`'s `extract_content`:
    the whole body runs inside one `try/except Exception`, which logs at WARNING and returns
    `ALLOW_ALL`.

    Args:
        text: The `robots.txt` body, exactly as fetched — not pre-validated, not
            pre-truncated (this module bounds its own scan via `MAX_LINES`).
        user_agent: The full `User-Agent` header value this crawler sends —
            `app.features.crawl.http_client.CRAWL_USER_AGENT` in production. The PRODUCT
            TOKEN this module matches group names against is derived from it
            (`_product_token`), never hardcoded a second time, so the two cannot drift.
            Keyword-only so a two-string call cannot silently transpose its arguments.

    Returns:
        A `RobotsRules` — see that class, and the module docstring's "group selection"
        section for exactly how the winning group(s) are chosen. `ALLOW_ALL` for a `text`
        this module cannot parse, or one declaring no group this crawler's token or `*`
        matches.
    """
    try:
        return _parse_robots(text, user_agent=user_agent)
    except Exception:
        logger.warning("robots: could not parse robots.txt; allowing everything", exc_info=True)
        return ALLOW_ALL


def effective_crawl_delay_ms(configured_ms: int, crawl_delay_s: float | None) -> int:
    """The politeness delay a run should actually use, combining the operator's own
    `crawl_politeness_delay_ms` with a site's `Crawl-delay`, if it published one.

    The larger of the two always wins — a site's own request is never used to go FASTER than
    the operator configured, only ever to go slower. The robots contribution is clamped to
    `MAX_ROBOTS_CRAWL_DELAY_MS` before the comparison, so an extreme published value (some
    sites genuinely say `Crawl-delay: 86400`) cannot make this run's politeness gate
    effectively unbounded — see the module docstring's "10-second ceiling" section for why
    only the robots side of the `max()` is ever clamped.

    Args:
        configured_ms: `Settings.crawl_politeness_delay_ms` — the operator's own floor.
        crawl_delay_s: `RobotsRules.crawl_delay_s` — `None` when the site published none.

    Returns:
        The delay, in milliseconds, `app.features.crawl.service.CrawlService.execute_run`
        widens the run's shared `PolitenessGate` to before `crawl_site` begins (module
        docstring's D3 section) — never applied to sitemap discovery's own remaining fetches.
    """
    robots_ms = (
        0 if crawl_delay_s is None else min(int(crawl_delay_s * 1000), MAX_ROBOTS_CRAWL_DELAY_MS)
    )
    return max(configured_ms, robots_ms)
