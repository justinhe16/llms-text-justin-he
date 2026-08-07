"""Tests for `app.worker.settings` — the configuration arq runs the worker with.

These are mostly assertions about *constants*, which normally earns a shrug. They are here
because every value in this file regresses silently: nothing crashes, no other test goes
red, and the symptom arrives days later as an Upstash bill, a crawl killed mid-flight by a
routine deploy, or a worker that never started at all.

**Every `Worker` built here passes `handle_signals=False`.** arq's constructor otherwise
calls `loop.add_signal_handler` for SIGINT and SIGTERM on whatever loop it finds, and
`Worker.close()` never removes them — so a test that constructed one with signals on would
leave pytest's own Ctrl-C handling replaced for the rest of the session. What that flag
would have selected is asserted from its inputs instead, and the drained-on-SIGTERM
behaviour itself is exercised for real in tests/test_worker_shutdown.py.
"""

import pytest
from anthropic import AsyncAnthropic
from arq.worker import Worker, get_kwargs

from app.core.settings import settings
from app.infrastructure.queue.pool import redis_settings_from_url
from app.worker import policy
from app.worker import settings as worker_settings
from app.worker.jobs import crawl_task, noop, reaper_tick, schedule_tick
from app.worker.settings import WorkerSettings


# backend/fly.toml's `kill_timeout`, restated because a TOML file cannot be imported. The
# two move together; the ordering test below is what notices if they stop agreeing.
FLY_KILL_TIMEOUT_SECONDS = 240

# Fly's documented maximum for `kill_timeout`, and therefore the hard ceiling on every
# shutdown budget underneath it.
FLY_KILL_TIMEOUT_CEILING_SECONDS = 300


def test_poll_delay_is_five_seconds() -> None:
    """`poll_delay` is 5. This is a cost assertion, not a style preference.

    An idle arq worker issues one Redis command per poll. At arq's default of 0.5s that is
    2/s — 172,800 commands a day to do nothing at all — and Upstash bills per command. At
    5s it is 17,280. The only thing it costs is up to 5s before a queued job starts, which
    does not matter for a crawl measured in tens of seconds.

    Nothing fails when this regresses. That is exactly why it is asserted here.
    """
    assert WorkerSettings.poll_delay == 5


def test_max_jobs_is_bounded() -> None:
    """Concurrency is capped, and low.

    Unbounded — or arq's default of 10 — concurrent crawls on one small Fly machine would
    exhaust the shared Supabase connection budget well before they exhausted CPU: the API
    and the worker draw from the same `db_pool_max_size`.
    """
    assert WorkerSettings.max_jobs == 2


def test_the_crawl_cap_lands_before_arqs_job_timeout() -> None:
    """`crawl_max_wall_clock_s` < `job_timeout`. THE ORDERING PER-166 CORRECTED.

    The application-level cap has to fire FIRST. When it does, `crawl_site`'s own
    `asyncio.timeout` ends the crawl, `CrawlService` records a clean `failed` with a
    sanitized message, and `runs.stats` says which cap stopped it. When it does not — which
    was the deployed state until PER-166, with `job_timeout` at 180s against a 300s crawl
    cap — arq cancels the whole job first instead, and the run's outcome is decided by
    cancellation handling and the reaper rather than by the crawler.

    Asserted as an inequality between the two real settings objects, not as two literals: the
    crawl cap is an environment-configurable `Settings` field and the timeout is not, so the
    relationship is the only thing that can be pinned here.
    """
    assert settings.crawl_max_wall_clock_s < WorkerSettings.job_timeout


def test_the_shutdown_drain_nests_inside_flys_kill_timeout() -> None:
    """job_completion_wait < kill_timeout <= Fly's ceiling.

    Two nested budgets, and the graceful shutdown is theater unless they nest: if
    `kill_timeout` drops below `job_completion_wait`, Fly sends SIGKILL through the middle of
    the drain and it makes no difference that arq was being polite.

    **`job_timeout < job_completion_wait` WAS asserted here, and PER-166 removed it — the
    assertion was wrong, not the timeout.** Its argument was that a drain shorter than the
    job timeout cancels a job that is still legitimately running. True as far as it goes, but
    it has a hard external ceiling underneath it: Fly caps `kill_timeout` at 300s, so a drain
    budget can never exceed that, and a `job_timeout` set above the 300s crawl cap — which is
    the whole point of the test above — can never fit inside one. The two constraints are
    unsatisfiable together. The old assertion held only because `job_timeout` was set to a
    value (180s) that was itself the bug.

    What changed underneath, and why removing it is safe rather than convenient: PER-166 made
    a cancelled crawl RECOVERABLE. `CrawlService.execute_run` catches `CancelledError`, hands
    the run back to `pending` (or fails it, if the budget is spent) and re-raises, and the
    stuck-run reaper sweeps the row if even that write does not land. When this assertion was
    written, a job cancelled by an expiring drain meant a permanently stranded run and a
    website nothing could ever crawl again — which is why the ladder mattered so much more
    then than it does now.

    The drain is therefore still non-zero, still ordered inside Fly's budget, and still
    exercised for real in tests/test_worker_shutdown.py. It is simply no longer claimed to
    cover the worst case, because it never could.
    """
    assert WorkerSettings.job_completion_wait < FLY_KILL_TIMEOUT_SECONDS
    assert FLY_KILL_TIMEOUT_SECONDS <= FLY_KILL_TIMEOUT_CEILING_SECONDS


def test_the_reaper_threshold_sits_above_everything_that_bounds_a_live_run() -> None:
    """STUCK_AFTER > job_timeout, > the drain budget, and > the crawl cap.

    The reaper's threshold is the one number in this file where being too small is actively
    harmful rather than merely wasteful: reaping a run that is legitimately still working
    produces a second crawl of the same site, a second payload upload, and a race between two
    workers to write the same row. Reaping one late costs five minutes.

    So it is asserted against every clock that can legitimately keep a run in `processing`:
    arq's own timeout, the drain a deploy may add on top of it, and the crawl's application
    cap. `STUCK_AFTER_SECONDS` is derived from `JOB_TIMEOUT_SECONDS` in `policy.py` rather
    than written as a literal, so this test is pinning the grace margin, not the arithmetic.
    """
    assert policy.STUCK_AFTER_SECONDS > policy.JOB_TIMEOUT_SECONDS
    assert policy.STUCK_AFTER_SECONDS > policy.JOB_COMPLETION_WAIT_SECONDS
    assert policy.STUCK_AFTER_SECONDS > settings.crawl_max_wall_clock_s


def test_the_orphan_threshold_sits_above_the_longest_retry_backoff() -> None:
    """A run waiting out its own backoff is `pending` with a DEFERRED job behind it, not an
    orphan. Sweeping it early would enqueue a second job for work that was never lost, so the
    orphan threshold has to clear the longest delay the retry ladder can ask for."""
    assert policy.ORPHANED_PENDING_AFTER_SECONDS > max(policy.RETRY_DELAYS_SECONDS)


def test_the_reaper_finishes_well_inside_its_own_cadence() -> None:
    """A pass that has not finished before the next one is due is not helped by running into
    it — the same argument `CRON_TICK_TIMEOUT_SECONDS` makes for the schedule tick."""
    assert policy.REAPER_TIMEOUT_SECONDS < policy.REAPER_INTERVAL_MINUTES * 60


def test_the_retry_backoff_grows_with_each_attempt() -> None:
    """~10s, then ~60s, then ~300s. A site that is briefly down often recovers within
    minutes; three tries in six seconds accomplish nothing but three identical failures.

    Asserted as a strictly increasing sequence produced by the real function rather than as
    three literals, so a future ladder of a different length still has to grow.
    """
    delays = [policy.retry_delay_seconds(attempt) for attempt in range(1, 4)]
    assert delays == sorted(delays)
    assert len(set(delays)) == len(delays), "each attempt must wait longer than the last"
    assert delays[0] >= 10

    # Clamped at both ends rather than raising — an attempt number past the end of the ladder
    # is an accounting bug somewhere else, and the longest delay is a better answer to it
    # than an IndexError inside a worker's error-handling path.
    assert policy.retry_delay_seconds(0) == delays[0]
    assert policy.retry_delay_seconds(99) == delays[-1]


def test_max_tries_matches_the_retry_budget_the_run_itself_carries() -> None:
    """arq's own ceiling is set to `MAX_ATTEMPTS`, as a backstop for the case where the two
    counters legitimately disagree — a job cancelled by SIGTERM increments arq's `job_try`
    without incrementing `runs.attempts`. See `WorkerSettings.max_tries`' own comment."""
    assert WorkerSettings.max_tries == policy.MAX_ATTEMPTS


def test_job_completion_wait_is_non_zero_so_sigterm_drains_instead_of_cancelling() -> None:
    """A non-zero `job_completion_wait` is what makes SIGTERM graceful at all.

    arq's default handler cancels in-flight jobs the moment the signal lands. Only a
    non-zero `job_completion_wait` selects `handle_sig_wait_for_completion`, which stops
    claiming new work and waits instead. Left at its default of 0, every Fly deploy would
    kill a running crawl — and nothing about that would look like a configuration problem
    while you were debugging it.
    """
    assert WorkerSettings.job_completion_wait > 0


def test_functions_is_not_empty_so_the_worker_can_actually_start() -> None:
    """arq refuses to construct a `Worker` with no registered functions.

    `Worker.__init__` raises `RuntimeError('at least one function or cron_job must be
    registered')` before it opens a socket, so an empty `functions` list does not produce
    an idle worker waiting for its first real job — it produces a machine that crash-loops
    on Fly with no HTTP listener to fail a health check, which is close to invisible.
    """
    assert WorkerSettings.functions
    assert noop in WorkerSettings.functions


def test_crawl_task_is_registered_beside_noop() -> None:
    """`crawl_task` joins `functions` rather than replacing `noop` — see `noop`'s own
    docstring for why it stays."""
    assert crawl_task in WorkerSettings.functions


def test_every_setting_declared_here_actually_reaches_the_worker() -> None:
    """Guards the silent-drop failure mode of arq's settings loading.

    `arq.worker.get_kwargs` builds the `Worker(...)` call from `WorkerSettings.__dict__`
    intersected with `Worker.__init__`'s parameter names. Two things fall out of that, and
    both fail quietly:

    * A setting moved onto a base class disappears, because `__dict__` does not include
      inherited attributes, and arq's default silently takes its place.
    * A setting misspelled here is not an error either. It simply is not passed.

    So this asserts the intersection is total: everything declared on the class is a real
    `Worker` parameter.
    """
    passed = get_kwargs(WorkerSettings)
    declared = {name for name in vars(WorkerSettings) if not name.startswith("__")}

    unrecognized = declared - set(passed)
    assert not unrecognized, (
        f"{sorted(unrecognized)} are declared on WorkerSettings but are not parameters of "
        "arq's Worker, so arq ignores them entirely."
    )


async def test_the_declared_values_arrive_on_a_constructed_worker() -> None:
    """The settings survive the trip through `get_kwargs` with their values intact.

    The test above proves the names are recognized; this proves the values land. Together
    they cover both halves of "arq is really running with what this file says".

    `worker.functions` includes `"cron:schedule_tick"` alongside the two plain job names:
    `Worker.__init__` folds every `cron_jobs` entry into the same `functions` dict it builds
    from `functions=`, keyed by each `CronJob`'s own `.name` (arq's default is
    `"cron:" + coroutine.__qualname__` — see `test_cron_jobs_registers_the_schedule_tick`
    below for where that default is pinned rather than assumed here).
    """
    worker = Worker(**{**get_kwargs(WorkerSettings), "handle_signals": False})
    try:
        assert worker.poll_delay_s == WorkerSettings.poll_delay
        assert worker.max_jobs == WorkerSettings.max_jobs
        assert worker.job_timeout_s == WorkerSettings.job_timeout
        assert worker._job_completion_wait == WorkerSettings.job_completion_wait
        assert worker.max_tries == WorkerSettings.max_tries
        assert set(worker.functions) == {
            noop.__qualname__,
            crawl_task.__qualname__,
            "cron:schedule_tick",
            "cron:reaper_tick",
        }
        # The declared `cron_jobs` list itself reached the constructed `Worker`, not merely
        # its names landing in `functions` above.
        assert [job.coroutine for job in worker.cron_jobs] == [schedule_tick, reaper_tick]
    finally:
        await worker.close()


def test_cron_jobs_registers_the_schedule_tick() -> None:
    """`cron_jobs` names the tick, and it fires once a minute with one retry.

    `second == 0`, not `{0}`: the installed arq (checked directly with a throwaway `cron()`
    call, not guessed) stores whatever was passed to `second=` verbatim on the `CronJob`
    dataclass rather than normalizing it to a set at construction time — normalization
    happens later, inside `CronJob.calculate_next`, not here.
    """
    assert WorkerSettings.cron_jobs
    tick, _reaper = WorkerSettings.cron_jobs
    assert tick.coroutine is schedule_tick
    assert tick.name == "cron:schedule_tick"
    assert tick.second == 0
    assert tick.max_tries == 1
    assert tick.timeout_s == worker_settings.CRON_TICK_TIMEOUT_SECONDS


def test_cron_jobs_registers_the_reaper_every_five_minutes() -> None:
    """The second cron job, and the only thing in this system that can un-stick a run.

    `minute` is asserted as the full set of firing minutes rather than as "every five",
    because that set is what arq actually evaluates and because an interval that stopped
    dividing the hour would change it in a way worth seeing. `max_tries == 1` for the same
    reason the schedule tick has it: a failed pass is re-evaluated from the database five
    minutes later, never re-run against the state it half-left.
    """
    _tick, reaper = WorkerSettings.cron_jobs
    assert reaper.coroutine is reaper_tick
    assert reaper.name == "cron:reaper_tick"
    assert reaper.second == 0
    assert reaper.minute == set(range(0, 60, policy.REAPER_INTERVAL_MINUTES))
    assert reaper.max_tries == 1
    assert reaper.timeout_s == policy.REAPER_TIMEOUT_SECONDS


def test_crawl_task_is_registered_under_the_literal_name_a_sibling_ticket_enqueues() -> None:
    """PER-160 enqueues this job **by the literal string** `"crawl_task"`, not by importing
    the function — a queue producer and its consumer are two different processes and the
    only thing they agree on is a name on the wire. arq registers a bare coroutine under
    `coroutine.__qualname__` (`arq.worker.func`), so this pins that `crawl_task`'s
    `__qualname__` is exactly `"crawl_task"` — no wrapping in `arq.worker.func(name=...)`,
    no nesting inside a class that would prefix it, and no renaming the function itself.
    """
    assert crawl_task.__qualname__ == "crawl_task"


def test_a_tls_url_gets_tls_with_hostname_verification() -> None:
    """A `rediss://` URL produces a TLS connection with hostname verification on.

    Production is Upstash over `rediss://`, and this is the branch `make dev` can never
    exercise. arq derives `ssl` from the scheme but leaves `ssl_check_hostname` off, which
    validates that a certificate chains to a trusted CA but not that it was issued for the
    host actually dialled — on a connection that carries the Redis password.
    """
    redis_settings = redis_settings_from_url(
        "rediss://default:not-a-real-password@example-12345.upstash.io:6379"
    )

    assert redis_settings.ssl is True
    assert redis_settings.ssl_check_hostname is True
    assert redis_settings.host == "example-12345.upstash.io"
    assert redis_settings.port == 6379


async def test_worker_settings_construct_against_a_tls_url() -> None:
    """The whole point of deriving TLS: a TLS worker constructs like any other.

    No connection is made — `Worker.__init__` does not dial Redis, `Worker.main()` does —
    so this asserts the configuration reaches the worker, not that Upstash answered.
    """
    redis_settings = redis_settings_from_url(
        "rediss://default:not-a-real-password@example-12345.upstash.io:6379"
    )
    worker = Worker(
        functions=[noop],
        redis_settings=redis_settings,
        queue_name="per_157_tls_construction_test",
        handle_signals=False,
    )
    try:
        assert worker.redis_settings is not None
        assert worker.redis_settings.ssl is True
    finally:
        await worker.close()


def test_a_plain_url_stays_plaintext_so_local_development_works() -> None:
    """`redis://` must not get TLS bolted onto it.

    docker-compose.yml's `redis:7-alpine` speaks no TLS. Hardcoding `ssl=True` — the
    obvious way to "just make production work" — breaks `make dev` outright, and hardcoding
    `ssl=False` would put the Upstash password on the wire in cleartext. Deriving it from
    the scheme is the only version that is right in both places, so both directions are
    asserted.
    """
    redis_settings = redis_settings_from_url("redis://localhost:6379/0")

    assert redis_settings.ssl is False
    assert redis_settings.host == "localhost"
    assert redis_settings.database == 0

    # Not merely "not True": arq applies `ssl_check_hostname` only when ssl is on, so
    # asserting the default here documents that the override above is scoped to TLS.
    assert redis_settings.ssl_check_hostname is False


def test_the_worker_connects_wherever_redis_url_points() -> None:
    """`WorkerSettings.redis_settings` is built from REDIS_URL, not from a literal.

    tests/conftest.py sets REDIS_URL before anything under `app` is imported, so this
    asserts the wiring rather than a value: the worker follows configuration in every
    environment, which is what lets one class serve both Upstash and the local container.
    """
    assert WorkerSettings.redis_settings == redis_settings_from_url(settings.redis_url)


def test_startup_and_shutdown_hooks_are_registered() -> None:
    """`on_startup`/`on_shutdown` are wired, and are this module's own functions.

    Without `on_startup` the worker has no Postgres pool and every job that touches the
    database fails one at a time; without `on_shutdown` every deploy leaks that pool's
    connections against Supabase's cap.
    """
    assert WorkerSettings.on_startup is worker_settings.open_worker_resources
    assert WorkerSettings.on_shutdown is worker_settings.close_worker_resources


class _SpyClient:
    """A stand-in for `httpx.AsyncClient` that only records whether `aclose()` ran."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _SpyAnthropicClient:
    """A stand-in for `AsyncAnthropic` that only exposes `close()` — deliberately NOT
    `aclose()`, unlike `_SpyClient` above. `AsyncAnthropic.close()` is a coroutine, spelled
    differently from every `httpx.AsyncClient` this module also closes, and a test built
    around a spy that happened to expose BOTH names would not catch a
    `close_worker_resources` that called the wrong one — it would just silently invoke
    whichever the implementation picked. This spy is written so that copying the
    `await client.aclose()` idiom from `http_client`/`storage_client` onto the Anthropic
    client raises `AttributeError`, exactly as it would against the real SDK.
    """

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_close_worker_resources_closes_the_http_client_and_the_storage_client() -> None:
    """PER-163 added a second `httpx.AsyncClient` (`ctx["storage_client"]`, built by
    `build_storage_client` for `SupabaseStorage`) beside the crawl task's own
    `ctx["http_client"]`. Both must actually be closed on shutdown — `close_pool()` closing
    the Postgres pool says nothing about either of these, and a client left open leaks a
    connection pool every deploy.
    """
    http_client = _SpyClient()
    storage_client = _SpyClient()
    ctx: dict[object, object] = {"http_client": http_client, "storage_client": storage_client}

    await worker_settings.close_worker_resources(ctx)

    assert http_client.closed
    assert storage_client.closed


async def test_close_worker_resources_closes_the_anthropic_client_via_close_not_aclose() -> None:
    """PER-180's third client, closed with the SDK's own `close()` rather than the `aclose()`
    every `httpx.AsyncClient` above uses. `_SpyAnthropicClient` only defines `close`, so this
    test fails with `AttributeError` if `close_worker_resources` is ever "fixed" to call
    `aclose()` here by analogy with its two neighbours — exactly the copy-paste bug this spy
    exists to catch, rather than merely trusting a docstring to prevent it.
    """
    anthropic_client = _SpyAnthropicClient()
    ctx: dict[object, object] = {"anthropic_client": anthropic_client}

    await worker_settings.close_worker_resources(ctx)

    assert anthropic_client.closed


async def test_close_worker_resources_tolerates_a_startup_that_never_opened_anything() -> None:
    """`Worker.run()`'s `finally` calls `close_worker_resources` even when `on_startup`
    failed before it got as far as opening any client — every `ctx.get(...)` inside it must
    be `None` and skipped rather than raising `KeyError`, `"anthropic_client"` included: a
    flag-off worker never sets that key at all (see the `open_worker_resources` tests below),
    and this same empty-`ctx` call is what proves `close_worker_resources` tolerates that
    absence exactly as it tolerates every other missing key."""
    await worker_settings.close_worker_resources({})


# -----------------------------------------------------------------------------------------
# PER-180: `ctx["anthropic_client"]` is the one conditional resource `open_worker_resources`
# publishes — built only when `settings.crawl_enrich_with_llm` is on. `open_pool` is
# monkeypatched in both tests below so that exercising the real function does not require a
# real Postgres connection; everything else `open_worker_resources` builds
# (`build_crawl_client`, `build_storage_client`, `build_supabase_storage`) opens no socket on
# construction (see each factory's own docstring), so those run for real.
# -----------------------------------------------------------------------------------------


async def _fake_open_pool(settings: object) -> object:
    """A stand-in for `app.infrastructure.db.pool.open_pool`, monkeypatched onto the NAME
    `open_worker_resources` calls rather than onto the real `db.pool` module — so the real
    module's process-wide singleton is never touched and nothing here needs a live Postgres.
    What `open_worker_resources` does with the pool it gets back is covered by
    `tests/test_pool.py`; these two tests are only about the Anthropic client beside it.
    """
    return object()


async def test_open_worker_resources_builds_no_anthropic_client_while_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance criterion 1, against the real function. With the flag off — the default,
    and the case this whole test suite and CI run under — `ctx` never gets an
    `"anthropic_client"` key at all, which is what lets `app.worker.jobs.crawl_task` tell
    "the flag is off" apart from "the client failed to build" with a plain `ctx.get(...)`.
    """
    monkeypatch.setattr(worker_settings, "open_pool", _fake_open_pool)
    monkeypatch.setattr(worker_settings.settings, "crawl_enrich_with_llm", False)

    ctx: dict[str, object] = {}
    await worker_settings.open_worker_resources(ctx)

    assert "anthropic_client" not in ctx


async def test_open_worker_resources_builds_the_anthropic_client_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the guard: with the flag on and a key configured, `ctx` gets a real
    `AsyncAnthropic` — built with no socket opened and no network call made, the same
    property `build_anthropic_client`'s own docstring documents."""
    monkeypatch.setattr(worker_settings, "open_pool", _fake_open_pool)
    monkeypatch.setattr(worker_settings.settings, "crawl_enrich_with_llm", True)
    monkeypatch.setattr(worker_settings.settings, "anthropic_api_key", "not-a-real-key")

    ctx: dict[str, object] = {}
    await worker_settings.open_worker_resources(ctx)

    assert "anthropic_client" in ctx
    assert isinstance(ctx["anthropic_client"], AsyncAnthropic)
