"""Sitemap discovery: filling `crawl_site`'s frontier before its own loop ever runs.

**What this module is, and what it deliberately is not.** `discover_sitemap_urls` walks the
sitemap protocol's own discovery order for one website's origin — `/sitemap.xml`, then
`/sitemap_index.xml`, then whatever `robots.txt` itself declares — parses whichever XML
document answers first with usable content, and returns the `<url><loc>` entries it found as
`DiscoveredUrl`s (`internals/url_ranking.py`), ready for that module's `select_urls` to rank
and `crawl_site`'s `extra_urls` to fetch. It is not link extraction: nothing here reads a
fetched page's HTML — every URL this module returns came out of a sitemap or a `robots.txt`
`Sitemap:` line, never out of an `<a href>`. That is `internals/links.py`'s job (PER-178),
and it is a strictly SECONDARY one: `service.py` runs it only when this module comes back
with nothing, so a site that ships a sitemap never has its markup read for links at all.

**`robots.txt` is fetched exactly once per run, unconditionally, before either well-known
probe — a PER-191 change from earlier revisions of this module, which fetched it only when
both probes had already come back empty, and read nothing out of it but `Sitemap:` lines.**
`_fetch_robots` is that ONE fetch, and it hands the SAME response to both consumers:
`internals/robots.py`'s `parse_robots`, which this module never re-implements, and
`_robots_sitemap_urls`, which still reads only `Sitemap:` lines. A hostile or slow
`robots.txt` therefore still costs this run one fetch, never two. The parsed `RobotsRules`
travel out on `DiscoveryResult.robots`, unconditionally, on every return path this function
has, including its failure ones — see "document counting," below, for the one deliberate
consequence of fetching `robots.txt` FIRST rather than last. What this module still does not
do is ENFORCE those rules: `internals/url_ranking.py`'s `select_urls` is what drops a
`Disallow`-covered candidate, and `internals/crawler.py`'s `crawl_site` is what refuses a
disallowed seed. This module only reads `robots.txt` and reports what it said.

**Every request this module makes — both well-known probes, `robots.txt` itself, every
`robots.txt`-declared target, and every child of a sitemap index — goes through the run's
`PolitenessGate` (`internals/fetcher.py`) when the caller supplies one, at the SAME delay
`crawl_site`'s own frontier fetches wait on — never a site's own `Crawl-delay`, which
`internals/robots.py`'s module docstring explains is deliberately deferred to the CRAWL
phase. An earlier revision of this paragraph claimed `crawler.py`'s politeness gate "already
owns rate-limiting on the requests this module does not make" — that was true only because
this module made no gated requests of its own at the time. It does now, and that sentence is
corrected here rather than left to mislead a future reader.** It caches nothing across runs —
every run starts its discovery cold, the same way `crawl_site` starts its frontier cold. And
it does not special-case a sitemap's `<image:image>`, `<video:video>`, or `<news:news>`
extensions, or any element besides `<loc>`, `<lastmod>`, and `<priority>` under a `<url>` —
`<changefreq>` included, despite being named in the sitemap spec right next to the two this
module does read.

**Every fetch goes through `internals/fetcher.py`'s `fetch_page`, so SSRF is not re-derived
here.** `fetch_page` calls `internals/ssrf.py`'s `validate_url` on every hop before a socket
opens — the well-known probes, every `robots.txt`-declared target, and every child of a
sitemap index all cross that same gate, with no second implementation of "is this address
safe to connect to" anywhere in this module. What this module adds ON TOP of that guard is
narrower and specific to discovery: refusing to even ATTEMPT a fetch of an off-origin target
in the first place (see "same-origin, enforced twice" below) — SSRF protects this process
from an internal target; it does nothing to stop this process being pointed at an arbitrary
PUBLIC third party, which same-origin filtering closes for free.

**The byte share: 50 MiB / 12.5 MiB / 37.5 MiB.** Sitemap discovery runs and finishes BEFORE
`crawl_site`'s own seed fetch (`service.py`), sharing one `ByteBudget` for the whole run — see
`crawl_site`'s `budget:` parameter docstring for the other half of this. If discovery alone
could exhaust that shared budget, the seed fetch that follows would raise
`ByteBudgetExceededError`, `crawler.py` would record `seed_error`, and a run with a huge
sitemap and a perfectly reachable seed would FAIL — the exact outcome "hitting a cap is a
success, not a failure" (ARCHITECTURE.md §3.4) forbids, and precisely the scenario a 40 MB
`sitemap.xml` is meant to test. `_DiscoveryBudget` is the fix: discovery gets a second,
TIGHTER ceiling, `SITEMAP_BYTE_SHARE` (`0.25`) of `settings.crawl_max_bytes`, layered on top
of the run's own budget rather than instead of it. At the default `crawl_max_bytes`
(52,428,800 bytes, 50 MiB), that ceiling is 13,107,200 bytes (12.5 MiB) — so the ticket's own
40 MB sitemap trips the discovery ceiling mid-stream, discovery returns whatever it already
parsed (see "partial harvest" below), and the page crawl that follows still has the full
37.5 MiB the discovery share left untouched. `SITEMAP_BYTE_SHARE` is a module constant, not a
third `Settings` field — it is a property of this algorithm's own safety margin, not
something a deployment should be tuning.

**`stats["bytes_fetched"]` includes sitemap bytes, and that is correct, not an oversight.**
Because discovery spends from the SAME `ByteBudget` a run's page fetches spend from,
`budget.used` — which becomes `CrawlResult.stats["bytes_fetched"]` — counts sitemap bytes
too. The alternative (excluding them from the counter while still charging them against the
cap) would let a run report `cap_hit == "bytes"` with `bytes_fetched` well under
`crawl_max_bytes`, an unexplainable row: `bytes_fetched` IS `budget.used`, and `budget` is
the one object that decides `cap_hit == "bytes"`, so the two must never disagree. The one
real consequence: `bytes_fetched / pages_crawled` is no longer a page's average size once a
run did any discovery at all.

That invariant holds in BOTH directions, which is what makes `_DiscoveryBudget`'s charge
order (below) safe rather than merely convenient. A chunk refused by discovery's own share is
never charged to the run, so `bytes_fetched` can under-count what physically arrived — by at
most one chunk, on the one fetch that trips the share. That is the harmless direction: it
under-counts bytes the run never got to keep (`fetch_page` discards the whole partial body on
a `ByteBudgetExceededError`), and it can never produce the unexplainable row above, because
nothing is charged to the run's counter that `cap_hit == "bytes"` did not also see.

**XML hardening: stdlib `ElementTree` plus a DOCTYPE refusal, not `defusedxml` or `lxml`.**
Measured in this repository's own pinned interpreter (CPython 3.12.13, expat 2.8.1): a
3-level internal-entity bomb parses cleanly with `xml.etree.ElementTree.fromstring` and
expands to a 10,000-character text node — stdlib `ET` performs no entity-expansion limiting
of its own. An external entity (`<!ENTITY xxe SYSTEM "file:///etc/passwd">`) IS already
refused, with `ParseError: undefined entity` — external entities are safe on this
interpreter without help. This venv's expat (2.8.1) DOES carry billion-laughs amplification
protection and refused a 9-level bomb with `ParseError: limit on input amplification factor
... breached` — and this module does NOT rely on that, for two reasons stated here rather
than assumed: first, it is a property of whichever expat the RUNNING CPython happens to be
linked against, not of anything this repository pins, and CI's container and a laptop can
differ; second, even where it is present, its default activation threshold is 8 MiB with a
100x amplification factor, so a 12.5 MiB discovery document (this module's own byte-share
ceiling, above) could still legally expand to roughly 1.25 GB before that guard fires — an
OOM on a small Fly machine, not a caught `ParseError`. So every document this module parses
is checked with `_DOCTYPE_PATTERN` — `re.compile(r"<!DOCTYPE", re.IGNORECASE)` — and refused
before `ET.fromstring` ever sees it if that pattern matches anywhere in the text. Two bonuses
fall out of that one rule, both deliberate: an HTML error page served with `200` at
`/sitemap.xml` — the single most common non-sitemap response this module will actually meet
in the wild — begins `<!DOCTYPE html>`, so the rule refuses it, `_collect` returns `[]`, and
discovery falls through to the next entry point exactly as it should
(`test_an_html_error_page_served_at_the_sitemap_path_does_not_stop_discovery`); and a raw
`<!DOCTYPE` sequence cannot legally appear inside XML element CONTENT (a literal `<` there
must be escaped), so a match is always either a real doctype declaration, a comment
containing one, or a malformed document — every one of which this module wants to refuse
regardless. Rejected `defusedxml`: `requirements.txt`'s own convention is that every pin
carries a paragraph justifying itself, and a new runtime dependency in a two-process image is
a heavy way to buy roughly four lines of policy this module can state and test on its own.
Rejected direct `lxml`: it needs an encode-plus-explicit-parser-encoding dance to accept a
document carrying its own `<?xml encoding=...?>` declaration (`lxml.etree.fromstring` raises
`ValueError` on a `str` that has one), it would promote `lxml` from "the parser
`internals/extract.py`'s trafilatura dependency happens to use internally" to a first-order
import of this feature (requiring a rewrite of that dependency's own justifying comment), and
it ships no inline type stubs — under this project's `warn_return_any = true`, every value
this module derived from it would come back typed `Any`.

**Namespace-agnostic tag matching is required, not defensive over-engineering.** `ET` reports
a namespaced document's tags in Clark notation —
`{http://www.sitemaps.org/schemas/sitemap/0.9}urlset` — but plenty of real sitemaps omit the
namespace declaration entirely (`tests/fixtures/sitemap_child_docs.xml` is one, checked in
specifically to prove `_local` handles it), so every tag comparison in this module goes
through `_local(tag)` (`tag.rpartition("}")[2]`) rather than a raw string compare.

**Why the return type is `DiscoveryResult`, not the ticket's own `list[DiscoveredUrl]`.** A
bare list cannot carry a fourth value this module needs to report — WHICH of the ticket's four
discovery paths actually produced it — and that value cannot be recovered afterwards from
`DiscoveredUrl.source` either: `url_ranking.DiscoveredUrlSource` has no `"sitemap_index"`
member, and that module's own docstring for `source="robots"` already promises
`lastmod`/`priority` are "always `None`" for it — false the moment a sitemap document is
reached VIA `robots.txt`, which routinely carries both. A wrapper result type is therefore
not a convenience, it is the only way to report the fourth value honestly without either
adding a member to `url_ranking`'s vocabulary for a distinction that module does not
otherwise care about, or breaking a promise that module's docstring already makes.

**`discovery_source` names the ENTRY POINT that was tried, not the document's own root
element — and that is the only reading that is well-defined.** Under a "document kind"
reading, a `robots.txt` that points at a `<sitemapindex>` has no single correct answer — is
that `"robots"`, because that is how it was reached, or `"sitemap_index"`, because of what the
document actually was? Under the entry-point reading there is no such conflict: the four
values (`"sitemap"`, `"sitemap_index"`, `"robots"`, `"none"`) are mutually exclusive and
collectively exhaustive, and they map 1:1 onto the ticket's own numbered discovery order.
`tests/test_crawl_sitemap.py` pins the ambiguity down with a test rather than leaving it to
this paragraph: the SAME sitemap-index body, served once at `/sitemap.xml` and once at
`/sitemap_index.xml`, reports `"sitemap"` in the first case and `"sitemap_index"` in the
second — same document kind, different entry point, different `source`.

**Every `DiscoveredUrl` this module emits carries `source="sitemap"` — always, regardless of
which of the four entry points produced it.** `DiscoveredUrl.source` is a per-URL field
(`url_ranking.py`) documenting where THAT URL came from, and every `<url><loc>` this module
ever reads did come out of an XML sitemap document — even the ones reached by following a
`robots.txt` `Sitemap:` line, because `robots.txt` itself never contributes a URL, only a
pointer to a document that does. `DiscoveredUrlSource`'s `"robots"` literal exists in that
module's vocabulary for a hypothetical caller that discovers URLs directly out of
`robots.txt` content (there is none — `robots.txt` has no such directive) and goes
deliberately unused by this one; `DiscoveryResult.source`, a different field on a different
type, is what actually reports "reached via robots.txt."

**Same-origin, enforced twice, for two different reasons.** (1) The criterion "a sitemap
listing off-origin URLs yields only same-origin ones" is a statement about THIS FUNCTION'S
RETURN VALUE, so it is checked and tested here — `_urls_from_urlset` drops any `<loc>` whose
origin does not match the website's own, exactly like `url_ranking.select_urls`'s
`"off_origin"` rule does downstream, except that deferring entirely to that downstream rule
would not satisfy a criterion that is about what THIS function returns. (2) Off-origin
targets are never even FETCHED, for child sitemaps and `robots.txt`-declared targets alike —
a stricter guarantee than (1), and for a different threat: `internals/ssrf.py`'s guard
protects this process from an internal target, but does nothing to stop this process being
pointed at an arbitrary PUBLIC third party. Without this second check, any user who registers
a site whose `robots.txt` says `Sitemap: https://victim.example/…` would cause a GET to
`victim.example` on every scheduled run of THEIR OWN site — a request-amplification primitive
this module closes for free, because any URL harvested from such a document would be dropped
downstream by rule (1) anyway. The one real-world false negative this leaves —
`docs.example.com` declaring `Sitemap: https://www.example.com/sitemap.xml` (an apex/`www`
split, or an `http`/`https` split) — costs nothing either, because every URL such a sitemap
would list is `www.example.com`, which rule (1) drops as off-origin regardless.
`websites/internals/url_normalize.py`'s own module docstring makes exactly this argument for
why `www.` is never folded into a site's origin; this module inherits the same origin
definition (`_origin_key`, collapsing only the scheme's default port, the same equality
`NormalizedUrl.origin` and `url_ranking._origin` already use) for the same reason — a LOCAL
helper rather than an import of either: `url_ranking._origin` takes a `_NormalizedCandidate`,
which would drag `_parse_candidate`'s tracking-parameter stripping and trailing-slash
normalization along for a comparison that needs neither, and `url_normalize.py` belongs to a
different feature entirely (ARCHITECTURE.md §3.1 forbids importing another feature's
`internals/`). A ~10-line local helper matching this module's own needs is exactly the
precedent `url_ranking.py` set for itself — see that module's own `_DEFAULT_PORTS` and
`_authority` docstrings.

**Document counting is an ATTEMPT counter, not a success counter — the arithmetic is worth
stating and is pinned by tests.** `crawl_sitemap_max_documents` counts every sitemap-document
fetch this module attempts — both well-known probes, every `robots.txt`-declared target, and
every child of a sitemap index — regardless of whether it 404s, is refused by the SSRF guard,
or fails to parse. A success-only counter would let a hostile `robots.txt` list ten thousand
dead `Sitemap:` lines and this module would fetch every single one of them before giving up;
counting attempts closes that for the cost of one integer. `robots.txt` ITSELF is STILL not
counted — it is not a sitemap document, and is fetched at most once per run (`_fetch_robots`,
which bypasses the counter entirely, exactly as it did before PER-191). At the default of 5,
a site whose `robots.txt`-declared targets are all this module ever reaches has 5 documents
to spend on them (`robots.txt` itself costs nothing against the cap); `/sitemap.xml` serving
an index with ten children only gets to fetch 4 of them before the cap silently stops the
rest.

**`robots.txt` is now fetched BEFORE either well-known probe, not after — a deliberate
consequence of making it unconditional (PER-191), not an oversight.** Earlier revisions of
this module fetched `robots.txt` only once both probes had already failed, so a hostile or
oversized `robots.txt` could never cost a run anything on a site whose `/sitemap.xml`
answered directly. That ordering is no longer available once `robots.txt` must be fetched
every run to read its `Disallow`/`Crawl-delay` rules (`internals/robots.py`) — the rules this
module reports on `DiscoveryResult.robots` are needed whether or not `/sitemap.xml` exists,
so the one fetch that produces them cannot wait on either probe failing first. The
consequence: a `robots.txt` large enough to exhaust the discovery byte share
(`SITEMAP_BYTE_SHARE`, above) can now do so before either probe has even been attempted,
where previously it could only do so after both had already failed. This is accepted rather
than engineered around, for the same reason the discovery byte share itself is accepted: a
run that loses its sitemap discovery to a byte cap still crawls the seed successfully
(ARCHITECTURE.md §3.4's "hitting a cap is a success" rule), and `tests/test_crawl_sitemap.py`
pins an unreadable `robots.txt` — a 404, a timeout, a 500, an HTML error page, or one that
tripped the byte share mid-fetch — as allow-everything (`DiscoveryResult.robots ==
robots.ALLOW_ALL`) rather than as a reason to fail the run.

**Byte-budget exhaustion STOPS discovery and returns whatever was already harvested — this is
a deliberate refinement on the ticket's own "returns `[]`" wording, not a contradiction of
it.** If a sitemap INDEX's fourth child blows the discovery share, throwing away the first
three children's URLs on the way out would serve nobody; nothing about that byte-cap trip
makes those already-parsed URLs any less real. The ticket's "returns `[]`" criterion is still
satisfied by the case it is actually written about — a single oversized `sitemap.xml`, where
nothing was harvested before the ceiling tripped, so the result genuinely IS `[]` —and
`tests/test_crawl_sitemap.py` pins both shapes so the refinement stays visible rather than
silently subsuming the ticket's own wording.
"""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.settings import Settings
from app.features.crawl.http_client import CRAWL_USER_AGENT
from app.features.crawl.internals.fetcher import (
    ByteBudget,
    ByteBudgetExceededError,
    PolitenessGate,
    fetch_page,
)
from app.features.crawl.internals.robots import ALLOW_ALL, RobotsRules, parse_robots
from app.features.crawl.internals.ssrf import Resolver
from app.features.crawl.internals.url_ranking import DiscoveredUrl


logger = logging.getLogger(__name__)

SITEMAP_BYTE_SHARE: Final = 0.25
"""Public — tests import this rather than hardcoding `0.25` — the fraction of
`settings.crawl_max_bytes` one run's sitemap discovery may spend before it is cut off. See
the module docstring's "byte share" section for the numbers and the failure mode this
constant exists to prevent."""

_WELL_KNOWN_SITEMAP_PATH: Final = "/sitemap.xml"
_WELL_KNOWN_INDEX_PATH: Final = "/sitemap_index.xml"
_ROBOTS_PATH: Final = "/robots.txt"
_SITEMAP_DIRECTIVE: Final = "sitemap:"

_DOCTYPE_PATTERN: Final = re.compile(r"<!DOCTYPE", re.IGNORECASE)
"""See the module docstring's XML-hardening section for the measured facts this pattern
stands in for, and why nothing in this module leans on expat's own amplification guard
instead."""

_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}
"""The same two entries `url_ranking._DEFAULT_PORTS` and `url_normalize._DEFAULT_PORTS`
each declare, reimplemented here rather than imported — see the module docstring's
"same-origin, enforced twice" section for why."""

DiscoverySource = Literal["sitemap", "sitemap_index", "robots", "none"]
"""Which of the four discovery entry points produced a `DiscoveryResult` — see the module
docstring for why this names the entry point rather than the document's own root element."""

_ENTRY_POINTS: Final[tuple[tuple[str, DiscoverySource], ...]] = (
    (_WELL_KNOWN_SITEMAP_PATH, "sitemap"),
    (_WELL_KNOWN_INDEX_PATH, "sitemap_index"),
)
"""The two well-known probes, in the order the sitemap protocol itself specifies trying
them, paired with the `DiscoverySource` each one reports on success. Typed explicitly as
`tuple[str, DiscoverySource]` — not inferred from two string literals — so
`discover_sitemap_urls`'s `DiscoveryResult(urls, source)` construction below type-checks
without a cast."""


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """What `discover_sitemap_urls` hands back — always, whether anything was found or not.

    Frozen because a discovery result is a fact about one call, not something a caller
    should edit — the same reasoning `DiscoveredUrl` and `CrawledPage` are frozen for.
    """

    urls: list[DiscoveredUrl]
    """Every same-origin URL discovery found, in the order the winning document(s) listed
    them. Never includes an off-origin `<loc>` — see the module docstring's "same-origin,
    enforced twice" section — and every entry carries `source="sitemap"` regardless of which
    `DiscoverySource` this result reports (see below)."""

    source: DiscoverySource
    """WHICH ENTRY POINT produced `urls` — `"sitemap"` for `/sitemap.xml`, `"sitemap_index"`
    for `/sitemap_index.xml`, `"robots"` for a `robots.txt` `Sitemap:` target, or `"none"`
    when nothing was found. Names the entry point, not the document's own root element — see
    the module docstring for why that reading is the only one that is well-defined."""

    robots: RobotsRules = ALLOW_ALL
    """This run's parsed `robots.txt` rules — `internals/robots.py`'s `parse_robots`, run over
    the SAME response `_fetch_robots` reads `Sitemap:` targets from, never a second fetch.
    Defaulted, and appended LAST, so every `DiscoveryResult(urls, source)` construction that
    predates PER-191 keeps compiling. Present on every return path this function has,
    including the failure ones (an unparseable `origin`, an exhausted cap, an unhandled
    exception) — `ALLOW_ALL` there is not a placeholder, it is the honest answer for "this run
    never got far enough to read a robots.txt," which fails OPEN exactly as an unreadable
    `robots.txt` does."""


class _DiscoveryBudget(ByteBudget):
    """A `ByteBudget` that checks its own, tighter cap BEFORE spending against `parent`.

    Sitemap discovery runs BEFORE `crawl_site`'s own seed fetch, sharing one `ByteBudget` for
    the whole run (`service.py`). If discovery alone could exhaust that shared budget, the
    seed fetch that follows would raise `ByteBudgetExceededError`, and a run with a huge
    sitemap and a perfectly reachable seed would FAIL — the exact outcome "hitting a cap is a
    success, not a failure" (ARCHITECTURE.md §3.4) forbids. So discovery gets a SECOND,
    tighter ceiling — `SITEMAP_BYTE_SHARE` of the run's total — layered on top of, never
    instead of, the run's own budget.

    **The order of the two `take()` calls below is the whole guarantee, and the obvious
    ordering is the wrong one.** Charging `parent` first looks like the honest accounting —
    the bytes did cross the wire — but it hands `parent` every byte of a chunk BEFORE this
    object's tighter cap has had any chance to refuse it. A chunk is not bounded in size:
    `fetch_page` streams with `response.aiter_bytes()`, whose chunking is decided by the
    transport, and a transport that hands back a whole 40 MB body in one piece would charge
    all 40 MB to the run before the 12.5 MB share ever fired. That is not a hypothetical — it
    is exactly what `httpx.MockTransport` does with a plain `content=` response, and it is
    what `tests/test_crawler_caps.py`'s
    `test_a_huge_sitemap_does_not_starve_the_page_crawl` pins down. Under that ordering the
    share is decorative: discovery could spend the entire run budget and starve the seed,
    which is the one failure this class exists to prevent.

    So the share is checked FIRST, and `parent` is only charged for a chunk this budget has
    already accepted. The cost is that the final, refused chunk is never charged to `parent`
    even though it did arrive — `budget.used` under-counts the wire by at most one chunk on
    the one fetch that trips the share. That is the safe direction to be wrong in, and it is
    invisible downstream anyway: `fetch_page` discards the whole partial body on a
    `ByteBudgetExceededError` (`internals/fetcher.py`), so nothing that chunk paid for is
    ever kept.

    `parent`'s own cap still ALWAYS binds — the second call can refuse a chunk this one
    accepted, which is what keeps a caller-supplied budget smaller than
    `settings.crawl_max_bytes` authoritative. The share is a second, tighter ceiling, never a
    licence to spend more than the run's own budget allows.
    """

    def __init__(self, parent: ByteBudget, max_bytes: int) -> None:
        super().__init__(max_bytes)
        self._parent = parent

    def take(self, num_bytes: int) -> None:
        # Discovery's own tighter ceiling FIRST — see the class docstring. `ByteBudget.take`
        # raises without mutating its counter when a chunk would exceed the cap, so a refused
        # chunk leaves BOTH budgets untouched rather than half-charged.
        super().take(num_bytes)
        self._parent.take(num_bytes)


@dataclass(slots=True)
class _DiscoveryState:
    """Mutable bookkeeping threaded through one `discover_sitemap_urls` call — the document
    counter, the stop flag, the URL accumulator, and everything `_collect` and its helpers
    need that is not itself part of any one document's parse. Not exported: a private
    accumulator so those functions' signatures do not grow a parameter apiece for every cap
    this module enforces.
    """

    client: httpx.AsyncClient
    budget: _DiscoveryBudget
    settings: Settings
    resolver: Resolver | None
    origin_key: tuple[str, str, int]
    documents_fetched: int = 0
    urls_collected: int = 0
    stop: bool = False
    """Set once ANY cap trips — the document cap, the byte share, or the URL cap — so every
    loop in this module can stop at the next opportunity instead of continuing to spend a
    budget that is already gone."""

    gate: PolitenessGate | None = None
    """The run's shared `PolitenessGate` (`internals/fetcher.py`), or `None` when the caller
    supplied none — see `discover_sitemap_urls`'s own `gate` parameter. `_guarded_fetch`
    awaits it, when set, immediately before every fetch this module makes."""

    def remaining_urls(self) -> int:
        """How many more `DiscoveredUrl`s this run's discovery may still accumulate before
        `settings.crawl_sitemap_max_urls` binds. Never negative — `_urls_from_urlset` and the
        index-recursion loop in `_collect` both stop as soon as this reaches zero."""
        return max(0, self.settings.crawl_sitemap_max_urls - self.urls_collected)


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """`(scheme, host.lower(), port-or-default)` for `url`, or `None` if it is not a usable
    same-origin comparison key: an unparseable URL, a non-http(s) scheme, a hostless URL, or
    a port `urlsplit(...).port` itself raises `ValueError` on.

    See the module docstring's "same-origin, enforced twice" section for why this is a local
    helper rather than an import of `url_ranking._origin` or `url_normalize.normalize_url`.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return (scheme, host.lower(), port if port is not None else _DEFAULT_PORTS[scheme])


def _same_origin(url: str, origin_key: tuple[str, str, int]) -> bool:
    """`True` iff `url` parses to the same `(scheme, host, port)` as `origin_key`. `False`
    for anything `_origin_key` itself refuses to parse — an unparseable candidate is never
    "same origin" by default, which is what keeps every drop site below failing CLOSED
    rather than open.
    """
    return _origin_key(url) == origin_key


def _parse_lastmod(text: str | None) -> datetime | None:
    """Parse a sitemap `<lastmod>` value, or `None` for anything `datetime.fromisoformat`
    cannot read.

    Verified on this project's pinned interpreter (Python 3.12, whose `fromisoformat` accepts
    a trailing `Z` since 3.11): `"2025-03-14"` -> a naive midnight; `"2025-03-10T09:15:00Z"`
    and `"2025-02-01T00:00:00+02:00"` -> timezone-aware; `"2025-03"`, `""`, and garbage all
    raise `ValueError` and become `None`. A naive result is not rejected —
    `url_ranking._to_utc` already treats a naive `lastmod` as UTC (see that function's own
    docstring for why: it is what a `lastmod` with no offset means, and refusing it would
    silently discard a real signal a sitemap that omits a UTC suffix still gives us).
    """
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_priority(text: str | None) -> float | None:
    """Parse a sitemap `<priority>` value, or `None` if it is absent, unparseable, or outside
    the sitemap protocol's own `0.0`-`1.0` range.

    A value outside range is discarded rather than clamped: clamping would invent a priority
    the sitemap never actually declared, and `url_ranking.SITEMAP_DEFAULT_PRIORITY` already
    treats "no declared priority" as neutral — exactly the right answer for a value this
    module cannot trust either.
    """
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not (0.0 <= value <= 1.0):
        return None
    return value


def _local(tag: str) -> str:
    """The local (namespace-stripped) part of an `ElementTree` tag.

    `ET` returns a Clark-notation tag — `{http://www.sitemaps.org/schemas/sitemap/0.9}urlset`
    — for a namespaced document, but plenty of real sitemaps omit the namespace entirely (see
    `tests/fixtures/sitemap_child_docs.xml`), so matching the raw tag would silently miss
    them. `rpartition` rather than `split`: a tag with no `}` at all (the no-namespace case)
    returns `("", "", tag)`, so `[2]` is `tag` unchanged either way — one expression handles
    both shapes.
    """
    return tag.rpartition("}")[2]


def _parse_document(text: str, url: str) -> ET.Element | None:
    """Parse `text` as XML, refusing anything that declares a `<!DOCTYPE`, or return `None`
    and log why.

    **The DOCTYPE refusal runs BEFORE `ET.fromstring` ever sees the string** — the entire
    defense described in the module docstring's XML-hardening section, because neither
    `xml.etree.ElementTree` nor the particular expat build a given interpreter happens to be
    linked against can be trusted to catch a hostile document on their own.
    """
    if _DOCTYPE_PATTERN.search(text):
        logger.warning(
            "crawl: refusing a sitemap document at %s: declares a DOCTYPE",
            url,
            extra={"url": url},
        )
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        logger.warning("crawl: could not parse sitemap document at %s", url, exc_info=True)
        return None


def _urls_from_urlset(
    root: ET.Element, *, origin_key: tuple[str, str, int], remaining: int
) -> list[DiscoveredUrl]:
    """Read every `<url>` child of a `<urlset>` document into a `DiscoveredUrl`, dropping
    anything off-origin or lacking a `<loc>`, and stopping once `remaining` entries have been
    kept — `remaining` counts entries actually KEPT (post origin-filter), matching
    `crawl_sitemap_max_urls`'s own contract in the module docstring.

    `<changefreq>` is read by nothing here — see the module docstring's "what this module
    deliberately is not" — and every child element under `<url>` besides `<loc>`,
    `<lastmod>`, and `<priority>` is silently ignored, exactly as the sitemap protocol
    requires an unrecognized element to be treated.
    """
    urls: list[DiscoveredUrl] = []
    for element in root:
        if _local(element.tag) != "url":
            continue
        if len(urls) >= remaining:
            break
        loc_text: str | None = None
        lastmod_text: str | None = None
        priority_text: str | None = None
        for child in element:
            local = _local(child.tag)
            if local == "loc":
                loc_text = (child.text or "").strip()
            elif local == "lastmod":
                lastmod_text = child.text
            elif local == "priority":
                priority_text = child.text
        if not loc_text or not _same_origin(loc_text, origin_key):
            continue
        urls.append(
            DiscoveredUrl(
                url=loc_text,
                lastmod=_parse_lastmod(lastmod_text),
                priority=_parse_priority(priority_text),
                source="sitemap",
            )
        )
    return urls


def _child_sitemap_locs(root: ET.Element, *, origin_key: tuple[str, str, int]) -> list[str]:
    """Read every `<sitemap><loc>` child of a `<sitemapindex>` document, same-origin only, in
    document order.

    `<lastmod>` on an index entry (a sitemap FILE's own last-modified time, not a page's) is
    read by nothing here — see the module docstring's "what this module deliberately is not"
    section.
    """
    locs: list[str] = []
    for element in root:
        if _local(element.tag) != "sitemap":
            continue
        loc_text: str | None = None
        for child in element:
            if _local(child.tag) == "loc":
                loc_text = (child.text or "").strip()
        if loc_text and _same_origin(loc_text, origin_key):
            locs.append(loc_text)
    return locs


def _robots_sitemap_urls(text: str, *, origin: str) -> list[str]:
    """Every `Sitemap:` target `robots.txt`'s `text` declares, same-origin only, deduped in
    the order they were declared.

    Every OTHER directive — `User-agent`, `Disallow`, `Allow`, `Crawl-delay` — is read by
    nothing here: this function reads `Sitemap:` lines and nothing else. Since PER-191 those
    other directives ARE read, just not by this function — `_fetch_robots` runs
    `internals/robots.py`'s `parse_robots` over the SAME `text` this function reads, so the
    two consumers see identical bytes without a second fetch. Each line is cut at its first
    `#` before anything else happens, so a trailing comment (`Sitemap: ...   # off-origin,
    never fetched`) is stripped
    rather than becoming part of the URL. `line.lower().startswith(...)` makes the match
    case-insensitive (`SITEMAP:`, `sitemap:`, `Sitemap:` all match), and
    `urljoin(origin + "/", value)` accepts the RELATIVE form real sites emit
    (`sitemap: /sitemaps/guides.xml`) as well as the absolute form the spec technically
    requires. Same-origin filtering happens here too, not only downstream — see the module
    docstring's "same-origin, enforced twice" section for why a target this function drops is
    never even fetched.
    """
    origin_key = _origin_key(origin)
    if origin_key is None:
        return []
    seen: dict[str, None] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or not line.lower().startswith(_SITEMAP_DIRECTIVE):
            continue
        value = line[len(_SITEMAP_DIRECTIVE) :].strip()
        if not value:
            continue
        target = urljoin(origin + "/", value)
        if _same_origin(target, origin_key):
            seen.setdefault(target, None)
    return list(seen)


async def _guarded_fetch(state: _DiscoveryState, url: str) -> str | None:
    """The one guarded fetch every document in this module goes through — a well-known
    probe, a `robots.txt`-declared target, a sitemap-index child, or `robots.txt` itself.
    Every failure mode described in the module docstring's "not fatal" argument lives here,
    in one place, so `_fetch_text` and `_fetch_robots` only have to decide WHETHER to count
    the attempt, never HOW to survive one.

    Awaits `state.gate.wait_for_turn()` first, when the caller supplied one — the same
    position `internals/crawler.py`'s `fetch_frontier_url` awaits its own gate, and the ONE
    place every request this module makes (including `robots.txt` itself) is serialized to
    the run's politeness delay. See the module docstring's "every request this module makes"
    paragraph for why that includes `robots.txt`, and `internals/robots.py`'s module
    docstring for why that delay is the OPERATOR-CONFIGURED one, never a site's own
    `Crawl-delay`.

    Each failure mode this function can meet is handled once, here:

    * `ByteBudgetExceededError` stops discovery outright (`state.stop = True`) — the shared
      budget's share is spent, and every later fetch in this module would fail the same way,
      so there is nothing left to gain by trying another URL.
    * Any other exception — an `httpx` timeout or connection failure, `FetchError`, and
      `SsrfBlockedError` alike — is logged at WARNING and treated as "this one document could
      not be fetched," never as a reason to stop trying the rest. `except Exception`
      deliberately does NOT catch `asyncio.CancelledError` (`BaseException`, not `Exception`,
      since Python 3.8) — the same property `internals/crawler.py` relies on to let a worker
      shutdown propagate rather than being swallowed here.
    * `SsrfBlockedError` specifically has already logged its own WARNING inside
      `internals/ssrf.py`'s `_refused` before it ever reached this function — the line this
      function adds on top is a SECOND, discovery-context WARNING naming the URL, not a
      duplicate of the security log. Both are intended to exist.
    * A response outside `200 <= status < 300` is logged and treated as "this document does
      not exist here," matching `fetch_page`'s own contract of never raising for a non-2xx
      response — this is the one place that turns "fetched successfully" into "usable as a
      sitemap."

    One more thing that is NOT a bug: a sitemap served with no `Content-Type` header at all
    makes `fetcher._looks_like_html` return `True` (its own docstring explains why an ABSENT
    header is treated as "might be HTML"), so `fetch_page` runs `extract_content` over this
    module's own XML body. That parse is wasted work but never wrong — `extract_content`
    never raises, and `page.content` below is still the raw, undecoded-beyond-transport body
    regardless of what trafilatura made of it — so this module reads `page.content` and
    ignores everything else `fetch_page` computed for a document it was never really about.
    """
    if state.gate is not None:
        await state.gate.wait_for_turn()
    try:
        page = await fetch_page(state.client, url, budget=state.budget, resolver=state.resolver)
    except ByteBudgetExceededError:
        state.stop = True
        logger.warning(
            "crawl: sitemap discovery exhausted its byte share fetching %s",
            url,
            extra={"url": url},
        )
        return None
    except Exception:
        logger.warning("crawl: sitemap fetch failed for %s", url, exc_info=True)
        return None
    if not (200 <= page.status < 300):
        logger.warning(
            "crawl: sitemap fetch for %s returned %d",
            url,
            page.status,
            extra={"url": url, "status": page.status},
        )
        return None
    return page.content


async def _fetch_text(state: _DiscoveryState, url: str) -> str | None:
    """Fetch a SITEMAP DOCUMENT — the only kind of fetch counted against
    `settings.crawl_sitemap_max_documents`: both well-known probes, every `robots.txt`-
    declared target, and every child of a sitemap index. Counted BEFORE the attempt is made,
    not after (see the module docstring's "document counting is an attempt counter" section)
    — a fetch that never happens because the cap was already spent still cost this module a
    decision, and a fetch that 404s costs exactly as much as one that succeeds.
    """
    if state.documents_fetched >= state.settings.crawl_sitemap_max_documents:
        state.stop = True
        return None
    state.documents_fetched += 1
    return await _guarded_fetch(state, url)


async def _fetch_robots(state: _DiscoveryState, origin: str) -> tuple[RobotsRules, list[str]]:
    """The ONE `robots.txt` fetch of a run — see the module docstring's opening section.
    Returns this run's parsed `RobotsRules` (`internals/robots.py`'s `parse_robots`) and the
    same-origin `Sitemap:` targets `_robots_sitemap_urls` reads, BOTH derived from the SAME
    response — `robots.txt` is never fetched a second time to satisfy the second consumer.

    Deliberately bypasses `_fetch_text`'s document counter — `robots.txt` is not a sitemap
    document, and the module docstring's document-counting arithmetic
    (`crawl_sitemap_max_documents` leaves N documents for whatever `robots.txt` declares) only
    holds if fetching it is free. Still goes through the same `_guarded_fetch` as everything
    else, so a byte-budget trip or an SSRF refusal on `robots.txt` itself stops discovery
    exactly like it would for any other document.

    Never raises: `parse_robots` has its own "never raises, fail open" contract, and a missing
    or unfetchable `robots.txt` (`_guarded_fetch` returning `None`) returns `(ALLOW_ALL, [])`
    — the same fail-open answer a `robots.txt` this module COULD fetch but not parse would
    produce, so a caller cannot distinguish "unreachable" from "unreadable" and does not need
    to.
    """
    text = await _guarded_fetch(state, origin + _ROBOTS_PATH)
    if text is None:
        return ALLOW_ALL, []
    rules = parse_robots(text, user_agent=CRAWL_USER_AGENT)
    targets = _robots_sitemap_urls(text, origin=origin)
    return rules, targets


async def _collect(state: _DiscoveryState, url: str, *, allow_index: bool) -> list[DiscoveredUrl]:
    """Fetch and parse ONE sitemap document at `url`, returning whatever same-origin URLs it
    (or, for an index, its allowed children) yielded — `[]` for anything that failed to
    fetch, failed to parse, or does not look like a sitemap document at all.

    `allow_index` is the entire depth-1 recursion rule (module docstring's discovery-order
    section). Called with `allow_index=True` for a document reached DIRECTLY by this
    module's own top-level walk (a well-known probe, or a robots-declared target), and with
    `allow_index=False` for every child this function recurses into. A grandchild that is
    ITSELF an index is still fetched and parsed — it costs one document against the cap
    either way — but with `allow_index` now `False` it matches neither the `urlset` branch
    nor the (disabled) `sitemapindex` branch below, falls through to "anything else," and
    contributes nothing. One boolean, flipped once per recursive call, is the whole rule;
    there is no separate depth counter to keep in sync with it.
    """
    text = await _fetch_text(state, url)
    if text is None:
        return []
    root = _parse_document(text, url)
    if root is None:
        return []

    local = _local(root.tag)
    if local == "urlset":
        urls = _urls_from_urlset(
            root, origin_key=state.origin_key, remaining=state.remaining_urls()
        )
        state.urls_collected += len(urls)
        return urls

    if local == "sitemapindex" and allow_index:
        collected: list[DiscoveredUrl] = []
        for child_loc in _child_sitemap_locs(root, origin_key=state.origin_key):
            collected.extend(await _collect(state, child_loc, allow_index=False))
            if state.stop or state.remaining_urls() == 0:
                break
        return collected

    # Anything else — an RSS/Atom feed some sites also serve at `/sitemap.xml`, or an HTML
    # document that happened to declare no DOCTYPE and slipped past `_parse_document` — is
    # not a sitemap this module knows how to read. Falling through to `[]` here, rather than
    # raising, is what lets discovery move on to the next entry point.
    return []


async def discover_sitemap_urls(
    client: httpx.AsyncClient,
    origin: str,
    *,
    budget: ByteBudget,
    settings: Settings,
    resolver: Resolver | None = None,
    gate: PolitenessGate | None = None,
) -> DiscoveryResult:
    """Discover this website's crawl frontier from its sitemap, before `crawl_site` runs.

    Fetches `origin`'s `robots.txt` first, unconditionally — see the module docstring's
    opening section — then tries, in order, `origin + "/sitemap.xml"`, then
    `origin + "/sitemap_index.xml"`, then every same-origin `Sitemap:` target that same
    `robots.txt` fetch declared, stopping at the first entry point that yields at least one
    URL. See the module docstring for the full reasoning behind every cap, refusal, and the
    return type.

    Args:
        client: The shared crawl client (`build_crawl_client`) — the same one `crawl_site`
            fetches pages through. Every request this function makes goes through it, and
            therefore through `internals/fetcher.py`'s `fetch_page` and its SSRF guard.
        origin: The website's own normalized origin (`website.origin`,
            `websites/internals/url_normalize.py`) — scheme, host, and a non-default port
            only, no trailing slash. Compared against every candidate URL for the
            same-origin checks this module enforces (module docstring).
        budget: The run's shared `ByteBudget` (`internals/fetcher.py`) — the SAME object
            `crawl_site`'s own fetches will spend from afterwards. This function never spends
            more than `SITEMAP_BYTE_SHARE` of `settings.crawl_max_bytes` from it; see
            `_DiscoveryBudget`.
        settings: Read for `crawl_max_bytes` (the byte share is derived from it),
            `crawl_sitemap_max_documents`, and `crawl_sitemap_max_urls`.
        resolver: Forwarded to every `fetch_page` call. Tests inject a fake one; production
            code never does.
        gate: The run's shared `PolitenessGate` (`internals/fetcher.py`) — the SAME object
            `crawl_site`'s own frontier fetches will wait on afterwards, at the SAME,
            operator-configured delay (never a site's own `Crawl-delay` — see
            `internals/robots.py`'s module docstring for why widening it to that is deferred
            to the crawl phase). `None` — the default — means this function's own fetches are
            not gated at all, which is what every call site before PER-191 is equivalent to.

    Returns:
        A `DiscoveryResult`. Never raises: every failure mode this module can produce — a
        failed fetch, malformed XML, an SSRF refusal, an exhausted document, URL, or byte
        cap, or a bug in this module itself — is caught, logged, and turned into an empty or
        partial result instead, because discovery failing is never allowed to fail the run it
        is trying to help (ARCHITECTURE.md §3.4). `DiscoveryResult.robots` carries whatever
        `robots.txt` was actually read on every one of those paths — `ALLOW_ALL` when it was
        never reachable at all (an unparseable `origin`, below) or never usable once fetched.
    """
    origin_key = _origin_key(origin)
    if origin_key is None:
        # Fails CLOSED, mirroring `url_ranking.select_urls`'s own unparseable-seed rule (that
        # module's own docstring): an origin this module cannot itself parse is one it cannot
        # safely compare anything against, so nothing is discovered rather than everything
        # being treated as same-origin by default. Defensive rather than expected — `origin`
        # is `website.origin`, already validated by `url_normalize.normalize_url` at
        # website-creation time. No `robots.txt` fetch is attempted either — there is nothing
        # this module could safely compare a `Sitemap:` target's origin against — so `robots`
        # stays `ALLOW_ALL` on the `DiscoveryResult` this returns.
        logger.warning("crawl: sitemap discovery given an unparseable origin %r", origin)
        return DiscoveryResult([], "none")

    share = max(1, int(settings.crawl_max_bytes * SITEMAP_BYTE_SHARE))
    state = _DiscoveryState(
        client=client,
        budget=_DiscoveryBudget(budget, share),
        settings=settings,
        resolver=resolver,
        origin_key=origin_key,
        gate=gate,
    )

    rules: RobotsRules = ALLOW_ALL
    try:
        # THE ONE `robots.txt` FETCH OF THIS RUN, first thing inside the `try` and before
        # either well-known probe — module docstring's opening section, and its "document
        # counting" section for the byte-share consequence of fetching it here rather than
        # last. `robots_targets` is fed to the SAME `if not state.stop:` block below that used
        # to fetch `robots.txt` itself; there is no second fetch anywhere in this function.
        rules, robots_targets = await _fetch_robots(state, origin)

        for path, source in _ENTRY_POINTS:
            urls = await _collect(state, origin + path, allow_index=True)
            if urls:
                return DiscoveryResult(urls, source, rules)
            if state.stop:
                break

        if not state.stop:
            collected: list[DiscoveredUrl] = []
            # Not deduped across `robots.txt` targets, or against either well-known probe:
            # `url_ranking.select_urls`'s own `"duplicate"` rule already merges a URL's
            # `priority`/`lastmod` order-independently downstream, so a second pass here
            # would only repeat work that module already has to do for every OTHER source of
            # duplication (a URL two `Sitemap:` targets both list).
            for target in robots_targets:
                collected.extend(await _collect(state, target, allow_index=True))
                if state.stop or state.remaining_urls() == 0:
                    break
            if collected:
                return DiscoveryResult(collected, "robots", rules)
    except Exception:
        # Nothing above this line may fail the run that is waiting on this result (module
        # docstring) — a bug in THIS module is exactly as harmless to a crawl as a missing
        # sitemap, so it is caught, logged with its full traceback, and the run proceeds on
        # the seed alone. `rules` still carries whatever `_fetch_robots` managed before the
        # exception, which is `ALLOW_ALL` unless the failure happened after it returned.
        logger.warning("crawl: sitemap discovery failed", exc_info=True)

    logger.info(
        "crawl: sitemap discovery complete",
        extra={
            "documents_fetched": state.documents_fetched,
            "urls_collected": state.urls_collected,
            "bytes_used": state.budget.used,
        },
    )
    return DiscoveryResult([], "none", rules)
