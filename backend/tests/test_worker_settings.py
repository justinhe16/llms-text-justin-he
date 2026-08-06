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

from arq.worker import Worker, get_kwargs

from app.core.settings import settings
from app.infrastructure.queue.pool import redis_settings_from_url
from app.worker import settings as worker_settings
from app.worker.jobs import crawl_task, noop
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


def test_shutdown_budgets_are_ordered_inside_flys_kill_timeout() -> None:
    """job_timeout < job_completion_wait < kill_timeout <= Fly's ceiling.

    Three nested budgets, and the graceful shutdown is theater unless they nest in that
    order. If `job_completion_wait` ever drops below `job_timeout`, arq stops waiting while
    the job is still legitimately running and cancels it — the exact SIGTERM behaviour the
    setting exists to prevent. If `kill_timeout` drops below `job_completion_wait`, Fly
    sends SIGKILL through the middle of the drain and it makes no difference that arq was
    being polite.
    """
    assert WorkerSettings.job_timeout < WorkerSettings.job_completion_wait
    assert WorkerSettings.job_completion_wait < FLY_KILL_TIMEOUT_SECONDS
    assert FLY_KILL_TIMEOUT_SECONDS <= FLY_KILL_TIMEOUT_CEILING_SECONDS


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
    """
    worker = Worker(**{**get_kwargs(WorkerSettings), "handle_signals": False})
    try:
        assert worker.poll_delay_s == WorkerSettings.poll_delay
        assert worker.max_jobs == WorkerSettings.max_jobs
        assert worker.job_timeout_s == WorkerSettings.job_timeout
        assert worker._job_completion_wait == WorkerSettings.job_completion_wait
        assert set(worker.functions) == {noop.__qualname__, crawl_task.__qualname__}
    finally:
        await worker.close()


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
