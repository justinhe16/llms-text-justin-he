"""Structured logging, shared by both processes that run from the backend image.

It lives in `app.core` rather than in `app.main` because **the ARQ worker never imports
`app.main`**, and a worker with no logging configuration is close to a worker with no
logging: `arq`'s CLI calls `dictConfig` with a config that names only the `arq` logger
(`arq.logs.default_log_config`), so everything this codebase logs under `app.*` falls
through to a root logger with no handlers. Python's `lastResort` handler then prints
WARNING and above with no timestamp, no logger name, and no level — and silently drops
every INFO line, including the worker's own "ready" message and `LOG_LEVEL` entirely.

**Every line is one JSON object on stdout.** `fly logs` is the only log surface this
system has (ARCHITECTURE.md §3.8) — there is no error-tracking service on either side, by
design — so the format is chosen to be read with `jq` rather than by eye:

    fly logs --app llms-text-justin-he | jq 'select(.run_id == "…")'

reconstructs one crawl out of a stream interleaved with every other job. That only works
if *every* line is JSON, which is why `configure_logging()` also drags uvicorn's own
loggers onto this handler instead of leaving them printing their own format beside it (see
`_REROUTED_LOGGERS` below). arq's equivalent cannot be done from here at all — its CLI
applies its own `dictConfig` after importing the worker's settings module — so it is passed
on the command line instead: see `app.worker.settings.ARQ_LOG_CONFIG`.

**Correlation ids travel in `contextvars`, never in module globals or function
arguments.** A module-level "current run id" would be shared by every task on the event
loop, so two concurrent crawls would overwrite each other's id and produce logs blaming
the wrong run — worse than no correlation id at all. A `ContextVar` is copied into each
`asyncio.Task` at creation, so a value bound inside one job (or one request) is invisible
to every other, and `JsonFormatter` can pick it up without a single call site passing a
logger around. `tests/test_logging.py` proves that isolation under real concurrency.

**Nothing here may log or format a configuration value** — see
[ARCHITECTURE.md §9.4](../../../ARCHITECTURE.md#94-never-echo-or-log-a-secret). Because
"never" is only as good as the least careful call site, `RedactionFilter` below is
installed on the handler as well: it rewrites bearer tokens, JWTs, credentialed
connection strings, and named-credential assignments out of every record before any
handler formats one. It is insurance, not permission — a call site that interpolates a
secret is still a bug, and one the filter may not recognise.
"""

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final, Literal

from app.core.settings import settings


ProcessName = Literal["app", "worker"]
"""Which process emitted a line. `app` is the FastAPI API, `worker` is the ARQ worker —
the two names backend/fly.toml's `[processes]` block uses, so that `fly logs` and this
field agree about what to call them."""


# ---------------------------------------------------------------------------------------
# Correlation ids.
# ---------------------------------------------------------------------------------------

# `default=None` on every one of these, rather than "": a line emitted outside any bound
# scope (process startup, the health check) then carries no correlation key at all, instead
# of an empty one that `jq 'select(.run_id == "")'` would have to know to filter out.
_request_id: ContextVar[str | None] = ContextVar("llms_text_request_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("llms_text_run_id", default=None)
_tick_id: ContextVar[str | None] = ContextVar("llms_text_tick_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("llms_text_user_id", default=None)

_CORRELATION_VARS: Final[tuple[tuple[str, ContextVar[str | None]], ...]] = (
    ("request_id", _request_id),
    ("run_id", _run_id),
    ("tick_id", _tick_id),
    ("user_id", _user_id),
)


def correlation_fields() -> dict[str, str]:
    """Every correlation id bound in the current context, omitting the unbound ones."""
    return {name: value for name, var in _CORRELATION_VARS if (value := var.get()) is not None}


@contextmanager
def _bound(var: ContextVar[str | None], value: str | None) -> Iterator[None]:
    """Bind `value` for the duration of the block, then restore what was there before.

    `reset(token)` rather than `set(None)` on the way out, so a nested binding restores the
    outer one instead of clearing it — and so this is safe to use inside a scope that
    already bound the same variable.
    """
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


def request_id_context(request_id: str) -> AbstractContextManager[None]:
    """Bind `request_id` to every log line emitted inside the block. Used by the API's
    `RequestContextMiddleware` (`app.api.middleware.request_context`)."""
    return _bound(_request_id, request_id)


def run_id_context(run_id: str) -> AbstractContextManager[None]:
    """Bind `run_id` to every log line emitted inside the block.

    Entered once by `app.worker.jobs.crawl_task`, which is what makes every line the crawl
    produces — including ones logged several modules deep, in `internals/fetcher.py` or
    `internals/ssrf.py`, which are never handed a run id — carry the run it belongs to.
    """
    return _bound(_run_id, run_id)


def tick_id_context(tick_id: str) -> AbstractContextManager[None]:
    """Bind `tick_id` to every log line emitted inside one cron tick
    (`app.worker.jobs.schedule_tick`). The tick's equivalent of `run_id_context`."""
    return _bound(_tick_id, tick_id)


def user_id_context(user_id: str | None) -> AbstractContextManager[None]:
    """Bind `user_id` for the duration of the block.

    Entered by the request middleware with `None`, which is what stops a value bound during
    one request from surviving into the next one handled by the same task. The value itself
    is filled in later, by `bind_user_id` below, once authentication has actually run.
    """
    return _bound(_user_id, user_id)


def bind_user_id(user_id: str) -> None:
    """Record who the current request is authenticated as, with no matching reset.

    Called from `app.core.auth.dependencies` the moment a token verifies, which is deeper
    in the call stack than any scope that could unbind it — the reset belongs to
    `user_id_context`, which the middleware entered before routing began.

    **Never call this with anything but a verified `sub` claim.** It is a user id, not a
    credential; the token it came out of must never be passed here.
    """
    _user_id.set(user_id)


# ---------------------------------------------------------------------------------------
# Redaction.
# ---------------------------------------------------------------------------------------

REDACTED: Final = "[REDACTED]"
"""What a redacted value is replaced with. A fixed, obviously-not-a-value string, so that
a redacted line still shows an operator that *something* was there — which is the signal
that a call site needs fixing."""

# Ordered, and the order is load-bearing: the two token rules run before the
# named-credential rules, so that `{"authorization": "Bearer eyJhbG…"}` has its token
# rewritten by rule 1 before rule 4 truncates the match at the first space. Reversing them
# would leave the token itself in the line.
_REDACTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # 1. `Bearer <token>` in any casing, including inside a rendered header mapping.
    #    The lookahead requires at least one non-letter, and the length floor is 12, so
    #    ordinary prose after the word "bearer" — `_reject("no Bearer credentials on the
    #    request")` in app/core/auth/dependencies.py is the live example — is left alone.
    (
        re.compile(
            r"(?i)\b(bearer)\s+(?=[A-Za-z0-9\-._~+/=]*[\d\-._~+/=])[A-Za-z0-9\-._~+/=]{12,}"
        ),
        rf"\1 {REDACTED}",
    ),
    # 2. A bare JWT, with or without a scheme in front of it. Supabase access tokens AND
    #    the service-role key are both JWTs, and both start `eyJ` — the base64url of the
    #    `{"` that opens every JOSE header — which is what makes this specific enough to
    #    apply to any text without matching dotted module paths or version numbers.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), REDACTED),
    # 3. Credentials embedded in a connection string: `postgresql://user:pw@host`,
    #    `rediss://default:pw@host`. The username is kept — it is not the secret, and
    #    losing it would make a misconfigured DSN much harder to recognise — and only the
    #    password is rewritten. DATABASE_URL and REDIS_URL are the two this exists for.
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s:/?#@]*):([^\s@/]+)@"),
        rf"\g<1>\g<2>:{REDACTED}@",
    ),
    # 4a. A named credential assigned a QUOTED value, in a dict repr, JSON fragment, or
    #     header dump: `'authorization': '…'`, `"api_key": "…"`. Matched before 4b so the
    #     whole quoted run is replaced rather than stopping at the first space inside it.
    (
        re.compile(
            r"""(?i)(["']?(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key"""
            r"""|api[_-]?key|secret[_-]?key|service[_-]?role[_-]?key|access[_-]?token"""
            r"""|refresh[_-]?token|session[_-]?token|client[_-]?secret|password|passwd"""
            r"""|token)["']?\s*[:=]\s*)(["'])(?:(?!\2).)*\2"""
        ),
        rf"\g<1>\g<2>{REDACTED}\g<2>",
    ),
    # 4b. The same names assigned an UNQUOTED value — an f-string, a `key=value` pair, a
    #     shell-style export. Deliberately no leading `\b`: the name this most needs to
    #     catch is `SUPABASE_SECRET_KEY=…`, where the underscore before `SECRET` means
    #     there is no word boundary to anchor to. The negative lookahead keeps a value this
    #     filter already rewrote from being wrapped a second time.
    (
        re.compile(
            r"""(?i)((?:authorization|proxy-authorization|cookie|set-cookie|x-api-key"""
            r"""|api[_-]?key|secret[_-]?key|service[_-]?role[_-]?key|access[_-]?token"""
            r"""|refresh[_-]?token|session[_-]?token|client[_-]?secret|password|passwd"""
            r"""|token)\s*[:=]\s*)(?!\[REDACTED\])([^\s,;)}\]'"]+)"""
        ),
        rf"\g<1>{REDACTED}",
    ),
    # 5. Opaque provider keys, which match none of the shapes above because they are just
    #    a prefix and a long random tail: Supabase publishable/secret keys and personal
    #    access tokens, Fly deploy tokens, GitHub tokens.
    (
        re.compile(
            r"\b(?:sb_secret_|sb_publishable_|sbp_|fo1_|ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_\-]{8,}"
        ),
        REDACTED,
    ),
)


def redact(text: str) -> str:
    """Rewrite every credential-shaped substring of `text` to `REDACTED`.

    Idempotent: `REDACTED` matches none of the patterns above, so running this over an
    already-redacted line is a no-op rather than a second layer of brackets. That matters
    because both `RedactionFilter` and `JsonFormatter` call it — belt and braces, so a
    handler attached without the filter (pytest's `caplog`, for one) still cannot print a
    token, and a formatter swapped out for a plain one still cannot either.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# Everything `logging` itself puts on a record, computed from a throwaway record rather
# than hard-coded, so a new attribute in a future Python release does not start leaking
# into the JSON as if it were a caller's `extra=`. `message` and `asctime` are added by
# `Formatter.format` rather than by the constructor, and `taskName` only exists on 3.12+,
# so both are named explicitly. Read by `RedactionFilter` (which must not rewrite
# `logging`'s own fields) and by `JsonFormatter` (which must not emit them twice).
#
# `color_message` is not one of `logging`'s — it is uvicorn's, attached to every record it
# emits as an `extra` holding the same message with ANSI escapes in it. Once uvicorn's
# loggers are rerouted onto this handler (see `_REROUTED_LOGGERS`), promoting it would put a
# duplicate of every startup line, escape codes and all, into the JSON.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime", "taskName", "color_message"}


class RedactionFilter(logging.Filter):
    """Scrub credential-shaped values out of a record before any handler formats it.

    Attached to the handler rather than to a logger, because a filter on a logger only sees
    records logged directly on *that* logger — never the ones that reach it by propagation,
    which is every record this application emits.

    When it finds something, it rewrites `record.msg` to the already-interpolated message
    and clears `record.args`. That is deliberately destructive: it means the redacted text
    is what every downstream handler and formatter sees, rather than each of them
    re-rendering the original arguments and only *this* handler's output being clean. A
    record with nothing to redact is left exactly as it arrived, so the common case does
    not lose its `%`-style template.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        for key, value in list(record.__dict__.items()):
            if isinstance(value, str) and key not in _RESERVED_RECORD_ATTRS:
                record.__dict__[key] = redact(value)
        return True


# ---------------------------------------------------------------------------------------
# The JSON formatter.
# ---------------------------------------------------------------------------------------


def _json_default(value: object) -> str:
    """Render anything `json` cannot — a `UUID`, a `datetime`, an exception — as its
    `str()`, so one un-serializable value in an `extra=` never costs the whole line."""
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: `ts`, `level`, `logger`, `message`, `process`, whatever
    correlation ids are bound, and whatever the call site passed as `extra=`.

    `process` is a constructor argument rather than a lookup, so a test can build a
    worker-shaped formatter without configuring the process-wide logger for the whole
    session.

    An exception is serialized as its **complete** traceback under `exception`. With no
    error-tracking service anywhere in this system, that line *is* the incident record —
    there is no second copy to go and read — so it is never truncated.
    """

    def __init__(self, process: ProcessName) -> None:
        super().__init__()
        self._process = process

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "process": self._process,
        }
        # Anything the call site passed as `extra=`. Underscore-prefixed names are skipped
        # so a caller can stash something on a record for another filter without it becoming
        # a public log field, and `setdefault` keeps an `extra=` from renaming the envelope
        # out from under a `jq` filter — a stray `extra={"logger": ...}` is a mistake, not
        # an override.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload.setdefault(key, redact(value) if isinstance(value, str) else value)

        # CORRELATION IDS ARE APPLIED LAST, AND THEREFORE WIN. A bound `run_id` is a fact
        # about which job is executing; an `extra={"run_id": ...}` is a caller's claim, and a
        # caller that is wrong must not be able to make a line lie about which run produced
        # it. Applying them after the loop above is what guarantees that.
        #
        # It still leaves the useful case working, because `correlation_fields()` omits
        # whatever is unbound: `ScheduleService._enqueue` logs an `extra={"run_id": ...}`
        # from inside a cron tick, where only `tick_id` is bound, and that value survives —
        # it is the one line in a batch that ties a failure to the specific run it stranded.
        payload.update(correlation_fields())

        # Truthiness, not `is not None`: `logger.log(..., exc_info=False)` — which is what a
        # call site that decides per call whether to attach a traceback passes on the happy
        # path — leaves `record.exc_info` as the literal `False`, and `formatException(False)`
        # raises inside the handler, which drops the line entirely.
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exception"] = redact(record.exc_text)
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))

        return json.dumps(payload, default=_json_default, ensure_ascii=False)


# ---------------------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------------------

# Third-party loggers that are noisy at INFO. `httpx` is the one that matters here and not
# merely in principle: it logs a line per outbound request, and outbound requests are
# literally what the crawler does, so at INFO a single hundred-page crawl buries its own
# hundred-odd application lines under its own hundred-odd HTTP lines. `httpcore` and
# `hpack` are the layers underneath it and are chattier still at DEBUG, which `LOG_LEVEL=
# DEBUG` would otherwise turn on for the whole process just to see per-fetch detail.
#
# `uvicorn.access` is here for a different reason: it is not noise, it is a DUPLICATE. The
# request middleware emits its own one-line summary with the status, the duration, and the
# user, so uvicorn's access line would say a strictly poorer version of the same thing once
# per request.
_QUIETED_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "hpack",
    "urllib3",
    "asyncio",
    "uvicorn.access",
)

# Loggers that ship with their own handler and their own format, which
# `configure_logging()` strips so their lines come out as JSON like everything else. Only
# the API's, because arq's equivalent cannot be done from here: `arq`'s CLI applies its own
# `dictConfig` *after* importing `app.worker.settings`, so anything this function did to
# the `arq` logger would be undone a moment later. `app.worker.settings.ARQ_LOG_CONFIG`,
# passed with `--custom-log-dict`, is the worker's version of this list.
_REROUTED_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")

_configured = False


def configure_logging(process: ProcessName = "app") -> None:
    """Install the JSON handler on the root logger at `LOG_LEVEL`. Idempotent.

    The first call wins, which is what makes this safe to call from both
    `app.main.create_app()` and `app.worker.settings` (at module import) without the two
    fighting over the root logger — the property the previous `basicConfig` implementation
    got for free and this one has to state. In a deployed container only one of those two
    ever runs; in the test suite both modules may be imported into one process, and the
    guard is what keeps every log line from being emitted twice.

    Args:
        process: Which process this is, recorded on every line as `process`. Defaults to
            `"app"` so that anything importing `app.main` — including
            `scripts/export_openapi.py` and the test suite — is labelled correctly without
            having to say so.
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(process))
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    # Added, never swapped for what is already there. Under uvicorn and under arq the root
    # logger has no handlers at this point, so this is the only one; under pytest it is not,
    # and removing the suite's own capture handler to tidy up would break `caplog`.
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in _QUIETED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    if process == "app":
        for name in _REROUTED_LOGGERS:
            rerouted = logging.getLogger(name)
            rerouted.handlers.clear()
            rerouted.propagate = True

    _configured = True
