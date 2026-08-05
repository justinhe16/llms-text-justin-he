"""Tests for the schedules API: `GET` and `PUT /websites/{id}/schedule`.

Every test here drives the real application (`app.main.app`) over a real Postgres, with a
real signed JWT — see `tests/test_websites_api.py`'s module docstring, which this file
mirrors, and the "signed-in API clients" section of `conftest.py`. Nothing about
authentication or authorization is stubbed:

* **Any signed-in user reads a schedule.** `test_get_by_a_non_owner_returns_the_same_body`
  is the test that fails the day someone adds a well-meaning `WHERE user_id = $1` to
  `SchedulesReader` — which does not even have a `user_id` column to filter by.
* **Only the website's owner writes its schedule.**
  `test_put_by_a_non_owner_is_403_and_the_row_is_unchanged` and
  `test_put_on_another_users_website_with_no_existing_schedule_is_403_and_creates_nothing`
  check both halves — the status code *and* that nothing was written. A `403` returned
  *after* a write would pass a status-only assertion.
* **`next_run_at` is a business decision, not a straight echo of the request.**
  `test_put_a_no_op_does_not_push_next_run_at_out` and
  `test_put_changing_the_interval_while_active_does_not_cause_an_immediate_fire` are the two
  cases `internals/next_run.py`'s docstring calls out as "not in the ticket, and the two
  decisions that matter most" — `tests/test_next_run.py` proves the pure function is right in
  isolation, and this file proves the service actually wires it up.

Row cleanup is handled by the `websites_db` fixture (`conftest.py`), which deletes only
websites owned by this suite's two fake users — `schedules` cascades from that delete, so
nothing here needs its own teardown.

Fixture rows are seeded with `seed_website` and `seed_schedule` from `conftest.py` rather than
through the API, for the reason that file's "Seed helpers" section gives: a suite that builds
its own fixtures by calling the endpoint under test cannot tell "the read is wrong" apart from
"the write is wrong". `seed_schedule` grew `last_run_at` and `auto_publish` keyword arguments
for this suite — they are exactly the two columns a `PUT` must never touch, so seeding them to
known non-default values is what turns "untouched" into an assertion rather than an assumption.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from asyncpg import Pool
from conftest import TEST_USER_A_ID, parse, seed_schedule, seed_website
from httpx import AsyncClient


# A time base for seeded schedules. Fixed relative to "now" rather than to a literal date so
# seeded rows always sort and compare sensibly. Mirrors test_websites_api.py's `_NOW`.
_NOW = datetime.now(UTC)


async def _fetch_schedule_row(pool: Pool, website_id: UUID) -> dict[str, Any] | None:
    """Read every column of a schedule directly, including `auto_publish` — which the API
    never returns — so a test can assert the full row before and after a write."""
    record = await pool.fetchrow(
        """
        SELECT id, website_id, active, interval_minutes, next_run_at, last_run_at, auto_publish
        FROM schedules
        WHERE website_id = $1
        """,
        website_id,
    )
    return dict(record) if record is not None else None


def _assert_close(actual: datetime, expected: datetime, *, tolerance_seconds: float = 5.0) -> None:
    """Assert `actual` is within a few seconds of `expected`.

    Used only for values this suite did not seed itself — `next_run_at` computed by the
    service from its own `datetime.now(UTC)`, which a test cannot know exactly without
    duplicating the service's clock read. Every value the test itself seeded is instead
    compared with plain `==` (via `conftest.parse`), because that comparison can and should
    be exact.
    """
    assert abs((actual - expected).total_seconds()) <= tolerance_seconds


# -----------------------------------------------------------------------------------------
# GET /websites/{id}/schedule
# -----------------------------------------------------------------------------------------


async def test_get_with_no_schedule_returns_200_with_a_null_body(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """A website with no schedule yet is a normal `200`, never a `404`."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://no-schedule.example")

    response = await user_client.get(f"/websites/{website_id}/schedule")

    assert response.status_code == 200
    assert response.json() is None


async def test_get_with_a_schedule_returns_every_field_and_never_auto_publish(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """Every response field the schema declares, and the one field it deliberately omits."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://has-schedule.example")
    next_run_at = _NOW + timedelta(hours=6)
    last_run_at = _NOW - timedelta(hours=1)
    schedule_id = await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=360,
        next_run_at=next_run_at,
        last_run_at=last_run_at,
        auto_publish=True,
    )

    response = await user_client.get(f"/websites/{website_id}/schedule")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(schedule_id)
    assert body["website_id"] == str(website_id)
    assert body["active"] is True
    assert body["interval_minutes"] == 360
    assert parse(body["next_run_at"]) == next_run_at
    assert parse(body["last_run_at"]) == last_run_at
    assert "auto_publish" not in body


async def test_get_for_an_unknown_website_returns_404(user_client: AsyncClient) -> None:
    assert (await user_client.get(f"/websites/{uuid4()}/schedule")).status_code == 404


async def test_get_by_a_non_owner_returns_the_same_body(
    user_client: AsyncClient, second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """Reads are public (ARCHITECTURE.md §4.1) — a stranger sees exactly what the owner sees.

    This is THE test for this endpoint: an owner filter added to `SchedulesReader` "for
    safety" has nothing to filter by (there is no `user_id` on `schedules`), so the only way
    such a filter could exist is a join back to `websites` — which this test would catch.
    """
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://readable-schedule.example"
    )
    await seed_schedule(websites_db, website_id, active=True, interval_minutes=60)

    owner_response = await user_client.get(f"/websites/{website_id}/schedule")
    stranger_response = await second_user_client.get(f"/websites/{website_id}/schedule")

    assert stranger_response.status_code == 200
    assert stranger_response.json() == owner_response.json()


async def test_get_with_a_malformed_id_returns_422(user_client: AsyncClient) -> None:
    assert (await user_client.get("/websites/not-a-uuid/schedule")).status_code == 422


# -----------------------------------------------------------------------------------------
# PUT /websites/{id}/schedule
# -----------------------------------------------------------------------------------------


async def test_put_creating_a_schedule_schedules_from_now(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The first `PUT` for a website with no schedule creates one, active, due one interval
    from now — and exactly one row lands in `schedules`."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://create-schedule.example")

    request_time = datetime.now(UTC)
    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 360}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is True
    assert body["interval_minutes"] == 360
    _assert_close(parse(body["next_run_at"]), request_time + timedelta(minutes=360))
    assert (
        await websites_db.fetchval(
            "SELECT count(*) FROM schedules WHERE website_id = $1", website_id
        )
        == 1
    )


async def test_put_changing_the_interval_while_active_does_not_cause_an_immediate_fire(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The weekly -> hourly acceptance criterion: recomputed from now, not from the stale
    weekly `next_run_at`, and strictly in the future — never an immediate fire."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://reschedule.example")
    old_next_run_at = _NOW + timedelta(days=6)
    await seed_schedule(
        websites_db, website_id, active=True, interval_minutes=10080, next_run_at=old_next_run_at
    )

    request_time = datetime.now(UTC)
    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 60}
    )

    assert response.status_code == 200
    next_run_at = parse(response.json()["next_run_at"])
    assert next_run_at > request_time
    assert next_run_at != old_next_run_at
    _assert_close(next_run_at, request_time + timedelta(minutes=60))


async def test_put_deactivating_clears_next_run_at(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://deactivate.example")
    await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=60,
        next_run_at=_NOW + timedelta(hours=1),
    )

    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": False, "interval_minutes": 60}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["next_run_at"] is None

    row = await _fetch_schedule_row(websites_db, website_id)
    assert row is not None
    assert row["active"] is False
    assert row["next_run_at"] is None


async def test_put_reactivating_schedules_from_now(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://reactivate.example")
    await seed_schedule(
        websites_db, website_id, active=False, interval_minutes=360, next_run_at=None
    )

    request_time = datetime.now(UTC)
    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 360}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is True
    _assert_close(parse(body["next_run_at"]), request_time + timedelta(minutes=360))


async def test_put_a_no_op_does_not_push_next_run_at_out(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The starvation guard: the same `active` + the same `interval_minutes` must return the
    seeded `next_run_at` byte-identical, not a value recomputed from a later `now()`."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://no-op.example")
    seeded_next_run_at = _NOW + timedelta(hours=2, minutes=13, seconds=7)
    await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=360,
        next_run_at=seeded_next_run_at,
    )

    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 360}
    )

    assert response.status_code == 200
    assert parse(response.json()["next_run_at"]) == seeded_next_run_at


async def test_put_by_a_non_owner_is_403_and_the_row_is_unchanged(
    user_client: AsyncClient, second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """Every column, not just the status code — a refused write must not have written
    anything, including into columns the request did not even mention."""
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://protected-schedule.example"
    )
    await seed_schedule(
        websites_db,
        website_id,
        active=True,
        interval_minutes=1440,
        next_run_at=_NOW + timedelta(hours=10),
        last_run_at=_NOW - timedelta(days=1),
        auto_publish=True,
    )
    before = await _fetch_schedule_row(websites_db, website_id)

    response = await second_user_client.put(
        f"/websites/{website_id}/schedule", json={"active": False, "interval_minutes": 60}
    )

    assert response.status_code == 403
    after = await _fetch_schedule_row(websites_db, website_id)
    assert after == before


@pytest.mark.parametrize(
    "interval_minutes",
    [
        pytest.param(0, id="zero"),
        pytest.param(59, id="just-under-hourly"),
        pytest.param(1441, id="just-over-daily"),
        pytest.param(-60, id="negative"),
        pytest.param("sixty", id="non-numeric"),
        pytest.param(1, id="one"),
    ],
)
async def test_put_an_invalid_interval_is_422(
    user_client: AsyncClient, websites_db: Pool, interval_minutes: Any
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://invalid-interval-{interval_minutes}.example"
    )

    response = await user_client.put(
        f"/websites/{website_id}/schedule",
        json={"active": True, "interval_minutes": interval_minutes},
    )

    assert response.status_code == 422


async def test_put_an_invalid_interval_names_every_allowed_value(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """Pydantic's own `Literal` message already names every allowed preset — nothing in this
    feature has to hand-write that text."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://named-presets.example")

    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 1}
    )

    assert response.status_code == 422
    text = response.text
    for preset in (60, 360, 1440, 10080):
        assert str(preset) in text


async def test_put_twice_never_creates_a_second_row(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://put-twice.example")

    first = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 60}
    )
    second = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 1440}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        await websites_db.fetchval(
            "SELECT count(*) FROM schedules WHERE website_id = $1", website_id
        )
        == 1
    )


async def test_put_never_modifies_last_run_at(user_client: AsyncClient, websites_db: Pool) -> None:
    """`last_run_at` belongs to the cron tick and the run pipeline, neither of which exists
    yet — a `PUT` through every kind of transition must leave it exactly as seeded."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://last-run-at.example")
    known_last_run_at = _NOW - timedelta(days=3)
    await seed_schedule(
        websites_db,
        website_id,
        active=False,
        interval_minutes=360,
        next_run_at=None,
        last_run_at=known_last_run_at,
    )

    bodies = [
        {"active": True, "interval_minutes": 360},  # activate
        {"active": True, "interval_minutes": 60},  # change interval
        {"active": False, "interval_minutes": 60},  # deactivate
        {"active": True, "interval_minutes": 60},  # reactivate
    ]
    for body in bodies:
        response = await user_client.put(f"/websites/{website_id}/schedule", json=body)
        assert response.status_code == 200
        assert parse(response.json()["last_run_at"]) == known_last_run_at

    row = await _fetch_schedule_row(websites_db, website_id)
    assert row is not None
    assert row["last_run_at"] == known_last_run_at


async def test_put_cannot_write_auto_publish(user_client: AsyncClient, websites_db: Pool) -> None:
    """`auto_publish` is reserved (db/schema.prisma). A body that carries it is not rejected
    — `UpsertScheduleRequest` has no `extra="forbid"` — but it must be silently ignored, not
    silently honored."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://auto-publish.example")
    await seed_schedule(websites_db, website_id, active=False, auto_publish=True)

    response = await user_client.put(
        f"/websites/{website_id}/schedule",
        json={"active": True, "interval_minutes": 60, "auto_publish": False},
    )

    assert response.status_code == 200
    assert "auto_publish" not in response.json()
    row = await _fetch_schedule_row(websites_db, website_id)
    assert row is not None
    assert row["auto_publish"] is True


async def test_put_on_an_unknown_website_returns_404(user_client: AsyncClient) -> None:
    response = await user_client.put(
        f"/websites/{uuid4()}/schedule", json={"active": True, "interval_minutes": 60}
    )
    assert response.status_code == 404


async def test_put_on_another_users_website_with_no_existing_schedule_is_403_and_creates_nothing(
    user_client: AsyncClient, second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """The 403 must fire before any row is created — `require_owner` runs before the
    transaction opens, so a non-owner's first-ever `PUT` for a website leaves nothing behind."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://no-schedule-yet.example")

    response = await second_user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 60}
    )

    assert response.status_code == 403
    assert (
        await websites_db.fetchval(
            "SELECT count(*) FROM schedules WHERE website_id = $1", website_id
        )
        == 0
    )


async def test_enabling_a_daily_schedule_persists_active_and_schedules_a_day_out(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The ticket's acceptance criterion, spelled out literally: enabling a daily (1440)
    schedule persists `active=true` and a `next_run_at` roughly 24 hours out."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://daily.example")

    request_time = datetime.now(UTC)
    response = await user_client.put(
        f"/websites/{website_id}/schedule", json={"active": True, "interval_minutes": 1440}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is True
    _assert_close(parse(body["next_run_at"]), request_time + timedelta(hours=24))

    row = await _fetch_schedule_row(websites_db, website_id)
    assert row is not None
    assert row["active"] is True


# -----------------------------------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "body"),
    [
        pytest.param("GET", None, id="get"),
        pytest.param("PUT", {"active": True, "interval_minutes": 60}, id="put"),
    ],
)
async def test_every_endpoint_requires_authentication(
    unauthenticated_client: AsyncClient, method: str, body: dict[str, Any] | None
) -> None:
    """No token, no access — including on the read endpoint. "Unscoped" means every
    *signed-in* user sees everything; it does not mean anonymous access."""
    response = await unauthenticated_client.request(
        method, f"/websites/{uuid4()}/schedule", json=body
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
