"""Tests for the websites API: `POST`, `GET`, `GET /{id}`, and `DELETE /websites`.

Every test here drives the real application (`app.main.app`) over a real Postgres, with a
real signed JWT — see the "signed-in API clients" section of `conftest.py`. Nothing about
authentication or authorization is stubbed, because the two claims this feature has to make
are exactly the ones a stub would paper over:

* **Any signed-in user reads everything.** `test_listing_returns_other_users_websites` and
  `test_a_non_owner_can_read_a_website_by_id` are the tests that fail the day someone adds a
  well-meaning `WHERE user_id = $1` to the reader. They are the most valuable tests in this
  file.
* **Only the owner writes.** `test_deleting_another_users_website_is_403_and_the_row_survives`
  checks both halves — the status code *and* that the row is still there afterwards. A 403
  that had already deleted the row would pass a status-only assertion.

Row cleanup is handled by the `websites_db` fixture, which deletes only rows owned by this
suite's two fake users. So **assert containment, never equality**, on the list endpoint: the
database may legitimately hold other rows, and a `len(body) == 2` assertion would be green
in CI and red on a developer's machine.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from asyncpg import Connection, Pool, UniqueViolationError
from conftest import TEST_USER_A_ID, TEST_USER_B_ID
from fastapi import HTTPException
from httpx import AsyncClient

from app.features.websites.internals.websites_reader import WebsitesReader
from app.features.websites.internals.websites_writer import WebsitesWriter
from app.features.websites.schemas import CreateWebsiteRequest
from app.features.websites.service import WebsiteService


# A time base for seeded runs. Fixed relative to "now" rather than to a literal date so the
# rows always sort sensibly against `websites.created_at`, which the database sets itself.
_NOW = datetime.now(UTC)


async def _seed_website(pool: Pool, user_id: UUID, origin: str, url: str | None = None) -> UUID:
    """Insert a website directly, bypassing the API.

    Read-path tests seed with SQL on purpose: a test that builds its fixtures by calling
    `POST /websites` cannot distinguish "the list query is wrong" from "the create endpoint
    is wrong", and it silently stops testing anything the day create starts failing.
    """
    website_id: UUID = await pool.fetchval(
        "INSERT INTO websites (user_id, url, origin) VALUES ($1, $2, $3) RETURNING id",
        user_id,
        url or f"{origin}/",
        origin,
    )
    return website_id


async def _seed_run(
    pool: Pool,
    website_id: UUID,
    *,
    started_at: datetime,
    status: str = "completed",
    completed_at: datetime | None = None,
    stats: dict[str, Any] | str | None = None,
) -> UUID:
    """Insert a run directly.

    `stats` is passed as a JSON *string* cast to jsonb: asyncpg encodes and decodes
    `json`/`jsonb` as `str` and will not accept a Python dict for a jsonb parameter without a
    custom type codec. It also accepts a raw string, so a test can seed deliberately
    malformed stats.

    `"trigger"` is quoted because it is a SQL keyword, matching the generated migration.
    """
    encoded = json.dumps(stats) if isinstance(stats, dict) else stats
    run_id: UUID = await pool.fetchval(
        """
        INSERT INTO runs (website_id, "trigger", status, started_at, completed_at, stats)
        VALUES ($1, 'manual'::run_trigger, $2::run_status, $3, $4, $5::jsonb)
        RETURNING id
        """,
        website_id,
        status,
        started_at,
        completed_at,
        encoded,
    )
    return run_id


async def _seed_schedule(
    pool: Pool,
    website_id: UUID,
    *,
    active: bool = True,
    interval_minutes: int = 360,
    next_run_at: datetime | None = None,
) -> UUID:
    schedule_id: UUID = await pool.fetchval(
        """
        INSERT INTO schedules (website_id, active, interval_minutes, next_run_at)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        website_id,
        active,
        interval_minutes,
        next_run_at,
    )
    return schedule_id


def _ids(body: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in body]


def _parse(timestamp: str) -> datetime:
    """Parse a timestamp out of a JSON response, for comparison against what was seeded.

    Every column involved is `timestamptz` with microsecond precision, so this round trip is
    exact — an assertion may compare for equality rather than for approximate closeness.
    """
    return datetime.fromisoformat(timestamp)


# -----------------------------------------------------------------------------------------
# POST /websites
# -----------------------------------------------------------------------------------------


async def test_creating_a_website_returns_201_with_the_normalized_origin(
    user_client: AsyncClient,
) -> None:
    """The ticket's acceptance criterion, spelled out literally.

    `https://Example.com:443/path/` must normalize to the origin `https://example.com` while
    `url` keeps what was typed.
    """
    response = await user_client.post("/websites", json={"url": "https://Example.com:443/path/"})

    assert response.status_code == 201
    body = response.json()
    assert body["origin"] == "https://example.com"
    assert body["url"] == "https://Example.com:443/path/"
    assert body["user_id"] == str(TEST_USER_A_ID)
    assert body["title"] is None
    assert UUID(body["id"])
    assert body["created_at"]


async def test_creating_a_duplicate_origin_returns_409_carrying_the_existing_id(
    user_client: AsyncClient,
) -> None:
    """The 409 must hand back the existing website's id.

    That id is what lets the frontend navigate to the site you already added instead of
    showing an error you cannot act on — the whole reason this is not a bare 409.
    """
    first = await user_client.post("/websites", json={"url": "https://dupe.example/pricing"})
    assert first.status_code == 201
    existing_id = first.json()["id"]

    # A different spelling of the same origin, to prove the dedupe runs on the normalized
    # value rather than on string equality of the submitted URL.
    second = await user_client.post("/websites", json={"url": "https://DUPE.example:443/other?a=1"})

    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["website_id"] == existing_id
    assert detail["code"] == "website_already_exists"
    assert detail["origin"] == "https://dupe.example"


async def test_two_users_may_each_add_the_same_origin(
    user_client: AsyncClient, second_user_client: AsyncClient
) -> None:
    """`UNIQUE (user_id, origin)` is a per-user dedupe key, not a global one.

    If this ever returns 409, the constraint has been misread as a tenancy boundary.
    """
    mine = await user_client.post("/websites", json={"url": "https://shared.example"})
    theirs = await second_user_client.post("/websites", json={"url": "https://shared.example"})

    assert mine.status_code == 201
    assert theirs.status_code == 201
    assert mine.json()["id"] != theirs.json()["id"]
    assert mine.json()["user_id"] == str(TEST_USER_A_ID)
    assert theirs.json()["user_id"] == str(TEST_USER_B_ID)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("ftp://example.com", id="ftp-scheme"),
        pytest.param("example.com", id="relative"),
        pytest.param("/websites", id="path-only"),
        pytest.param("not a url", id="garbage"),
        pytest.param("", id="empty"),
        pytest.param("https://user:pw@example.com", id="credentials"),
        pytest.param("https://example.com/" + "a" * 4000, id="too-long"),
    ],
)
async def test_an_invalid_url_is_rejected_with_422(user_client: AsyncClient, url: str) -> None:
    """Validation happens in the request schema, so this is FastAPI's native 422."""
    response = await user_client.post("/websites", json={"url": url})
    assert response.status_code == 422


async def test_a_body_without_a_url_is_rejected_with_422(user_client: AsyncClient) -> None:
    assert (await user_client.post("/websites", json={})).status_code == 422


def _make_the_pre_check_miss_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the duplicate pre-check return `None` on its first call only.

    This is the loser of a concurrent create, reproduced deterministically: it looks for an
    existing origin at the moment the other request has not committed yet, finds nothing,
    and proceeds to an INSERT that then fails against the real constraint. Every later call
    — including the re-read inside the `except` branch — sees the database as it really is.
    """
    real_lookup = WebsitesReader.get_by_user_and_origin
    calls = 0

    async def pre_check_misses_once(
        self: WebsitesReader, user_id: UUID, origin: str
    ) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await real_lookup(self, user_id, origin)

    monkeypatch.setattr(WebsitesReader, "get_by_user_and_origin", pre_check_misses_once)


async def test_losing_the_duplicate_race_produces_the_same_409(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent create that slips past the pre-check must still get the 409 with the id.

    `create_website` checks for an existing origin and then inserts, which is two statements
    and therefore not atomic: two simultaneous POSTs of the same URL can both find nothing.
    The database's `UNIQUE (user_id, origin)` index is what actually enforces uniqueness, and
    the service turns the loser's `UniqueViolationError` back into the same 409 the
    sequential path returns — so a client cannot tell the two apart.

    That branch is unreachable from the outside without real concurrency, so the race is
    simulated at its seam: the pre-check returns `None` exactly once, as it would for the
    loser, while the row genuinely exists in the database. The INSERT then fails for real
    against the real constraint — the part worth testing is not stubbed.

    Driven against `WebsiteService` directly rather than over HTTP, because the thing under
    test is service-layer recovery, and the router adds nothing but a status code here.
    """
    existing_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://race.example")
    _make_the_pre_check_miss_once(monkeypatch)

    service = WebsiteService(websites_db)
    with pytest.raises(HTTPException) as raised:
        await service.create_website(
            CreateWebsiteRequest(url="https://race.example/deep/link"), TEST_USER_A_ID
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["website_id"] == str(existing_id)
    assert raised.value.detail["code"] == "website_already_exists"
    # The failed INSERT was rolled back — the race must not leave a second row behind.
    assert (
        await websites_db.fetchval(
            "SELECT count(*) FROM websites WHERE user_id = $1 AND origin = $2",
            TEST_USER_A_ID,
            "https://race.example",
        )
        == 1
    )


async def test_a_unique_violation_on_a_different_constraint_is_not_reported_as_a_duplicate(
    websites_db: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the origin constraint becomes a 409; anything else surfaces as itself.

    Without the constraint-name check, a unique violation added by some later migration
    would be reported as "you already added this website" and point the frontend at a
    website id that has nothing to do with the failure.

    **The setup is what makes this test able to fail.** A row with this origin must already
    exist, and the pre-check must miss it — otherwise the `except` branch's re-read finds
    nothing, falls through to its own `raise`, and the test passes whether the constraint
    guard is there or not. (It was written that way first; deleting the guard did not fail
    it. Verified by deleting the guard again after this rewrite.) With the row present,
    removing the guard turns this into a 409 and the test goes red.
    """
    await _seed_website(websites_db, TEST_USER_A_ID, "https://other-constraint.example")
    _make_the_pre_check_miss_once(monkeypatch)

    async def insert_violates_another_constraint(
        self: WebsitesWriter, user_id: UUID, url: str, origin: str
    ) -> dict[str, Any]:
        # `constraint_name` is None here, which is precisely "not the origin index".
        raise UniqueViolationError("duplicate key value violates some other unique constraint")

    monkeypatch.setattr(WebsitesWriter, "insert", insert_violates_another_constraint)

    service = WebsiteService(websites_db)
    with pytest.raises(UniqueViolationError):
        await service.create_website(
            CreateWebsiteRequest(url="https://other-constraint.example"), TEST_USER_A_ID
        )


# -----------------------------------------------------------------------------------------
# GET /websites
# -----------------------------------------------------------------------------------------


async def test_listing_returns_other_users_websites(
    user_client: AsyncClient, second_user_client: AsyncClient
) -> None:
    """THE test for this ticket. Reads are unscoped (ARCHITECTURE.md §4.1).

    User A must see User B's website in the list, with B's `user_id` on it so the UI can
    render an owner and gate the delete control. An owner filter added to the reader "for
    safety" fails exactly here and nowhere else.
    """
    mine = (await user_client.post("/websites", json={"url": "https://mine.example"})).json()
    theirs = (
        await second_user_client.post("/websites", json={"url": "https://theirs.example"})
    ).json()

    response = await user_client.get("/websites")

    assert response.status_code == 200
    body = response.json()
    assert mine["id"] in _ids(body)
    assert theirs["id"] in _ids(body), "reads must not be filtered by the calling user"

    listed_theirs = next(item for item in body if item["id"] == theirs["id"])
    assert listed_theirs["user_id"] == str(TEST_USER_B_ID)


@pytest.mark.parametrize("include", [None, "latest_run"], ids=["plain", "with-fold"])
async def test_the_list_is_ordered_most_recently_active_first(
    user_client: AsyncClient, websites_db: Pool, include: str | None
) -> None:
    """A website that ran recently sorts above one added later that has never run.

    Run for both list queries, because they are two separate SQL statements that share one
    `_ACTIVITY_ORDER` constant: asking for the fold must change what a row *contains*, never
    what order rows arrive in. Only checking the plain list would leave the other query's
    ordering unverified the day someone edits it.

    Asserts relative position rather than exact indices: the table may hold rows this suite
    did not create.
    """
    older = await _seed_website(websites_db, TEST_USER_A_ID, "https://older.example")
    newer = await _seed_website(websites_db, TEST_USER_A_ID, "https://newer.example")
    await _seed_run(websites_db, older, started_at=_NOW + timedelta(hours=1))

    params = {"include": include} if include else {}
    ids = _ids((await user_client.get("/websites", params=params)).json())

    assert ids.index(str(older)) < ids.index(str(newer))


async def test_include_latest_run_folds_in_the_newest_run_and_the_schedule(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The fold must pick the NEWEST run, not an arbitrary one.

    Three runs are seeded out of chronological order so that a query which forgot its
    `ORDER BY started_at DESC` would have to get lucky twice to pass.
    """
    website_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://folded.example")
    await _seed_run(websites_db, website_id, started_at=_NOW - timedelta(days=2), status="failed")
    newest = await _seed_run(
        websites_db,
        website_id,
        started_at=_NOW - timedelta(hours=1),
        completed_at=_NOW - timedelta(minutes=50),
        status="completed",
        stats={"pages_crawled": 42, "duration_ms": 1234},
    )
    await _seed_run(websites_db, website_id, started_at=_NOW - timedelta(days=1), status="failed")
    next_run_at = _NOW + timedelta(hours=5)
    await _seed_schedule(
        websites_db, website_id, active=True, interval_minutes=360, next_run_at=next_run_at
    )

    body = (await user_client.get("/websites", params={"include": "latest_run"})).json()
    item = next(entry for entry in body if entry["id"] == str(website_id))

    assert item["latest_run"]["id"] == str(newest)
    assert item["latest_run"]["status"] == "completed"
    assert item["latest_run"]["pages_crawled"] == 42
    assert _parse(item["latest_run"]["started_at"]) == _NOW - timedelta(hours=1)
    assert _parse(item["latest_run"]["completed_at"]) == _NOW - timedelta(minutes=50)

    # Each field against the value that was seeded. A dict comparison that fills the
    # timestamp in from the response itself (`"next_run_at": item[...]["next_run_at"]`)
    # reads like an assertion but compares that field to itself and can never fail.
    assert item["schedule"]["active"] is True
    assert item["schedule"]["interval_minutes"] == 360
    assert _parse(item["schedule"]["next_run_at"]) == next_run_at


async def test_a_website_with_no_run_or_schedule_folds_to_nulls(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://bare.example")

    body = (await user_client.get("/websites", params={"include": "latest_run"})).json()
    item = next(entry for entry in body if entry["id"] == str(website_id))

    assert item["latest_run"] is None
    assert item["schedule"] is None


async def test_the_fold_is_null_without_include(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://unfolded.example")
    await _seed_run(websites_db, website_id, started_at=_NOW, stats={"pages_crawled": 7})

    body = (await user_client.get("/websites")).json()
    item = next(entry for entry in body if entry["id"] == str(website_id))

    assert item["latest_run"] is None
    assert item["schedule"] is None


@pytest.mark.parametrize(
    "stats",
    [
        pytest.param('{"pages_crawled": "many"}', id="string-instead-of-number"),
        pytest.param('{"pages_crawled": null}', id="json-null"),
        pytest.param('{"duration_ms": 10}', id="key-absent"),
        pytest.param("{}", id="empty-object"),
        pytest.param(None, id="no-stats-at-all"),
    ],
)
async def test_malformed_stats_yield_a_null_page_count_rather_than_a_500(
    user_client: AsyncClient, websites_db: Pool, stats: str | None
) -> None:
    """`runs.stats` is jsonb with no constrained shape (ARCHITECTURE.md §3.4).

    One row with an unexpected `pages_crawled` must not take down the list endpoint for
    every website.
    """
    website_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://weird-stats.example")
    await _seed_run(websites_db, website_id, started_at=_NOW, stats=stats)

    response = await user_client.get("/websites", params={"include": "latest_run"})

    assert response.status_code == 200
    item = next(entry for entry in response.json() if entry["id"] == str(website_id))
    assert item["latest_run"]["pages_crawled"] is None


async def test_a_fractional_page_count_is_truncated_rather_than_raising(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """`jsonb_typeof` calls 12.5 a number, and `'12.5'::bigint` raises. Hence `::numeric`."""
    website_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://fractional.example")
    await _seed_run(websites_db, website_id, started_at=_NOW, stats='{"pages_crawled": 12.5}')

    response = await user_client.get("/websites", params={"include": "latest_run"})

    assert response.status_code == 200
    item = next(entry for entry in response.json() if entry["id"] == str(website_id))
    assert item["latest_run"]["pages_crawled"] == 12


async def test_an_unknown_include_value_is_422(user_client: AsyncClient) -> None:
    """`?include=latest_runs` must say so, not silently return an unfolded list."""
    assert (
        await user_client.get("/websites", params={"include": "latest_runs"})
    ).status_code == 422


class _CountingHandle:
    """A `DbHandle` that counts the queries issued through it, delegating to a real one.

    The measurement instrument for the N+1 acceptance criterion, and deliberately placed at
    the seam a repository actually uses (`Reader.fetch_all` -> `self._db.fetch`) rather than
    at the HTTP layer: it counts statements, which is the thing the criterion is about.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self.query_count = 0

    async def fetch(self, query: str, *args: object) -> Any:
        self.query_count += 1
        return await self._connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> Any:
        self.query_count += 1
        return await self._connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object) -> Any:
        self.query_count += 1
        return await self._connection.fetchval(query, *args)


@pytest.fixture
async def counting_reader(
    websites_db: Pool,
) -> AsyncIterator[tuple[WebsitesReader, _CountingHandle]]:
    async with websites_db.acquire() as connection:
        handle = _CountingHandle(connection)
        yield WebsitesReader(handle), handle


@pytest.mark.parametrize("website_count", [1, 3, 10])
async def test_the_latest_run_fold_costs_one_query_regardless_of_row_count(
    websites_db: Pool,
    counting_reader: tuple[WebsitesReader, _CountingHandle],
    website_count: int,
) -> None:
    """ "Bounded number of queries" is measured, not assumed.

    Each seeded website gets two runs and a schedule, so a per-row implementation would
    issue on the order of `2 * website_count` extra statements. The assertion is a constant
    1 for every row count in the parametrization — which is what makes it a statement about
    complexity rather than about a particular fixture size.
    """
    for index in range(website_count):
        website_id = await _seed_website(
            websites_db, TEST_USER_A_ID, f"https://n-plus-one-{index}.example"
        )
        await _seed_run(websites_db, website_id, started_at=_NOW - timedelta(days=1))
        await _seed_run(websites_db, website_id, started_at=_NOW)
        await _seed_schedule(websites_db, website_id)

    reader, handle = counting_reader
    rows = await reader.list_all_with_latest_run()

    assert handle.query_count == 1
    assert len([row for row in rows if row["user_id"] == TEST_USER_A_ID]) >= website_count


# -----------------------------------------------------------------------------------------
# GET /websites/{id}
# -----------------------------------------------------------------------------------------


async def test_a_non_owner_can_read_a_website_by_id(
    user_client: AsyncClient, second_user_client: AsyncClient
) -> None:
    """The other half of "reads are unscoped": a single fetch, by a stranger, succeeds.

    This is also what makes 403-not-404 the right answer on DELETE — the resource's
    existence is already public to every signed-in user.
    """
    created = (await user_client.post("/websites", json={"url": "https://readable.example"})).json()

    response = await second_user_client.get(f"/websites/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


async def test_getting_an_unknown_id_returns_404(user_client: AsyncClient) -> None:
    assert (await user_client.get(f"/websites/{uuid4()}")).status_code == 404


async def test_getting_a_malformed_id_returns_422(user_client: AsyncClient) -> None:
    """The path parameter is a UUID, so a non-UUID never reaches the service."""
    assert (await user_client.get("/websites/not-a-uuid")).status_code == 422


# -----------------------------------------------------------------------------------------
# DELETE /websites/{id}
# -----------------------------------------------------------------------------------------


async def test_deleting_your_own_website_returns_204_and_cascades(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """Deleting a website takes its schedule and its runs with it.

    The cascade is enforced by the foreign keys, not by application code — which is exactly
    why it is worth a test: nothing in `WebsitesWriter` would fail if the FK were dropped.
    """
    website_id = await _seed_website(websites_db, TEST_USER_A_ID, "https://cascade.example")
    await _seed_schedule(websites_db, website_id)
    await _seed_run(websites_db, website_id, started_at=_NOW)
    await _seed_run(websites_db, website_id, started_at=_NOW - timedelta(days=1))

    response = await user_client.delete(f"/websites/{website_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert (
        await websites_db.fetchval("SELECT count(*) FROM websites WHERE id = $1", website_id) == 0
    )
    assert (
        await websites_db.fetchval(
            "SELECT count(*) FROM schedules WHERE website_id = $1", website_id
        )
        == 0
    )
    assert (
        await websites_db.fetchval("SELECT count(*) FROM runs WHERE website_id = $1", website_id)
        == 0
    )


async def test_deleting_another_users_website_is_403_and_the_row_survives(
    user_client: AsyncClient, second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """Writes are owner-only, and a refused write must not have written anything.

    Asserting the row survives is the half that matters: a 403 returned *after* the delete
    would satisfy a status-code-only test.
    """
    created = (
        await user_client.post("/websites", json={"url": "https://protected.example"})
    ).json()

    response = await second_user_client.delete(f"/websites/{created['id']}")

    assert response.status_code == 403
    assert (
        await websites_db.fetchval(
            "SELECT count(*) FROM websites WHERE id = $1", UUID(created["id"])
        )
        == 1
    )
    # And it is still readable by that same non-owner — 403 is about writing, not seeing.
    assert (await second_user_client.get(f"/websites/{created['id']}")).status_code == 200


async def test_the_403_body_does_not_name_the_owner(
    user_client: AsyncClient, second_user_client: AsyncClient
) -> None:
    """A refusal says "not yours", never "it belongs to <user>"."""
    created = (await user_client.post("/websites", json={"url": "https://quiet.example"})).json()

    response = await second_user_client.delete(f"/websites/{created['id']}")

    assert response.status_code == 403
    assert str(TEST_USER_A_ID) not in response.text


async def test_deleting_an_unknown_id_returns_404(user_client: AsyncClient) -> None:
    """404 when the row does not exist — the fetch precedes the ownership check."""
    assert (await user_client.delete(f"/websites/{uuid4()}")).status_code == 404


# -----------------------------------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("POST", "/websites", {"url": "https://anon.example"}, id="create"),
        pytest.param("GET", "/websites", None, id="list"),
        pytest.param("GET", f"/websites/{uuid4()}", None, id="get"),
        pytest.param("DELETE", f"/websites/{uuid4()}", None, id="delete"),
    ],
)
async def test_every_endpoint_requires_authentication(
    unauthenticated_client: AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """No token, no access — including on the read endpoints.

    "Unscoped" means every *signed-in* user sees everything; it does not mean anonymous
    access. The distinction is the whole of ARCHITECTURE.md §4's first half.
    """
    response = await unauthenticated_client.request(method, path, json=body)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_a_garbage_token_is_401_not_500(unauthenticated_client: AsyncClient) -> None:
    """Reached through the real verification path, so a malformed token cannot become a 500."""
    response = await unauthenticated_client.get(
        "/websites", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert response.status_code == 401
