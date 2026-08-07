"""Tests for the runs API: `GET /websites/{id}/runs` and `GET /runs/{id}`.

Every test here drives the real application (`app.main.app`) over a real Postgres, with a
real signed JWT — see `tests/conftest.py`'s "signed-in API clients" section. As with
`tests/test_websites_api.py`, the two claims this feature makes are exactly the ones a stub
would paper over:

* **Any signed-in user reads any website's runs, and any run's full detail.**
  `test_a_non_owner_can_read_both_endpoints` is the test that fails the day someone adds a
  well-meaning `WHERE user_id = $1` to `RunsReader` — there is no `user_id` column on
  `runs` at all, but the same mistake is possible against `website_id`-as-ownership.
* **The keyset cursor is correct under concurrent writes and ties**, not merely "returns
  the right rows on an empty table". `test_inserting_a_newer_run_mid_pagination_does_not_
  corrupt_the_walk` and `test_a_tie_group_spanning_a_page_boundary_is_not_duplicated_or_
  dropped` are the two that would pass a naive OFFSET-based implementation's happy path and
  fail its actual job.

Every test seeds its own website with a unique origin and reads only that website's runs
(`GET /websites/{id}/runs` is scoped by `website_id` in its own right — that is the
endpoint's documented contract, not an ownership check, see ARCHITECTURE.md §4.1), so
equality assertions on one website's run list are safe even though `TEST_DATABASE_URL` may
be a developer's local database holding unrelated rows (`tests/test_websites_api.py`'s
module docstring explains why that matters for the *unscoped* `/websites` list, which this
suite does not touch).
"""

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from asyncpg import Pool
from conftest import TEST_USER_A_ID, explain_plan, plan_nodes, seed_run, seed_website
from httpx import AsyncClient

from app.features.runs.internals.run_cursor import (
    MAX_CURSOR_LENGTH,
    RunCursor,
    decode_cursor,
    encode_cursor,
)
from app.features.runs.internals.runs_reader import (
    _LIST_BY_WEBSITE,
    _LIST_BY_WEBSITE_AFTER_CURSOR,
    _LIST_BY_WEBSITE_WITH_STATUS,
    _PREVIOUS_COMPLETED_INDEX,
)
from app.features.runs.service import MAX_LIMIT


# A time base for seeded runs, fixed relative to "now". NOT shared with
# `tests/test_websites_api.py`'s `_NOW` — see the seed helpers' section docstring in
# `conftest.py` for why each suite keeps its own.
_NOW = datetime.now(UTC)


# -----------------------------------------------------------------------------------------
# GET /websites/{id}/runs — ordering, pagination, and the keyset property
# -----------------------------------------------------------------------------------------


async def test_history_returns_runs_newest_first(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://newest-first.example")
    oldest = await seed_run(websites_db, website_id, started_at=_NOW - timedelta(hours=2))
    middle = await seed_run(websites_db, website_id, started_at=_NOW - timedelta(hours=1))
    newest = await seed_run(websites_db, website_id, started_at=_NOW)

    response = await user_client.get(f"/websites/{website_id}/runs")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(newest), str(middle), str(oldest)]


async def test_pagination_walks_the_full_set_without_duplicates_or_gaps(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """A full walk of a set that does not divide evenly by the page size, so the last page
    is short — the case an off-by-one in the `LIMIT n + 1` probe would get wrong."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://paginate.example")
    seeded_ids = [
        str(await seed_run(websites_db, website_id, started_at=_NOW - timedelta(seconds=i)))
        for i in range(23)
    ]

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(len(seeded_ids) + 1):  # generous upper bound; the loop breaks itself
        params: dict[str, Any] = {"limit": 5}
        if cursor is not None:
            params["cursor"] = cursor
        response = await user_client.get(f"/websites/{website_id}/runs", params=params)
        assert response.status_code == 200
        body = response.json()
        collected.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("pagination did not terminate within the expected number of pages")

    assert sorted(collected) == sorted(seeded_ids)
    assert len(collected) == len(set(collected))


async def test_inserting_a_newer_run_mid_pagination_does_not_corrupt_the_walk(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """THE keyset test. A cursor names a position in the ordering, not an offset from the
    start of the list — so a row inserted after pagination begins, with a `started_at`
    newer than the cursor, must never appear on a later page (it sorts ahead of the
    cursor's position, which the client has already passed) and must not shift any
    already-seeded row out of place. An OFFSET-based implementation fails this exact test:
    the new row shifts every later row's offset by one, either duplicating or skipping one.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://keyset.example")
    seeded_ids = [
        str(await seed_run(websites_db, website_id, started_at=_NOW - timedelta(seconds=i)))
        for i in range(10)
    ]

    first_page = await user_client.get(f"/websites/{website_id}/runs", params={"limit": 4})
    assert first_page.status_code == 200
    first_body = first_page.json()
    collected = [item["id"] for item in first_body["items"]]
    cursor = first_body["next_cursor"]
    assert cursor is not None

    # Inserted "during" pagination, with a NEWER started_at than anything seeded above —
    # it must never appear on the remaining pages, since they are all strictly older than
    # the cursor already handed to the client.
    intruder = await seed_run(websites_db, website_id, started_at=_NOW + timedelta(hours=1))

    while cursor is not None:
        response = await user_client.get(
            f"/websites/{website_id}/runs", params={"limit": 4, "cursor": cursor}
        )
        assert response.status_code == 200
        body = response.json()
        collected.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]

    assert sorted(collected) == sorted(seeded_ids)
    assert len(collected) == len(set(collected))
    assert str(intruder) not in collected


async def test_a_tie_group_spanning_a_page_boundary_is_not_duplicated_or_dropped(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """Two (here, three) runs sharing a `started_at` down to the microsecond — the routine
    case for scheduled runs kicked off by one cron tick — must each appear exactly once
    even when the page boundary falls inside the tie group. `limit=2` against a 3-way tie
    plus one newer row forces exactly that: the cursor built from the first page names a
    row from the MIDDLE of the tie group, not its edge.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://tie.example")
    tie_time = _NOW - timedelta(hours=1)
    tied = [await seed_run(websites_db, website_id, started_at=tie_time) for _ in range(3)]
    newer = await seed_run(websites_db, website_id, started_at=_NOW)

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(5):  # generous upper bound; the loop breaks itself
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await user_client.get(f"/websites/{website_id}/runs", params=params)
        assert response.status_code == 200
        body = response.json()
        collected.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("pagination did not terminate within the expected number of pages")

    expected = {str(newer), *(str(run_id) for run_id in tied)}
    assert sorted(collected) == sorted(expected)
    assert len(collected) == len(set(collected))


async def test_limit_above_the_maximum_is_clamped_not_rejected(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://clamp.example")
    for i in range(MAX_LIMIT + 10):
        await seed_run(websites_db, website_id, started_at=_NOW - timedelta(seconds=i))

    response = await user_client.get(f"/websites/{website_id}/runs", params={"limit": 10_000})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == MAX_LIMIT
    assert body["next_cursor"] is not None


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_non_positive_limit_is_a_native_422(
    user_client: AsyncClient, websites_db: Pool, limit: int
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://bad-limit.example")
    response = await user_client.get(f"/websites/{website_id}/runs", params={"limit": limit})
    assert response.status_code == 422


# -----------------------------------------------------------------------------------------
# The cursor — malformed input, table-driven
# -----------------------------------------------------------------------------------------


def _b64(payload: str) -> str:
    """Encode a raw string the way `encode_cursor` would, without going through it — so a
    test can build a cursor whose JSON `encode_cursor` itself would never produce."""
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


_MALFORMED_CURSORS = [
    pytest.param("not-valid-base64-!!!", id="not-base64-at-all"),
    pytest.param(_b64("not json"), id="valid-base64-of-non-json"),
    pytest.param(
        _b64(json.dumps(["2026-01-01T00:00:00+00:00", str(uuid4())])),
        id="valid-json-array",
    ),
    pytest.param(
        _b64(json.dumps({"started_at": "2026-01-01T00:00:00+00:00"})),
        id="object-missing-id",
    ),
    pytest.param(
        _b64(json.dumps({"started_at": 123, "id": str(uuid4())})),
        id="non-string-field",
    ),
    pytest.param(
        _b64(json.dumps({"started_at": "not-a-timestamp", "id": str(uuid4())})),
        id="unparseable-timestamp",
    ),
    pytest.param(
        _b64(json.dumps({"started_at": "2026-01-01T00:00:00", "id": str(uuid4())})),
        id="naive-timestamp-no-offset",
    ),
    pytest.param(
        _b64(json.dumps({"started_at": "2026-01-01T00:00:00+00:00", "id": "not-a-uuid"})),
        id="bad-uuid",
    ),
    pytest.param("a" * (MAX_CURSOR_LENGTH + 1), id="over-long"),
    pytest.param("", id="empty-string"),
]


@pytest.mark.parametrize("cursor", _MALFORMED_CURSORS)
async def test_a_malformed_cursor_is_422_never_500(
    user_client: AsyncClient, websites_db: Pool, cursor: str
) -> None:
    """Every byte of `?cursor=` is attacker-controlled (`run_cursor.py`'s module
    docstring); none of the shapes below may reach the database or raise anything other
    than the router's `422`.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://bad-cursor.example")

    response = await user_client.get(f"/websites/{website_id}/runs", params={"cursor": cursor})

    assert response.status_code == 422


def test_cursor_round_trip_preserves_microsecond_precision_and_utc_offset() -> None:
    """A unit test of `encode_cursor`/`decode_cursor` directly — no HTTP, no database.

    `datetime.isoformat()` is what the encoder relies on to preserve both microseconds and
    the UTC offset; this exercises that a value with a non-UTC, non-zero offset and
    nonzero microseconds survives the round trip exactly rather than being truncated to
    second resolution or normalized to a different offset.
    """
    started_at = datetime(2026, 8, 4, 13, 38, 1, 438329, tzinfo=timezone(timedelta(hours=-7)))
    run_id = uuid4()

    decoded = decode_cursor(encode_cursor(started_at, run_id))

    assert decoded == RunCursor(started_at=started_at, run_id=run_id)
    assert decoded.started_at.microsecond == 438329
    assert decoded.started_at.utcoffset() == timedelta(hours=-7)


# -----------------------------------------------------------------------------------------
# Field shape — list excludes llms_txt, detail includes it, duration_ms, stats
# -----------------------------------------------------------------------------------------


async def test_list_items_exclude_llms_txt_and_storage_path(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """Absence, not falsiness: a `None` value would also make `item["llms_txt"]` falsy, so
    this asserts the KEY is missing from the JSON rather than merely empty."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://no-llms-txt.example")
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        llms_txt="# Example\n\nSome generated content.",
        storage_path="runs/abc/raw.jsonl",
    )

    response = await user_client.get(f"/websites/{website_id}/runs")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "llms_txt" not in item
    assert "storage_path" not in item


async def test_detail_includes_llms_txt_and_storage_path(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://detail.example")
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        llms_txt="# Example\n\nGenerated body.",
        storage_path="runs/def/raw.jsonl",
    )

    response = await user_client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(run_id)
    assert body["llms_txt"] == "# Example\n\nGenerated body."
    assert body["storage_path"] == "runs/def/raw.jsonl"


async def test_duration_ms_is_none_in_flight_and_exact_once_completed(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://duration.example")
    in_flight = await seed_run(
        websites_db, website_id, started_at=_NOW, status="processing", completed_at=None
    )
    started = _NOW - timedelta(hours=2)
    completed_at = started + timedelta(milliseconds=42_137)
    completed_run = await seed_run(
        websites_db, website_id, started_at=started, completed_at=completed_at, status="completed"
    )

    response = await user_client.get(f"/websites/{website_id}/runs")

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[str(in_flight)]["duration_ms"] is None
    assert by_id[str(completed_run)]["duration_ms"] == 42_137


@pytest.mark.parametrize(
    "stats",
    [
        # Genuinely invalid JSON *syntax* cannot appear here: Postgres validates it at the
        # `::jsonb` cast, so a row seeded with one would fail to insert in the first place
        # rather than reach the application. What CAN land in the column is valid JSON that
        # is not an object -- every case below is one of those.
        pytest.param("null", id="json-null"),
        pytest.param('"just a string"', id="json-string-not-object"),
        pytest.param("42", id="json-number-not-object"),
        pytest.param('["an", "array", "not", "an", "object"]', id="json-array-not-object"),
        pytest.param(None, id="no-stats-at-all"),
    ],
)
async def test_malformed_or_absent_stats_do_not_500_the_history_endpoint(
    user_client: AsyncClient, websites_db: Pool, stats: str | None
) -> None:
    """`runs.stats` is jsonb with no shape Postgres enforces (ARCHITECTURE.md §3.4). One
    row with unexpected, non-object stats must not take down the whole page."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://malformed-stats.example")
    await seed_run(websites_db, website_id, started_at=_NOW, stats=stats)

    response = await user_client.get(f"/websites/{website_id}/runs")

    assert response.status_code == 200
    assert response.json()["items"][0]["stats"] is None


async def test_well_formed_stats_round_trip_as_an_object(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://good-stats.example")
    await seed_run(
        websites_db, website_id, started_at=_NOW, stats={"pages_crawled": 12, "tokens": 4096}
    )

    response = await user_client.get(f"/websites/{website_id}/runs")

    assert response.status_code == 200
    assert response.json()["items"][0]["stats"] == {"pages_crawled": 12, "tokens": 4096}


# -----------------------------------------------------------------------------------------
# status filter
# -----------------------------------------------------------------------------------------


async def test_status_filter_returns_only_matching_runs(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://status-filter.example")
    completed = await seed_run(websites_db, website_id, started_at=_NOW, status="completed")
    await seed_run(websites_db, website_id, started_at=_NOW - timedelta(minutes=1), status="failed")
    await seed_run(
        websites_db, website_id, started_at=_NOW - timedelta(minutes=2), status="pending"
    )

    response = await user_client.get(f"/websites/{website_id}/runs", params={"status": "completed"})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(completed)]


async def test_an_invalid_status_value_is_422(user_client: AsyncClient, websites_db: Pool) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://bad-status.example")

    response = await user_client.get(
        f"/websites/{website_id}/runs", params={"status": "not-a-real-status"}
    )

    assert response.status_code == 422


# -----------------------------------------------------------------------------------------
# 404s and authorization
# -----------------------------------------------------------------------------------------


async def test_unknown_website_is_404_on_the_history_endpoint(user_client: AsyncClient) -> None:
    assert (await user_client.get(f"/websites/{uuid4()}/runs")).status_code == 404


async def test_unknown_run_is_404_on_the_detail_endpoint(user_client: AsyncClient) -> None:
    assert (await user_client.get(f"/runs/{uuid4()}")).status_code == 404


async def test_a_non_owner_can_read_both_endpoints(
    second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """Reads are unscoped (ARCHITECTURE.md §4.1). The website below is owned by
    `TEST_USER_A_ID`; `second_user_client` is signed in as `TEST_USER_B_ID` and must still
    get a `200` from both endpoints. This is the test that fails the day either query grows
    a `WHERE user_id = $1` — there is no such column on `runs`, but the same mistake is
    possible via an implicit ownership check on `website_id`.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://not-mine.example")
    run_id = await seed_run(websites_db, website_id, started_at=_NOW)

    history = await second_user_client.get(f"/websites/{website_id}/runs")
    detail = await second_user_client.get(f"/runs/{run_id}")

    assert history.status_code == 200
    assert any(item["id"] == str(run_id) for item in history.json()["items"])
    assert detail.status_code == 200
    assert detail.json()["id"] == str(run_id)


@pytest.mark.parametrize(
    ("path",),
    [
        pytest.param(f"/websites/{uuid4()}/runs", id="history"),
        pytest.param(f"/runs/{uuid4()}", id="detail"),
    ],
)
async def test_both_endpoints_require_authentication(
    unauthenticated_client: AsyncClient, path: str
) -> None:
    response = await unauthenticated_client.get(path)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# -----------------------------------------------------------------------------------------
# The EXPLAIN test — the ticket's explicit acceptance criterion for the keyset query.
#
# Small tables seq-scan no matter what a query looks like, so this seeds several thousand
# rows for ONE website and runs `ANALYZE` before asking the planner anything; without both,
# every assertion below would be vacuously true on an empty table.
#
# Explains the EXACT SQL strings `RunsReader.list_by_website` runs — imported from the
# reader module, never retyped here — so this test cannot silently drift from the
# implementation. Assertions are entirely about plan SHAPE (node types, index names), never
# about `ANALYZE`'s wall-clock timings, which are noise in CI.
# -----------------------------------------------------------------------------------------

_EXPLAIN_ROW_COUNT = 5_000
_EXPLAIN_PENDING_COUNT = 5  # rare among 5,000 rows -- deliberately highly selective


async def _seed_many_runs_for_explain(pool: Pool, website_id: UUID) -> None:
    """Bulk-insert `_EXPLAIN_ROW_COUNT` rows in one round trip, not one `seed_run` call per
    row -- this test is measuring the query planner's choice of index, not exercising the
    endpoint's behavior, so the shape of each row (evenly spaced timestamps, all 'manual')
    is all that matters, and a single `INSERT ... SELECT FROM generate_series` is what
    keeps seeding thousands of rows fast enough to run on every `pytest` invocation.
    """
    await pool.execute(
        """
        INSERT INTO runs (website_id, "trigger", status, started_at, completed_at)
        SELECT
            $1,
            'manual'::run_trigger,
            (CASE WHEN i <= $3 THEN 'pending' ELSE 'completed' END)::run_status,
            now() - (i || ' seconds')::interval,
            CASE
                WHEN i <= $3 THEN NULL
                ELSE now() - (i || ' seconds')::interval + interval '1 second'
            END
        FROM generate_series(1, $2) AS s(i)
        """,
        website_id,
        _EXPLAIN_ROW_COUNT,
        _EXPLAIN_PENDING_COUNT,
    )
    await pool.execute("ANALYZE runs")


def _assert_uses_an_index_not_a_seq_scan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The baseline shape assertion every regime below must satisfy, and the ONLY one the
    `status='pending'` regime is held to — see that regime's comment for why.
    """
    nodes = plan_nodes(plan)
    node_types = [node["Node Type"] for node in nodes]
    assert "Seq Scan" not in node_types, node_types
    assert any(t in ("Index Scan", "Index Only Scan") for t in node_types), node_types
    return nodes


def _assert_uses_the_composite_index_via_its_natural_ordering(
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """The stricter shape the no-filter and `status='completed'` regimes must satisfy on
    top of `_assert_uses_an_index_not_a_seq_scan`: no plain `Sort` node, and the composite
    index specifically (pinned by name).

    `"Sort"` is checked for literal equality, not membership/substring, which is exactly
    what lets `"Incremental Sort"` through: Postgres labels that node type with that exact,
    different string. Incremental Sort is EXPECTED here, not merely tolerated — the index
    is `(website_id, started_at DESC)` and does not include `id`, so Postgres gets the
    `started_at` ordering for free from the index and sorts only WITHIN each same-timestamp
    tie group to also satisfy `id DESC`. A plain `Sort` node would mean the index's
    ordering was not used at all, which is the actual regression this guards against — and
    is exactly what the `status='pending'` regime below is allowed to do instead, because it
    answers the query a completely different way (see that regime's comment).
    """
    nodes = _assert_uses_an_index_not_a_seq_scan(plan)
    node_types = [node["Node Type"] for node in nodes]
    assert "Sort" not in node_types, node_types
    assert any(node.get("Index Name") == "runs_website_id_started_at_idx" for node in nodes)
    return nodes


async def test_history_query_plan_uses_the_composite_index_not_a_seq_scan(
    websites_db: Pool,
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://explain.example")
    await _seed_many_runs_for_explain(websites_db, website_id)
    page_probe_limit = 26  # a page of 25 plus the `LIMIT n + 1` probe row

    # Regime 1: no status filter -- the plain history page. Must use the composite index
    # by name: it is the only index that could possibly answer "this website's runs,
    # newest first" without a sort.
    _assert_uses_the_composite_index_via_its_natural_ordering(
        await explain_plan(websites_db, _LIST_BY_WEBSITE, website_id, page_probe_limit)
    )

    # Regime 2: status='completed' -- an extra Filter over the SAME Index Cond as regime 1
    # (`status` is not a column of the composite index). 'completed' is the overwhelming
    # majority of seeded rows, so it is nowhere near selective enough to justify anything
    # but the composite index.
    _assert_uses_the_composite_index_via_its_natural_ordering(
        await explain_plan(
            websites_db, _LIST_BY_WEBSITE_WITH_STATUS, website_id, "completed", page_probe_limit
        )
    )

    # Regime 3: status='pending' -- deliberately NOT pinning an index name, and NOT
    # asserting "no plain Sort". Measured on this suite's database: with only
    # `_EXPLAIN_PENDING_COUNT` matching rows out of `_EXPLAIN_ROW_COUNT`, the planner
    # switches to the partial `runs_status_active_idx` (which is not ordered by
    # `started_at`/`id` at all) and sorts the handful of matches with a plain `Sort`
    # afterward -- cheap, because there are only a handful of them. That is a legitimate,
    # data-dependent choice, not a regression: whether it happens at all depends on the
    # planner's row-count estimate for this specific value, which can differ by Postgres
    # version and by how many 'pending'/'processing' rows exist elsewhere in the table.
    # Asserting a plan shape stronger than "used an index, not a sequential scan" here
    # would make this test flaky across environments for a difference that is not a bug.
    _assert_uses_an_index_not_a_seq_scan(
        await explain_plan(
            websites_db, _LIST_BY_WEBSITE_WITH_STATUS, website_id, "pending", page_probe_limit
        )
    )


async def test_the_keyset_cursor_bound_reaches_the_index_condition(websites_db: Pool) -> None:
    """The single most important assertion in this suite.

    Regimes 1-3 above explain the FIRST-page queries. This one explains
    `_LIST_BY_WEBSITE_AFTER_CURSOR` — the actual keyset query, the one every page after the
    first runs, and the only one where the row-value comparison exists to be got wrong.

    Asserting "an index scan happened" is not enough here, because the OR-expanded form
    `(started_at < $2 OR (started_at = $2 AND id < $3))` **also** produces an Index Scan on
    the composite index. It looks identical at the node-type level. The difference — the
    entire reason `runs_reader.py` carries a long comment forbidding that rewrite — is
    *where the `started_at` bound ends up*:

      * row-value form: `Index Cond: (website_id = ... AND started_at <= ...)`, so the scan
        STARTS at the cursor and reads ~a page of rows.
      * OR-expanded form: `Index Cond: (website_id = ...)` alone, with the whole disjunction
        demoted to a `Filter`, so the scan starts at the newest run and walks — then
        discards — every row the client has already paged past. Linear in page depth.

    So this asserts on the Index Cond text specifically: `started_at` must appear in it.
    That is what makes the forbidden rewrite fail a test rather than merely fail a
    dashboard six months from now, and it is why this assertion is worth its brittleness to
    plan-text formatting.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://explain-cursor.example")
    await _seed_many_runs_for_explain(websites_db, website_id)

    # A cursor pointing deliberately deep into the history — 1,000 rows in. On the
    # OR-expanded form this is exactly where the wasted work would show up; on the row-value
    # form the plan is identical to a shallow page's.
    deep_row = await websites_db.fetchrow(
        """
        SELECT started_at, id FROM runs
        WHERE website_id = $1
        ORDER BY started_at DESC, id DESC
        OFFSET 1000 LIMIT 1
        """,
        website_id,
    )
    assert deep_row is not None

    plan = await explain_plan(
        websites_db,
        _LIST_BY_WEBSITE_AFTER_CURSOR,
        website_id,
        deep_row["started_at"],
        deep_row["id"],
        26,
    )

    # The same shape guarantees the first page gets: composite index, by name, no plain Sort.
    nodes = _assert_uses_the_composite_index_via_its_natural_ordering(plan)

    index_scans = [n for n in nodes if n["Node Type"] in ("Index Scan", "Index Only Scan")]
    assert index_scans, [n["Node Type"] for n in nodes]

    index_conds = [n.get("Index Cond", "") for n in index_scans]
    assert any("started_at" in cond for cond in index_conds), (
        "The cursor's started_at bound did not reach the Index Cond, so the scan is not "
        "starting at the cursor -- it is starting at the newest run and filtering. This is "
        "the exact regression the OR-expanded WHERE clause causes; see the module docstring "
        f"of app/features/runs/internals/runs_reader.py. Index Conds: {index_conds}"
    )


async def test_previous_completed_index_query_plan_avoids_a_seq_scan(websites_db: Pool) -> None:
    """`_PREVIOUS_COMPLETED_INDEX`'s own EXPLAIN test — the worker-path sibling of the one
    above, not `test_stats_api.py`'s `LIMIT 1` tests: `_LATEST_COMPLETED_INDEX` there is a
    plain range filter (`started_at >= $2 AND started_at < $3`) with nothing to get wrong at
    the Index Cond level, while `_PREVIOUS_COMPLETED_INDEX` compares the sort key as the same
    kind of ROW `_LIST_BY_WEBSITE_AFTER_CURSOR` does — `(started_at, id) < ($2, $3)` — against
    a cursor drawn from a real row deep in a website's history, so the identical OR-expansion
    regression `test_the_keyset_cursor_bound_reaches_the_index_condition` guards against is
    possible here too, and for a query that runs on every successful crawl
    (`CrawlService._build_index_diff`), not merely on a page load.

    **Two CTEs, `previous` and `previous_completed`, each with their own row-value
    comparison** (`internals/runs_reader.py`'s own comment above `_PREVIOUS_COMPLETED_INDEX`
    explains why there are two), so this asserts the started_at bound reached the Index Cond
    of EVERY `Index Scan`/`Index Only Scan` node in the plan, not merely one of them — a
    regression in either CTE's WHERE clause is exactly as real as a regression in the other.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://explain-previous.example")
    await _seed_many_runs_for_explain(websites_db, website_id)

    # Same deep cursor shape as the keyset test above -- 1,000 rows in, where the OR-expanded
    # form's wasted work would show up.
    deep_row = await websites_db.fetchrow(
        """
        SELECT started_at, id FROM runs
        WHERE website_id = $1
        ORDER BY started_at DESC, id DESC
        OFFSET 1000 LIMIT 1
        """,
        website_id,
    )
    assert deep_row is not None

    plan = await explain_plan(
        websites_db, _PREVIOUS_COMPLETED_INDEX, website_id, deep_row["started_at"], deep_row["id"]
    )

    # The same shape guarantee as the cursor test: composite index, by name, no plain Sort
    # (an `Incremental Sort` atop the index's own `started_at` ordering is expected, exactly
    # as it is for the first-page and cursor regimes above).
    nodes = _assert_uses_the_composite_index_via_its_natural_ordering(plan)

    index_scans = [n for n in nodes if n["Node Type"] in ("Index Scan", "Index Only Scan")]
    assert index_scans, [n["Node Type"] for n in nodes]

    index_conds = [n.get("Index Cond", "") for n in index_scans]
    assert all("started_at" in cond for cond in index_conds), (
        "At least one of the two CTEs' started_at bound did not reach its Index Cond, so that "
        "scan is starting at the newest run and filtering rather than starting at the cursor. "
        "This is the exact regression the OR-expanded WHERE clause causes; see the module "
        f"docstring of app/features/runs/internals/runs_reader.py. Index Conds: {index_conds}"
    )
