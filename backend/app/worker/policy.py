"""The numbers the worker runs by, and the arguments for each of them.

Split out of `app/worker/settings.py` by PER-166, for one reason: the stuck-run reaper's
staleness threshold is **derived** from `JOB_TIMEOUT_SECONDS`, and the job that applies it
(`app.worker.jobs.reaper_tick`) is imported BY `settings.py`. Leaving the constant in
`settings.py` would have made that derivation a circular import, and the usual workaround —
restating the number in `jobs.py` with a comment asking the next person to keep the two in
sync — is exactly the kind of silent drift the reaper exists to clean up after. One module
owns the ladder; `settings.py` hands the arq-facing half of it to `Worker(...)`, and
`jobs.py` reads the reaper's half.

**Nothing here is a `Settings` field, and that is deliberate.** These are properties of how
arq is run, not of the environment it runs in — `app.core.settings.Settings` carries the
crawl caps, the pool sizes, and the abuse limits precisely because those are things an
operator may want to change per environment. Changing `job_timeout` or the reaper's grace
margin means re-reading the ladder below, not setting an environment variable.
"""

from typing import Final


# --- the poll loop ---------------------------------------------------------------------

# HOW OFTEN THE WORKER ASKS REDIS FOR WORK. THIS IS NOT A TUNING PREFERENCE.
#
# An idle arq worker issues exactly one command per poll — a single ZRANGEBYSCORE over the
# queue's sorted set (`Worker._poll_iteration`); the heartbeat beside it early-returns
# until `health_check_interval` elapses. So the poll interval alone sets the idle command
# rate, and Upstash bills per command:
#
#     poll_delay = 0.5 (arq's default)  ->  2 polls/s   x 86,400 s = 172,800 commands/day
#     poll_delay = 5   (this setting)   ->  0.2 polls/s x 86,400 s =  17,280 commands/day
#
# THE CRON JOBS DO NOT CHANGE THAT ARITHMETIC, and there are two of them now. arq decides
# whether a cron job is due with local time math inside the same poll iteration and only
# touches Redis on the tick it actually fires, so `schedule_tick` (every 60s) adds roughly
# one enqueue a minute and `reaper_tick` (every 5 minutes, `REAPER_INTERVAL_MINUTES` below)
# adds roughly one every five — call it 1,730 commands a day between them, on top of the
# 17,280 above, before any real crawl work. What would invalidate this block is a cron job
# on a SUB-MINUTE schedule, which would fire inside individual poll iterations rather than
# between them; neither of these is one.
#
# Ten times cheaper, for doing nothing at all. What it costs is up to 5 seconds of latency
# before a queued job starts, which is irrelevant against a crawl measured in tens of
# seconds. Anyone tempted to lower this for snappier local development should set it in
# their own environment, not here — tests/test_worker_settings.py asserts this exact value
# because regressing it is silent and shows up as a bill.
POLL_DELAY_SECONDS: Final = 5

# Concurrent jobs per worker machine. Crawls are I/O heavy, so the ceiling is memory and
# Postgres connections rather than CPU.
#
# Note what `db_pool_max_size` is NOT: a shared budget. The API and the worker each build
# their own pool from that one setting, so the number Supabase sees is up to
# `db_pool_max_size` per process — today up to 20, not 10 — and raising it raises both.
# That is comfortable at two concurrent crawls and would not be at ten. If crawl
# concurrency ever grows, the pool sizing needs to become per-process before this does.
#
# Both cron jobs draw from this same budget — arq runs cron jobs through the same
# concurrency semaphore as queued ones. Two crawls already in flight therefore DELAY a tick
# rather than skipping it, and both ticks are written to survive that: the schedule tick's
# work is idempotent (the schedules it did not get to are still due) and the reaper's is
# too (a run it did not reach this time is still stuck five minutes later).
MAX_JOBS: Final = 2


# --- how long one job may run --------------------------------------------------------

# THE OUTER BACKSTOP FOR A WEDGED JOB — not the thing that is supposed to stop a long crawl.
#
# `Settings.crawl_max_wall_clock_s` (300s) is what ends a crawl that is simply taking too
# long: `crawl_site`'s own `asyncio.timeout` fires, the run records a clean `failed` with a
# sanitized message, and `runs.stats` says which cap stopped it. This number sits at twice
# that, so the application-level cap always lands FIRST and arq's cancellation is reserved
# for a job wedged somewhere the crawl's own budget does not cover — a hung write, a bug
# outside the timeout block, an `httpx` transport that never returns.
#
# **This was 180 until PER-166, which is to say it was BELOW the 300s crawl cap** — the
# inverse of the ordering above, so in practice arq cancelled every long crawl before its
# own budget expired and the run was left `processing` with nothing recorded. PER-159
# documented that inversion rather than fixing it, because raising `job_timeout` breaks the
# old `job_timeout < job_completion_wait` assertion in tests/test_worker_settings.py. That
# assertion is the thing that was wrong; see JOB_COMPLETION_WAIT_SECONDS below.
JOB_TIMEOUT_SECONDS: Final = 600

# GRACEFUL SHUTDOWN, AND WHY IT IS NO LONGER ORDERED AGAINST `JOB_TIMEOUT_SECONDS`.
#
# Fly sends SIGTERM on every deploy. arq's DEFAULT signal handler cancels in-flight jobs
# immediately; setting `job_completion_wait` to a non-zero value swaps in
# `handle_sig_wait_for_completion`, which instead stops claiming new work
# (`allow_pick_jobs = False`) and waits this long for what is already running to finish.
#
# The shutdown budget nests inside Fly's, and that ordering is as load-bearing as it ever
# was:
#
#     this (200)  <  fly.toml kill_timeout (240s)  <=  Fly's ceiling (300s)
#
# What is NO LONGER true is `JOB_TIMEOUT_SECONDS < this`. It was asserted until PER-166 on
# the grounds that a drain shorter than the job timeout "cancels a job that is still
# legitimately running". That reasoning has a hard ceiling underneath it: Fly caps
# `kill_timeout` at 300s, so a drain budget can never exceed ~300s, and a `job_timeout`
# that sits above the 300s crawl cap — which is the whole point of raising it — can never
# fit inside one. The two constraints are simply unsatisfiable together, and the old
# assertion was satisfiable only because `job_timeout` was set to a value (180s) that was
# itself wrong.
#
# So the drain is now what it always physically was: a best-effort window that covers a
# crawl with a couple of minutes left to run, not every crawl. A job still running when it
# expires is cancelled, and PER-166 is what makes that recoverable rather than terminal —
# `crawl_task` hands the run back to `pending` on cancellation, and the stuck-run reaper
# rescues it if even that write does not land. Before the reaper existed, a cancelled job
# meant a permanently stranded run, which is why the old ordering mattered so much more.
JOB_COMPLETION_WAIT_SECONDS: Final = 200

# How long ONE schedule tick may run before arq cancels it — deliberately much tighter than
# JOB_TIMEOUT_SECONDS above, which governs `crawl_task`. A tick that has not finished in 50s
# has not finished inside its own 60-second window, so letting it run all the way to the
# 600s `job_timeout` would only delay the signal that the tick is saturated — it would
# already be overdue for its NEXT run before arq ever cancelled it. Cancelling loses nothing
# correctness-wise: `SchedulesReader.lock_due`'s `FOR UPDATE SKIP LOCKED` makes two
# overlapping ticks safe rather than corrupting, and an uncommitted transaction simply rolls
# back, so the next tick redoes whatever work this one did not finish.
CRON_TICK_TIMEOUT_SECONDS: Final = 50


# --- the retry policy ------------------------------------------------------------------

# How many times a run may be CLAIMED before the next failure is terminal.
#
# Counted against `runs.attempts` — incremented by `RunsWriter.claim_pending` inside the
# atomic `pending -> processing` UPDATE — and NOT against arq's `ctx["job_try"]`. The two
# measure different things and are allowed to disagree: `job_try` belongs to one arq job id
# and resets whenever the reaper re-enqueues a run under a new one, while `attempts` belongs
# to the run and is the only counter a reaper running in another process can read at all.
#
# `WorkerSettings.max_tries` is set to this same number, deliberately, as an independent
# arq-side backstop rather than as the policy itself. When they disagree — a job cancelled
# by SIGTERM increments `job_try` without ever incrementing `attempts` — arq gives up on
# that job id and the run is left `pending` with nothing behind it, which the reaper's
# orphan sweep re-enqueues under a fresh id with a fresh `job_try`. The run's own budget is
# unaffected, which is the behaviour we want: a deploy is not a failed attempt.
MAX_ATTEMPTS: Final = 3

# Backoff before the next attempt, indexed by the attempt that just failed. A site that is
# briefly down often recovers within minutes; three tries in six seconds accomplish nothing
# but three identical failures.
#
# Only the first two entries are reachable at `MAX_ATTEMPTS = 3` — after attempt 3 there is
# no retry to defer, only a terminal failure. The third is kept because the sequence, not
# the pair, is the policy: raising `MAX_ATTEMPTS` should extend the ladder rather than
# silently reuse its last rung. `retry_delay_seconds` below clamps past the end for exactly
# that reason.
RETRY_DELAYS_SECONDS: Final[tuple[int, ...]] = (10, 60, 300)


def retry_delay_seconds(attempt: int) -> int:
    """How long to defer the retry of an attempt numbered `attempt` (1-based).

    `attempt` is `runs.attempts` as it stands AFTER the claim that just failed — the value
    `RunsWriter.claim_pending` returned — so attempt 1's failure defers by
    `RETRY_DELAYS_SECONDS[0]`.

    Clamped at both ends rather than raising: an out-of-range attempt number means the
    caller's retry accounting has already gone wrong somewhere, and answering with the
    longest delay in the ladder is a strictly better failure mode than an `IndexError`
    inside a worker's error-handling path.
    """
    index = min(max(attempt, 1), len(RETRY_DELAYS_SECONDS)) - 1
    return RETRY_DELAYS_SECONDS[index]


# --- the reaper ------------------------------------------------------------------------

# How often the stuck-run reaper runs, as a number of minutes between firings. Read by
# `WorkerSettings.cron_jobs`, which turns it into the `minute={0, 5, 10, ...}` set arq's
# cron scheduler actually wants — see that call site.
REAPER_INTERVAL_MINUTES: Final = 5

# How long the reaper waits, past the point at which a job could no longer legitimately be
# running, before it decides a `processing` run has been abandoned.
#
# `REAPER_GRACE_SECONDS` is the margin, and it is generous on purpose. The ticket's own
# framing is the right one: reaping a run that is legitimately still working produces a
# DUPLICATE CRAWL — two workers fetching the same site, two payloads uploaded, one of them
# orphaned — while reaping one late merely delays a rescue by five minutes. The costs are
# not symmetric, so neither is the threshold.
#
# `STUCK_AFTER_SECONDS` is measured from `runs.claimed_at`, not from `runs.started_at`. See
# that column's comment in db/schema.prisma: `started_at` is when the run was enqueued, and
# a backlogged queue can leave a run pending for an hour before a worker touches it.
REAPER_GRACE_SECONDS: Final = 300
STUCK_AFTER_SECONDS: Final = JOB_TIMEOUT_SECONDS + REAPER_GRACE_SECONDS

# How long a run may sit `pending` before the reaper assumes no job is coming for it.
#
# These are the rows the insert-then-enqueue ordering can strand: `RunService.trigger_run`
# and the cron tick both commit a `pending` run and THEN enqueue, so a crash — or a Redis
# outage that also defeats `abandon_unqueued_run` — leaves a row with nothing acting on it.
#
# Must stay comfortably above the longest entry in `RETRY_DELAYS_SECONDS` (300s): a run
# waiting out its own backoff is `pending` with a DEFERRED job behind it, and sweeping it
# early would enqueue a second job for work that was never lost.
ORPHANED_PENDING_AFTER_SECONDS: Final = 600

# How long ONE reaper pass may run before arq cancels it. Well under the five minutes
# before the next one, for the same reason `CRON_TICK_TIMEOUT_SECONDS` sits under sixty
# seconds: a pass that has not finished in two minutes is not going to be helped by being
# allowed to run into its successor, and everything it does is idempotent — a run it did
# not reach is still stuck when the next pass looks.
REAPER_TIMEOUT_SECONDS: Final = 120
