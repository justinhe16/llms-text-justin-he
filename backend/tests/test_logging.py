"""Tests for `app.core.logging` — the JSON formatter, the redaction filter, and the
`contextvar` correlation ids both processes hang their logs off.

Every test drives a REAL logging pipeline: a `logging.StreamHandler` over a `StringIO`,
carrying the same `JsonFormatter` and the same `RedactionFilter` that
`configure_logging()` installs, attached to the real root logger. Calling
`JsonFormatter().format(record)` on a hand-built `LogRecord` would assert nothing about
propagation, about filters running before formatters, or about `extra=` surviving the
trip — which is most of what can actually break here.

The two tests that matter most, and why:

* **`test_concurrent_tasks_never_see_each_others_run_id`.** The whole reason
  `app.core.logging` uses `contextvars` rather than a module-level "current run id" is
  that a worker runs `max_jobs = 2` crawls at once. A global would make the second crawl's
  id overwrite the first's, and the resulting logs would confidently attribute one run's
  failures to the other — worse than having no correlation id at all. The test uses an
  `asyncio.Barrier` so that every task has bound its id *before* any task logs, which is
  precisely the interleaving a global implementation passes a naive test on and fails in
  production.
* **The redaction suite.** Its false-positive tests are as load-bearing as its
  true-positive ones. A filter that redacts too much makes logs useless, which is how
  filters get switched off.
"""

import asyncio
import json
import logging
import logging.config
import tomllib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from arq.utils import import_string
from conftest import _JsonLogCapture

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    bind_user_id,
    configure_logging,
    correlation_fields,
    redact,
    request_id_context,
    run_id_context,
    tick_id_context,
    user_id_context,
)
from app.worker import jobs
from app.worker.settings import ARQ_LOG_CONFIG


# The dotted path `arq --custom-log-dict` is given, restated as a literal because that is
# how it appears in backend/fly.toml and scripts/dev.sh: as a string neither file can
# import, and which nothing else would notice going stale.
ARQ_LOG_CONFIG_PATH = "app.worker.settings.ARQ_LOG_CONFIG"

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent


# -----------------------------------------------------------------------------------------
# The JSON envelope.
# -----------------------------------------------------------------------------------------


def test_every_line_is_one_json_object_carrying_the_documented_fields(
    json_logs: _JsonLogCapture,
) -> None:
    """`fly logs | jq` is the entire log surface, so this is an acceptance criterion."""
    logging.getLogger("app.tests.envelope").info("hello")

    line = json_logs.only
    assert set(line) == {"ts", "level", "logger", "message", "process"}
    assert line["level"] == "INFO"
    assert line["logger"] == "app.tests.envelope"
    assert line["message"] == "hello"
    assert line["process"] == "app"
    # ISO-8601, UTC, parseable — not a bare epoch float and not a local timestamp.
    assert line["ts"].endswith("+00:00")


def test_the_message_is_rendered_from_its_arguments(json_logs: _JsonLogCapture) -> None:
    """`%`-style arguments are interpolated, not shipped as a template plus an args list."""
    logging.getLogger("app.tests.args").warning("fetched %d page(s) in %sms", 7, 120)

    assert json_logs.only["message"] == "fetched 7 page(s) in 120ms"


def test_the_process_field_distinguishes_the_worker_from_the_api() -> None:
    """`process` comes from the formatter, so one process cannot mislabel the other.

    Built directly rather than through `configure_logging`, which is idempotent and has
    already run for this process — see `test_configure_logging_is_idempotent`.
    """
    record = logging.LogRecord("app.worker.jobs", logging.INFO, __file__, 1, "up", None, None)

    assert json.loads(JsonFormatter("worker").format(record))["process"] == "worker"
    assert json.loads(JsonFormatter("app").format(record))["process"] == "app"


def test_a_log_records_own_process_attribute_never_shadows_the_process_field(
    json_logs: _JsonLogCapture,
) -> None:
    """`logging.LogRecord` already has an attribute called `process` — the OS pid.

    If the formatter's "promote everything that is not a reserved attribute" loop did not
    know that, every line would report an integer pid under the key that is supposed to say
    `app` or `worker`, and the `jq` filter the whole ticket is built around would match
    nothing.
    """
    logging.getLogger("app.tests.shadow").info("hello")

    assert json_logs.only["process"] == "app"


def test_extra_fields_are_promoted_to_top_level_json_keys(json_logs: _JsonLogCapture) -> None:
    """The point of structured logging: `jq 'select(.status == 429)'`, not a substring grep."""
    logging.getLogger("app.tests.extra").info(
        "GET /websites -> 200", extra={"status": 200, "duration_ms": 12.5, "path": "/websites"}
    )

    line = json_logs.only
    assert line["status"] == 200
    assert line["duration_ms"] == 12.5
    assert line["path"] == "/websites"


def test_a_bound_correlation_id_beats_an_extra_that_claims_otherwise(
    json_logs: _JsonLogCapture,
) -> None:
    """A bound `run_id` is a fact about which job is executing; an `extra=` is a caller's
    claim. A wrong caller must not be able to make a line lie about which run produced it,
    because the whole `jq 'select(.run_id == …)'` workflow rests on that field being true."""
    with run_id_context("the-real-run"):
        logging.getLogger("app.tests.precedence").info("hmm", extra={"run_id": "a-lie"})

    assert json_logs.only["run_id"] == "the-real-run"


def test_an_extra_still_supplies_a_correlation_field_that_nothing_has_bound(
    json_logs: _JsonLogCapture,
) -> None:
    """The case that keeps the rule above from being merely restrictive:
    `ScheduleService._enqueue` logs a `run_id` from inside a cron tick, where only `tick_id`
    is bound, and it is the one line in a batch that names the run a failure stranded."""
    with tick_id_context("the-tick"):
        logging.getLogger("app.tests.precedence").error(
            "could not enqueue", extra={"run_id": "the-stranded-run"}
        )

    line = json_logs.only
    assert line["tick_id"] == "the-tick"
    assert line["run_id"] == "the-stranded-run"


def test_an_extra_cannot_rename_the_envelope(json_logs: _JsonLogCapture) -> None:
    """`ts`, `level`, `logger`, `message` and `process` are the five keys every downstream
    `jq` expression assumes. An `extra=` colliding with one of them is a mistake, and the
    formatter treats it as one rather than as an override."""
    logging.getLogger("app.tests.envelope").info("real message", extra={"ts": "not-a-timestamp"})

    assert json_logs.only["ts"] != "not-a-timestamp"


def test_a_value_json_cannot_serialize_is_stringified_rather_than_losing_the_line(
    json_logs: _JsonLogCapture,
) -> None:
    """A `UUID` in an `extra=` is a mistake worth surviving, not worth dropping a line for."""
    run_id = uuid4()
    logging.getLogger("app.tests.default").info("done", extra={"run_id_value": run_id})

    assert json_logs.only["run_id_value"] == str(run_id)


def test_an_exception_is_serialized_with_its_complete_traceback(
    json_logs: _JsonLogCapture,
) -> None:
    """With no error-tracking service anywhere in this system, this line IS the incident
    record — there is no second copy to go and read, so it is never truncated."""

    def inner() -> None:
        raise ValueError("the crawl exploded")

    try:
        inner()
    except ValueError:
        logging.getLogger("app.tests.exc").error("crawl failed", exc_info=True)

    line = json_logs.only
    assert line["level"] == "ERROR"
    assert "Traceback (most recent call last)" in line["exception"]
    assert "ValueError: the crawl exploded" in line["exception"]
    # The frame the exception was actually raised in, not just the frame that logged it.
    assert "in inner" in line["exception"]


def test_an_unhandled_exception_inside_a_run_carries_its_run_id_and_its_traceback(
    json_logs: _JsonLogCapture,
) -> None:
    """The ticket's acceptance criterion, stated as one test: the two things an operator
    needs to debug a crash are on the same line."""
    with run_id_context("11111111-1111-4111-8111-111111111111"):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("app.tests.exc").exception("job failed")

    line = json_logs.only
    assert line["run_id"] == "11111111-1111-4111-8111-111111111111"
    assert "RuntimeError: boom" in line["exception"]


# -----------------------------------------------------------------------------------------
# Correlation ids.
# -----------------------------------------------------------------------------------------


def test_no_correlation_key_is_emitted_when_nothing_is_bound(json_logs: _JsonLogCapture) -> None:
    """An unbound id is ABSENT, not empty. `jq 'select(.run_id == "x")'` should not have to
    know about a sentinel, and a startup line genuinely belongs to no run."""
    logging.getLogger("app.tests.unbound").info("starting")

    assert "run_id" not in json_logs.only
    assert "request_id" not in json_logs.only


@pytest.mark.parametrize(
    ("binder", "field"),
    [
        (run_id_context, "run_id"),
        (request_id_context, "request_id"),
        (tick_id_context, "tick_id"),
        (user_id_context, "user_id"),
    ],
)
def test_a_bound_id_appears_on_every_line_inside_the_scope_and_none_outside_it(
    json_logs: _JsonLogCapture, binder: Any, field: str
) -> None:
    logger = logging.getLogger("app.tests.scope")

    logger.info("before")
    with binder("value-1"):
        logger.info("inside once")
        logger.info("inside twice")
    logger.info("after")

    before, first, second, after = json_logs.lines
    assert field not in before
    assert first[field] == second[field] == "value-1"
    assert field not in after


def test_a_nested_binding_restores_the_outer_value_rather_than_clearing_it() -> None:
    """`reset(token)` semantics, asserted directly. A `set(None)` on the way out would make
    a nested scope silently unbind the enclosing request."""
    with request_id_context("outer"):
        assert correlation_fields()["request_id"] == "outer"
        with request_id_context("inner"):
            assert correlation_fields()["request_id"] == "inner"
        assert correlation_fields()["request_id"] == "outer"
    assert "request_id" not in correlation_fields()


def test_bind_user_id_fills_in_a_scope_the_caller_already_opened() -> None:
    """How authentication reports a user without owning a scope of its own: the middleware
    opens `user_id_context(None)` before routing, an auth dependency calls `bind_user_id`
    deep inside it, and the middleware's exit is what unbinds."""
    with user_id_context(None):
        assert "user_id" not in correlation_fields()
        bind_user_id("aaaaaaaa-0000-4000-8000-000000000001")
        assert correlation_fields()["user_id"] == "aaaaaaaa-0000-4000-8000-000000000001"
    assert "user_id" not in correlation_fields()


async def test_concurrent_tasks_never_see_each_others_run_id(json_logs: _JsonLogCapture) -> None:
    """THE isolation property, measured under real concurrency rather than argued for.

    `asyncio.Barrier` is what makes this a real test. Without it, each task would bind,
    log, and finish before the next one started, and a module-level global would pass —
    the failure mode only appears when several ids are live at the same instant. Here every
    task binds, then waits for all the others to bind, and only then starts logging: with a
    global, every single line would carry whichever id happened to be bound last.

    Each line carries the id it EXPECTS as an ordinary `extra=` field, so the assertion is
    per line rather than per task — one leaked line out of sixty fails this.
    """
    tasks = 6
    steps = 10
    logger = logging.getLogger("app.tests.concurrency")
    barrier = asyncio.Barrier(tasks)

    async def one_run(index: int) -> None:
        run_id = f"run-{index:03d}"
        with run_id_context(run_id):
            await barrier.wait()
            for step in range(steps):
                logger.info("step %d", step, extra={"expected_run_id": run_id})
                # A real suspension point between lines, so the event loop genuinely
                # interleaves these tasks instead of running each to completion in turn.
                await asyncio.sleep(0)

    await asyncio.gather(*(one_run(index) for index in range(tasks)))

    lines = json_logs.lines
    assert len(lines) == tasks * steps
    assert all(line["run_id"] == line["expected_run_id"] for line in lines)
    assert len({line["run_id"] for line in lines}) == tasks


async def test_a_task_started_before_a_binding_never_inherits_it(
    json_logs: _JsonLogCapture,
) -> None:
    """A `contextvar` is copied into a task at CREATION. A task already running when a
    sibling binds its run id must not pick it up — the other half of isolation, and the one
    a "copy the context at log time" implementation would get wrong."""
    logger = logging.getLogger("app.tests.inheritance")
    started = asyncio.Event()
    may_finish = asyncio.Event()

    async def unbound_sibling() -> None:
        started.set()
        await may_finish.wait()
        logger.info("sibling")

    sibling = asyncio.create_task(unbound_sibling())
    await started.wait()

    with run_id_context("run-abc"):
        logger.info("owner")
        may_finish.set()
        await sibling

    owner, sibling_line = json_logs.lines
    assert owner["run_id"] == "run-abc"
    assert "run_id" not in sibling_line


# -----------------------------------------------------------------------------------------
# Redaction. See the module docstring for why the false-positive cases matter as much.
# -----------------------------------------------------------------------------------------

_A_JWT = (
    "eyJhbGciOiJFUzI1NiIsImtpZCI6ImFiYyIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJhYWFhYWFhYS0wMDAwLTQwMDAtODAwMC0wMDAwMDAwMDAwMDEifQ"
    ".c2lnbmF0dXJlLXRoYXQtaXMtbm90LXJlYWw"
)


@pytest.mark.parametrize(
    ("secret", "text"),
    [
        ("a bearer JWT", f"Authorization: Bearer {_A_JWT}"),
        ("a lowercase scheme", f"authorization: bearer {_A_JWT}"),
        ("a bare JWT", f"verifying {_A_JWT} against the cache"),
        ("an opaque bearer token", "Bearer 8f4c1b2e9d7a6f5c4b3a2e1d0c9b8a77"),
        ("a header mapping repr", f"headers={{'authorization': 'Bearer {_A_JWT}'}}"),
        ("a cookie", "Cookie: sb-access-token=abcdef0123456789"),
        (
            "a Postgres DSN",
            "connecting to postgresql://postgres:s3cr3t-p4ss@db.example.co:5432/postgres",
        ),
        ("an Upstash URL", "REDIS_URL=rediss://default:AX8sAAIjcDE@eu1.upstash.io:6379"),
        ("a service key assignment", "SUPABASE_SECRET_KEY=sb_secret_9Xq2LmNp7Rt4Vw8Zc1Bd"),
        ("a bare provider key", "uploading with sb_secret_9Xq2LmNp7Rt4Vw8Zc1Bd"),
        ("an api key kwarg", 'api_key="9Xq2LmNp7Rt4Vw8Zc1Bd"'),
        # RFC 3986 ends the userinfo at the LAST `@` in the authority, so a password with a
        # literal `@` in it — legal, and easy to end up with in a hand-set local Redis
        # password — is the case a first-`@` match half-redacts and calls done. Caught in
        # review; see rule 3's comment for why its password class deliberately spans `@`.
        (
            "a password containing an @",
            "rediss://default:AX8sAA@IjcDE@eu1.upstash.io:6379",
        ),
        (
            "a Postgres password containing an @, with a path after it",
            "postgresql://postgres:s3cr3t@p4ss@db.example.co:5432/postgres",
        ),
        # Every one of these prefixes starts with a word character, so a `\b` in rule 5
        # would refuse to match a key concatenated straight onto a preceding word.
        ("a provider key glued to a preceding word", "keysb_secret_9Xq2LmNp7Rt4Vw8Zc1Bd"),
    ],
)
def test_the_redaction_filter_scrubs_credential_shaped_values(secret: str, text: str) -> None:
    """One case per shape a credential has ever reached a log line in. `secret` is the
    parametrize id and exists to name the case in a failure report."""
    scrubbed = redact(text)

    assert REDACTED in scrubbed, secret
    # Every distinguishing fragment of every secret above, checked against every case —
    # including the TAILS (`IjcDE`, `p4ss`) that a rule stopping at the first `@` of an
    # authority would leave behind while still reporting a redaction it did not finish.
    for fragment in (
        _A_JWT,
        "s3cr3t-p4ss",
        "AX8sAAIjcDE",
        "IjcDE",
        "p4ss",
        "9Xq2LmNp7Rt4Vw8Zc1Bd",
        "abcdef0123456789",
    ):
        assert fragment not in scrubbed, f"{secret} leaked {fragment}"


@pytest.mark.parametrize(
    "text",
    [
        # The live example: app/core/auth/dependencies.py logs this on every unauthenticated
        # request, and an over-eager `Bearer \S+` rule would rewrite the word after it.
        "Rejected an authentication attempt: no Bearer credentials on the request",
        "Rejected an authentication attempt: token rejected during verification (PyJWTError)",
        "crawl: run 3f2a1b4c-0000-4000-8000-000000000001 completed: 12 page(s)",
        "GET /websites/3f2a1b4c-0000-4000-8000-000000000001/runs -> 200",
        "uploading to crawl-payloads/websites/abc/runs/def.jsonl.gz",
        "Missing required environment variable(s): DATABASE_URL, SUPABASE_SECRET_KEY.",
        "app.features.crawl.internals.fetcher",
    ],
)
def test_the_redaction_filter_leaves_ordinary_lines_alone(text: str) -> None:
    """A filter that mangles ordinary logs is a filter somebody switches off."""
    assert redact(text) == text


def test_redaction_is_idempotent() -> None:
    """Both the filter and the formatter call `redact`, so it runs twice on most lines."""
    once = redact(f"Authorization: Bearer {_A_JWT}")

    assert redact(once) == once


def test_a_token_never_reaches_the_stream_however_it_was_passed(
    json_logs: _JsonLogCapture,
) -> None:
    """Message, `%`-arguments, `extra=` values, and a traceback are four different routes
    into a log line. All four are covered, because a filter that only guards one of them
    guards none of them in practice."""
    logger = logging.getLogger("app.tests.redaction")

    logger.info("verifying %s", _A_JWT)
    logger.info("verifying a token", extra={"header": f"Bearer {_A_JWT}"})
    try:
        raise RuntimeError(f"upstream rejected Bearer {_A_JWT}")
    except RuntimeError:
        logger.exception("verification blew up")

    assert _A_JWT not in json_logs.raw
    assert json_logs.raw.count(REDACTED) >= 3
    # And the lines are still valid JSON afterwards — redaction must not corrupt the
    # envelope it runs inside.
    assert len(json_logs.lines) == 3


def test_the_filter_rewrites_the_record_so_a_plainer_handler_is_protected_too() -> None:
    """`RedactionFilter` mutates the record rather than only the text this handler prints,
    so a second handler — pytest's `caplog`, a debug console — cannot print what this one
    just scrubbed."""
    record = logging.LogRecord(
        "app.tests", logging.INFO, __file__, 1, "token is %s", (_A_JWT,), None
    )

    assert RedactionFilter().filter(record) is True
    assert _A_JWT not in record.getMessage()
    assert record.args is None


def test_a_record_with_nothing_to_redact_keeps_its_template_and_arguments() -> None:
    """The common case is left untouched, so the filter costs no fidelity when it finds
    nothing: `record.msg` is still the `%`-style template a downstream formatter expects."""
    record = logging.LogRecord(
        "app.tests", logging.INFO, __file__, 1, "fetched %d pages", (7,), None
    )

    assert RedactionFilter().filter(record) is True
    assert record.msg == "fetched %d pages"
    assert record.args == (7,)


# -----------------------------------------------------------------------------------------
# Process configuration.
# -----------------------------------------------------------------------------------------


def test_configure_logging_is_idempotent() -> None:
    """`app.main.create_app()` and `app.worker.settings` both call it, and the test suite
    imports both into one process. Without the guard every line would be emitted twice —
    which is exactly what the previous `basicConfig` implementation got for free and this
    one has to state explicitly."""
    root = logging.getLogger()
    before = list(root.handlers)

    configure_logging("app")
    configure_logging("worker")

    assert list(root.handlers) == before


def test_httpx_is_quieted_because_outbound_requests_are_this_systems_whole_job() -> None:
    """`httpx` logs a line per request at INFO, and the crawler makes up to
    `CRAWL_MAX_PAGES` of them per run. Left at INFO it would bury the run's own lines under
    its own transport chatter."""
    for name in ("httpx", "httpcore", "uvicorn.access"):
        assert logging.getLogger(name).level == logging.WARNING, name


# -----------------------------------------------------------------------------------------
# arq's own lines. See app/worker/settings.py's ARQ_LOG_CONFIG for the mechanism.
# -----------------------------------------------------------------------------------------


def test_the_arq_log_config_makes_arqs_own_lines_propagate_to_the_json_handler() -> None:
    """`disable_existing_loggers` is the dangerous key here: left at its default of `True`,
    applying this config would silence every `app.*` logger already created by the import
    that just happened, to configure one library."""
    assert ARQ_LOG_CONFIG["disable_existing_loggers"] is False

    arq_logger = logging.getLogger("arq")
    previous = (list(arq_logger.handlers), arq_logger.propagate, arq_logger.level)
    try:
        logging.config.dictConfig(ARQ_LOG_CONFIG)
        assert arq_logger.handlers == []
        assert arq_logger.propagate is True
        # An unrelated logger created before the call is still enabled.
        assert logging.getLogger("app.core.logging").disabled is False
    finally:
        arq_logger.handlers, arq_logger.propagate, arq_logger.level = previous


def test_the_dotted_path_arq_is_given_actually_resolves() -> None:
    """`arq --custom-log-dict` takes a STRING, resolved with the same `import_string` used
    here. A typo in it is a worker that fails to boot, in production, on the deploy that
    introduced it — and nothing else in the suite would catch it."""
    assert import_string(ARQ_LOG_CONFIG_PATH) is ARQ_LOG_CONFIG


def test_fly_toml_and_dev_sh_both_hand_arq_that_dotted_path() -> None:
    """The deployed worker and the local one must log the same way, and neither file can be
    imported to check itself. `test_worker_settings.py` restates fly.toml's `kill_timeout`
    for the same reason; this reads the files instead, because a dotted path is a name that
    silently stops resolving rather than a number that drifts."""
    fly = tomllib.loads((_BACKEND_DIR / "fly.toml").read_text())
    worker_command = fly["processes"]["worker"]
    assert f"--custom-log-dict {ARQ_LOG_CONFIG_PATH}" in worker_command

    dev_sh = (_REPO_ROOT / "scripts" / "dev.sh").read_text()
    assert ARQ_LOG_CONFIG_PATH in dev_sh
    assert "--custom-log-dict" in dev_sh


# -----------------------------------------------------------------------------------------
# The worker's two jobs bind their ids. Proven by driving the real job functions with the
# service layer stubbed out — the binding is the job's responsibility, and the point is that
# a line logged by something the job merely *called* comes back tagged.
# -----------------------------------------------------------------------------------------


async def test_crawl_task_binds_run_id_for_everything_it_calls(
    json_logs: _JsonLogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    deep_logger = logging.getLogger("app.features.crawl.internals.fetcher")

    class _Service:
        async def execute_run(self, requested: UUID, *, max_attempts: int) -> None:
            # Stands in for every module under CrawlService that logs without ever being
            # handed a run id.
            deep_logger.debug("fetched a page", extra={"status": 200})
            assert requested == run_id
            return None

    monkeypatch.setattr(jobs, "build_crawl_service", lambda *args, **kwargs: _Service())

    result = await jobs.crawl_task({"db_pool": None, "http_client": None, "storage": None}, run_id)

    assert result == "no outcome"
    assert [line["run_id"] for line in json_logs.lines] == [str(run_id), str(run_id)]
    assert json_logs.lines[0]["logger"] == "app.features.crawl.internals.fetcher"


async def test_schedule_tick_binds_one_tick_id_for_the_whole_tick(
    json_logs: _JsonLogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER-164's per-tick summary line is the one this has to reach — the ticket asks for
    the tick to be correlated, not for a second summary beside the one that exists."""

    class _Service:
        async def run_due_schedules(self, queue: object) -> object:
            raise RuntimeError("Redis went away")

    monkeypatch.setattr(jobs, "build_schedule_service", lambda *args: _Service())

    result = await jobs.schedule_tick({"db_pool": None, "redis": None})

    assert result == "failed"
    tick_ids = {line["tick_id"] for line in json_logs.lines}
    assert len(tick_ids) == 1
    # A UUID, not a counter: two workers minting tick ids must not collide.
    UUID(tick_ids.pop())
