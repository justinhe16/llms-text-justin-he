"""Ranking and selecting a crawl's frontier from raw discovered URLs.

**What this module is for.** A sitemap, a `robots.txt` `Sitemap:` line, and (once PER-176
lands) links found on already-fetched pages all surface far more URLs than any run should
ever fetch — a documentation site's sitemap alone can list thousands. `select_urls` takes
whatever was discovered, in whatever order it was discovered in, and decides which subset
`internals/crawler.py`'s `crawl_site(seed_url, extra_urls=...)` should actually spend its
page budget on. It does not discover anything itself — that is PER-176's job — and it does
not fetch, parse HTML, or call an LLM, so it stays on the right side of CLAUDE.md #9 and
ARCHITECTURE.md §3.4: nothing here is "real crawling logic," only a decision over metadata
a discovery step will have already collected.

**Not wired into `crawler.py` by this ticket.** Exactly like `internals/llms_txt.py` was
built as a standalone, fully-tested seam before anything called it for real, this module is
built and proven here; the ticket that makes `extra_urls` come from `select_urls` over
`internals/sitemap.py`'s discovered URLs, instead of an empty tuple, is PER-176.

**Why `DiscoveredUrl` lives here and not in `schemas.py`.** `schemas.py`'s own docstring
gives the reason for `CrawledPage`: a shape that never crosses the HTTP boundary and exists
only to move data between two functions in the same worker process is an honest plain
dataclass, not a Pydantic DTO with validation and an OpenAPI presence neither needs.
`DiscoveredUrl` is the same kind of value — built by a discovery step, consumed by
`select_urls`, and never returned by a router or read by the frontend — so it is declared
next to its one consumer instead of in the shared schemas module.

**Pipeline order, and why it is fixed.**

1. **Normalize.** Every candidate is reduced to the exact string that would be fetched
   (fragment stripped, scheme/host lowercased, tracking parameters dropped, trailing slash
   normalized) before anything else happens, because every later stage — dropping the seed,
   detecting duplicates, deciding what is "off origin" — is only correct if it compares like
   with like. Comparing un-normalized strings would let `https://example.com/docs` and
   `https://Example.com/docs/` both survive as if they were different URLs.
2. **Pre-filter drop rules.** Cheap, stateless checks (origin, path shape, query shape) run
   before scoring so that scoring never has to reason about URLs that were never going to be
   selected — a `/tag/`, `/2019/03/`, or off-origin URL gains nothing by being scored high.
   Cross-candidate state is limited to what pre-filtering genuinely needs: a set of
   normalized URLs already seen, for `"duplicate"`, and — when a duplicate's normalized URL
   already survived pre-filtering — an order-independent merge of its `priority` (the max of
   the non-`None` values seen) and `lastmod` (the latest of the non-`None` values seen) into
   the surviving candidate, so a URL's score never depends on which of its several
   discoveries `select_urls` happened to visit first.
3. **Score.** Only survivors are scored, deterministically and without a clock — see the
   weight constants below for the reasoning behind each term's magnitude.
4. **Sort** by `(-score, url)`. The URL tie-break is what makes the whole pipeline
   deterministic under a shuffled input: two candidates that score identically would
   otherwise be ordered however `sorted()`'s stability happened to preserve them, which
   depends on the order `candidates` arrived in.
5. **Walk the ranked list and cap.** `"localized_duplicate"` is the one rule that cannot be
   a pre-filter, because it is only true relative to what has *already been selected* —
   `/es/docs/intro` is only a duplicate once `/docs/intro` has actually been chosen, which is
   a fact that does not exist until the walk reaches that point. Running it here, against a
   ranked (not raw) order, also means the *better*-ranked of two localized copies is the one
   kept; running it as a pre-filter over raw input order would depend on which one happened
   to appear first in `candidates`.

**Why `"off_origin"` exists at all — it is not on the ticket's own list of drop rules.**
PER-176's link extraction will surface links to other domains (a partner's docs, a CDN, a
social share target) exactly as readily as it surfaces links to the site being crawled. An
`llms.txt` that quietly includes another site's pages is `url_normalize.py`'s `www.`
problem's other, worse half: not two spellings of one site failing to dedupe, but one site's
artifact silently describing a different site — "a wrong artifact that looks right," in that
module's own words. Origin is compared exactly on `(scheme, host, port)`, with the port
collapsed to the scheme's default the same way `websites/internals/url_normalize.py`'s
`NormalizedUrl.origin` does — that module is the existing definition of "origin" in this
codebase, and this rule is deliberately the same equality it already uses for
`UNIQUE (user_id, origin)`. It is not imported (ARCHITECTURE.md §3.1 forbids importing
another feature's `internals/`); only the small piece needed — collapsing a default port —
is reimplemented here.

**Why a duplicate's metadata is merged rather than discarded.** PER-176's link extraction
will routinely rediscover a URL a sitemap already listed — the same page, now carrying no
`lastmod`/`priority` at all (`source="links"` never populates either field). Simply keeping
whichever `DiscoveredUrl` `select_urls` happened to visit first would make a page's score
depend on `candidates`' input order — silently, and in the one place this module promises
the opposite (`select_urls`'s own docstring, and the determinism tests in
`tests/test_url_ranking.py`). `_merge_priority` and `_merge_lastmod` are commutative and
associative — the larger of two priorities, and the later of two `lastmod` values, do not
care which argument arrived first — so folding every duplicate of a survivor into one merged
entry, encountered in any order, always produces the same merged candidate and therefore the
same score.

**The reconciliation invariant.** For any call, `discovered_count == len(selected) +
sum(dropped.values())`. Every candidate is either selected or dropped under exactly one
named rule — never both, and never neither. `tests/test_url_ranking.py` asserts this
directly, and it is also why every drop site in this module `continue`s immediately: nothing
below the first matching rule for a candidate ever runs.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit


_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}
"""The same two entries `websites/internals/url_normalize.py` declares, reimplemented here
rather than imported (ARCHITECTURE.md §3.1) — the one piece of that module's definition of
"origin" this ticket actually needs, for the `"off_origin"` comparison below."""

_TRACKING_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    {"ref", "fbclid", "gclid", "mc_cid", "mc_eid"}
)
_TRACKING_PARAM_PREFIX: Final = "utm_"
"""Every `utm_*` key (`utm_source`, `utm_campaign`, `utm_medium`, ...) plus the fixed set of
single-vendor click ids above. Stripped from the normalized URL — never from `dropped` — so
that `https://example.com/docs?utm_source=newsletter` and `https://example.com/docs` dedupe
to the same candidate instead of surviving as two."""

_DOC_SEGMENT_NAMES: Final[frozenset[str]] = frozenset(
    {"docs", "doc", "guide", "guides", "reference", "api", "getting-started"}
)
"""Path segments that mark a page as documentation-shaped for `DOC_SEGMENT_BOOST`. Matched
against any segment, not only the first, so `/product/docs/setup` benefits the same as
`/docs/setup` — a site that nests its docs under a product name should not be penalized for
where the `docs` segment happens to sit."""

_TAXONOMY_SEGMENT_NAMES: Final[frozenset[str]] = frozenset(
    {"tag", "tags", "category", "categories", "author", "authors"}
)

_CHANGELOG_SEGMENT_NAMES: Final[frozenset[str]] = frozenset(
    {"changelog", "change-log", "release-notes"}
)
"""Exactly three whole-segment spellings, matched exactly — not a substring or prefix check.
That narrowness is deliberate: `/docs/releases` (a reference page worth keeping) contains
neither `"changelog"` nor `"release-notes"` as a full segment, so it is untouched by this
rule. A looser rule (matching any segment containing `"release"`) would drop it, which is
exactly the false positive this rule exists to avoid."""

_FEED_SUFFIXES: Final[tuple[str, ...]] = (".rss", ".atom", ".xml")

_KNOWN_LANGUAGE_CODES: Final[frozenset[str]] = frozenset(
    {
        "en",
        "es",
        "fr",
        "de",
        "it",
        "pt",
        "ru",
        "zh",
        "ja",
        "ko",
        "ar",
        "nl",
        "pl",
        "tr",
        "sv",
        "da",
        "no",
        "nb",
        "nn",
        "fi",
        "cs",
        "sk",
        "hu",
        "el",
        "he",
        "th",
        "vi",
        "id",
        "uk",
        "ro",
        "bg",
        "hr",
        "sl",
        "et",
        "lv",
        "lt",
        "fa",
        "hi",
    }
)
"""The known set `"localized_duplicate"` checks a leading path segment against — deliberately
a fixed vocabulary, never "any two-letter segment." A documentation site routinely has
legitimate top-level sections that happen to be two letters — `/ui/`, `/os/`, `/js/` — and
none of those are in this set, so none of them are ever mistaken for a locale prefix. `"id"`
is a genuine edge case (it is both the ISO 639-1 code for Indonesian and a plausible generic
path segment elsewhere); it stays in this set because the false negative — failing to fold a
real `/id/docs/...` Indonesian docs tree — is the more common shape on a documentation site,
and the pattern below still requires an exact, whole-segment match, not a substring. `"no"`
(Norwegian) is the same tradeoff for the same reason — a documentation site is far more
likely to have a `/no/` Norwegian docs tree than a path segment that abbreviates "no" on its
own (`no-code` and `no-op` are never a bare `no` segment, since the pattern below requires
the whole segment to match, not a prefix)."""

_LOCALE_SEGMENT_PATTERN: Final = re.compile(r"^([a-z]{2,3})(-[a-z]{2,4})?$", re.IGNORECASE)
"""A locale segment: a 2-3 letter language code, optionally followed by a hyphenated region
or script subtag (`pt-br`, `zh-CN`, `zh-hans`). The language part is what is checked against
`_KNOWN_LANGUAGE_CODES` — the region suffix is accepted in whatever case it appears in and is
not itself validated, since `"localized_duplicate"` only needs to recognize that the segment
IS a locale, not which one."""

SITEMAP_DEFAULT_PRIORITY: Final = 0.5
"""The default `<priority>` the sitemap protocol assigns a URL that declares none. Scoring
centers on this value — see `PRIORITY_WEIGHT` — specifically so that a site operator who
wrote `<priority>0.5</priority>` on purpose and a candidate that omitted the tag entirely
score identically. Treating an explicit 0.5 as anything other than neutral would read a
signal into a value the sitemap spec itself defines as "no particular priority.\""""

DEPTH_PENALTY: Final = 1.0
"""Cost per path segment. A shallow URL is more likely to be a canonical entry point
(`/docs`, `/pricing`) than a deep one (`/docs/v1/api/internal/foo/bar`), which is more likely
to be a narrow implementation detail. Set to `1.0` — one "unit" of score per segment — so
every other constant in this module is easiest to reason about relative to it: "worth N
levels of extra depth" is `N * DEPTH_PENALTY`, stated plainly in each constant below."""

DOC_SEGMENT_BOOST: Final = 4.0
"""Worth four levels of extra depth (`4 * DEPTH_PENALTY`). Large enough that
`/product/docs/guides/deploy` (three segments deep, doc-shaped) still outranks
`/blog/2025/some-post` (also three segments deep, not doc-shaped) by a wide margin — the
whole point of ranking a documentation-oriented frontier at all is that doc-shaped paths
should win most head-to-head comparisons against noise at a similar depth, not just narrowly
edge them out."""

ROOT_BOOST: Final = 6.0
"""Worth six levels of extra depth, and deliberately larger than `DOC_SEGMENT_BOOST`. The
site root is the one page every crawl already knows is significant without needing a
`/docs`-shaped path to say so — it is usually the seed itself (and therefore dropped under
`"seed"`), but a candidate set can legitimately rediscover it as a separate `DiscoveredUrl`
(a sitemap entry, a `rel=home` link), and when that happens it should outrank even the
best-shaped documentation page."""

PRIORITY_WEIGHT: Final = 2.0
"""The site operator's own `<priority>` signal, centered on `SITEMAP_DEFAULT_PRIORITY` so its
full swing is `±1.0` (`priority` ranges 0.0-1.0, so `priority - 0.5` ranges `-0.5..0.5`,
times this weight). That swing is deliberately comparable to `DEPTH_PENALTY` — roughly one
level of depth — because a sitemap's priority is a real, deliberately-authored signal and
deserves to move the ranking by about as much as structural depth does, not merely nudge it."""

LASTMOD_WEIGHT: Final = 0.4
"""`lastmod` recency, and deliberately the smallest weight in this module: its full swing is
only `±0.2` (see `_recency_term`), a fifth of one `DEPTH_PENALTY` level. The ticket is
explicit that a stable reference page must not rank below one that happens to have been
touched yesterday — recency is a tiebreaker among otherwise-similar candidates, never
something that should let a deeply-nested, freshly-edited page outrank a shallow, stable
one. `lastmod` also has no clock-based meaning here: see `_recency_term`, which ranks each
candidate's `lastmod` only against the min and max already present in this same candidate
set, and never reads the current time."""


DiscoveredUrlSource = Literal["sitemap", "robots", "links"]


@dataclass(frozen=True, slots=True)
class DiscoveredUrl:
    """One URL as a discovery step (sitemap parsing, a `robots.txt` `Sitemap:` line, or —
    once PER-176 lands — link extraction) found it, before `select_urls` has done anything
    to it. Frozen because a discovery is a fact about what was found, not something a caller
    should be able to edit in place.

    Never leaves the worker process and crosses no HTTP boundary — see the module docstring
    for why that makes a plain dataclass the right shape, the same reasoning
    `app.features.crawl.schemas`'s module docstring gives for `CrawledPage`.
    """

    url: str
    """Exactly as the discovery step produced it — not yet normalized. `select_urls`
    normalizes every candidate itself; a caller passing an already-normalized URL is
    harmless (normalizing twice is idempotent) but not required."""

    lastmod: datetime | None
    """The sitemap `<lastmod>` value, if the discovery step is a sitemap and the URL
    declared one. `None` when absent, and always `None` for `source="robots"` /
    `source="links"`, which have no equivalent field to read it from."""

    priority: float | None
    """The sitemap `<priority>` value, `0.0`-`1.0`, if declared. `None` when absent — see
    `SITEMAP_DEFAULT_PRIORITY` for why `None` and an explicit `0.5` are scored identically."""

    source: DiscoveredUrlSource
    """Which discovery step produced this candidate. Carried for callers that want to log or
    debug where a URL came from; `select_urls` does not branch on it — a `/docs/intro` found
    in a sitemap and one found in `robots.txt` or on a page are ranked by the same rules."""


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """What `select_urls` returns: the chosen frontier, and a full accounting of everything
    that did not make it in.
    """

    selected: list[str]
    """The normalized URLs chosen, in ranked order (highest score first) — the same order
    `internals/crawler.py`'s frontier will eventually be handed in. Never longer than
    `limit`, and never contains the (normalized) seed URL."""

    discovered_count: int
    """`len(candidates)` as given to `select_urls` — every candidate, including the ones
    later found to be unparseable or duplicates. The left side of the reconciliation
    invariant in the module docstring."""

    dropped: dict[str, int]
    """How many candidates each named rule removed. Contains only rules that actually fired
    — a rule that dropped nothing has no key, rather than a key with value `0` — and its
    keys appear in the fixed order the module docstring's pipeline describes (pre-filter
    rules in the order they are checked, then the two during-selection rules), regardless of
    what order candidates happened to trip them in."""


@dataclass(frozen=True, slots=True)
class _NormalizedCandidate:
    """The parsed, normalized form of one candidate URL — the shape every later pipeline
    stage actually operates on. Not part of this module's public contract; `DiscoveredUrl`
    and `str` are.
    """

    url: str
    """The normalized string: fragment stripped, scheme and host lowercased, tracking
    parameters dropped, trailing slash normalized. What would actually be fetched, and what
    every drop rule and the final `selected` list compares and reports."""

    scheme: str
    host: str
    port: int | None
    """`None` when the URL had no explicit port — never defaulted here. Only `_origin`
    (the `"off_origin"` comparison) collapses this to the scheme's default; `url` keeps
    whatever the candidate actually specified, because nothing in the normalize step's own
    rule list (module docstring, step 1) asks for a port to be rewritten."""

    path: str
    """Always starts with `/`. Trailing-slash-normalized: never ends with `/` unless it
    IS `/` (the root path)."""

    query: str
    """The surviving query string, tracking parameters removed and the rest sorted by
    `(key, value)` — without a leading `?`. Empty string when there is no query left."""


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAM_NAMES or lowered.startswith(_TRACKING_PARAM_PREFIX)


def _normalize_path(raw_path: str) -> str:
    if not raw_path:
        return "/"
    if raw_path != "/" and raw_path.endswith("/"):
        return raw_path.rstrip("/") or "/"
    return raw_path


def _normalize_query(raw_query: str) -> str:
    if not raw_query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    kept.sort()
    return urlencode(kept)


def _authority(host: str, port: int | None) -> str:
    """Rebuild a URL's authority component from an already-parsed host and optional port.

    `urlsplit().hostname` strips the brackets from an IPv6 literal (`[2001:db8::1]` becomes
    `2001:db8::1`), but the authority component of a URL requires them back — without this,
    `f"{host}:{port}"` on an IPv6 host produces a string with three-or-more colons that does
    not round-trip through `urlsplit` at all (it reparses with the wrong hostname and raises
    on `.port`). `websites/internals/url_normalize.py` already carries this exact fix, next
    to an identical comment; it is reimplemented here — this one small piece of it, not
    imported — for the same reason `_DEFAULT_PORTS`'s docstring gives. The only caller-visible
    difference from that module: brackets are added whenever `host` contains a colon, even
    with no `port` at all, since an unbracketed IPv6 literal on its own is exactly as
    unparseable as one with a port glued on.
    """
    bracketed = f"[{host}]" if ":" in host else host
    return bracketed if port is None else f"{bracketed}:{port}"


def _parse_candidate(url: str) -> _NormalizedCandidate | None:
    """Normalize `url` per the module docstring's step 1, or return `None`.

    The one function in this module that touches raw, untrusted input, so it is also the
    one function that must not raise — see `"unparseable"` in the module docstring. Every
    way a malformed URL can fail (an unparseable string, a missing host, an unsupported
    scheme, a non-numeric or out-of-range port) is funneled through the single `try/except`
    below rather than checked one at a time, because the caller only needs one answer:
    parseable or not.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        host = parts.hostname
        if not host:
            return None
        port = parts.port  # Raises ValueError for a non-numeric or out-of-range port.
    except ValueError:
        return None

    authority = _authority(host, port)
    path = _normalize_path(parts.path)
    query = _normalize_query(parts.query)
    normalized_url = f"{parts.scheme}://{authority}{path}"
    if query:
        normalized_url = f"{normalized_url}?{query}"

    return _NormalizedCandidate(
        url=normalized_url, scheme=parts.scheme, host=host, port=port, path=path, query=query
    )


def _origin(candidate: _NormalizedCandidate) -> tuple[str, str, int]:
    """`(scheme, host, port)` with the port collapsed to the scheme's default — the same
    equality `websites/internals/url_normalize.py`'s `NormalizedUrl.origin` already uses.
    See the module docstring for why `"off_origin"` matches this definition specifically.
    """
    port = candidate.port if candidate.port is not None else _DEFAULT_PORTS[candidate.scheme]
    return (candidate.scheme, candidate.host, port)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _is_dated_archive(path: str) -> bool:
    """A 4-digit segment in 1990-2099, anywhere in the path — `/2024/03/` and
    `/blog/2023/` both match on their year segment alone. A trailing month segment (`03` in
    the first example) is not required for a match; it is what the ticket calls "optionally
    followed by a month segment," not an additional condition to satisfy.
    """
    for segment in _segments(path):
        if len(segment) == 4 and segment.isdigit() and 1990 <= int(segment) <= 2099:
            return True
    return False


def _is_taxonomy(path: str) -> bool:
    return any(segment.lower() in _TAXONOMY_SEGMENT_NAMES for segment in _segments(path))


def _is_pagination(path: str, query: str) -> bool:
    """`?page=2` (a `page` query key, any value) or `/page/3/` (a `page` segment immediately
    followed by a numeric one). `query` is already the tracking-stripped, sorted string
    `_normalize_query` produced — re-parsed here rather than threading the parsed pairs
    through, since this is the only rule that needs them.
    """
    if query:
        for key, _value in parse_qsl(query, keep_blank_values=True):
            if key.lower() == "page":
                return True
    segments = _segments(path)
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() == "page" and segments[index + 1].isdigit():
            return True
    return False


def _is_feed(path: str) -> bool:
    segments = _segments(path)
    if not segments:
        return False
    last = segments[-1].lower()
    return last == "feed" or last.endswith(_FEED_SUFFIXES)


def _is_changelog(path: str) -> bool:
    return any(segment.lower() in _CHANGELOG_SEGMENT_NAMES for segment in _segments(path))


def _leading_locale_free_url(candidate: _NormalizedCandidate) -> str | None:
    """If `candidate`'s first path segment is a known locale (`_KNOWN_LANGUAGE_CODES`),
    return the normalized URL with that one segment removed — the URL `"localized_duplicate"`
    checks against what has already been selected. Returns `None` when the first segment is
    not a recognized locale, which is the common case and leaves the candidate untouched by
    this rule.
    """
    segments = _segments(candidate.path)
    if not segments:
        return None
    match = _LOCALE_SEGMENT_PATTERN.match(segments[0])
    if match is None or match.group(1).lower() not in _KNOWN_LANGUAGE_CODES:
        return None

    remaining = segments[1:]
    stripped_path = "/" + "/".join(remaining) if remaining else "/"
    authority = _authority(candidate.host, candidate.port)
    url = f"{candidate.scheme}://{authority}{stripped_path}"
    if candidate.query:
        url = f"{url}?{candidate.query}"
    return url


def _to_utc(value: datetime) -> datetime:
    """Normalize `value` to a timezone-aware UTC `datetime`.

    Comparing a naive and an aware `datetime` raises `TypeError` — the landmine the ticket
    calls out by name. A stored `lastmod` may legitimately be either, depending on whether
    the sitemap that declared it included a UTC offset, so every value is normalized before
    it is ever compared against another. A naive value is assumed to already be UTC rather
    than rejected: it is what `datetime.fromisoformat` produces for a `lastmod` string with
    no offset, and treating it as "unknown timezone, drop it" would silently throw away a
    real signal for the sitemaps that omit one.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _merge_priority(existing: float | None, incoming: float | None) -> float | None:
    """The larger of two declared `priority` values for the same normalized URL — `None`
    only when both are. `None` is handled explicitly rather than folded into `max()` because
    a genuinely declared `0.0` must beat a genuinely absent value, and `max(0.0, None)`
    itself raises. Commutative and associative, which is what makes folding three or more
    duplicates of the same URL, in any order, produce the same result — see the module
    docstring's "Why a duplicate's metadata is merged" section and `select_urls`'s
    duplicate-handling branch, the only caller.
    """
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    return max(existing, incoming)


def _merge_lastmod(existing: datetime | None, incoming: datetime | None) -> datetime | None:
    """The later of two declared `lastmod` values for the same normalized URL — `None` only
    when both are. Compared through `_to_utc` so a naive/aware mix can never raise, the same
    landmine `_to_utc` itself documents — but the value RETURNED is whichever original
    `datetime` won, not a UTC-normalized copy, because `_score` already calls `_to_utc` again
    wherever it reads a `lastmod`; this function does not need to be the only place that
    normalizes one.

    Commutative and associative **up to the UTC instant**, which is the only thing any
    caller reads. Two values naming the same instant with different `tzinfo` are ordered by
    argument position rather than by value, so `merge(naive, aware)` and `merge(aware, naive)`
    can return different objects — equal instants, distinct representations. Every consumer
    re-normalizes through `_to_utc`, so the difference is unobservable in a score.
    """
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    return existing if _to_utc(existing) >= _to_utc(incoming) else incoming


def _recency_term(
    lastmod: datetime | None, earliest: datetime | None, latest: datetime | None
) -> float:
    """The `LASTMOD_WEIGHT`-scaled contribution of `lastmod` to a candidate's score, ranged
    `-0.5..0.5` before the weight is applied (so `±LASTMOD_WEIGHT / 2` after it) —
    `0.5` is the neutral midpoint a candidate with no `lastmod` receives, exactly mirroring
    how `SITEMAP_DEFAULT_PRIORITY` centers `priority`.

    `earliest`/`latest` are the min and max `lastmod` already present across the whole
    surviving candidate set (computed once by the caller, not per candidate) — recency is
    relative to *this crawl's own discovered URLs*, never to the current time, which is the
    acceptance criterion this function exists to satisfy: nothing here reads the system
    clock, in any form. When every survivor shares the same `lastmod` (or only one distinct
    value exists), `earliest == latest` and every candidate scores the neutral midpoint,
    since there is no spread to rank within.
    """
    if lastmod is None or earliest is None or latest is None or earliest >= latest:
        return 0.0
    fraction = (_to_utc(lastmod) - earliest) / (latest - earliest)
    return fraction - 0.5


def _score(
    candidate: _NormalizedCandidate,
    *,
    priority: float | None,
    lastmod: datetime | None,
    earliest_lastmod: datetime | None,
    latest_lastmod: datetime | None,
) -> float:
    segments = _segments(candidate.path)
    depth = len(segments)
    is_root = depth == 0
    has_doc_segment = any(segment.lower() in _DOC_SEGMENT_NAMES for segment in segments)
    priority_term = 0.0 if priority is None else priority - SITEMAP_DEFAULT_PRIORITY

    return (
        -DEPTH_PENALTY * depth
        + (DOC_SEGMENT_BOOST if has_doc_segment else 0.0)
        + (ROOT_BOOST if is_root else 0.0)
        + PRIORITY_WEIGHT * priority_term
        + LASTMOD_WEIGHT * _recency_term(lastmod, earliest_lastmod, latest_lastmod)
    )


# Pre-filter rules in the order they are checked (module docstring, step 2), followed by the
# two during-selection rules (step 5) — the fixed order `SelectionResult.dropped`'s keys
# appear in, regardless of what order candidates happened to trip them in.
_RULE_ORDER: Final[tuple[str, ...]] = (
    "unparseable",
    "duplicate",
    "seed",
    "off_origin",
    "dated_archive",
    "taxonomy",
    "pagination",
    "feed",
    "changelog",
    "localized_duplicate",
    "over_limit",
)


def select_urls(
    candidates: Sequence[DiscoveredUrl], *, seed_url: str, limit: int
) -> SelectionResult:
    """Rank `candidates` and select at most `limit` of them to crawl.

    Pure and deterministic: no `async`, no network or filesystem access, and no clock read.
    Calling this twice with the same arguments, or once with `candidates` shuffled into any
    order, produces the same `selected` list — including when the same URL is discovered
    more than once with different `priority`/`lastmod` values, which is exactly what
    `"duplicate"`'s order-independent metadata merge (module docstring) guarantees.
    `tests/test_url_ranking.py` asserts both directly.

    Args:
        candidates: Every URL a discovery step found, in whatever order it found them.
        seed_url: The run's own seed — normalized the same way every candidate is, and
            compared against it for the `"seed"` and `"off_origin"` rules. Not itself
            included in `selected` even if a candidate happens to be a byte-for-byte repeat
            of it, because the seed is fetched separately (`crawl_site`'s own first fetch)
            and already counts against the run's page cap. If `seed_url` itself cannot be
            parsed — a defensive case, since `crawl_site` has already fetched it
            successfully by the time this runs, not an expected one — every candidate fails
            the `"off_origin"` comparison against an origin that cannot be determined, and
            `selected` comes back empty. Failing CLOSED here is deliberate: failing open
            (treating an unparseable seed as "no origin restriction") would silently defeat
            the one rule `"off_origin"` exists for.
        limit: The maximum number of URLs to select. `limit <= 0` returns an empty
            `selected` without raising — every surviving candidate is counted under
            `"over_limit"` instead.

    Returns:
        A `SelectionResult`. See its own docstring, and the module docstring's
        reconciliation invariant.
    """
    discovered_count = len(candidates)
    counts: dict[str, int] = {}

    def _drop(rule: str) -> None:
        counts[rule] = counts.get(rule, 0) + 1

    seed_candidate = _parse_candidate(seed_url)
    seed_normalized_url = seed_candidate.url if seed_candidate is not None else None
    seed_origin = _origin(seed_candidate) if seed_candidate is not None else None

    seen: set[str] = set()
    # Keyed by normalized URL rather than a plain list, so a later duplicate of an already-
    # surviving candidate can be folded into it (see the module docstring's "Why a
    # duplicate's metadata is merged" section) instead of merely being counted and discarded.
    survivors: dict[str, tuple[_NormalizedCandidate, float | None, datetime | None]] = {}

    for candidate in candidates:
        normalized = _parse_candidate(candidate.url)
        if normalized is None:
            _drop("unparseable")
            continue
        if normalized.url in seen:
            _drop("duplicate")
            # `existing` is `None` when the first occurrence of this URL was itself dropped
            # by a rule below (e.g. "dated_archive") rather than surviving — there is no
            # entry to merge into, and this duplicate's metadata is discarded exactly as
            # before. When `existing` IS present, folding this candidate's `priority`/
            # `lastmod` into it — via `_merge_priority`/`_merge_lastmod`, both commutative
            # and associative — is what keeps the survivor's score independent of which
            # duplicate `select_urls` happened to visit first.
            existing = survivors.get(normalized.url)
            if existing is not None:
                existing_normalized, existing_priority, existing_lastmod = existing
                survivors[normalized.url] = (
                    existing_normalized,
                    _merge_priority(existing_priority, candidate.priority),
                    _merge_lastmod(existing_lastmod, candidate.lastmod),
                )
            continue
        seen.add(normalized.url)

        if normalized.url == seed_normalized_url:
            _drop("seed")
            continue
        if _origin(normalized) != seed_origin:
            _drop("off_origin")
            continue
        if _is_dated_archive(normalized.path):
            _drop("dated_archive")
            continue
        if _is_taxonomy(normalized.path):
            _drop("taxonomy")
            continue
        if _is_pagination(normalized.path, normalized.query):
            _drop("pagination")
            continue
        if _is_feed(normalized.path):
            _drop("feed")
            continue
        if _is_changelog(normalized.path):
            _drop("changelog")
            continue

        survivors[normalized.url] = (normalized, candidate.priority, candidate.lastmod)

    lastmods_utc = [
        _to_utc(lastmod) for _n, _p, lastmod in survivors.values() if lastmod is not None
    ]
    earliest_lastmod = min(lastmods_utc) if lastmods_utc else None
    latest_lastmod = max(lastmods_utc) if lastmods_utc else None

    scored = [
        (
            normalized,
            _score(
                normalized,
                priority=priority,
                lastmod=lastmod,
                earliest_lastmod=earliest_lastmod,
                latest_lastmod=latest_lastmod,
            ),
        )
        for normalized, priority, lastmod in survivors.values()
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0].url))

    effective_limit = max(limit, 0)
    selected: list[str] = []
    selected_urls: set[str] = set()

    for normalized, _score_value in scored:
        delocalized_url = _leading_locale_free_url(normalized)
        if delocalized_url is not None and delocalized_url in selected_urls:
            _drop("localized_duplicate")
            continue
        if len(selected) >= effective_limit:
            _drop("over_limit")
            continue
        selected.append(normalized.url)
        selected_urls.add(normalized.url)

    dropped = {rule: counts[rule] for rule in _RULE_ORDER if counts.get(rule, 0) > 0}
    return SelectionResult(selected=selected, discovered_count=discovered_count, dropped=dropped)
