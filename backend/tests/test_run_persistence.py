"""Tests for `CrawlService.execute_run`'s persistence half (PER-163): upload, then write —
never the other order, and never a path that leaves a run `processing`.

Driven against a real Postgres, the same way `tests/test_crawl_task.py` is, and for the same
reason its module docstring gives: every website here is seeded with a public IP literal
(`8.8.8.8`), never a hostname, so `internals/ssrf.py`'s real `getaddrinfo` call never has
anything to resolve and this suite's autouse `_forbid_real_network` fixture (which only
patches `httpx.AsyncHTTPTransport`) is not asked to stop a DNS lookup it cannot see.

The Storage half is a `conftest.py.FakeStorage` — a structural stand-in for `SupabaseStorage`
that records what it was asked to upload and can be told to fail — rather than a second
`httpx.MockTransport`. `tests/test_storage_client.py` already exercises the real HTTP
request `SupabaseStorage.upload` builds; this file is about what `CrawlService` does with
whatever `.upload()` returns or raises, which needs a fake with the right shape, not a second
copy of that HTTP-layer test.
"""

import gzip
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import anthropic
import httpx
import pytest
from asyncpg import Connection, Pool
from conftest import (
    TEST_USER_A_ID,
    FakeAnthropic,
    FakeAnthropicResponse,
    FakeStorage,
    fake_summary_response,
    seed_run,
    seed_website,
)
from httpx import AsyncClient

from app.core.settings import settings
from app.features.crawl.internals.crawler import CrawlResult
from app.features.crawl.internals.payload import PAYLOAD_CONTENT_TYPE
from app.features.crawl.internals.robots import ALLOW_ALL
from app.features.crawl.internals.sitemap import DiscoveryResult
from app.features.crawl.internals.url_ranking import normalize_url
from app.features.crawl.schemas import CrawledPage
from app.features.crawl.service import TransientCrawlError, build_crawl_service
from app.features.runs.internals.runs_writer import RunsWriter
from app.features.runs.service import RunService
from app.infrastructure.db.transaction import transaction as real_transaction
from app.infrastructure.storage.supabase_storage import StorageUploadError


_NOW = datetime.now(UTC)
_SEED_IP = "8.8.8.8"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def _seed_pending(
    pool: Pool, suffix: str, *, enrich_with_llm: bool = False
) -> tuple[UUID, UUID]:
    website_id = await seed_website(
        pool, TEST_USER_A_ID, f"http://{_SEED_IP}/{suffix}", enrich_with_llm=enrich_with_llm
    )
    run_id = await seed_run(pool, website_id, started_at=_NOW, status="pending")
    return website_id, run_id


async def _seed_pending_clean_origin(pool: Pool, suffix: str) -> tuple[UUID, UUID]:
    """Like `_seed_pending`, but with an `origin` that carries no path component.

    `_seed_pending`'s own origin (`f"http://{_SEED_IP}/{suffix}"`) already has a path — fine
    for tests that never look past `crawl_site`, but `discover_sitemap_urls` builds its probe
    URLs as `website.origin + "/sitemap.xml"`, so a path-bearing origin would put those probes
    at `/{suffix}/sitemap.xml` instead of the clean `/sitemap.xml` this file's PER-176 tests
    below assert against. `website.url` still gets its own distinct path (`suffix`), so the
    seed fetch and the three discovery probes never collide.
    """
    website_id = await seed_website(
        pool, TEST_USER_A_ID, f"http://{_SEED_IP}", url=f"http://{_SEED_IP}/{suffix}"
    )
    run_id = await seed_run(pool, website_id, started_at=_NOW, status="pending")
    return website_id, run_id


async def _execute(
    pool: Pool,
    storage: FakeStorage,
    run_id: UUID,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 1,
    anthropic_client: object | None = None,
) -> object:
    """Run one crawl attempt, with the retry budget already spent by default.

    `max_attempts=1` is deliberate for this file. PER-166 made a transient failure
    (`StorageUploadError`, a connect error on the seed) return its run to `pending` and ask
    for a redelivery instead of writing a terminal row — behaviour this suite is not about
    and which would turn every "…leaves the run failed" test below into a test of the retry
    policy instead of a test of persistence. Setting the budget to one attempt makes every
    failure this file exercises the LAST one, which is exactly the case each of these
    assertions was written for: what gets written when there is nothing left to try.

    The retry path itself is covered in tests/test_crawl_retry.py, and
    `test_no_failure_mode_ever_leaves_a_run_processing` below parametrizes this argument so
    the "no run is ever left processing" invariant is checked on both sides of the budget.

    `anthropic_client` defaults to `None`, matching `build_crawl_service`'s own default —
    every test above the PER-180 section leaves it unset and exercises the module-level
    `settings` object's real, flag-off default, exactly as before this parameter existed. The
    enrichment tests below pass a `conftest.FakeAnthropic` and monkeypatch
    `settings.crawl_enrich_with_llm` to `True` themselves.
    """
    async with _mock_client(handler) as http_client:
        service = build_crawl_service(
            pool, http_client, storage, settings, anthropic_client=anthropic_client
        )
        return await service.execute_run(run_id, max_attempts=max_attempts)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="hello world")


async def test_success_writes_a_completed_row_with_artifact_storage_path_and_stats(
    websites_db: Pool,
) -> None:
    website_id, run_id = await _seed_pending(websites_db, "success")
    storage = FakeStorage()

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["llms_txt"]
    assert row["llms_full_txt"], "both artifacts are written by the same UPDATE (PER-179)"
    assert row["storage_path"] == f"crawl-payloads/{website_id}/{run_id}.jsonl.gz"
    assert row["completed_at"] is not None

    stats = json.loads(row["stats"])
    assert stats["version"] == 8
    assert stats["pages_crawled"] == 1
    assert "cap_hit" in stats
    assert stats["pages_empty_content"] == 1, "the ok_handler's body has no extractable content"
    # `links_emitted` is 0 while `pages_crawled` is 1, and that is the point rather than a
    # regression: `_ok_handler` serves "hello world", which is far short of `MIN_BODY_CHARS`,
    # so extraction marks the single fetched page `is_empty` and the real `generate_llms_txt`
    # omits it from the index. Under the version-2 stub this assertion read `== 1`, because
    # the stub emitted one bullet per fetched page no matter what was on it. This is exactly
    # the divergence `RUN_STATS_VERSION` 3 exists to record.
    assert stats["links_emitted"] == 0
    assert stats["full_txt_truncated"] == 0


async def test_the_uploaded_payload_round_trips_to_the_page_the_mock_transport_served(
    websites_db: Pool,
) -> None:
    website_id, run_id = await _seed_pending(websites_db, "round-trip")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="round trip content")

    await _execute(websites_db, storage, run_id, handler)

    assert len(storage.calls) == 1
    object_path, data, content_type = storage.calls[0]
    assert object_path == f"{website_id}/{run_id}.jsonl.gz"
    assert content_type == PAYLOAD_CONTENT_TYPE

    lines = gzip.decompress(data).decode("utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["content"] == "round trip content"
    assert record["status"] == 200
    assert record["bytes"] == len(b"round trip content")


async def test_upload_failure_leaves_the_run_failed_with_llms_txt_and_storage_path_null(
    websites_db: Pool,
) -> None:
    _website_id, run_id = await _seed_pending(websites_db, "upload-fails")
    storage = FakeStorage(fail=StorageUploadError("Supabase Storage returned 500 for ..."))

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is None

    row = await websites_db.fetchrow(
        "SELECT status, llms_txt, llms_full_txt, storage_path, completed_at, error "
        "FROM runs WHERE id = $1",
        run_id,
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["storage_path"] is None
    assert row["llms_txt"] is None
    assert row["llms_full_txt"] is None
    assert row["completed_at"] is not None
    assert row["error"] == "Could not store this run's output."


async def test_a_db_write_failure_after_a_successful_upload_still_ends_failed(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upload already succeeded by the time the write fails — the object stays uploaded
    (an accepted orphan, ARCHITECTURE.md §11), and the run must still end `failed`, never
    `processing`."""
    _website_id, run_id = await _seed_pending(websites_db, "db-write-fails")
    storage = FakeStorage()

    # Mirrors `RunsWriter.mark_processing_completed`'s real signature, `llms_full_txt`
    # included. Not cosmetic: a fake missing a parameter the caller now passes would raise
    # `TypeError` instead of this `RuntimeError`, and because `execute_run` catches both the
    # same way, the test would still go green while no longer exercising the failure it names.
    async def _raise(
        self: RunsWriter,
        run_id: UUID,
        *,
        llms_txt: str,
        llms_full_txt: str,
        storage_path: str,
        stats: dict[str, object],
    ) -> bool:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(RunsWriter, "mark_processing_completed", _raise)

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is None

    assert len(storage.calls) == 1, "the object must still have been uploaded"

    row = await websites_db.fetchrow("SELECT status FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "failed"


async def test_the_stored_error_never_leaks_exception_internals_on_an_upload_failure(
    websites_db: Pool,
) -> None:
    """A sanitizer that merely happens not to mention anything dangerous in its fixed
    strings would still pass a test built only around those fixed strings. This puts
    dangerous content INSIDE the underlying exception's own message instead, so it only
    passes if `_safe_error_message` actually replaces it rather than including it."""
    _website_id, run_id = await _seed_pending(websites_db, "upload-leaks")
    dangerous_detail = (
        "upstream at internal-db.flycast:5432 refused the connection\n"
        "Traceback (most recent call last): ..."
    )
    storage = FakeStorage(fail=StorageUploadError(dangerous_detail))

    await _execute(websites_db, storage, run_id, _ok_handler)

    row = await websites_db.fetchrow("SELECT error FROM runs WHERE id = $1", run_id)
    assert row is not None
    error = row["error"]
    assert error is not None
    assert "internal-db.flycast" not in error
    assert "Traceback" not in error
    assert dangerous_detail not in error


async def test_cap_hit_from_the_crawl_result_lands_in_the_stored_stats(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CrawlService` always calls `crawl_site` with an empty frontier, so monkeypatching
    `crawl_site` itself is the only deterministic way to exercise a cap end to end.

    PER-176 added a second network call ahead of `crawl_site` — sitemap discovery — that
    monkeypatching `crawl_site` alone does not stop: `discover_sitemap_urls` would make three
    real requests through this test's own transport before `crawl_site` is ever reached, and
    every one of them would hit `handler`'s `AssertionError` below. Left unmonkeypatched, that
    `AssertionError` is not even a visible test failure — it is an ordinary `Exception` from
    `discover_sitemap_urls`'s point of view, caught and logged at WARNING like any other
    fetch failure (`internals/sitemap.py`), so the assertion this test's `handler` exists to
    make would silently stop proving anything. `discover_sitemap_urls` is therefore
    monkeypatched too, returning an empty result, so `handler`'s assertion stays load-bearing.
    """
    _website_id, run_id = await _seed_pending(websites_db, "cap-hit")
    storage = FakeStorage()

    page = CrawledPage(
        url=f"http://{_SEED_IP}/cap-hit",
        status=200,
        title=None,
        content="x",
        fetched_at=datetime.now(UTC),
        content_bytes=1,
        description=None,
        markdown="",
        is_empty=True,
    )
    fake_result = CrawlResult(
        pages=[page],
        stats={
            "pages_crawled": 1,
            "pages_failed": 0,
            "bytes_fetched": 1,
            "duration_ms": 1,
            "cap_hit": "pages",
            "pages_empty_content": 0,
        },
        cap_hit="pages",
        seed_error=None,
    )

    async def fake_crawl_site(*args: object, **kwargs: object) -> CrawlResult:
        return fake_result

    async def fake_discover_sitemap_urls(*args: object, **kwargs: object) -> DiscoveryResult:
        # `robots=ALLOW_ALL` explicit rather than relied on as `DiscoveryResult`'s default
        # (PER-191) — this fake's whole point is to stand in for a network call, and stating
        # the value makes that intent legible rather than incidental.
        return DiscoveryResult([], "none", ALLOW_ALL)

    monkeypatch.setattr("app.features.crawl.service.crawl_site", fake_crawl_site)
    monkeypatch.setattr(
        "app.features.crawl.service.discover_sitemap_urls", fake_discover_sitemap_urls
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("crawl_site is mocked; no HTTP request should be made")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert json.loads(row["stats"])["cap_hit"] == "pages"


async def test_links_emitted_counts_the_indexed_pages_and_the_full_text_is_persisted(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version-3 divergence, end to end against a real row: two pages fetched, one of them
    with no extractable content, so `pages_crawled` is 2 and `links_emitted` is 1. Monkeypatches
    `crawl_site` for the same reason the cap test above does — it is the only deterministic way
    to hand `execute_run` a page whose markdown is real."""
    _website_id, run_id = await _seed_pending(websites_db, "links-emitted")
    storage = FakeStorage()

    body = (
        "This page carries a body long enough that extraction would not have called it empty, "
        "which is what makes it the one page of the two below that earns a place in the index "
        "and a section in the expansion beside it."
    )

    def _page(path: str, *, markdown: str, is_empty: bool) -> CrawledPage:
        return CrawledPage(
            url=f"http://{_SEED_IP}{path}",
            status=200,
            title="Indexed Page" if not is_empty else None,
            content="x",
            fetched_at=datetime.now(UTC),
            content_bytes=1,
            description=None,
            markdown=markdown,
            is_empty=is_empty,
        )

    fake_result = CrawlResult(
        pages=[
            _page("/docs/real", markdown=body, is_empty=False),
            _page("/docs/shell", markdown="", is_empty=True),
        ],
        stats={
            "pages_crawled": 2,
            "pages_failed": 0,
            "bytes_fetched": 2,
            "duration_ms": 1,
            "cap_hit": None,
            "pages_empty_content": 1,
        },
        cap_hit=None,
        seed_error=None,
    )

    async def fake_crawl_site(*args: object, **kwargs: object) -> CrawlResult:
        return fake_result

    monkeypatch.setattr("app.features.crawl.service.crawl_site", fake_crawl_site)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("crawl_site is mocked; no HTTP request should be made")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow(
        "SELECT llms_txt, llms_full_txt, stats FROM runs WHERE id = $1", run_id
    )
    assert row is not None

    stats = json.loads(row["stats"])
    assert stats["pages_crawled"] == 2
    assert stats["links_emitted"] == 1, "the empty page is fetched and counted, but not listed"
    assert stats["full_txt_truncated"] == 0

    assert "Indexed Page" in row["llms_txt"]
    assert "/docs/shell" not in row["llms_txt"]
    assert body in row["llms_full_txt"], "the expansion inlines the indexed page's markdown"


async def test_partial_stats_survive_an_upload_failure(websites_db: Pool) -> None:
    _website_id, run_id = await _seed_pending(websites_db, "partial-stats")
    storage = FakeStorage(fail=StorageUploadError("boom"))

    await _execute(websites_db, storage, run_id, _ok_handler)

    row = await websites_db.fetchrow("SELECT stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    stats = json.loads(row["stats"])
    assert stats["pages_crawled"] == 1
    assert stats["version"] == 8
    # A failure row carries the same KEYS as a success row, at their hoisted defaults — the
    # shape `runs.stats` stores must not depend on how far a run got before it failed. The
    # four PER-180 counters are part of that same guarantee now: this suite's `_execute`
    # helper builds a `CrawlService` with `crawl_enrich_with_llm` off and no `anthropic_client`
    # (the default `Settings`, unmodified), so the enrichment guard never fires and every one
    # of them stays at its hoisted 0 exactly like `links_emitted` and `full_txt_truncated` do.
    assert stats["links_emitted"] == 0
    assert stats["full_txt_truncated"] == 0
    assert stats["pages_enriched"] == 0
    assert stats["enrich_failures"] == 0
    assert stats["enrich_input_tokens"] == 0
    assert stats["enrich_output_tokens"] == 0
    # `llms_txt_bytes`/`index_diff` are the ONE pair on this row that is NOT a hoisted
    # default, and that asymmetry is deliberate: `execute_run` computes both right after
    # `generate_llms_txt`, strictly before the Storage upload that fails this attempt — see
    # the module docstring's "PER-193 added a FOURTH call" paragraph. This is a first run for
    # a freshly seeded website, so the diff has nothing to compare against.
    assert stats["llms_txt_bytes"] > 0
    assert stats["index_diff"] == {"state": "first_run", "previous_run_completed": None}


@pytest.mark.parametrize(
    ("mode", "max_attempts", "expected_status"),
    [
        ("succeeds", 1, "completed"),
        # The budget is spent, so every failure below is terminal.
        ("seed_fails", 1, "failed"),
        ("upload_fails", 1, "failed"),
        ("db_write_fails", 1, "failed"),
        # PER-166: the same three failures with budget left. The two RETRYABLE ones end
        # `pending` — back in the queue, which is a state something acts on — and the
        # permanent one still ends `failed` on its first attempt, because a `RuntimeError`
        # from a database write is not something a second try answers differently. The
        # invariant this test is named for holds in every row: never `processing`.
        ("seed_fails", 3, "pending"),
        ("upload_fails", 3, "pending"),
        ("db_write_fails", 3, "failed"),
    ],
)
async def test_no_failure_mode_ever_leaves_a_run_processing(
    websites_db: Pool,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    max_attempts: int,
    expected_status: str,
) -> None:
    _website_id, run_id = await _seed_pending(websites_db, f"no-processing-{mode}-{max_attempts}")
    storage = FakeStorage()
    handler: Callable[[httpx.Request], httpx.Response] = _ok_handler

    if mode == "seed_fails":

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connection failure", request=request)

    elif mode == "upload_fails":
        storage = FakeStorage(fail=StorageUploadError("boom"))
    elif mode == "db_write_fails":

        async def _raise(
            self: RunsWriter,
            run_id: UUID,
            *,
            llms_txt: str,
            storage_path: str,
            stats: dict[str, object],
        ) -> bool:
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(RunsWriter, "mark_processing_completed", _raise)

    # A retryable failure with budget left leaves through `TransientCrawlError` rather than
    # returning — that is how the service asks `crawl_task` for a redelivery. Suppressed
    # here because this test is about the row it left behind, not about the signal.
    with suppress(TransientCrawlError):
        await _execute(websites_db, storage, run_id, handler, max_attempts=max_attempts)

    row = await websites_db.fetchrow("SELECT status FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == expected_status
    assert row["status"] != "processing"


async def test_the_upload_never_happens_inside_a_database_transaction(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ORDERING TEST. Not an index comparison that happens to pass: a real invariant
    over the event list, checked with a transaction-depth counter, so a future change that
    reorders upload-then-write (or nests one inside the other in some subtler way) cannot
    slip past this by coincidence."""
    _website_id, run_id = await _seed_pending(websites_db, "ordering")
    events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def tracking_transaction(pool: Pool) -> AsyncIterator[Connection]:
        events.append(("tx", "enter"))
        try:
            async with real_transaction(pool) as conn:
                yield conn
        finally:
            events.append(("tx", "exit"))

    monkeypatch.setattr("app.features.runs.service.transaction", tracking_transaction)

    class _TrackingStorage(FakeStorage):
        async def upload(self, object_path: str, data: bytes, *, content_type: str) -> str:
            events.append(("upload", "start"))
            try:
                return await super().upload(object_path, data, content_type=content_type)
            finally:
                events.append(("upload", "end"))

    storage = _TrackingStorage()

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is not None

    # A real invariant over the event list: transaction depth, incremented on "tx enter" and
    # decremented on "tx exit", must be exactly 0 at every "upload" event — never merely "the
    # upload came before the LAST transaction", which an accidental reordering elsewhere
    # could satisfy by coincidence.
    depth = 0
    saw_an_upload_event = False
    for kind, phase in events:
        if kind == "tx":
            depth += 1 if phase == "enter" else -1
            assert depth >= 0, "a transaction exited more times than it entered"
        else:
            saw_an_upload_event = True
            assert depth == 0, f"upload {phase!r} happened while a transaction was open"
    assert saw_an_upload_event
    assert depth == 0, "a transaction was left open"


# -----------------------------------------------------------------------------------------
# PER-193: the index diff against the previous completed run, computed inside `execute_run`
# right after this run's own `llms_txt` exists. `tests/test_index_diff.py` pins the pure diff
# function in isolation; what is pinned here is the WIRING — that `execute_run` actually calls
# it with the right previous run, that a read failure degrades to `index_diff: None` rather
# than failing the crawl, and that the read never happens inside a transaction.
# -----------------------------------------------------------------------------------------


def _diff_page(path: str, *, title: str = "Page") -> CrawledPage:
    """A `CrawledPage` with real, non-empty content — shaped so `generate_llms_txt` lists it,
    which is the precondition for it showing up on either side of a diff."""
    return CrawledPage(
        url=f"http://{_SEED_IP}{path}",
        status=200,
        title=title,
        content="x",
        fetched_at=datetime.now(UTC),
        content_bytes=1,
        description=None,
        markdown="Real content, long enough to survive extraction's emptiness check here.",
        is_empty=False,
    )


async def test_a_first_run_stores_a_first_run_diff_block(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A website's very first run has nothing to compare against — `index_diff` records
    `state: "first_run"` and `previous_run_completed: None` (no earlier run of ANY kind),
    never a `"compared"` block padded out with fabricated zeroes."""
    _website_id, run_id = await _seed_pending(websites_db, "diff-first-run")
    storage = FakeStorage()

    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/diff-first-run")],
            stats=_crawl_stats(pages_crawled=1),
            cap_hit=None,
            seed_error=None,
        ),
    )

    outcome = await _execute(websites_db, storage, run_id, _unreachable_handler)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    assert stats["index_diff"] == {"state": "first_run", "previous_run_completed": None}


async def test_a_second_run_diffs_against_the_first_and_records_added_and_removed_pages(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two REAL `execute_run` calls against the same website, serving different page sets —
    the end-to-end version of `tests/test_index_diff.py`'s pure-function coverage. The second
    run's `llms_txt` is diffed against the first's stored `llms_txt`, not against anything
    this test hands it directly."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/diff-second")
    storage = FakeStorage()

    run_id_1 = await seed_run(websites_db, website_id, started_at=_NOW, status="pending")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/docs/a", title="Page A"), _diff_page("/docs/b", title="Page B")],
            stats=_crawl_stats(pages_crawled=2),
            cap_hit=None,
            seed_error=None,
        ),
    )
    outcome_1 = await _execute(websites_db, storage, run_id_1, _unreachable_handler)
    assert outcome_1 is not None

    # Started strictly after run 1. Page A survives unchanged, B is dropped, C is new.
    run_id_2 = await seed_run(
        websites_db, website_id, started_at=_NOW + timedelta(minutes=1), status="pending"
    )
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/docs/a", title="Page A"), _diff_page("/docs/c", title="Page C")],
            stats=_crawl_stats(pages_crawled=2),
            cap_hit=None,
            seed_error=None,
        ),
    )
    outcome_2 = await _execute(websites_db, storage, run_id_2, _unreachable_handler)
    assert outcome_2 is not None

    stats_2 = json.loads(
        await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id_2)
    )
    diff = stats_2["index_diff"]
    assert diff["state"] == "compared"
    assert diff["compared_to_run_id"] == str(run_id_1)
    assert diff["previous_run_completed"] is True
    assert diff["pages_added"] == 1
    assert diff["pages_removed"] == 1
    assert diff["metadata_changed"] == 0
    assert {entry["url"] for entry in diff["added_sample"]} == {f"http://{_SEED_IP}/docs/c"}
    assert {entry["url"] for entry in diff["removed_sample"]} == {f"http://{_SEED_IP}/docs/b"}


async def test_the_diff_skips_a_failed_run_and_compares_against_the_last_completed_one(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completed, then failed, then completed. The third run's `compared_to_run_id` is the
    FIRST run, not the second — and `previous_run_completed` is `False`, because the run
    immediately before this one (the second) did not complete, even though an earlier one
    did."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/diff-skip")
    storage = FakeStorage()

    run_id_1 = await seed_run(websites_db, website_id, started_at=_NOW, status="pending")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/docs/a")],
            stats=_crawl_stats(pages_crawled=1),
            cap_hit=None,
            seed_error=None,
        ),
    )
    outcome_1 = await _execute(websites_db, storage, run_id_1, _unreachable_handler)
    assert outcome_1 is not None

    # A failed run in between, seeded directly rather than executed — its own `llms_txt`
    # is `NULL`, exactly as `RunsWriter.mark_processing_failed` leaves it.
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW + timedelta(minutes=1),
        status="failed",
        completed_at=_NOW + timedelta(minutes=1),
        error="simulated failure",
    )

    run_id_3 = await seed_run(
        websites_db, website_id, started_at=_NOW + timedelta(minutes=2), status="pending"
    )
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/docs/a")],
            stats=_crawl_stats(pages_crawled=1),
            cap_hit=None,
            seed_error=None,
        ),
    )
    outcome_3 = await _execute(websites_db, storage, run_id_3, _unreachable_handler)
    assert outcome_3 is not None

    stats_3 = json.loads(
        await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id_3)
    )
    diff = stats_3["index_diff"]
    assert diff["state"] == "compared"
    assert diff["compared_to_run_id"] == str(run_id_1)
    assert diff["previous_run_completed"] is False


async def test_a_previous_run_read_failure_does_not_fail_the_crawl(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RunService.get_previous_completed_index` raising must not fail an otherwise-successful
    crawl — the run still completes, with `index_diff` recorded as `None` rather than the
    exception propagating out of `execute_run`."""
    _website_id, run_id = await _seed_pending(websites_db, "diff-read-fails")
    storage = FakeStorage()

    async def _raise(self: RunService, *args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(RunService, "get_previous_completed_index", _raise)

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["index_diff"] is None
    # The read failure must not have suppressed the index itself — only the COMPARISON.
    assert stats["llms_txt_bytes"] > 0


async def test_the_previous_run_read_never_happens_inside_a_database_transaction(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `test_the_upload_never_happens_inside_a_database_transaction` and
    `test_the_anthropic_call_never_happens_inside_a_database_transaction` above exactly — the
    transaction-boundary acceptance criterion for the PER-193 diff read: no transaction may be
    open while `RunService.get_previous_completed_index` runs."""
    _website_id, run_id = await _seed_pending(websites_db, "diff-no-tx")
    storage = FakeStorage()

    events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def tracking_transaction(pool: Pool) -> AsyncIterator[Connection]:
        events.append(("tx", "enter"))
        try:
            async with real_transaction(pool) as conn:
                yield conn
        finally:
            events.append(("tx", "exit"))

    monkeypatch.setattr("app.features.runs.service.transaction", tracking_transaction)

    original_get_previous_completed_index = RunService.get_previous_completed_index

    async def tracking_get_previous_completed_index(
        self: RunService, *args: object, **kwargs: object
    ) -> object:
        events.append(("previous_run_read", "start"))
        try:
            return await original_get_previous_completed_index(self, *args, **kwargs)
        finally:
            events.append(("previous_run_read", "end"))

    monkeypatch.setattr(
        RunService, "get_previous_completed_index", tracking_get_previous_completed_index
    )

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is not None

    depth = 0
    saw_read = False
    for kind, phase in events:
        if kind == "tx":
            depth += 1 if phase == "enter" else -1
            assert depth >= 0, "a transaction exited more times than it entered"
        else:
            saw_read = True
            assert depth == 0, f"{kind} {phase!r} happened while a transaction was open"
    assert saw_read, "the previous-run read never happened at all"
    assert depth == 0, "a transaction was left open"


async def test_llms_txt_bytes_matches_the_stored_artifact(websites_db: Pool) -> None:
    _website_id, run_id = await _seed_pending(websites_db, "llms-txt-bytes")
    storage = FakeStorage()

    outcome = await _execute(websites_db, storage, run_id, _ok_handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT llms_txt, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    stats = json.loads(row["stats"])
    assert stats["llms_txt_bytes"] == len(row["llms_txt"].encode())


# -----------------------------------------------------------------------------------------
# PER-176: sitemap discovery, wired ahead of `crawl_site` in `execute_run`. Each test below
# drives the real `discover_sitemap_urls` (unlike `test_cap_hit_...` above, which
# monkeypatches it out) through the same `httpx.MockTransport` the seed fetch shares, and
# asserts on the run row PERSISTENCE end of it — `tests/test_crawl_sitemap.py` is where
# discovery's own algorithm is pinned down in isolation.
# -----------------------------------------------------------------------------------------


async def test_a_site_with_no_sitemap_still_completes_a_single_page_run(
    websites_db: Pool,
) -> None:
    """Criterion 8: a site with no sitemap at all still produces a successful single-page
    run. All three discovery probes — `/sitemap.xml`, `/sitemap_index.xml`, `/robots.txt` —
    404, `discover_sitemap_urls` returns `DiscoveryResult([], "none")`, and the seed alone is
    enough to complete the run — hitting a discovery "cap" (here, finding nothing at all) is
    a success, never a failure (ARCHITECTURE.md §3.4)."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "no-sitemap")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["pages_crawled"] == 1
    assert stats["discovery_source"] == "none"


async def test_an_ssrf_refused_sitemap_target_does_not_fail_the_run(websites_db: Pool) -> None:
    """Criterion 6: a `robots.txt`-declared `Sitemap:` target that the SSRF guard refuses
    (here, one carrying credentials — `internals/ssrf.py`'s check 3) never fails the RUN, only
    discovery. `/sitemap.xml` and `/sitemap_index.xml` both 404, `robots.txt` declares a
    same-origin-but-credentialed target so the refusal is the guard's, not the same-origin
    pre-filter's (see `tests/test_crawl_sitemap.py`'s SSRF suite for the module-level version
    of this same shape), and the run still completes on the seed alone.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "ssrf-robots")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text=f"Sitemap: http://user:pass@{_SEED_IP}/sitemap-target.xml\n"
            )
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["discovery_source"] == "none"


async def test_discovery_counters_and_the_selected_frontier_land_in_the_stored_stats(
    websites_db: Pool,
) -> None:
    """Criterion 10, end to end: a real sitemap served at the seeded origin's `/sitemap.xml`
    is discovered, ranked, and actually fetched — the only test in this file that proves
    `select_urls`' output reaches `crawl_site(extra_urls=...)` for real, via `pages_crawled`
    counting the seed AND all three sitemap-derived pages."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "e2e")
    storage = FakeStorage()

    sitemap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://{_SEED_IP}/e2e/page-1</loc></url>
  <url><loc>http://{_SEED_IP}/e2e/page-2</loc></url>
  <url><loc>http://{_SEED_IP}/e2e/page-3</loc></url>
</urlset>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200, text=sitemap_body, headers={"Content-Type": "application/xml"}
            )
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["discovery_source"] == "sitemap"
    assert stats["urls_discovered"] == 3
    assert stats["urls_selected"] == 3
    assert stats["pages_crawled"] == 4


# -----------------------------------------------------------------------------------------
# PER-178: the depth-1 link-extraction fallback, wired into `execute_run` behind sitemap
# discovery. Each test below drives the real `extract_links` -> `select_urls` -> `crawl_site`
# pipeline through the same `httpx.MockTransport` the seed fetch and the sitemap probes
# share, and asserts on the run row it produces. `tests/test_crawl_links.py` pins the parser
# in isolation and `tests/test_crawler_caps.py` pins the callback contract; what is pinned
# here is WHEN the fallback runs at all, and what `runs.stats` says when it does.
# -----------------------------------------------------------------------------------------


def _links_page(*hrefs: str) -> str:
    """An HTML page linking to each of `hrefs`, padded with enough prose to clear
    `extract.MIN_BODY_CHARS` so the page is not counted as empty — these tests are about the
    frontier, and a page's `is_empty` flag is a different ticket's assertion."""
    links = "".join(f'<a href="{href}">{href}</a>' for href in hrefs)
    prose = "This page documents the configuration options this service accepts. " * 6
    return f"<html><body><nav>{links}</nav><main><p>{prose}</p></main></body></html>"


def _no_sitemap(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """A handler serving 404 for all three sitemap discovery probes and `response` for
    everything else — the shape of a site that ships no sitemap at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        return response

    return handler


async def test_a_site_with_no_sitemap_falls_back_to_the_links_on_its_seed_page(
    websites_db: Pool,
) -> None:
    """The [Fallback ordering] criterion's second half, end to end: all three discovery
    probes 404, so the seed page's own links become the frontier, are fetched, and land in
    `runs.stats` under `discovery_source: "links"`.

    `pages_crawled == 4` — the seed plus its three same-origin links — is what proves the
    extracted URLs genuinely reached `crawl_site`, rather than merely being counted.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "links-fallback")
    storage = FakeStorage()

    seed_html = _links_page(
        "/docs/intro",
        "/docs/config",
        "/docs/deploy",
        "https://other.test/off-origin",
        "mailto:support@acme.test",
        "#on-this-page",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        if request.url.path == "/links-fallback":
            return httpx.Response(200, html=seed_html)
        return httpx.Response(200, html=_links_page())

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["discovery_source"] == "links"
    assert stats["urls_discovered"] == 3, "off-origin, mailto: and fragment-only never count"
    assert stats["urls_selected"] == 3
    assert stats["pages_crawled"] == 4
    assert stats["version"] == 8, (
        "a new VALUE for an existing key is still not a new shape — PER-178 added "
        '`discovery_source: "links"` and deliberately did not bump for it. This row reads 8 '
        "because PER-180, PER-191, PER-193, and PER-194 each added new KEYS after that, which "
        "are shape changes; the number moved for reasons that have nothing to do with the "
        "value asserted above."
    )


async def test_a_site_with_a_sitemap_never_consults_the_links_on_its_seed_page(
    websites_db: Pool,
) -> None:
    """The [Fallback ordering] criterion's first half. The seed page here links to two pages
    that the sitemap does not list; the handler raises if either is ever requested, so this
    goes red on the request itself rather than on a `discovery_source` that merely disagrees.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "sitemap-wins")
    storage = FakeStorage()

    sitemap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://{_SEED_IP}/from-the-sitemap</loc></url>
</urlset>"""
    seed_html = _links_page("/only-in-the-markup", "/also-only-in-the-markup")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200, text=sitemap_body, headers={"Content-Type": "application/xml"}
            )
        if request.url.path.endswith("only-in-the-markup"):
            raise AssertionError(
                f"link extraction ran on a site that has a sitemap: {request.url.path}"
            )
        if request.url.path == "/sitemap-wins":
            return httpx.Response(200, html=seed_html)
        return httpx.Response(200, html=_links_page())

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    assert stats["discovery_source"] == "sitemap"
    assert stats["urls_discovered"] == 1
    assert stats["pages_crawled"] == 2


async def test_a_sitemap_whose_urls_ranking_drops_does_not_fall_back_to_links(
    websites_db: Pool,
) -> None:
    """The fallback's trigger is "sitemap discovery found NOTHING", not "the frontier came out
    empty" — and this is the one case where those two differ.

    The sitemap here lists a single `/tag/` page, which `select_urls` drops under its
    `"taxonomy"` rule, so `extra_urls` is empty even though discovery succeeded. Falling back
    to scraped links here would quietly overrule the site operator's own statement about which
    pages matter; the run stays a single-page one and still reports `discovery_source:
    "sitemap"`, because that IS where its (subsequently emptied) frontier came from.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "all-dropped")
    storage = FakeStorage()

    sitemap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://{_SEED_IP}/tag/release-notes</loc></url>
</urlset>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200, text=sitemap_body, headers={"Content-Type": "application/xml"}
            )
        if request.url.path == "/only-in-the-markup":
            raise AssertionError("the link fallback ran for a site that has a sitemap")
        if request.url.path == "/all-dropped":
            return httpx.Response(200, html=_links_page("/only-in-the-markup"))
        return httpx.Response(200, html=_links_page())

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    assert stats["discovery_source"] == "sitemap"
    assert stats["urls_discovered"] == 1
    assert stats["urls_selected"] == 0
    assert stats["pages_crawled"] == 1


async def test_extracted_links_are_ranked_by_select_urls_before_anything_is_fetched(
    websites_db: Pool,
) -> None:
    """The [Ranking] criterion. Extracted links are candidates, not a frontier: they go
    through the SAME `select_urls` a sitemap's URLs do, so its drop rules apply to them
    unchanged.

    Four of the six same-origin links below are structurally not worth fetching — a `/tag/`
    taxonomy, a dated archive, a feed, and a changelog — and the handler raises if any of them
    is requested. `urls_discovered: 6, urls_selected: 2` is the reconciliation this ticket
    asks for: extraction found six, ranking kept two.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "ranked")
    storage = FakeStorage()

    seed_html = _links_page(
        "/docs/intro",
        "/docs/config",
        "/tag/releases",
        "/blog/2019/hello",
        "/feed.xml",
        "/changelog",
    )
    dropped = ("/tag/releases", "/blog/2019/hello", "/feed.xml", "/changelog")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        if request.url.path in dropped:
            raise AssertionError(f"select_urls should have dropped {request.url.path}")
        if request.url.path == "/ranked":
            return httpx.Response(200, html=seed_html)
        return httpx.Response(200, html=_links_page())

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    assert stats["discovery_source"] == "links"
    assert stats["urls_discovered"] == 6
    assert stats["urls_selected"] == 2
    assert stats["pages_crawled"] == 3


async def test_a_link_derived_frontier_respects_crawl_max_pages(websites_db: Pool) -> None:
    """The [Ranking] criterion's other half: the same `crawl_max_pages` cap, applied to a
    frontier that came off a page rather than out of a sitemap. `select_urls` is handed
    `limit=max_pages - 1` because the seed already spends one of the budget's pages, so a cap
    of three yields the seed plus the two best-ranked links — and `cap_hit` stays `None`,
    because a frontier trimmed by RANKING never reached the loop's own truncation.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "capped")
    storage = FakeStorage()

    seed_html = _links_page(*(f"/docs/page-{i}" for i in range(10)))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        if request.url.path == "/capped":
            return httpx.Response(200, html=seed_html)
        return httpx.Response(200, html=_links_page())

    capped_settings = settings.model_copy(update={"crawl_max_pages": 3})
    async with _mock_client(handler) as http_client:
        service = build_crawl_service(websites_db, http_client, storage, capped_settings)
        outcome = await service.execute_run(run_id, max_attempts=1)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    assert stats["discovery_source"] == "links"
    assert stats["urls_discovered"] == 10
    assert stats["urls_selected"] == 2
    assert stats["pages_crawled"] == 3
    assert stats["cap_hit"] is None


async def test_a_site_whose_seed_page_has_no_links_still_completes_a_single_page_run(
    websites_db: Pool,
) -> None:
    """The [Edge] criterion, end to end. No sitemap and no links is not a failure — it is the
    same successful single-page run a site with no sitemap already produced before this
    fallback existed, and it reports `discovery_source: "none"` rather than `"links"`: the
    same rule `internals/sitemap.py` applies to its own entry points, where a path that ran
    and yielded nothing is not the source of anything.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "no-links")
    storage = FakeStorage()

    handler = _no_sitemap(httpx.Response(200, html=_links_page()))
    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["discovery_source"] == "none"
    assert stats["urls_discovered"] == 0
    assert stats["urls_selected"] == 0
    assert stats["pages_crawled"] == 1


async def test_a_failed_seed_reports_no_discovery_source_even_with_the_fallback_armed(
    websites_db: Pool,
) -> None:
    """The fallback needs a fetched seed page to read, so a run whose seed never landed never
    reaches it — and the hoisted `"none"`/0/0 defaults in `execute_run` fall straight through
    into the failure row's partial stats, with no extra plumbing."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "dead-seed")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        raise httpx.ConnectError("simulated connection failure", request=request)

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "failed"
    stats = json.loads(row["stats"])
    assert stats["discovery_source"] == "none"
    assert stats["urls_discovered"] == 0
    assert stats["pages_crawled"] == 0


async def test_links_found_but_all_ranked_away_still_report_the_links_source(
    websites_db: Pool,
) -> None:
    """The direct mirror of `test_a_sitemap_whose_urls_ranking_drops_does_not_fall_back_to_
    links`, for the fallback path — and the reason `discovery_source` is decided on what
    EXTRACTION found rather than on what ranking kept.

    Every link on this seed page is structurally not worth fetching, so `select_urls` empties
    the frontier and the run is a single-page one. `discovery_source` is still `"links"`,
    exactly as a sitemap whose every URL is dropped still reports `"sitemap"`: the key names
    where the frontier came from, and `urls_discovered`/`urls_selected` are what say how much
    of it survived. Reporting `"none"` here would make a run that found six links
    indistinguishable from one that found a blank page.
    """
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "all-ranked-away")
    storage = FakeStorage()

    seed_html = _links_page("/tag/releases", "/blog/2019/hello", "/feed.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        if request.url.path == "/all-ranked-away":
            return httpx.Response(200, html=seed_html)
        raise AssertionError(f"select_urls should have dropped {request.url.path}")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    assert stats["discovery_source"] == "links"
    assert stats["urls_discovered"] == 3
    assert stats["urls_selected"] == 0
    assert stats["pages_crawled"] == 1


# -----------------------------------------------------------------------------------------
# PER-180: flag-gated, model-assisted per-page summarization
# (`app.features.crawl.internals.enrich`), end to end against a real Postgres row.
#
# **PER-194 turned the gate two-level, and every test below is updated for it.** A run
# enriches only when BOTH `Settings.crawl_enrich_with_llm` (the deployment flag) AND the
# website's own `enrich_with_llm` column are true — so every test in this section that wants
# enrichment to actually run now does two things, not one: `monkeypatch.setattr(settings,
# "crawl_enrich_with_llm", True)` for the deployment half, and `_seed_pending(...,
# enrich_with_llm=True)` for the website's own opt-in. The module-level `settings` object is
# real, so every test above this section that never touches it keeps exercising the actual,
# flag-off default; the new tests at the end of this section — starting with
# `test_a_website_that_did_not_opt_in_is_never_enriched_even_with_the_flag_on` — are what pin
# each half of the gate independently.
#
# `crawl_site` is monkeypatched to hand `execute_run` pre-built `CrawledPage`s, the same
# pattern `test_cap_hit_from_the_crawl_result_lands_in_the_stored_stats` and
# `test_links_emitted_counts_the_indexed_pages_and_the_full_text_is_persisted` above already
# use — it is the only deterministic way to give a page markdown substantial enough to be
# sent to the model, since the real extraction pipeline over a MockTransport's plain-text
# bodies would mark every page `is_empty` and enrichment would never make a request at all.
# `discover_sitemap_urls` is deliberately left unmocked, exactly as those two tests leave it:
# its own probes hit `handler`'s `AssertionError`, which `internals/sitemap.py` catches and
# logs at WARNING as an ordinary fetch failure, never propagating.
# -----------------------------------------------------------------------------------------


def _enrich_page(
    suffix: str,
    *,
    title: str = "Extracted Title",
    description: str = "Extracted description.",
    markdown: str = "Real page content, long enough to be sent to the model for summarization.",
) -> CrawledPage:
    """A `CrawledPage` shaped for the enrichment tests below: real, substantial `markdown` so
    `enrich_pages` never skips it, and an extracted `title`/`description` distinct enough
    from any model-written one that a test can tell which metadata reached the artifact."""
    return CrawledPage(
        url=f"http://{_SEED_IP}/{suffix}",
        status=200,
        title=title,
        content="raw response body",
        fetched_at=datetime.now(UTC),
        content_bytes=1,
        description=description,
        markdown=markdown,
        is_empty=False,
    )


def _crawl_stats(*, pages_crawled: int) -> dict[str, Any]:
    return {
        "pages_crawled": pages_crawled,
        "pages_failed": 0,
        "bytes_fetched": pages_crawled,
        "duration_ms": 1,
        "cap_hit": None,
        "pages_empty_content": 0,
    }


def _mock_crawl_site(monkeypatch: pytest.MonkeyPatch, result: CrawlResult) -> None:
    async def fake_crawl_site(*args: object, **kwargs: object) -> CrawlResult:
        return result

    monkeypatch.setattr("app.features.crawl.service.crawl_site", fake_crawl_site)


def _unreachable_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError("crawl_site is mocked; no HTTP request should be made")


def _always_fails(_kwargs: dict[str, Any]) -> FakeAnthropicResponse:
    raise anthropic.APIConnectionError(
        message="the model is unreachable",
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )


async def test_model_titles_reach_llms_txt_and_the_extracted_title_does_not(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(
        websites_db, "enrich-model-title", enrich_with_llm=True
    )

    page = _enrich_page(
        "enrich-model-title", title="Extracted Title Nobody Should See In The Artifact"
    )
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )
    fake_anthropic = FakeAnthropic(
        respond=lambda _kwargs: fake_summary_response(
            "Model Written Title", "Model written description of the page."
        )
    )

    outcome = await _execute(
        websites_db, FakeStorage(), run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT llms_txt, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert "Model Written Title" in row["llms_txt"]
    assert "Extracted Title Nobody Should See In The Artifact" not in row["llms_txt"]

    stats = json.loads(row["stats"])
    assert stats["version"] == 8
    assert stats["pages_enriched"] == 1
    assert stats["enrich_failures"] == 0


async def test_a_total_enrichment_failure_still_completes_with_the_deterministic_artifact(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criteria 3 and 4: every page's request fails, and the run still ends `completed` with
    an artifact byte-identical to the flag-off run's — never `failed`, and never a
    partially-model-written index."""
    page = _enrich_page("enrich-total-failure")
    crawl_result = CrawlResult(
        pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
    )
    _mock_crawl_site(monkeypatch, crawl_result)

    _website_id_off, run_id_off = await _seed_pending(websites_db, "enrich-total-failure-off")
    outcome_off = await _execute(websites_db, FakeStorage(), run_id_off, _unreachable_handler)
    assert outcome_off is not None
    row_off = await websites_db.fetchrow(
        "SELECT status, llms_txt, llms_full_txt FROM runs WHERE id = $1", run_id_off
    )
    assert row_off is not None
    assert row_off["status"] == "completed"

    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id_on, run_id_on = await _seed_pending(
        websites_db, "enrich-total-failure-on", enrich_with_llm=True
    )
    fake_anthropic = FakeAnthropic(respond=_always_fails)
    outcome_on = await _execute(
        websites_db, FakeStorage(), run_id_on, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome_on is not None

    row_on = await websites_db.fetchrow(
        "SELECT status, llms_txt, llms_full_txt, stats FROM runs WHERE id = $1", run_id_on
    )
    assert row_on is not None
    assert row_on["status"] == "completed"
    assert row_on["llms_txt"] == row_off["llms_txt"]
    assert row_on["llms_full_txt"] == row_off["llms_full_txt"]

    stats_on = json.loads(row_on["stats"])
    assert stats_on["pages_enriched"] == 0
    assert stats_on["enrich_failures"] == 1


async def test_a_per_page_enrichment_failure_mixes_model_and_extracted_titles(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "enrich-mixed", enrich_with_llm=True)

    ok_page = _enrich_page(
        "enrich-mixed/ok",
        title="Extracted OK Title",
        markdown="Real content for the page that will be summarized successfully.",
    )
    failing_page = _enrich_page(
        "enrich-mixed/fails",
        title="Extracted Fallback Title",
        markdown="Real content for the page whose summarization request will fail.",
    )
    crawl_result = CrawlResult(
        pages=[ok_page, failing_page],
        stats=_crawl_stats(pages_crawled=2),
        cap_hit=None,
        seed_error=None,
    )
    _mock_crawl_site(monkeypatch, crawl_result)

    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        if kwargs["messages"][0]["content"] == failing_page.markdown:
            raise anthropic.APIConnectionError(
                message="down",
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            )
        return fake_summary_response("Model OK Title", "Model written description of the page.")

    fake_anthropic = FakeAnthropic(respond=respond)

    outcome = await _execute(
        websites_db, FakeStorage(), run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT llms_txt, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert "Model OK Title" in row["llms_txt"]
    assert "Extracted Fallback Title" in row["llms_txt"]
    assert "Extracted OK Title" not in row["llms_txt"]

    stats = json.loads(row["stats"])
    assert stats["pages_enriched"] == 1
    assert stats["enrich_failures"] == 1


async def test_the_archived_payload_keeps_the_extracted_metadata_not_the_models(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 7, pinned end to end: `serialize_payload` is called on `result.pages` — the
    ORIGINAL, extracted pages — never on `artifact_pages`, so the gzip-compressed JSONL this
    run uploads to Storage keeps trafilatura's own title and description even though the
    artifact `runs.llms_txt` stores carries the model's."""
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "enrich-payload", enrich_with_llm=True)

    page = _enrich_page(
        "enrich-payload",
        title="Extracted Title For The Archive",
        description="Extracted description for the archive.",
    )
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )
    fake_anthropic = FakeAnthropic(
        respond=lambda _kwargs: fake_summary_response(
            "Model Written Title", "Model written description."
        )
    )
    storage = FakeStorage()

    outcome = await _execute(
        websites_db, storage, run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    assert len(storage.calls) == 1
    _object_path, data, _content_type = storage.calls[0]
    lines = gzip.decompress(data).decode("utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["title"] == "Extracted Title For The Archive"
    assert record["description"] == "Extracted description for the archive."
    assert record["title"] != "Model Written Title"

    row = await websites_db.fetchrow("SELECT llms_txt FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert "Model Written Title" in row["llms_txt"]


async def test_all_four_enrichment_counters_are_recorded(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "enrich-counters", enrich_with_llm=True)

    page_a = _enrich_page("enrich-counters/a", markdown="Real content A, long enough to summarize.")
    page_b = _enrich_page("enrich-counters/b", markdown="Real content B, long enough to summarize.")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page_a, page_b],
            stats=_crawl_stats(pages_crawled=2),
            cap_hit=None,
            seed_error=None,
        ),
    )

    def respond(kwargs: dict[str, Any]) -> FakeAnthropicResponse:
        return fake_summary_response(
            "Model Title", "Model description.", input_tokens=123, output_tokens=45
        )

    fake_anthropic = FakeAnthropic(respond=respond)

    outcome = await _execute(
        websites_db, FakeStorage(), run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    stats = json.loads(row["stats"])
    assert stats["pages_enriched"] == 2
    assert stats["enrich_failures"] == 0
    assert stats["enrich_input_tokens"] == 246
    assert stats["enrich_output_tokens"] == 90


async def test_the_anthropic_call_never_happens_inside_a_database_transaction(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 7, ASSERTED rather than assumed — the enrichment call's own
    sibling of `test_the_upload_never_happens_inside_a_database_transaction` above, sharing
    its transaction-depth-counter machinery over the SAME `events` list, so both network
    calls this run makes are checked against one continuous timeline rather than two
    independent ones that could pass separately while still overlapping a transaction
    between them."""
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "enrich-no-tx", enrich_with_llm=True)

    page = _enrich_page("enrich-no-tx")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )

    events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def tracking_transaction(pool: Pool) -> AsyncIterator[Connection]:
        events.append(("tx", "enter"))
        try:
            async with real_transaction(pool) as conn:
                yield conn
        finally:
            events.append(("tx", "exit"))

    monkeypatch.setattr("app.features.runs.service.transaction", tracking_transaction)

    class _TrackingStorage(FakeStorage):
        async def upload(self, object_path: str, data: bytes, *, content_type: str) -> str:
            events.append(("upload", "start"))
            try:
                return await super().upload(object_path, data, content_type=content_type)
            finally:
                events.append(("upload", "end"))

    class _TrackingAnthropic(FakeAnthropic):
        async def _create(self, **kwargs: Any) -> FakeAnthropicResponse:
            events.append(("enrich", "start"))
            try:
                return await super()._create(**kwargs)
            finally:
                events.append(("enrich", "end"))

    storage = _TrackingStorage()
    fake_anthropic = _TrackingAnthropic()

    outcome = await _execute(
        websites_db, storage, run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    depth = 0
    saw_enrich = False
    saw_upload = False
    for kind, phase in events:
        if kind == "tx":
            depth += 1 if phase == "enter" else -1
            assert depth >= 0, "a transaction exited more times than it entered"
        else:
            if kind == "enrich":
                saw_enrich = True
            elif kind == "upload":
                saw_upload = True
            assert depth == 0, f"{kind} {phase!r} happened while a transaction was open"
    assert saw_enrich, "the Anthropic call never happened at all"
    assert saw_upload, "the Storage upload never happened at all"
    assert depth == 0, "a transaction was left open"


# -----------------------------------------------------------------------------------------
# PER-194: the per-website opt-in — a run enriches only when the deployment flag AND the
# website's own `enrich_with_llm` are both true — and the content-hash side-channel the
# retrofit diff joins against. `tests/test_run_stats.py` and `tests/test_index_diff.py` pin
# the pure halves of this in isolation; what is pinned here is the WIRING against a real
# Postgres row, end to end.
# -----------------------------------------------------------------------------------------


async def test_a_website_that_did_not_opt_in_is_never_enriched_even_with_the_flag_on(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Compat]. The deployment flag alone is no longer enough to enrich a run — a website
    that never set `enrich_with_llm` must see byte-identical behaviour to the flag-off world,
    even with a real (fake) Anthropic client in hand and the deployment flag on."""
    page = _enrich_page("no-opt-in", title="Extracted Title")
    crawl_result = CrawlResult(
        pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
    )

    _mock_crawl_site(monkeypatch, crawl_result)
    _website_id_off, run_id_off = await _seed_pending(websites_db, "no-opt-in-off")
    outcome_off = await _execute(websites_db, FakeStorage(), run_id_off, _unreachable_handler)
    assert outcome_off is not None
    row_off = await websites_db.fetchrow(
        "SELECT llms_txt, llms_full_txt FROM runs WHERE id = $1", run_id_off
    )
    assert row_off is not None

    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _mock_crawl_site(monkeypatch, crawl_result)
    _website_id_on, run_id_on = await _seed_pending(websites_db, "no-opt-in-on")
    fake_anthropic = FakeAnthropic()
    outcome_on = await _execute(
        websites_db, FakeStorage(), run_id_on, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome_on is not None

    assert fake_anthropic.calls == [], (
        "a website that never opted in must never be sent to the model"
    )

    row_on = await websites_db.fetchrow(
        "SELECT llms_txt, llms_full_txt, stats FROM runs WHERE id = $1", run_id_on
    )
    assert row_on is not None
    assert row_on["llms_txt"] == row_off["llms_txt"]
    assert row_on["llms_full_txt"] == row_off["llms_full_txt"]

    stats_on = json.loads(row_on["stats"])
    assert stats_on["enrich_requested"] is False
    assert stats_on["enrich_applied"] is False
    assert stats_on["enrich_unavailable_reason"] is None
    assert stats_on["pages_enriched"] == 0
    assert stats_on["enrich_failures"] == 0
    assert stats_on["enrich_input_tokens"] == 0
    assert stats_on["enrich_output_tokens"] == 0


async def test_an_opted_in_website_with_the_global_flag_off_completes_and_records_deployment_disabled(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Fallback]. The website asked; the deployment cannot provide it because the flag
    itself is off — the run still completes with extracted metadata, and records why."""
    _website_id, run_id = await _seed_pending(websites_db, "flag-off", enrich_with_llm=True)
    page = _enrich_page("flag-off", title="Extracted Title")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )

    outcome = await _execute(websites_db, FakeStorage(), run_id, _unreachable_handler)
    assert outcome is not None

    row = await websites_db.fetchrow(
        "SELECT status, llms_txt, stats FROM runs WHERE id = $1", run_id
    )
    assert row is not None
    assert row["status"] == "completed"
    assert "Extracted Title" in row["llms_txt"]

    stats = json.loads(row["stats"])
    assert stats["enrich_requested"] is True
    assert stats["enrich_applied"] is False
    assert stats["enrich_unavailable_reason"] == "deployment_disabled"


async def test_an_opted_in_website_with_no_client_records_no_api_key(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Fallback]. The deployment flag is on, but this worker process built no
    `AsyncAnthropic` client — the `open_worker_resources` shape a missing `ANTHROPIC_API_KEY`
    produces, reproduced here by simply not passing `anthropic_client`."""
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "no-client", enrich_with_llm=True)
    page = _enrich_page("no-client", title="Extracted Title")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )

    outcome = await _execute(websites_db, FakeStorage(), run_id, _unreachable_handler)
    assert outcome is not None

    row = await websites_db.fetchrow(
        "SELECT status, llms_txt, stats FROM runs WHERE id = $1", run_id
    )
    assert row is not None
    assert row["status"] == "completed"
    assert "Extracted Title" in row["llms_txt"]

    stats = json.loads(row["stats"])
    assert stats["enrich_requested"] is True
    assert stats["enrich_applied"] is False
    assert stats["enrich_unavailable_reason"] == "no_api_key"


async def test_every_enrichment_request_failing_records_api_error_and_completes_with_extracted_titles(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[Fallback]. The deployment can enrich and the website asked, but every request fails
    — the run still completes with extracted metadata, and `enrich_unavailable_reason` names
    the third reason: the pass ran and produced nothing usable."""
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "api-error", enrich_with_llm=True)
    page = _enrich_page("api-error", title="Extracted Title")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )
    fake_anthropic = FakeAnthropic(respond=_always_fails)

    outcome = await _execute(
        websites_db, FakeStorage(), run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    row = await websites_db.fetchrow(
        "SELECT status, llms_txt, stats FROM runs WHERE id = $1", run_id
    )
    assert row is not None
    assert row["status"] == "completed"
    assert "Extracted Title" in row["llms_txt"]

    stats = json.loads(row["stats"])
    assert stats["enrich_requested"] is True
    assert stats["enrich_applied"] is False
    assert stats["enrich_unavailable_reason"] == "api_error"
    assert stats["enrich_failures"] == 1


async def test_an_opted_in_website_with_the_flag_on_enriches_and_records_applied(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary success path, end to end: both halves of the gate are true, the pass
    succeeds, and the row says so."""
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)
    _website_id, run_id = await _seed_pending(websites_db, "applied", enrich_with_llm=True)
    page = _enrich_page("applied", title="Extracted Title")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )
    fake_anthropic = FakeAnthropic(
        respond=lambda _kwargs: fake_summary_response("Model Title", "Model written description.")
    )

    outcome = await _execute(
        websites_db, FakeStorage(), run_id, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT llms_txt, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert "Model Title" in row["llms_txt"]

    stats = json.loads(row["stats"])
    assert stats["enrich_requested"] is True
    assert stats["enrich_applied"] is True
    assert stats["enrich_unavailable_reason"] is None


async def test_the_content_hash_map_is_recorded_and_keyed_by_the_normalized_url(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`runs.stats["content_hashes"]` is recorded on every run — enrichment need not even be
    requested — keyed by the SAME normalized form `IndexEntry.key` uses, which is what makes
    the join in `internals/index_diff.py`'s `build_index_diff` exact."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "content-hash")
    page = _diff_page("/docs/a")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )

    outcome = await _execute(websites_db, FakeStorage(), run_id, _unreachable_handler)
    assert outcome is not None

    stats = json.loads(await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id))
    hashes = stats["content_hashes"]
    assert list(hashes.keys()) == [normalize_url(page.url)]


async def test_the_first_run_after_opting_in_reports_a_not_comparable_metadata_diff_but_a_real_content_diff(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end mode-flip test against real Postgres. A website's first run happens
    before it opts in; its second run happens after both the website and the deployment have
    switched enrichment on. The comparison between them marks `metadata_changed`
    not-comparable but still reports a real `content_changed` — here, `0`, because the page's
    body genuinely did not change between the two runs, which is the point: `content_changed`
    is a REAL comparison, not a blanked one."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"http://{_SEED_IP}/mode-flip")

    run_id_1 = await seed_run(websites_db, website_id, started_at=_NOW, status="pending")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/docs/a", title="Page A")],
            stats=_crawl_stats(pages_crawled=1),
            cap_hit=None,
            seed_error=None,
        ),
    )
    outcome_1 = await _execute(websites_db, FakeStorage(), run_id_1, _unreachable_handler)
    assert outcome_1 is not None

    # Opt in, and turn the deployment flag on, strictly between the two runs.
    await websites_db.execute(
        "UPDATE websites SET enrich_with_llm = TRUE WHERE id = $1", website_id
    )
    monkeypatch.setattr(settings, "crawl_enrich_with_llm", True)

    run_id_2 = await seed_run(
        websites_db, website_id, started_at=_NOW + timedelta(minutes=1), status="pending"
    )
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[_diff_page("/docs/a", title="Page A")],
            stats=_crawl_stats(pages_crawled=1),
            cap_hit=None,
            seed_error=None,
        ),
    )
    fake_anthropic = FakeAnthropic(
        respond=lambda _kwargs: fake_summary_response("Model Title", "Model written description.")
    )
    outcome_2 = await _execute(
        websites_db, FakeStorage(), run_id_2, _unreachable_handler, anthropic_client=fake_anthropic
    )
    assert outcome_2 is not None

    stats_2 = json.loads(
        await websites_db.fetchval("SELECT stats FROM runs WHERE id = $1", run_id_2)
    )
    diff = stats_2["index_diff"]
    assert diff["state"] == "compared"
    assert diff["metadata_changed"] is None
    assert diff["metadata_not_comparable_reason"] == "enrichment_enabled"
    # The page's body is identical in both runs — a real, computed `0`, not a blanked `None`.
    assert diff["content_changed"] == 0
    # Everything mode-independent still reports normally.
    assert diff["pages_added"] == 0
    assert diff["pages_removed"] == 0


async def test_content_hashes_never_reach_the_runs_list_or_the_run_detail(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch, user_client: AsyncClient
) -> None:
    """[content_hashes stripped]. `content_hashes` is a worker-only key —
    `runs/service.py`'s `_public_stats` must strip it from both `GET /websites/{id}/runs` and
    `GET /runs/{id}`, while an ordinary key like `pages_crawled` still reaches both."""
    website_id, run_id = await _seed_pending_clean_origin(websites_db, "content-hash-hidden")
    page = _diff_page("/docs/a")
    _mock_crawl_site(
        monkeypatch,
        CrawlResult(
            pages=[page], stats=_crawl_stats(pages_crawled=1), cap_hit=None, seed_error=None
        ),
    )

    outcome = await _execute(websites_db, FakeStorage(), run_id, _unreachable_handler)
    assert outcome is not None

    list_response = await user_client.get(f"/websites/{website_id}/runs")
    assert list_response.status_code == 200
    list_item = next(item for item in list_response.json()["items"] if item["id"] == str(run_id))
    assert "content_hashes" not in list_item["stats"]
    assert "pages_crawled" in list_item["stats"]

    detail_response = await user_client.get(f"/runs/{run_id}")
    assert detail_response.status_code == 200
    detail_stats = detail_response.json()["stats"]
    assert "content_hashes" not in detail_stats
    assert "pages_crawled" in detail_stats


# -----------------------------------------------------------------------------------------
# PER-191: robots.txt's Disallow and Crawl-delay, wired into execute_run behind the shared
# PolitenessGate and select_urls' own "robots_disallowed" rule. Each test below drives the
# real discover_sitemap_urls -> select_urls -> crawl_site pipeline through the same
# httpx.MockTransport every other test in this section shares, and asserts on the run row
# it produces. tests/test_crawl_robots.py pins the parser in isolation,
# tests/test_url_ranking.py pins the drop rule, and tests/test_crawler_caps.py pins the
# seed-only check and the gate — what is pinned here is the end-to-end wiring.
# -----------------------------------------------------------------------------------------


async def test_a_disallowed_sitemap_url_is_never_fetched_and_is_counted(
    websites_db: Pool,
) -> None:
    """[Disallow — frontier]. `robots.txt` disallows `/robots-frontier/private`; the sitemap
    lists it alongside two allowed pages. The handler raises if it is ever requested — a
    regression that let it through would surface as `pages_crawled`/`pages_failed` disagreeing
    with the counts asserted below, not merely as a missing artifact bullet."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "robots-frontier")
    storage = FakeStorage()

    sitemap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://{_SEED_IP}/robots-frontier/allowed-1</loc></url>
  <url><loc>http://{_SEED_IP}/robots-frontier/allowed-2</loc></url>
  <url><loc>http://{_SEED_IP}/robots-frontier/private</loc></url>
</urlset>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200, text=sitemap_body, headers={"Content-Type": "application/xml"}
            )
        if request.url.path == "/sitemap_index.xml":
            return httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /robots-frontier/private\n")
        if request.url.path == "/robots-frontier/private":
            raise AssertionError("a disallowed URL must never be fetched")
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["pages_crawled"] == 3, "seed + allowed-1 + allowed-2 — private never fetched"
    assert stats["pages_failed"] == 0
    assert stats["urls_robots_disallowed"] == 1


async def test_a_run_whose_seed_is_disallowed_fails_with_a_specific_message(
    websites_db: Pool,
) -> None:
    """[Disallow — seed]. `robots.txt` disallows the run's own seed path. The seed is never
    requested at all — the handler raises if it is — and the run fails with the fixed,
    specific message `_safe_error_message` maps `RobotsDisallowedError` to."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "robots-seed")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /robots-seed\n")
        if request.url.path == "/robots-seed":
            raise AssertionError("a disallowed seed must never be fetched")
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is None

    row = await websites_db.fetchrow("SELECT status, error FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "This site's robots.txt disallows crawling this URL."


async def test_a_crawl_delay_is_recorded_in_the_stored_stats(websites_db: Pool) -> None:
    """[Crawl-delay]. `robots.txt` declares `Crawl-delay: 3600`; the sitemap is empty and the
    seed page carries no links at all, so there is no frontier and the politeness gate is
    never actually awaited by anything. This shape is mandatory: any test that combines a real
    frontier with a large `Crawl-delay` would really sleep, since `service.py` injects no fake
    clock into `PolitenessGate`. `MAX_ROBOTS_CRAWL_DELAY_MS` (10s) is what clamps 3600s down to
    the value asserted below."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "crawl-delay")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 3600\n")
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["crawl_delay_ms"] == 10_000


async def test_an_unreadable_robots_txt_completes_the_run(websites_db: Pool) -> None:
    """[Failure is open], persisted end to end: a 500 at `/robots.txt` allows everything, and
    the run still completes on the seed alone rather than failing."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "robots-500")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml"):
            return httpx.Response(404)
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT status, stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert row["status"] == "completed"
    stats = json.loads(row["stats"])
    assert stats["urls_robots_disallowed"] == 0
    assert stats["crawl_delay_ms"] == settings.crawl_politeness_delay_ms


async def test_stats_version_is_eight(websites_db: Pool) -> None:
    """[Observability]. A live row lands with `RUN_STATS_VERSION` 8 — the persistence-layer
    companion to `tests/test_run_stats.py::test_run_stats_version_is_pinned`, which only
    checks the constant itself."""
    _website_id, run_id = await _seed_pending_clean_origin(websites_db, "version-eight")
    storage = FakeStorage()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text="hello world")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    stats = json.loads(row["stats"])
    assert stats["version"] == 8
