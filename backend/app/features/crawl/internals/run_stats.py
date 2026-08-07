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

RUN_STATS_VERSION: Final = 7
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
silence on the subject.

**Version 5** rows add four keys and redefine nothing (PER-180): `pages_enriched`,
`enrich_failures`, `enrich_input_tokens`, and `enrich_output_tokens` describe
`internals/enrich.py`'s flag-gated, model-assisted summarization pass — how many pages
received a model-written title and description in place of their extracted ones, how many
requests were attempted and could not be used, and how many tokens the pass spent doing it.
Every version-4 key keeps its version-4 meaning here.

**This bump and PER-178's refusal to bump are the same rule applied to two different
changes**, and reading them together is what makes the rule legible. `"links"` added a new
VALUE to a key that already existed, so every row still carried exactly the same keys with
exactly the same meanings and there was nothing a reader could get wrong — no bump. These
four add new KEYS, so a row written before this deploy and a row written after genuinely
differ in shape, and a reader who assumed `pages_enriched` was merely absent-because-zero on
an older row would be inventing data that was never recorded. The version moves when the
SHAPE moves, and only then.

All four are `0` on every run where `crawl_enrich_with_llm` is off, and — the point worth
stating rather than assuming — **`0` is a real, recorded value here, not an absent key the
way a version-3 row's silence on `discovery_source` was before version 4 existed.** A reader
does not need to know which release wrote a version-5 row to know these four numbers mean
something; they mean "this run's enrichment pass touched zero pages, in this many ways,"
which is exactly as true for a flag-off run as for a flag-on run that got unlucky.

That last clause is the reason `pages_enriched` cannot be read alone. `pages_enriched == 0`
on a row where `pages_crawled > 0` is ambiguous by itself — it means either "the flag was
off" or "the flag was on and every single call failed" — and `enrich_failures` is what tells
those two apart: `0` failures alongside `0` enriched pages says the flag never ran at all;
any positive `enrich_failures` alongside `0` enriched pages says it ran and lost completely.
Contrast `SelectionResult.dropped` (`internals/url_ranking.py`), whose per-rule counters DO
reconcile exactly against how many candidates were considered — that module owns every
candidate it is handed and can account for all of them. This module's enrichment counters do
not reconcile the same way: `pages_enriched + enrich_failures` need NOT equal
`pages_crawled`, because a page whose truncated markdown is empty after `.strip()` is never
sent to the model at all (`internals/enrich.py`'s own docstring — a precondition of making a
request, not a content judgement) and is counted in neither. The gap between `pages_crawled`
and `pages_enriched + enrich_failures` is exactly the number of pages enrichment skipped for
having nothing to send.

The two token keys exist so a run's model spend is readable from the row that already
describes everything else about it, rather than requiring a trip to a billing dashboard to
answer "did this run's enrichment pass cost anything, and how much" — the same motivation
`bytes_fetched` already serves for the crawl's own network cost.

**Version 6** rows add two keys and redefine nothing (PER-191): `urls_robots_disallowed` and
`crawl_delay_ms` describe how this run's `robots.txt` — read once, unconditionally, by
`internals/sitemap.py`'s `discover_sitemap_urls` — affected the frontier and the fetch
pace. `urls_robots_disallowed` is `SelectionResult.dropped.get("robots_disallowed", 0)` from
whichever `select_urls` call actually produced this run's frontier (the sitemap-derived one,
or the depth-1 link fallback's, exactly as `urls_discovered`/`urls_selected` already do —
`service.py` overwrites all three together when the fallback runs). `crawl_delay_ms` is the
politeness gap this run's crawl phase actually used —
`internals/robots.py`'s `effective_crawl_delay_ms(configured_ms, robots.crawl_delay_s)` — not
merely `Settings.crawl_politeness_delay_ms` copied across.

Every version-5 key keeps its version-5 meaning here. Both new keys are recorded on EVERY row
from this version onward, including a run whose `robots.txt` was unreadable or declared no
restriction at all: `urls_robots_disallowed: 0` there is a real, recorded zero — this run's
robots.txt disallowed nothing — the same "0 is data, not an absent key" rule version 5's own
paragraph makes for `pages_enriched`.

**`crawl_delay_ms == Settings.crawl_politeness_delay_ms` is not, by itself, evidence that the
site published no `Crawl-delay`** — the same "cannot be read alone" caveat `pages_enriched`
carries, and for a structurally identical reason: a site can publish `Crawl-delay: 0.1` and
still leave this key exactly at the operator's own configured floor, because
`effective_crawl_delay_ms` never lowers it, only ever raises it. Reading `crawl_delay_ms` on
its own tells you the pace this run's crawl phase actually used; it does not, alone, tell you
whether that pace came from `Settings` or from the site itself — `robots.txt`'s own
`Crawl-delay`, when the run needs that answered, is not itself a stored field (this key is
the derived NUMBER a run used, not the site's raw declaration).

**Version 7** rows add two more keys and redefine nothing (PER-193): `llms_txt_bytes` and
`index_diff` are what the Trends tab's "what changed in the latest run" panel and its
output-focused tiles read (`app.features.runs.service._to_latest`). Neither is derivable
from an existing key, which is why both are new columns of the dict rather than a
`RUN_STATS_VERSION` bump for nothing. PER-191 and PER-193 deploy separately, so this is a
second bump in short order for the same reason PER-179/PER-176 and PER-179's own version-3
paragraph give for theirs: version 6 was already live and writing rows by the time this
ticket's diff computation landed, and folding these two keys into 6 would have left two
different shapes both stamped `version: 6`.

`llms_txt_bytes` is `len(llms_txt.encode())` — the size of the generated index in UTF-8
bytes. `0` on every failure path, and that `0` is a real recorded measurement in exactly the
sense every other zero in this dict is: a run that never produced an index has an index of
zero bytes, the same way `links_emitted` is `0` on a seed failure rather than absent.

`index_diff` is `dict | None` — see `internals/index_diff.py`'s `build_index_diff` for its
two shapes (`{"state": "first_run", ...}` and `{"state": "compared", ...}`) and everything
each key inside them means. `None` is not "not yet computed"; it is a recorded fact that this
run produced no index to compare (every failure path before `generate_llms_txt` ran) or that
computing the comparison itself failed and was swallowed (`CrawlService._build_index_diff`'s
own docstring explains why that failure must not fail the run). Either way, `None` here means
exactly what `runs.error` being `None` means elsewhere in this dict's sibling fields:
there is nothing to report, not that reporting was skipped.

**A version-6 row's silence on both keys is different in kind from a version-7 row's `0`/
`None`.** A version-6 reader has no `llms_txt_bytes` or `index_diff` key to read at all; a
version-7 reader always has both, and their values — `0` and `None` included — are exactly as
trustworthy as every other key in this dict. `RunsReader._WEBSITE_STATS`'s
`jsonb_typeof(...) = 'number'`/`'object'` guards are what let a query written against
version-7 rows run harmlessly over the version-1-through-6 rows already in the table, the
same defensive pattern every earlier version bump in this file already established for its
own new keys."""


def build_run_stats(
    crawl_stats: Mapping[str, Any],
    *,
    links_emitted: int,
    full_txt_truncated: int,
    discovery_source: str,
    urls_discovered: int,
    urls_selected: int,
    urls_robots_disallowed: int,
    crawl_delay_ms: int,
    pages_enriched: int,
    enrich_failures: int,
    enrich_input_tokens: int,
    enrich_output_tokens: int,
    llms_txt_bytes: int,
    index_diff: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine `crawl_stats` (from `CrawlResult.stats`) with the two numbers only the
    artifacts know, the three only the discovery step knows, the two only `robots.txt`
    handling knows, the four only the enrichment pass knows, the two only the diff step
    knows, and `version`, into the exact `dict` `runs.stats` stores.

    **A row's `status` and its `index_diff` can disagree about whether an index was ever
    persisted, and that is expected rather than a bug.** `CrawlService.execute_run` computes
    `llms_txt_bytes`/`index_diff` right after generating this run's `llms_txt`, well before
    the Storage upload — so a run whose upload (or the write in `record_success`) fails AFTER
    that point calls this function again, on the failure path, with a fully-populated
    `index_diff` describing an index that was never actually written to `runs.llms_txt`. That
    is harmless, and deliberately so: every reader of this column filters on
    `status = 'completed'` first (`RunsReader._WEBSITE_STATS`'s `r.status = 'completed'`
    guards, `RunService._to_latest`'s callers) — a `failed` row's `stats` is never read as if
    it described a persisted artifact, regardless of what its `index_diff` happens to say.
    That filter is therefore not a defensive nicety; it is the thing that makes this
    non-atomicity safe to leave alone rather than a case this function needs to guard against
    itself.

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
        urls_robots_disallowed: How many candidates `url_ranking.select_urls` dropped under
            `"robots_disallowed"` — `SelectionResult.dropped.get("robots_disallowed", 0)` from
            whichever `select_urls` call actually produced this run's frontier (PER-191). `0`
            is a real, recorded value here — either this run's `robots.txt` disallowed
            nothing, or there was no `robots.txt` to read — never an absent key.
        crawl_delay_ms: The politeness gap this run's crawl phase actually used, in
            milliseconds — `internals/robots.py`'s `effective_crawl_delay_ms(configured_ms,
            robots.crawl_delay_s)`. See `RUN_STATS_VERSION`'s own version-6 paragraph for why
            this being equal to `Settings.crawl_politeness_delay_ms` is not, by itself,
            evidence the site published no `Crawl-delay`.
        pages_enriched: How many pages `internals/enrich.py`'s `enrich_pages` gave a
            model-written title and description — `len(EnrichmentResult.summaries)`. `0` on
            every run where `crawl_enrich_with_llm` is off, and see `RUN_STATS_VERSION`'s own
            version-5 paragraph for why `0` here is not, by itself, enough to tell "the flag
            was off" apart from "the flag was on and nothing survived."
        enrich_failures: How many pages had a summarization request attempted and could not
            be used — `EnrichmentResult.failures`. Does NOT include a page skipped for having
            no text to send; see that field's own docstring and `internals/enrich.py`'s
            module docstring for why that is a precondition rather than a failure.
        enrich_input_tokens: Summed `response.usage.input_tokens` across every enrichment
            request this run made — `EnrichmentResult.input_tokens`. `0` when enrichment
            never ran.
        enrich_output_tokens: Summed `response.usage.output_tokens` across every enrichment
            request this run made — `EnrichmentResult.output_tokens`. `0` when enrichment
            never ran.
        llms_txt_bytes: `len(llms_txt.encode())` — the generated index's size in UTF-8 bytes.
            `0` on every path that never generated one (a seed failure, or any failure before
            `generate_llms_txt` ran), which is a real recorded zero and not an absent key —
            see `RUN_STATS_VERSION`'s version-7 paragraph.
        index_diff: The block `internals/index_diff.py`'s `build_index_diff` returns, or
            `None` when this run produced no index to compare (every path above that left
            `llms_txt_bytes` at `0`) or when computing the comparison itself failed and was
            swallowed by the caller (`CrawlService._build_index_diff`). Passed through
            unchanged — this module does not itself call `build_index_diff` or import
            anything from `internals/index_diff.py`, keeping the same one-way "the caller
            already did the work, this module only assembles the dict" relationship it has
            with every other keyword argument here.

    Returns:
        `crawl_stats` spread first, followed by `links_emitted`, `full_txt_truncated`,
        `discovery_source`, `urls_discovered`, `urls_selected`, `urls_robots_disallowed`,
        `crawl_delay_ms`, `pages_enriched`, `enrich_failures`, `enrich_input_tokens`,
        `enrich_output_tokens`, `llms_txt_bytes`, `index_diff`, and `version`. `crawl_stats`'s
        own keys come first and are never overwritten by the fourteen added here, because none
        of those fourteen names is a key `CrawlResult.stats` has ever produced.
    """
    stats = {
        **crawl_stats,
        "links_emitted": links_emitted,
        "full_txt_truncated": full_txt_truncated,
        "discovery_source": discovery_source,
        "urls_discovered": urls_discovered,
        "urls_selected": urls_selected,
        "urls_robots_disallowed": urls_robots_disallowed,
        "crawl_delay_ms": crawl_delay_ms,
        "pages_enriched": pages_enriched,
        "enrich_failures": enrich_failures,
        "enrich_input_tokens": enrich_input_tokens,
        "enrich_output_tokens": enrich_output_tokens,
        "llms_txt_bytes": llms_txt_bytes,
        "index_diff": index_diff,
        "version": RUN_STATS_VERSION,
    }
    # "Generation complete, with stats" — passed as `extra=stats` rather than folded into the
    # message text, so a `jq` filter can select on any of `pages_crawled`, `bytes_fetched`,
    # `cap_hit`, etc. directly instead of parsing them back out of a rendered string.
    logger.debug("crawl: generation complete", extra=stats)
    return stats
