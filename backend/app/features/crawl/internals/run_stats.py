"""Building the `dict` `runs.stats` stores — one owner, so the persisted shape never drifts.

Pure, feature-owned, no I/O, the same category as `internals/payload.py` beside it.
`CrawlService.execute_run` is the only caller: it has both a `CrawlResult` (from
`internals/crawler.py`) and a fact the crawl loop cannot know about itself — how many links
the generated artifact actually lists — and this module is where the two are combined into
the one `dict` `RunService.record_success`/`record_failure` persist as jsonb.

**Why this lives here and not inside `internals/crawler.py`.** `CrawlResult.stats` is the
crawl loop's own concern: pages fetched, pages failed, bytes moved, how long it took, which
cap (if any) stopped it. None of that is specific to *persistence* — a caller that only
wanted to log a crawl's numbers would want exactly that dict and nothing more. `links_emitted`
and `version` are persistence-shaped concerns instead: `links_emitted` only exists because
`runs.stats` is a place a UI reads from, and `version` only exists because a stored jsonb
value outlives the code that wrote it. Building the persisted shape here, one call site away
from the write path, keeps `crawler.py`'s dict exactly as wide as the crawl loop's own job and
gives PER-159's crawl loop nothing new to keep in sync with this ticket's writer.
"""

import logging
from collections.abc import Mapping
from typing import Any, Final


logger = logging.getLogger(__name__)

RUN_STATS_VERSION: Final = 1
"""Which definition of `links_emitted` (and, by extension, of this whole dict's shape) a
stored row was written under. `links_emitted` means "one bullet per fetched page" today —
see its own docstring below — and that definition changes the moment real link extraction
and ranking land (ARCHITECTURE.md §3.4). A future reader of an old row needs `version` to
know which meaning it is looking at; without it, a row written before that milestone and one
written after would carry the same key with two silently different meanings."""


def build_run_stats(crawl_stats: Mapping[str, Any], *, links_emitted: int) -> dict[str, Any]:
    """Combine `crawl_stats` (from `CrawlResult.stats`) with `links_emitted` and `version`
    into the exact `dict` `runs.stats` stores.

    Args:
        crawl_stats: `CrawlResult.stats` — `pages_crawled`, `pages_failed`, `bytes_fetched`,
            `duration_ms`, and `cap_hit`. Passed through unchanged and unre-derived: in
            particular, `cap_hit` is never recomputed here. The crawl loop is the only code
            that knows which cap (if any) actually stopped a run — recomputing that
            downstream from partial information would risk disagreeing with the very
            component that decided it.
        links_emitted: The number of links the generated `llms.txt` artifact lists. Today
            `generate_llms_txt` (`internals/llms_txt.py`, THE STUB SEAM) emits exactly one
            bullet per fetched page, so this equals `crawl_stats["pages_crawled"]` — but it
            is recorded as its own key, not folded into or derived from `pages_crawled` at
            read time, because the two are only equal by virtue of today's placeholder
            implementation. The day real extraction and ranking land, a generated artifact
            will list some subset (or reordering, or dedupe) of the pages fetched, and
            `pages_crawled` and `links_emitted` will diverge; `version: 1` on a row written
            before that day is what tells a future reader "this row's `links_emitted` was
            computed under the one-bullet-per-page rule," rather than leaving it to guess
            from the row's age. Do not add link extraction here or anywhere upstream of it
            to make this number "more accurate" (CLAUDE.md #9) — it is exactly as accurate as
            the artifact it describes.

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
