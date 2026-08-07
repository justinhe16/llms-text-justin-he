"""Model-assisted per-page summarization: a written title and description for every crawled
page, in place of what `internals/extract.py` pulled out of the page's own markup.

**This is the model-assisted layer ABOVE the seam, not a change to the seam itself**
(ARCHITECTURE.md §11, §3.4). `internals/llms_txt.py`'s `generate_llms_txt` and
`generate_llms_full_txt` still read `CrawledPage.title`/`CrawledPage.description` exactly as
before and still know nothing about a model; what this module does is decide, BEFORE that
seam is ever called, whether the `CrawledPage`s it is handed still carry trafilatura's own
title and description or a model's. It is flag-gated on `settings.crawl_enrich_with_llm`
(default OFF), all-or-nothing rather than gap-filling (see `Settings.crawl_enrich_with_llm`'s
own docstring for why there is no third mode), and degrades to the deterministic artifact
`internals/llms_txt.py` has always produced the moment either condition fails to hold.

**`enrich_pages` never raises for an API reason, and nothing it does can fail a run.** Every
exception this module's own request loop can produce — a bad response shape, a malformed or
`MAX_TOKENS`-truncated completion, a rate limit the SDK's own retries did not clear, a network
failure — is caught per page and turned into "this page keeps its extracted metadata," never
into an exception that reaches `CrawlService.execute_run`. `CrawlService` wraps the call to
this module in its own `try/except Exception` anyway (see that method's own comment for why
that is belt-and-braces rather than redundant), but the contract starts here: a model that is
down, rate-limited, or wrong is exactly the situation this feature exists to survive.

**Reads no `is_empty` flag, and that is deliberate.** A page is skipped from THIS module's
request loop when its truncated markdown is empty after `.strip()` — because the Anthropic
Messages API rejects an empty text block in a request, which is a PRECONDITION of making a
request at all, not a judgement about the page's content. `CrawledPage.is_empty` is a
*different*, extraction-level signal (`internals/extract.py`'s `MIN_BODY_CHARS` threshold),
and ARCHITECTURE.md §3.4 is explicit that `generate_llms_txt` is "the ONE place in the
codebase that branches on that flag" — reading it here, even as a shortcut that would usually
agree with the emptiness check above, would make that sentence false.
`tests/test_crawl_enrich.py` constructs pages where the two disagree in both directions to
keep this honest.

**Why concurrent `messages.create` calls, and not the Message Batches API — read this before
"optimizing" this module to use it.** The Batch API is roughly half the per-token price of
ordinary requests, which looks like the obvious choice for exactly the workload this module
has: many small, independent, non-interactive completions. It is wrong for this codebase's
own reason, not a general one: Anthropic's own documentation gives batches up to 24 hours to
complete, and most complete "in less than 1 hour" — a *typical* case this module cannot rely
on, because arq's `job_timeout` is 600 seconds (`app/worker/policy.py`) and `crawl_site`'s own
wall-clock cap is 300 seconds (`Settings.crawl_max_wall_clock_s`) — both dramatically smaller
than even the FAST case for a batch, let alone the documented ceiling. A job that submitted a
batch and then polled `batches.retrieve()` waiting for `processing_status == "ended"` would be
cancelled by arq's own timeout long before a batch could plausibly return; `CrawlService`
would hand the run back to `pending` on that cancellation, and the retry would walk into
exactly the same wall next attempt. Making the Batch API work here would mean splitting one
run across two separate jobs (submit, then a later job that polls and finishes it) and
reworking the stuck-run reaper's staleness clock to account for a run that is intentionally
`processing` for up to a day — a different milestone than "add summarization to a run that
otherwise completes in tens of seconds." The ordinary Messages API, called concurrently under
`ENRICH_WALL_CLOCK_S` below, finishes within the same job that already fetched the pages. The
cost difference this trades away is small in absolute terms: at roughly 2,000-4,000 input
tokens and roughly 40 output tokens per page (`MAX_TOKENS` below, and `crawl_enrich_max_chars`
in `Settings`), a 100-page run costs on the order of $0.30 at `claude-haiku-4-5`'s per-token
pricing — not a number worth a two-job redesign to halve.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Final

import anthropic
from anthropic import AsyncAnthropic

from app.core.settings import Settings
from app.features.crawl.schemas import CrawledPage


logger = logging.getLogger(__name__)

MODEL: Final = "claude-haiku-4-5"
"""The one model this feature is allowed to call — see CLAUDE.md's "OUT OF SCOPE" list. Not
read from `Settings`: swapping models is a product decision with cost and quality
consequences of its own, not an environment-specific tuning knob."""

MAX_TOKENS: Final = 100
"""Generous for a 3-4 word title plus a 9-10 word description — real completions run closer
to 30-50 output tokens — with margin left over for the JSON structure `_SUMMARY_SCHEMA`
wraps them in. Sized to make a `MAX_TOKENS`-truncated completion (and therefore a malformed,
unparseable one — see `_parse_summary`) a rare edge case this module still has to survive
rather than the common one."""

TEMPERATURE: Final = 0.3
"""Low, not zero: a title and description are closer to extraction than to creative writing,
so this favours the model's most likely reading of the page over variety between runs, while
leaving enough room that two structurally similar pages do not read as copy-pasted. `Sampling`
parameters like this are still accepted on `claude-haiku-4-5` — they are only removed on
newer model families this codebase does not call (Opus 4.7+, Sonnet 5, and later)."""

PROMPT: Final = (
    "Generate a 9-10 word description and a 3-4 word title of the entire page based on ALL "
    "the content."
)
"""Pinned VERBATIM from Firecrawl's own equivalent prompt — DO NOT REWORD this string. It is
copied rather than rewritten because it is a known-good prompt for exactly this shape of
output on exactly this class of input (a crawled page's extracted content), and a
paraphrase that reads as equivalent to a human is not guaranteed to be equivalent to a model
tuned against the original wording. Sent as `system=`, not folded into the user turn — see
`enrich_pages` for why."""

_SUMMARY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["title", "description"],
    "additionalProperties": False,
}
"""The structured-output schema handed to `output_config={"format": {...}}`. Pins SHAPE only
— two required string properties, nothing else — because Claude's structured outputs do not
support `minLength`/`maxLength` on a string property at all; the WORD counts `PROMPT` asks
for live entirely in the prompt text, not in this schema, and are never independently
enforced here. Length is still bounded downstream regardless of what the model returns:
`internals/llms_txt.py`'s `_clean` cuts any title or description at `MAX_TEXT_CHARS` (500
characters) before it reaches an artifact, and `MAX_TOKENS` above already puts a hard ceiling
on how much text a response can contain in the first place."""

ENRICH_WALL_CLOCK_S: Final = 90.0
"""The whole-phase budget for one run's enrichment pass — `enrich_pages` wraps its entire
`asyncio.gather` in `asyncio.timeout(ENRICH_WALL_CLOCK_S)`, distinct from
`ANTHROPIC_REQUEST_TIMEOUT_S` (`anthropic_client.py`), which bounds one request.

Sized against the same budget arithmetic that constant's own docstring gives: enrichment runs
after `crawl_site`'s own 300-second wall-clock cap and before the Storage upload's 120-second
budget, all inside arq's 600-second `job_timeout` — call it roughly 600 - 300 - 120 = 180
seconds of genuinely free room in the worst case, once the crawl and the upload have both
taken their full allowance. 90 seconds leaves that worst case a wide margin rather than
consuming all of it, because this feature is explicitly allowed to give up early and fall
back (see the module docstring's "never raises" paragraph) — spending the crawl's remaining
budget chasing full coverage would trade a small quality gain for the very failure mode
(a job that runs out of time) this constant exists to avoid becoming."""


@dataclass(frozen=True, slots=True)
class PageSummary:
    """One page's model-written title and description — `enrich_pages`' unit of output, and
    exactly what `apply_summaries` copies onto a `CrawledPage`."""

    title: str
    description: str


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """What `enrich_pages` hands back — always, whether every page succeeded or not.

    **Deviates from the ticket's own signature, which asked for a bare
    `dict[str, PageSummary]`.** A plain dict cannot carry the token counters acceptance
    criterion 9 requires (`enrich_input_tokens`/`enrich_output_tokens` in `runs.stats`,
    `internals/run_stats.py`'s version-5 keys) or `failures`, which `internals/run_stats.py`
    needs to tell "the flag is off" apart from "every call failed" when `pages_enriched` is
    0 (see that module's own version-5 docstring). Shaped like this codebase's existing
    `CrawlResult`/`DiscoveryResult`/`SelectionResult` — a small frozen result type per phase,
    rather than a bare collection — for exactly the same reason those three exist as their
    own types instead of a tuple or a dict.
    """

    summaries: dict[str, PageSummary]
    """Keyed by `CrawledPage.url`. Only pages that were actually summarized appear here — a
    page skipped for empty markdown, or one whose request failed, is simply absent, never
    present with a placeholder value."""

    failures: int
    """How many pages had a request attempted and got back something this module could not
    use — an SDK exception, a malformed or truncated response, an empty/missing title or
    description after parsing.

    **Does NOT count a page skipped for empty markdown.** Skipping such a page is not a
    failure to summarize it; there is no text to summarize, and no request was ever made
    (see the module docstring's "reads no `is_empty` flag" paragraph). Counting it here would
    conflate "the model failed" with "there was nothing to ask the model about," which is
    exactly the ambiguity `internals/run_stats.py`'s version-5 docstring says this counter
    exists to resolve."""

    input_tokens: int
    """Summed `response.usage.input_tokens` across every page that was actually sent —
    `runs.stats["enrich_input_tokens"]`, the readable half of this feature's per-run cost."""

    output_tokens: int
    """Summed `response.usage.output_tokens` across every page that was actually sent —
    `runs.stats["enrich_output_tokens"]`, the other half of this feature's per-run cost."""


def _parse_summary(response: Any) -> PageSummary:
    """Turn one `messages.create` response into a `PageSummary`, or raise.

    Every `raise` below is caught by `enrich_pages`' per-page `try/except` and turned into a
    fallback to that page's extracted metadata — nothing here needs its own recovery logic,
    only a clear reason to give up parsing this particular response.

    The text block is found with the two-argument form of `next()` — `next((... for ...),
    None)` — rather than a bare `next(...)`, because a bare `next()` finding nothing raises
    `StopIteration`, and `StopIteration` escaping a coroutine is its own hazard distinct from
    "this response had no usable text," worth avoiding on its own rather than folding into
    the `except` clause one layer up.
    """
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise ValueError("no text block in the response")
    parsed = json.loads(text)
    title = parsed["title"]
    description = parsed["description"]
    if not isinstance(title, str) or not isinstance(description, str):
        raise TypeError("title/description were not both strings")
    title = title.strip()
    description = description.strip()
    if not title or not description:
        raise ValueError("title/description were empty after stripping")
    return PageSummary(title=title, description=description)


async def enrich_pages(
    client: AsyncAnthropic, pages: list[CrawledPage], *, settings: Settings
) -> EnrichmentResult:
    """Ask `claude-haiku-4-5` for a title and description for every page in `pages` with
    extractable, non-empty (post-truncation) markdown, with up to
    `settings.crawl_enrich_concurrency` requests in flight at once, for at most
    `ENRICH_WALL_CLOCK_S` in total.

    Mirrors `internals/crawler.py`'s `crawl_site` semaphore-and-`nonlocal` shape: one
    semaphore bounding concurrent requests, one shared mutable accumulator (`summaries`) and
    a handful of `nonlocal` counters that every task writes into directly rather than
    returning a value for `asyncio.gather` to collect — see the `asyncio.timeout` note below
    for why that choice specifically matters here.

    Args:
        client: The shared `AsyncAnthropic` (`anthropic_client.py`'s `build_anthropic_client`,
            built once per worker process and only when `settings.crawl_enrich_with_llm` is
            `True`).
        pages: Every page this run fetched, in whatever order `crawl_site` collected them —
            this function makes no ordering promise of its own and `apply_summaries` below
            preserves whatever order it is given.
        settings: Read for `crawl_enrich_concurrency` and `crawl_enrich_max_chars`.

    Returns:
        An `EnrichmentResult`. Never raises: a total wall-clock timeout, an SDK error on
        every page, or any mixture of successes and failures all produce a normal return —
        see the module docstring for why nothing here may fail the run it is trying to help.
    """
    semaphore = asyncio.Semaphore(settings.crawl_enrich_concurrency)
    summaries: dict[str, PageSummary] = {}
    failures = 0
    input_tokens = 0
    output_tokens = 0

    async def _enrich_one(page: CrawledPage) -> None:
        nonlocal failures, input_tokens, output_tokens

        # Strip THEN cut, so a long page sends exactly `crawl_enrich_max_chars` characters of
        # its actual content rather than of a whitespace-padded prefix.
        text = page.markdown.strip()[: settings.crawl_enrich_max_chars]
        if not text:
            # Not a branch on `CrawledPage.is_empty` — see the module docstring's "reads no
            # `is_empty` flag" paragraph. Returns without ever touching the semaphore, so a
            # page with nothing to send never holds a concurrency slot another page could be
            # using for a real request.
            return

        async with semaphore:
            try:
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    temperature=TEMPERATURE,
                    system=PROMPT,
                    messages=[{"role": "user", "content": text}],
                    output_config={"format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}},
                )
                summary = _parse_summary(response)
            except (anthropic.APIError, ValueError, KeyError, IndexError, TypeError) as exc:
                # Never `str(exc)` and never `exc_info=True`. An SDK error's own message
                # routinely echoes the request that produced it — which, here, IS a crawled
                # page's content — and `fly logs` is a stream every signed-in user's crawl
                # targets share (ARCHITECTURE.md §3.8, §9.4). `type(exc).__name__` and the
                # URL are enough to debug this from the log; the page text itself never is.
                failures += 1
                logger.warning(
                    "crawl: enrichment failed for a page; falling back to its extracted "
                    "metadata (%s)",
                    type(exc).__name__,
                    extra={"url": page.url},
                )
                return

            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            summaries[page.url] = summary

    try:
        async with asyncio.timeout(ENRICH_WALL_CLOCK_S):
            await asyncio.gather(*(_enrich_one(page) for page in pages))
    except TimeoutError:
        # PARTIAL RESULTS ARE KEPT — this is exactly why `summaries` and the three counters
        # above are accumulated into shared, `nonlocal` state rather than collected from
        # `asyncio.gather`'s own return value: a timeout here cancels whichever `_enrich_one`
        # calls had not yet finished, but every one that already wrote into `summaries`
        # before the deadline keeps its result. A run that got through 80 of 100 pages before
        # running out of wall-clock time ships 80 model-written entries and falls back to
        # extraction for the other 20, rather than discarding all 80 because the last 20
        # never finished.
        logger.warning(
            "crawl: enrichment hit its wall-clock budget; keeping whatever finished",
            extra={
                "pages": len(pages),
                "summarized": len(summaries),
                "failures": failures,
            },
        )

    return EnrichmentResult(
        summaries=summaries,
        failures=failures,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def apply_summaries(
    pages: list[CrawledPage], summaries: dict[str, PageSummary]
) -> list[CrawledPage]:
    """Return a NEW list of pages, with every page present in `summaries` carrying the
    model's title and description in place of its extracted ones.

    `CrawledPage` is a frozen dataclass, so a page cannot be edited in place —
    `dataclasses.replace` builds a new instance for each one that has a summary, and this
    function returns a new list rather than mutating `pages`. That is what lets
    `CrawlService.execute_run` keep the ORIGINAL `pages` list (with extraction's own titles
    and descriptions) for the archived payload while passing this function's return value —
    named `artifact_pages` at that call site — to `internals/llms_txt.py`; see that method's
    own comment for why the two must stay distinct.

    Written as an explicit loop rather than a comprehension with a walrus assignment, so the
    "replace only if summarized, else keep as-is" branch reads as a plain `if`/`else` instead
    of a `summaries.get(page.url) and dataclasses.replace(...) or page`-shaped expression that
    would be easy to get subtly wrong (a summary with an empty title, however unreachable in
    practice, would be falsy and silently fall through to `page`).

    Order is preserved exactly — this function does not sort, and callers must not read a
    reordering into anything downstream that iterates `pages` and this return value together.
    """
    result: list[CrawledPage] = []
    for page in pages:
        summary = summaries.get(page.url)
        if summary is None:
            result.append(page)
        else:
            result.append(replace(page, title=summary.title, description=summary.description))
    return result
