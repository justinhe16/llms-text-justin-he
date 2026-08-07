"""Building the `dict` `runs.stats` stores — one owner, so the persisted shape never drifts.

Pure, feature-owned, no I/O, the same category as `internals/payload.py` beside it.
`CrawlService.execute_run` is the only caller: it has both a `CrawlResult` (from
`internals/crawler.py`) and two facts the crawl loop cannot know about itself — how many
links the generated artifact actually lists, and how many page bodies the full-text artifact
had to cut — and this module is where the three are combined into the one `dict`
`RunService.record_success`/`record_failure` persist as jsonb.

**Why this lives here and not inside `internals/crawler.py`.** `CrawlResult.stats` is the
crawl loop's own concern: pages fetched, pages failed, bytes moved, how long it took, which
cap (if any) stopped it, and how many of the fetched pages carried no extractable content
(`pages_empty_content`, added by PER-177). None of that is specific to *persistence* — a
caller that only wanted to log a crawl's numbers would want exactly that dict and nothing
more. `links_emitted`, `full_txt_truncated` and `version` are persistence-shaped concerns
instead: the first two only exist because `runs.stats` is a place a UI reads from, and are
facts about the ARTIFACTS rather than about the crawl — `internals/llms_txt.py` is what
decides both, and the crawl loop has finished by the time either is knowable. `version` only
exists because a stored jsonb value outlives the code that wrote it. Building the persisted
shape here, one
call site away from the write path, keeps `crawler.py`'s dict exactly as wide as the crawl
loop's own job and gives PER-159's crawl loop nothing new to keep in sync with this ticket's
writer.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final


logger = logging.getLogger(__name__)

RUN_STATS_VERSION: Final = 3
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
wrote it — which is what keeps this a stored field rather than a derivation."""


def build_run_stats(
    crawl_stats: Mapping[str, Any], *, links_emitted: int, full_txt_truncated: int
) -> dict[str, Any]:
    """Combine `crawl_stats` (from `CrawlResult.stats`) with the two numbers only the
    artifact knows, plus `version`, into the exact `dict` `runs.stats` stores.

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

    Returns:
        `{**crawl_stats, "links_emitted": …, "full_txt_truncated": …, "version": …}`.
        `crawl_stats`'s own keys come first and are never overwritten by the three added here,
        because none of `"links_emitted"`, `"full_txt_truncated"` or `"version"` is a key
        `CrawlResult.stats` has ever produced.
    """
    stats = {
        **crawl_stats,
        "links_emitted": links_emitted,
        "full_txt_truncated": full_txt_truncated,
        "version": RUN_STATS_VERSION,
    }
    # "Generation complete, with stats" — passed as `extra=stats` rather than folded into the
    # message text, so a `jq` filter can select on any of `pages_crawled`, `bytes_fetched`,
    # `cap_hit`, etc. directly instead of parsing them back out of a rendered string.
    logger.debug("crawl: generation complete", extra=stats)
    return stats
