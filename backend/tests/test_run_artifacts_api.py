"""Tests for `GET /runs/{id}/llms.txt` and `GET /runs/{id}/llms-full.txt` (PER-181) — the two
generated-artifact downloads.

Its own file rather than an addition to `tests/test_runs_api.py`, matching how this router is
already split by endpoint across suites: `test_run_trigger_api.py` for the `POST`,
`test_stats_api.py` for the stats `GET`, `test_runs_api.py` for `GET /websites/{id}/runs` and
`GET /runs/{id}` — that suite's own module docstring scopes it explicitly to those two and is
already long. A fourth file for these two artifact `GET`s is the established convention, not
a new one.

Every test here drives the real application (`app.main.app`) over a real Postgres, with a
real signed JWT — the same rig `test_runs_api.py` uses, via `tests/conftest.py`'s "signed-in
API clients" section. As with that suite, the claims worth testing are exactly the ones a
stub would paper over:

* **Any signed-in user may download any run's artifacts.**
  `test_a_non_owner_can_download_both_artifacts` is the mandatory test for this ticket — it
  fails the day either artifact query grows a `WHERE user_id = $1` or an accidental
  `require_owner` call.
* **The two endpoints never serve each other's column**, and a run with no `llms_full_txt`
  yet (every row written before PER-179, or a run still in flight) is a `404` with a detail
  distinct from "no such run" — never a `200` with `None`-as-a-string and never the same
  `detail` an unknown id gets.
* **`GET /runs/{id}` still does not carry `llms_full_txt`.** CLAUDE.md's non-negotiable
  constraint for this ticket, turned into a fact CI enforces rather than a comment trusted to
  stay true.

Seeding uses `conftest.seed_run` / `conftest.seed_website` directly against the database,
never through the API — the same reasoning `test_websites_api.py`'s module docstring gives.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asyncpg import Pool
from conftest import TEST_USER_A_ID, seed_run, seed_website
from fastapi import FastAPI
from httpx import AsyncClient


# A time base for seeded runs, fixed relative to "now". NOT shared with any other suite's
# `_NOW` — see `conftest.py`'s seed-helper section docstring for why each suite keeps its own.
_NOW = datetime.now(UTC)

# (id, URL path segment, the `seed_run` kwarg that backs it, the filename stem it produces).
# The one plain-tuple source both `_ARTIFACTS` (below) and the combined-axis parametrization
# in `test_a_run_with_no_artifact_is_404_with_a_distinct_detail` build from, so the two
# endpoints' tests cannot silently drift apart from each other.
_ARTIFACT_KINDS = [
    ("index", "llms.txt", "llms_txt", "llms"),
    ("full", "llms-full.txt", "llms_full_txt", "llms-full"),
]

# Every test below that applies to both artifacts is parametrized over this, rather than
# duplicated twice.
_ARTIFACTS = [
    pytest.param(path, column, stem, id=artifact_id)
    for artifact_id, path, column, stem in _ARTIFACT_KINDS
]


# -----------------------------------------------------------------------------------------
# A completed run serves its artifact
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "column", "stem"), _ARTIFACTS)
async def test_a_completed_run_serves_its_artifact_as_plain_text(
    user_client: AsyncClient, websites_db: Pool, path: str, column: str, stem: str
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://serves-plain.example")
    content = f"# {stem} content\n\nHello, world.\n"
    run_id = await seed_run(
        websites_db, website_id, started_at=_NOW, status="completed", **{column: content}
    )

    response = await user_client.get(f"/runs/{run_id}/{path}")

    assert response.status_code == 200
    assert response.text == content
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize(("path", "column", "stem"), _ARTIFACTS)
async def test_the_content_disposition_names_the_sanitized_origin(
    user_client: AsyncClient, websites_db: Pool, path: str, column: str, stem: str
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://Example.COM:8443", url="https://Example.COM:8443/"
    )
    run_id = await seed_run(
        websites_db, website_id, started_at=_NOW, status="completed", **{column: "content"}
    )

    response = await user_client.get(f"/runs/{run_id}/{path}")

    assert response.status_code == 200
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="{stem}-example-com-8443.txt"'
    )


async def test_each_endpoint_serves_only_its_own_artifact(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """One run with BOTH columns set to different strings — the test that catches a swap of
    the two query constants, which parametrizing the same run away would hide."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://swap-guard.example")
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        status="completed",
        llms_txt="the index",
        llms_full_txt="the expansion",
    )

    index = await user_client.get(f"/runs/{run_id}/llms.txt")
    full = await user_client.get(f"/runs/{run_id}/llms-full.txt")

    assert index.status_code == 200
    assert index.text == "the index"
    assert full.status_code == 200
    assert full.text == "the expansion"


# -----------------------------------------------------------------------------------------
# Authorization — reads are unscoped (ARCHITECTURE.md §4.1)
# -----------------------------------------------------------------------------------------


async def test_a_non_owner_can_download_both_artifacts(
    second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """MANDATORY for this ticket. The website below is owned by `TEST_USER_A_ID`;
    `second_user_client` is signed in as `TEST_USER_B_ID` and must still get a `200` from
    both endpoints, with the right bytes back — this is the test that fails the day either
    artifact query grows a `WHERE user_id = $1`, a `require_owner` call, or any other
    ownership check on this read path. Modelled on `test_runs_api.py`'s
    `test_a_non_owner_can_read_both_endpoints`.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://not-mine-either.example")
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        status="completed",
        llms_txt="the index",
        llms_full_txt="the expansion",
    )

    index = await second_user_client.get(f"/runs/{run_id}/llms.txt")
    full = await second_user_client.get(f"/runs/{run_id}/llms-full.txt")

    assert index.status_code == 200
    assert index.text == "the index"
    assert full.status_code == 200
    assert full.text == "the expansion"


# -----------------------------------------------------------------------------------------
# 404s — unknown run vs. a run with nothing to download, and they must not agree
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "column", "stem"), _ARTIFACTS)
async def test_an_unknown_run_is_404(
    user_client: AsyncClient, path: str, column: str, stem: str
) -> None:
    response = await user_client.get(f"/runs/{uuid4()}/{path}")

    assert response.status_code == 404
    assert response.json()["detail"] == "No run with that id"


@pytest.mark.parametrize(
    ("path", "column", "stem", "status_"),
    [
        pytest.param(path, column, stem, status_, id=f"{artifact_id}-{status_}")
        for artifact_id, path, column, stem in _ARTIFACT_KINDS
        for status_ in ("pending", "processing", "failed")
    ],
)
async def test_a_run_with_no_artifact_is_404_with_a_distinct_detail(
    user_client: AsyncClient,
    websites_db: Pool,
    path: str,
    column: str,
    stem: str,
    status_: str,
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://no-artifact-{status_}-{stem}.example"
    )
    run_id = await seed_run(websites_db, website_id, started_at=_NOW, status=status_)

    response = await user_client.get(f"/runs/{run_id}/{path}")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail == f"This run has no {stem}.txt artifact"
    assert detail != "No run with that id"


async def test_a_run_that_predates_the_full_artifact_still_serves_its_index(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The real pre-PER-179 row state: `llms_txt` set, `llms_full_txt` NULL, `completed`.
    `/llms.txt` must still serve; `/llms-full.txt` must 404 rather than serving `None` as a
    string or crashing."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://predates-full.example")
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        status="completed",
        llms_txt="the index",
        llms_full_txt=None,
    )

    index = await user_client.get(f"/runs/{run_id}/llms.txt")
    full = await user_client.get(f"/runs/{run_id}/llms-full.txt")

    assert index.status_code == 200
    assert index.text == "the index"
    assert full.status_code == 404
    assert full.json()["detail"] == "This run has no llms-full.txt artifact"


@pytest.mark.parametrize(("path", "column", "stem"), _ARTIFACTS)
async def test_an_empty_artifact_is_an_empty_200_not_a_404(
    user_client: AsyncClient, websites_db: Pool, path: str, column: str, stem: str
) -> None:
    """Pins the NULL check as `is None`, not truthiness — an empty string is a real, if
    unlikely, artifact, and must not be treated the same as "never generated"."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, f"https://empty-{stem}.example")
    run_id = await seed_run(
        websites_db, website_id, started_at=_NOW, status="completed", **{column: ""}
    )

    response = await user_client.get(f"/runs/{run_id}/{path}")

    assert response.status_code == 200
    assert response.text == ""


# -----------------------------------------------------------------------------------------
# Authentication and input validation
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "column", "stem"), _ARTIFACTS)
async def test_both_endpoints_require_authentication(
    unauthenticated_client: AsyncClient, path: str, column: str, stem: str
) -> None:
    response = await unauthenticated_client.get(f"/runs/{uuid4()}/{path}")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("path", "column", "stem"), _ARTIFACTS)
async def test_a_malformed_run_id_is_422(
    user_client: AsyncClient, path: str, column: str, stem: str
) -> None:
    response = await user_client.get(f"/runs/not-a-uuid/{path}")

    assert response.status_code == 422


# -----------------------------------------------------------------------------------------
# The OpenAPI document — Decision C's acceptance criterion
# -----------------------------------------------------------------------------------------


def test_the_openapi_document_declares_text_plain_for_both_endpoints(api_app: FastAPI) -> None:
    """`response_class=PlainTextResponse` is what makes `openapi.json` declare `text/plain`
    for these two routes' `200` instead of an empty `application/json` schema — see
    `app.api.routers.runs`'s module docstring, Decision C of this ticket. Omitting that one
    keyword produces a green `make openapi`, a green `tsc`, and a `Promise<void>` on the
    frontend, so this is asserted directly against the generated document rather than trusted
    to a code review catching a missing keyword argument.
    """
    schema = api_app.openapi()

    for path in ("/runs/{id}/llms.txt", "/runs/{id}/llms-full.txt"):
        content = schema["paths"][path]["get"]["responses"]["200"]["content"]
        assert set(content) == {"text/plain"}
        assert content["text/plain"]["schema"] == {"type": "string"}


# -----------------------------------------------------------------------------------------
# The constraint this ticket forbids relaxing
# -----------------------------------------------------------------------------------------


async def test_the_run_detail_endpoint_still_does_not_carry_llms_full_txt(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """`GET /runs/{id}` must never widen to include `llms_full_txt` — the Output tab polls it
    every 3 seconds (ARCHITECTURE.md §8.6), and `llms_full_txt` can be large. This turns that
    constraint into a fact CI enforces rather than a comment trusted to stay true.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://no-full-txt-leak.example")
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        status="completed",
        llms_txt="the index",
        llms_full_txt="x" * 10_000,
    )

    response = await user_client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert "llms_full_txt" not in response.json()
