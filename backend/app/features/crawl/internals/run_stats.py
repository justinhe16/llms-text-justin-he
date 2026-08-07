"""Building the `dict` `runs.stats` stores — one owner, so the persisted shape never drifts.

Pure, feature-owned, no I/O, the same category as `internals/payload.py` beside it.
`CrawlService.execute_run` is the only caller: it has both a `CrawlResult` (from
`internals/crawler.py`) and a fact the crawl loop cannot know about itself — how many links
the generated artifact actually lists — and this module is where the two are combined into
the one `dict` `RunService.record_success`/`record_failure` persist as jsonb.

**Why this lives here and not inside `internals/crawler.py`.** `CrawlResult.stats` is the
crawl loop's own concern: pages fetched, pages failed, bytes moved, how long it took, which
cap (if any) stopped it, and how many of the fetched pages carried no extractable content
(`pages_empty_content`, added by PER-177). None of that is specific to *persistence* — a
caller that only wanted to log a crawl's numbers would want exactly that dict and nothing
more. `links_emitted` and `version` are persistence-shaped concerns instead: `links_emitted`
only exists because `runs.stats` is a place a UI reads from, and `version` only exists because
a stored jsonb value outlives the code that wrote it. Building the persisted shape here, one
call site away from the write path, keeps `crawler.py`'s dict exactly as wide as the crawl
loop's own job and gives PER-159's crawl loop nothing new to keep in sync with this ticket's
writer.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final


logger = logging.getLogger(__name__)

RUN_STATS_VERSION: Final = 2
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
bullet per fetched page — so this bump is solely about the new key, not a redefinition of an
existing one; the version still exists for the same reason it did at 1: a future reader needs
`version` to know which SHAPE — not just which `links_emitted` semantics — a given row was
written under, because a row from before this milestone and one from after otherwise carry a
silently different set of keys behind the same schema-less jsonb column."""


def build_run_stats(crawl_stats: Mapping[str, Any], *, links_emitted: int) -> dict[str, Any]:
    """Combine `crawl_stats` (from `CrawlResult.stats`) with `links_emitted` and `version`
    into the exact `dict` `runs.stats` stores.

    Args:
        crawl_stats: `CrawlResult.stats` — `pages_crawled`, `pages_failed`, `bytes_fetched`,
            `duration_ms`, `cap_hit`, and `pages_empty_content`. Passed through unchanged and
            unre-derived: in particular, `cap_hit` is never recomputed here. The crawl loop is
            the only code that knows which cap (if any) actually stopped a run — recomputing
            that downstream from partial information would risk disagreeing with the very
            component that decided it.
        links_emitted: The number of links the generated `llms.txt` artifact lists. Today
            `generate_llms_txt` (`internals/llms_txt.py`, THE STUB SEAM) emits exactly one
            bullet per fetched page, so this equals `crawl_stats["pages_crawled"]` — but it
            is recorded as its own key, not folded into or derived from `pages_crawled` at
            read time, because the two are only equal by virtue of today's placeholder
            implementation. The day real extraction and ranking land, a generated artifact
            will list some subset (or reordering, or dedupe) of the pages fetched, and
            `pages_crawled` and `links_emitted` will diverge; `version: 2` (today's value —
            see `RUN_STATS_VERSION`) on a row written before that day is what tells a future
            reader "this row's `links_emitted` was computed under the one-bullet-per-page
            rule," rather than leaving it to guess from the row's age. Do not add link
            extraction here or anywhere upstream of it to make this number "more accurate"
            (CLAUDE.md #9) — it is exactly as accurate as the artifact it describes.

    Returns:
        `{**crawl_stats, "links_emitted": links_emitted, "version": RUN_STATS_VERSION}`.
        `crawl_stats`'s own keys come first and are never overwritten by the two added here,
        because neither `"links_emitted"` nor `"version"` is a key `CrawlResult.stats` has
        ever produced.
    """
    stats = {**crawl_stats, "links_emitted": links_emitted, "version": RUN_STATS_VERSION}
    # "Generation complete, with stats" — passed as `extra=stats` rather than folded into the
    # message text, so a `jq` filter can select on any of `pages_crawled`, `bytes_fetched`,
    # `cap_hit`, etc. directly instead of parsing them back out of a rendered string.
    logger.debug("crawl: generation complete", extra=stats)
    return stats
