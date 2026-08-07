"""Tests for `app.features.crawl.internals.enrich` — the flag-gated, model-assisted
per-page summarization pass PER-180 adds above `internals/llms_txt.py`'s seam.

Pure unit suite: no database, and no real socket. `conftest.FakeAnthropic` exposes only
`.messages.create(**kwargs)`, the one call this module makes, and the autouse
`_forbid_real_network` fixture (`tests/conftest.py`) would fail loudly if anything here
somehow reached a real `httpx` transport — `AsyncAnthropic` itself sits on the same
transport class that fixture patches, so an accidental real client is not a silent risk.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx
import pytest
from conftest import (
    FakeAnthropic,
    FakeAnthropicResponse,
    FakeAnthropicTextBlock,
    FakeAnthropicUsage,
    _JsonLogCapture,
    fake_summary_response,
)

from app.core.settings import Settings
from app.features.crawl.internals import enrich
from app.features.crawl.internals.enrich import (
    _SUMMARY_SCHEMA,
    MAX_TOKENS,
    MODEL,
    PROMPT,
    TEMPERATURE,
    PageSummary,
    apply_summaries,
    enrich_pages,
)
from app.features.crawl.schemas import CrawledPage


def _settings(**overrides: Any) -> Settings:
    """Build `Settings` from explicit values only, the same shape `tests/test_settings.py`'s
    own `_settings()` helper uses — the four unconditional variables plus PER-180's four,
    defaulted to a flag-on, fully-configured shape since every test in this file is
    exercising `enrich_pages` directly rather than the flag guard around it (that guard is
    `tests/test_run_persistence.py`'s and `CrawlService`'s job, not this module's)."""
    values: dict[str, Any] = {
        "database_url": "postgresql://localhost:5432/llms_text",
        "redis_url": "redis://localhost:6379/0",
        "supabase_url": "https://example.supabase.co",
        "supabase_secret_key": "placeholder",
        "crawl_enrich_with_llm": True,
        "anthropic_api_key": "not-a-real-key",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _page(
    url: str,
    *,
    markdown: str = "",
    is_empty: bool = False,
    title: str | None = "Extracted Title",
    description: str | None = "An extracted description.",
) -> CrawledPage:
    """A `CrawledPage` with everything `enrich_pages`/`apply_summaries` read set explicitly,
    and everything else a plausible, inert default — the same minimal-construction shape
    `tests/test_run_persistence.py`'s own `_page` helper uses."""
    return CrawledPage(
        url=url,
        status=200,
        title=title,
        content="<html>irrelevant to this module</html>",
        fetched_at=datetime.now(UTC),
        content_bytes=len(markdown),
        description=description,
        markdown=markdown,
        is_empty=is_empty,
    )


def _connection_error(message: str) -> anthropic.APIConnectionError:
    """A real `anthropic.APIError` subclass, cheap to construct without a live response —
    `enrich_pages`' `except` clause names the base class, so any subclass exercises the same
    branch. `httpx.Request` needs no real socket to build."""
    return anthropic.APIConnectionError(
        message=message, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


# -----------------------------------------------------------------------------------------
# The request itself: model, params, prompt, and output_config are the pinned ones — and
# `effort` is absent, per the brief's explicit warning that setting it errors on Haiku 4.5.
# -----------------------------------------------------------------------------------------


async def test_the_request_is_pinned_to_the_documented_shape() -> None:
    fake = FakeAnthropic()
    page = _page("https://example.test/pinned", markdown="Some real content for the page.")

    await enrich_pages(fake, [page], settings=_settings())

    assert len(fake.calls) == 1
    kwargs = fake.calls[0]
    assert kwargs["model"] == MODEL == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == MAX_TOKENS == 100
    assert kwargs["temperature"] == TEMPERATURE == 0.3
    assert kwargs["system"] == PROMPT
    assert kwargs["messages"] == [{"role": "user", "content": "Some real content for the page."}]
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}}

    # `effort` exists in the SDK's own `OutputConfigParam` TypedDict but errors on Haiku
    # 4.5 — it must never be set, anywhere in the request.
    assert "effort" not in kwargs["output_config"]
    assert "effort" not in kwargs
    # Haiku 4.5 is pre-4.6; thinking is off by default and this module must not turn it on.
    assert "thinking" not in kwargs


# -----------------------------------------------------------------------------------------
# Truncation: markdown is stripped, THEN cut to `crawl_enrich_max_chars` — so a long page
# sends exactly that many characters of its actual content, never of a whitespace-padded
# prefix.
# -----------------------------------------------------------------------------------------


async def test_markdown_is_stripped_then_truncated_to_the_configured_length() -> None:
    settings = _settings(crawl_enrich_max_chars=10)
    markdown = "   " + ("x" * 50) + "   "
    page = _page("https://example.test/long", markdown=markdown)
    fake = FakeAnthropic()

    await enrich_pages(fake, [page], settings=settings)

    assert len(fake.calls) == 1
    sent = fake.calls[0]["messages"][0]["content"]
    assert sent == "x" * 10
    assert len(sent) == 10


# -----------------------------------------------------------------------------------------
# Concurrency: at most `crawl_enrich_concurrency` requests in flight at once, proven by
# `FakeAnthropic`'s own depth counter around a genuine `await asyncio.sleep(0)` suspension —
# not assumed from the semaphore's existence.
# -----------------------------------------------------------------------------------------


async def test_concurrency_never_exceeds_the_configured_limit() -> None:
    settings = _settings(crawl_enrich_concurrency=3)
    pages = [_page(f"https://example.test/{i}", markdown=f"real content {i}") for i in range(10)]
    fake = FakeAnthropic()

    result = await enrich_pages(fake, pages, settings=settings)

    assert fake.peak_concurrency == 3
    assert result.failures == 0
    assert len(result.summaries) == 10


# -----------------------------------------------------------------------------------------
# Degradation: one page's API error falls back for that page alone; every page failing still
# returns normally; a malformed or MAX_TOKENS-truncated response degrades identically.
# -----------------------------------------------------------------------------------------


async def test_a_single_pages_api_error_falls_back_and_the_others_still_succeed() -> None:
    ok_page = _page("https://example.test/ok", markdown="This page has real content in it.")
    failing_page = _page("https://example.test/fails", markdown="This page fails to summarize.")

    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        if kwargs["messages"][0]["content"] == failing_page.markdown:
            raise _connection_error("connection reset")
        return fake_summary_response("A Title", "A short description of the page.")

    fake = FakeAnthropic(respond=respond)

    result = await enrich_pages(fake, [ok_page, failing_page], settings=_settings())

    assert failing_page.url not in result.summaries
    assert ok_page.url in result.summaries
    assert result.failures == 1


async def test_every_page_failing_returns_normally_with_no_summaries() -> None:
    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        raise _connection_error("the model is unreachable")

    fake = FakeAnthropic(respond=respond)
    pages = [_page(f"https://example.test/{i}", markdown=f"real content {i}") for i in range(3)]

    result = await enrich_pages(fake, pages, settings=_settings())

    assert result.summaries == {}
    assert result.failures == 3


@pytest.mark.parametrize(
    ("case", "malformed"),
    [
        ("no_text_block", FakeAnthropicResponse(content=[], usage=FakeAnthropicUsage(10, 5))),
        (
            "not_json",
            FakeAnthropicResponse(
                content=[FakeAnthropicTextBlock(type="text", text="not json at all")],
                usage=FakeAnthropicUsage(10, 5),
            ),
        ),
        (
            "missing_description",
            FakeAnthropicResponse(
                content=[
                    FakeAnthropicTextBlock(type="text", text=json.dumps({"title": "Only A Title"}))
                ],
                usage=FakeAnthropicUsage(10, 5),
            ),
        ),
        (
            "title_not_a_string",
            FakeAnthropicResponse(
                content=[
                    FakeAnthropicTextBlock(
                        type="text",
                        text=json.dumps({"title": 5, "description": "A description."}),
                    )
                ],
                usage=FakeAnthropicUsage(10, 5),
            ),
        ),
        (
            "blank_title_after_strip",
            FakeAnthropicResponse(
                content=[
                    FakeAnthropicTextBlock(
                        type="text",
                        text=json.dumps({"title": "   ", "description": "A description."}),
                    )
                ],
                usage=FakeAnthropicUsage(10, 5),
            ),
        ),
    ],
)
async def test_a_malformed_or_truncated_response_is_counted_as_a_failure_not_raised(
    case: str, malformed: FakeAnthropicResponse
) -> None:
    """Every shape here is what a `MAX_TOKENS`-truncated completion can plausibly look
    like too — this module makes no distinction between "the model refused" and "the model
    got cut off"; both fail `json.loads` or the shape check and degrade the same way."""
    fake = FakeAnthropic(respond=lambda _kwargs: malformed)
    page = _page("https://example.test/malformed", markdown="Real content for the page.")

    result = await enrich_pages(fake, [page], settings=_settings())

    assert result.summaries == {}, case
    assert result.failures == 1, case


# -----------------------------------------------------------------------------------------
# The whole-phase wall-clock cap. `enrich_pages` wraps its entire `asyncio.gather` in
# `asyncio.timeout(ENRICH_WALL_CLOCK_S)`, and the property that matters when it fires is that
# the pages which already finished are STILL THERE — which is the whole reason `summaries` is
# accumulated into shared `nonlocal` state rather than collected from `gather`'s return value.
# A refactor that innocently switched to `results = await gather(...)` would lose every
# summary on timeout and pass every other test in this file, so this pins the mechanism
# directly rather than trusting the comment that explains it.
# -----------------------------------------------------------------------------------------


class _SlowForMarkedPages(FakeAnthropic):
    """A `FakeAnthropic` whose response is delayed for any page whose markdown contains
    `marker`, so a test can drive some pages past the wall-clock cap while others finish
    inside it. The delay is a real `await`, not a blocking sleep: blocking the event loop
    would stop `asyncio.timeout` from ever firing and the test would hang instead of failing.
    """

    def __init__(self, *, marker: str, delay: float) -> None:
        super().__init__()
        self._marker = marker
        self._delay = delay

    async def _create(self, **kwargs: Any) -> FakeAnthropicResponse:
        if self._marker in kwargs["messages"][0]["content"]:
            await asyncio.sleep(self._delay)
        return await super()._create(**kwargs)


async def test_the_wall_clock_cap_keeps_the_pages_that_already_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(enrich, "ENRICH_WALL_CLOCK_S", 0.05)
    fast = [
        _page(f"https://example.test/fast-{i}", markdown=f"quick content {i}") for i in range(3)
    ]
    slow = _page("https://example.test/slow", markdown="SLOW content that never comes back")
    fake = _SlowForMarkedPages(marker="SLOW", delay=30.0)

    result = await enrich_pages(fake, [*fast, slow], settings=_settings())

    # The cap fired and cancelled the slow page — but did not discard the three that had
    # already written themselves into `summaries`.
    assert {page.url for page in fast} <= result.summaries.keys()
    assert slow.url not in result.summaries
    # A cancelled in-flight request is NOT a per-page API failure: nothing raised inside
    # `_enrich_one`, so `failures` stays at zero and the run's stats say the pass was cut
    # short rather than that the model rejected anything.
    assert result.failures == 0


async def test_the_wall_clock_cap_never_raises_out_of_enrich_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degradation guarantee at the phase level: a pass that runs out of time returns an
    `EnrichmentResult` like any other, so `CrawlService` falls back page by page and the run
    still completes. If `TimeoutError` escaped here, a slow model would fail a crawl that had
    already successfully fetched every page."""
    monkeypatch.setattr(enrich, "ENRICH_WALL_CLOCK_S", 0.05)
    page = _page("https://example.test/slow", markdown="SLOW content that never comes back")
    fake = _SlowForMarkedPages(marker="SLOW", delay=30.0)

    result = await enrich_pages(fake, [page], settings=_settings())

    assert result.summaries == {}
    assert result.failures == 0
    assert result.input_tokens == 0
    assert result.output_tokens == 0


# -----------------------------------------------------------------------------------------
# The `is_empty` flag is never read — only whether the truncated, stripped markdown is
# empty. Two pages that disagree with `is_empty` in both directions pin this at once.
# -----------------------------------------------------------------------------------------


async def test_whether_a_page_is_sent_depends_on_markdown_never_on_is_empty() -> None:
    blank_markdown_not_flagged_empty = _page(
        "https://example.test/blank", markdown="   ", is_empty=False
    )
    real_markdown_flagged_empty = _page(
        "https://example.test/shell",
        markdown="This looks like a JS shell to extraction, but the markdown is real.",
        is_empty=True,
    )

    fake = FakeAnthropic()
    result = await enrich_pages(
        fake,
        [blank_markdown_not_flagged_empty, real_markdown_flagged_empty],
        settings=_settings(),
    )

    # The blank-markdown page was never sent — one call total, for the other page — and it
    # is absent from both the summaries and the failure count: skipping it is not a failure
    # (module docstring's "reads no is_empty flag" paragraph).
    assert len(fake.calls) == 1
    assert blank_markdown_not_flagged_empty.url not in result.summaries
    assert result.failures == 0

    # The is_empty=True page WAS sent, and summarized, because its markdown was real — proof
    # that `is_empty` itself is never consulted, only the text.
    assert real_markdown_flagged_empty.url in result.summaries


# -----------------------------------------------------------------------------------------
# Token accounting: acceptance criterion 9's two counters, summed across every request that
# actually returned a usable response.
# -----------------------------------------------------------------------------------------


async def test_token_counts_are_summed_across_every_successful_page() -> None:
    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        return fake_summary_response(
            "A Title", "A short description.", input_tokens=100, output_tokens=20
        )

    fake = FakeAnthropic(respond=respond)
    pages = [_page(f"https://example.test/{i}", markdown=f"real content {i}") for i in range(3)]

    result = await enrich_pages(fake, pages, settings=_settings())

    assert result.input_tokens == 300
    assert result.output_tokens == 60
    assert len(result.summaries) == 3


async def test_a_failed_pages_tokens_are_not_counted() -> None:
    """A page whose request raises contributes nothing to either token counter — there is no
    `response.usage` to read when `messages.create` never returned one."""

    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        raise _connection_error("no usage to report")

    fake = FakeAnthropic(respond=respond)
    page = _page("https://example.test/fails", markdown="real content")

    result = await enrich_pages(fake, [page], settings=_settings())

    assert result.input_tokens == 0
    assert result.output_tokens == 0


# -----------------------------------------------------------------------------------------
# Secrets/privacy (acceptance criterion 8, and ARCHITECTURE.md §3.8/§9.4): a crawled page's
# content never reaches a log line, even when the underlying SDK error's own message would
# have echoed it back.
# -----------------------------------------------------------------------------------------


async def test_page_content_never_reaches_a_log_line(json_logs: _JsonLogCapture) -> None:
    sentinel = "sentinel-page-markdown-8f3a1c07"
    url = "https://example.test/leaky-error"

    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        # Simulates the real risk `internals/enrich.py`'s own module docstring names: an SDK
        # error's message can echo the request body it failed on, which here IS the page's
        # content.
        raise _connection_error(f"could not process request body containing {sentinel!r}")

    fake = FakeAnthropic(respond=respond)
    page = _page(url, markdown=f"real page content, including the sentinel {sentinel} inline")

    result = await enrich_pages(fake, [page], settings=_settings())

    assert result.failures == 1
    assert sentinel not in json_logs.raw
    assert any(line.get("url") == url for line in json_logs.lines)


# -----------------------------------------------------------------------------------------
# `apply_summaries`: a pure function over `CrawledPage`s and an `EnrichmentResult.summaries`
# dict, tested on its own rather than through `enrich_pages`.
# -----------------------------------------------------------------------------------------


def test_apply_summaries_replaces_only_summarized_pages_and_preserves_order() -> None:
    page_a = _page("https://example.test/a", markdown="a markdown", title="A", description="a desc")
    page_b = _page("https://example.test/b", markdown="b markdown", title="B", description="b desc")
    page_c = _page("https://example.test/c", markdown="c markdown", title="C", description="c desc")
    summaries = {page_b.url: PageSummary(title="Model B", description="Model description of b.")}

    result = apply_summaries([page_a, page_b, page_c], summaries)

    assert [page.url for page in result] == [page_a.url, page_b.url, page_c.url]

    # Untouched pages are the SAME object — no unnecessary `replace()` call for a page with
    # no summary.
    assert result[0] is page_a
    assert result[2] is page_c

    # The summarized page is a NEW object with only title/description replaced.
    assert result[1] is not page_b
    assert result[1].title == "Model B"
    assert result[1].description == "Model description of b."
    assert result[1].url == page_b.url
    assert result[1].markdown == page_b.markdown
    assert result[1].content == page_b.content
    assert result[1].status == page_b.status
    assert result[1].is_empty == page_b.is_empty


def test_apply_summaries_returns_a_new_list_leaving_the_original_untouched() -> None:
    """The frozen-dataclass reason `apply_summaries`' own docstring gives: this must return
    a NEW list for the artifact rather than editing the one the payload archives."""
    page = _page("https://example.test/x", markdown="x markdown", title="X")
    original = [page]
    summaries = {page.url: PageSummary(title="Model X", description="A model description.")}

    result = apply_summaries(original, summaries)

    assert result is not original
    assert original == [page]
    assert original[0].title == "X"
    assert result[0].title == "Model X"
