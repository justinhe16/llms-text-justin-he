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
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from asyncpg import Connection, Pool
from conftest import TEST_USER_A_ID, FakeStorage, seed_run, seed_website

from app.core.settings import settings
from app.features.crawl.internals.crawler import CrawlResult
from app.features.crawl.internals.payload import PAYLOAD_CONTENT_TYPE
from app.features.crawl.schemas import CrawledPage
from app.features.crawl.service import TransientCrawlError, build_crawl_service
from app.features.runs.internals.runs_writer import RunsWriter
from app.infrastructure.db.transaction import transaction as real_transaction
from app.infrastructure.storage.supabase_storage import StorageUploadError


_NOW = datetime.now(UTC)
_SEED_IP = "8.8.8.8"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def _seed_pending(pool: Pool, suffix: str) -> tuple[UUID, UUID]:
    website_id = await seed_website(pool, TEST_USER_A_ID, f"http://{_SEED_IP}/{suffix}")
    run_id = await seed_run(pool, website_id, started_at=_NOW, status="pending")
    return website_id, run_id


async def _execute(
    pool: Pool,
    storage: FakeStorage,
    run_id: UUID,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 1,
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
    """
    async with _mock_client(handler) as http_client:
        service = build_crawl_service(pool, http_client, storage, settings)
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
    assert row["storage_path"] == f"crawl-payloads/{website_id}/{run_id}.jsonl.gz"
    assert row["completed_at"] is not None

    stats = json.loads(row["stats"])
    assert stats["version"] == 2
    assert stats["links_emitted"] == 1
    assert stats["pages_crawled"] == 1
    assert "cap_hit" in stats
    assert stats["pages_empty_content"] == 1, "the ok_handler's body has no extractable content"


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
        "SELECT status, llms_txt, storage_path, completed_at, error FROM runs WHERE id = $1",
        run_id,
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["storage_path"] is None
    assert row["llms_txt"] is None
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
    `crawl_site` itself is the only deterministic way to exercise a cap end to end."""
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

    monkeypatch.setattr("app.features.crawl.service.crawl_site", fake_crawl_site)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("crawl_site is mocked; no HTTP request should be made")

    outcome = await _execute(websites_db, storage, run_id, handler)
    assert outcome is not None

    row = await websites_db.fetchrow("SELECT stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    assert json.loads(row["stats"])["cap_hit"] == "pages"


async def test_partial_stats_survive_an_upload_failure(websites_db: Pool) -> None:
    _website_id, run_id = await _seed_pending(websites_db, "partial-stats")
    storage = FakeStorage(fail=StorageUploadError("boom"))

    await _execute(websites_db, storage, run_id, _ok_handler)

    row = await websites_db.fetchrow("SELECT stats FROM runs WHERE id = $1", run_id)
    assert row is not None
    stats = json.loads(row["stats"])
    assert stats["pages_crawled"] == 1
    assert stats["version"] == 2


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
