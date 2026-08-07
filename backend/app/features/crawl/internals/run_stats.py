"""Building the `dict` `runs.stats` stores — one owner, so the persisted shape never drifts.

Pure, feature-owned, no I/O, the same category as `internals/payload.py` beside it.
`CrawlService.execute_run` is the only caller, because it is the one place that holds all
three kinds of fact this dict is made of: a `CrawlResult` (from `internals/crawler.py`); two
facts about the ARTIFACTS the crawl loop cannot know about itself — how many links the
generated index actually lists, and how many page bodies the full-text expansion had to cut;
and three facts about what happened BEFORE the crawl loop ran at all — which discovery entry
point produced the frontier, how many URLs it found, and how many survived ranking
(`internals/sitemap.py`, `internals/url_ranking.py`). This module is where all three are
combined into the one `dict` `RunService.record_success`/`record_failure` persist as jsonb.

**Why this lives here and not inside `internals/crawler.py`.** `CrawlResult.stats` is the
crawl loop's own concern: pages fetched, pages failed, bytes moved, how long it took, which
cap (if any) stopped it, and how many of the fetched pages carried no extractable content
(`pages_empty_content`, added by PER-177). None of that is specific to *persistence* — a
caller that only wanted to log a crawl's numbers would want exactly that dict and nothing
more. Everything else this module adds is a persistence-shaped concern instead, and each is
something the crawl loop is structurally incapable of reporting:

* `links_emitted` and `full_txt_truncated` are facts about the ARTIFACTS rather than about
  the crawl — `internals/llms_txt.py` is what decides both, and the crawl loop has finished
  by the time either is knowable.
* `discovery_source`, `urls_discovered` and `urls_selected` are facts about the frontier the
  crawl loop was HANDED. `crawl_site` receives `extra_urls` as a plain sequence — or, since
  PER-178, calls a `frontier_from_seed` function it knows nothing about — and has no idea
  whether the result came from a sitemap, a `robots.txt` directive, a page's own links, or
  nowhere at all; that is exactly the seam its own module docstring describes, and keeping
  these three out of `CrawlResult.stats` is what preserves it.
* `version` only exists because a stored jsonb value outlives the code that wrote it.

Building the persisted shape here, one call site away from the write path, keeps
`crawler.py`'s dict exactly as wide as the crawl loop's own job and gives PER-159's crawl
loop nothing new to keep in sync with this ticket's writer.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final


logger = logging.getLogger(__name__)

RUN_STATS_VERSION: Final = 4
"""Which definition of this whole dict's shape a stored row was written under — not just
`links_emitted`'s meaning, but which KEYS a row of this version even has.

**Version 1** rows are what every crawl wrote before PER-177 wired extraction into the fetch
path: `links_emitted` under the one-bullet-per-page stub rule (see its own docstring below),
and no `pages_empty_content` key at all — the concept did not exist yet, because nothing
upstream of `generate_llms_txt` parsed a page's content to know whether it had any.
**Version 2** rows add `pages_empty_content`, threaded straight through from
`CrawlResult.stats` (`internals/crawler.py`), now that every fetched page's HTML is run
through `internals/extract.py` and can genuinely come back with nothing on it.
`links_emitted` keeps its version-1 meaning in version 2 — the stub still emits exactly one
bullet per fetched page — so that bump was solely about the new key, not a redefinition of an
existing one.

**Version 3** rows are the first written by a real `generate_llms_txt` (PER-179), and they
change both of the things a version can change, at once.

A new KEY: `full_txt_truncated`, the number of pages whose body the `llms-full.txt` artifact
could not carry in full — `internals/llms_txt.py`'s `count_full_txt_truncations`, which
documents why a page dropped entirely by the whole-artifact cap is counted alongside one
merely cut by the per-page cap.

And a REDEFINITION, which is the more consequential half: `links_emitted` stops meaning "one
bullet per fetched page". The artifact now omits pages whose extraction came back empty, so
the number is the count of pages actually listed, and it is routinely LESS than
`pages_crawled` rather than equal to it by construction. That is exactly the divergence the
`links_emitted` docstring below predicted while the stub was still in place.

The redefinition is why a reader cannot interpret this key without `version`.
`links_emitted == pages_crawled` on a version-2 row is a tautology that says nothing about
the artifact; the same equality on a version-3 row is a real measurement that happened to
come out equal, and means every fetched page had extractable content. No row carries
anything else a reader could infer this from — a row's age is not a fact about the code that
wrote it — which is what keeps this a stored field rather than a derivation.

**Version 4** rows add three keys and redefine nothing (PER-176): `discovery_source`,
`urls_discovered` and `urls_selected` describe the frontier the crawl loop was HANDED, before
it fetched anything — which of `internals/sitemap.py`'s entry points produced it
(`"sitemap"`, `"sitemap_index"`, `"robots"`, or `"none"`), how many same-origin candidates
that produced, and how many `url_ranking.select_urls` kept. Every version-3 key keeps its
version-3 meaning here.

**PER-178 added a fifth `discovery_source` value, `"links"`, and deliberately did NOT bump
the version for it.** A version is about the SHAPE of this dict — which keys a row has, and
what each one means — and a new value in an existing key's vocabulary is neither. A reader
that knows what version 4 is already knows `discovery_source` names where the frontier came
from; `"links"` (the seed page's own `<a href>` links, `internals/links.py`, used only when
sitemap discovery found nothing) is one more answer to the question the key was already
asking, and every version-4 row still carries exactly these keys. Bumping here would have
made two identically-shaped dicts distinguishable only by a version number, which is the
opposite of what the field is for.

**Why this is version 4 and not three more keys folded into version 3.** PER-179 and PER-176
are separate merges, so they are separate deploys, and version 3 was already live and writing
rows by the time discovery landed. Folding these keys into 3 would have left two genuinely
different shapes both stamped `version: 3`, distinguishable only by a reader who happened to
know the deploy order — which is precisely the knowledge this field exists so that nobody
needs. Two bumps in one release is cosmetically odd and semantically honest, and for a value
stored permanently in jsonb, honest wins.

The bump is what makes the shapes unambiguous rather than merely documented: `build_run_stats`
writes the three discovery keys and `version` into the SAME dict in the same call, so no build
of this module can emit one without the other. A version-3 row therefore never carries the
discovery keys, and a version-4 row always does — there is no window, and no defensive read
of a possibly-absent key is required. Read `discovery_source: "none"` (an explicit value, and
the honest answer for a site with no sitemap) as different in kind from a version-3 row's
silence on the subject."""


def build_run_stats(
    crawl_stats: Mapping[str, Any],
    *,
    links_emitted: int,
    full_txt_truncated: int,
    discovery_source: str,
    urls_discovered: int,
    urls_selected: int,
) -> dict[str, Any]:
    """Combine `crawl_stats` (from `CrawlResult.stats`) with the two numbers only the
    artifacts know, the three only the discovery step knows, and `version`, into the exact
    `dict` `runs.stats` stores.

    Args:
        crawl_stats: `CrawlResult.stats` — `pages_crawled`, `pages_failed`, `bytes_fetched`,
            `duration_ms`, `cap_hit`, and `pages_empty_content`. Passed through unchanged and
            unre-derived: in particular, `cap_hit` is never recomputed here. The crawl loop is
            the only code that knows which cap (if any) actually stopped a run — recomputing
            that downstream from partial information would risk disagreeing with the very
            component that decided it.
        links_emitted: The number of links the generated `llms.txt` artifact lists —
            `internals/llms_txt.py`'s `count_indexed_pages`, and nothing a caller derived for
            itself. **This diverged from `crawl_stats["pages_crawled"]` in PER-179**, exactly
            as the stub-era version of this docstring predicted it would: the artifact omits
            pages whose extraction came back empty, so a run that fetched ten pages and found
            content on seven records `pages_crawled: 10, links_emitted: 7`. `version: 3` is
            what tells a future reader which of the two rules produced a given row's number
            (see `RUN_STATS_VERSION`); a version-2 row's `links_emitted` is one-per-page and
            says nothing about the artifact.

            Still recorded as its own key rather than derived at read time from
            `pages_crawled` minus `pages_empty_content`, and the divergence is precisely why:
            that subtraction is only correct for as long as "empty" is the ONLY reason a page
            can be left out, which is a property of today's selection rule rather than of the
            data. Ask the artifact what it listed; do not reconstruct it.
        full_txt_truncated: How many pages the `llms-full.txt` artifact could not carry in
            full — `internals/llms_txt.py`'s `count_full_txt_truncations`, which owns the
            definition (both the per-page cut and the whole-artifact drop count). New in
            version 3. Recorded because a run that silently lost half its text to a cap and
            one that fetched exactly what it published are otherwise indistinguishable in this
            row, and the caps are the sort of limit whose value is only revisited if someone
            can see how often it binds.
        discovery_source: Which discovery path actually produced this run's frontier — one of
            `internals/sitemap.py`'s four entry points (`"sitemap"`, `"sitemap_index"`,
            `"robots"`, `"none"`), or `"links"` for the depth-1 fallback that reads the seed
            page's own `<a href>` links when sitemap discovery found nothing
            (`internals/links.py`, PER-178). Named only when that path actually yielded at
            least one candidate: a fallback that ran and found no links reports `"none"`,
            exactly as a `/sitemap.xml` that 404s does, so `"none"` reads as "this run found
            no frontier anywhere" rather than as "no sitemap specifically". Typed as a plain
            `str`, not `sitemap.DiscoverySource` or `service.RunDiscoverySource`: this is a
            pure, I/O-free module, and importing a type from either would make it depend on
            an I/O module for the first time. The vocabulary these five strings are drawn
            from is owned by those modules, not by this one.
        urls_discovered: How many same-origin candidates the winning discovery path handed to
            `url_ranking.select_urls` — for a sitemap, not every `<loc>` in whatever document
            it read, which may have listed more before off-origin entries were dropped; for
            `"links"`, the same-origin links the seed page carried, after `extract_links`'s
            own filtering and deduping.
        urls_selected: How many of those candidates `select_urls` actually chose — the same
            `select_urls` call for both discovery paths, since a URL found on a page is
            ranked by the same rules as one found in a sitemap — i.e.
            `len(SelectionResult.selected)` — the size of the frontier `crawl_site` was
            actually given via `extra_urls`. Recorded next to `urls_discovered` rather than
            instead of it because the RATIO is the tunable thing: a run that discovered 4,000
            URLs and selected 24 says something about the ranking that neither number says
            alone.

    Returns:
        `crawl_stats` spread first, followed by `links_emitted`, `full_txt_truncated`,
        `discovery_source`, `urls_discovered`, `urls_selected`, and `version`.
        `crawl_stats`'s own keys come first and are never overwritten by the six added here,
        because none of those six names is a key
        `CrawlResult.stats` has ever produced.
    """
    stats = {
        **crawl_stats,
        "links_emitted": links_emitted,
        "full_txt_truncated": full_txt_truncated,
        "discovery_source": discovery_source,
        "urls_discovered": urls_discovered,
        "urls_selected": urls_selected,
        "version": RUN_STATS_VERSION,
    }
    # "Generation complete, with stats" — passed as `extra=stats` rather than folded into the
    # message text, so a `jq` filter can select on any of `pages_crawled`, `bytes_fetched`,
    # `cap_hit`, etc. directly instead of parsing them back out of a rendered string.
    logger.debug("crawl: generation complete", extra=stats)
    return stats
