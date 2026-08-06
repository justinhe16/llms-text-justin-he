"""`RequestContextMiddleware` — the API half of this system's correlation ids.

Three things, once per request:

1. **Accept or generate an `X-Request-ID`.** A caller that already has one (the Next.js
   BFF forwarding a browser request, a `curl` reproducing a bug) keeps it, so one id spans
   both hops. Anyone else gets a fresh `uuid4`.
2. **Bind it to a `contextvar`**, so every line logged anywhere inside the request carries
   it without a single handler, service, or reader being passed a logger. See
   `app.core.logging` for why a contextvar and not a module global.
3. **Echo it back** in the `X-Request-ID` response header, so the id in a user's failed
   request and the id in `fly logs` are the same string.

And, on the way out, it logs the **one-line request summary**: method, path, status,
`duration_ms`, and `user_id` when the request authenticated. That single line answers most
"what happened at 14:32?" questions with no other instrumentation, which is the whole
reason it exists.

**A pure ASGI middleware, not `BaseHTTPMiddleware`.** Two reasons, and both matter here.
`BaseHTTPMiddleware` runs the downstream app in a *separate* anyio task, and a task gets a
COPY of the context — so `user_id`, bound by an authentication dependency deep inside the
request, would be invisible again by the time this middleware came to log it. It also
buffers streaming responses. Implementing `__call__(scope, receive, send)` directly avoids
both: the downstream app is awaited in this same task, so a contextvar it sets is still
set when the await returns.

That last sentence rests on two things that are true of the versions pinned in
`backend/requirements.txt` and are not documented public contracts of either library:
Starlette's HTTP path spawns no task around the endpoint, and FastAPI resolves a request's
dependencies **sequentially** rather than concurrently. A future FastAPI that solved
independent sub-dependencies in parallel would hand each one a context of its own, and
`user_id` would silently stop arriving here.
`tests/test_request_context.py::test_the_summary_line_names_the_authenticated_user` is the
tripwire: it drives the real dependency through a real request, so the upgrade that breaks
this fails CI instead of quietly dropping a field.

**Nothing here reads the `Authorization` header.** `user_id` arrives through
`app.core.logging.bind_user_id`, called by `app.core.auth.dependencies` once a token has
actually verified — so what reaches a log line is a user id, never a credential
(ARCHITECTURE.md §9.4).
"""

import logging
import re
import time
from typing import Any, Final
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_context, user_id_context


logger = logging.getLogger(__name__)

REQUEST_ID_HEADER: Final = "x-request-id"
"""The header read on the way in and written on the way out. Lowercase because ASGI
carries header names lowercased on the wire; Starlette's mappings are case-insensitive, so
a client may send it in any casing."""

_SCOPE_KEY: Final = "request_id"
"""Where the id is stashed on `scope["state"]`, for the one reader that cannot use the
contextvar: `request_id_exception_handler` below runs *outside* this middleware's `with`
block, after it has already unbound."""

# An inbound header value is attacker-controlled, and it ends up in a log field an operator
# will later grep by. Restricting it to the shape of the ids this service generates —
# hex, dashes, and the punctuation W3C trace ids use — means a caller cannot inject
# structure into the log stream or make one id impossible to search for. Anything that does
# not match is discarded and replaced with a generated id rather than rejected with a 400:
# a malformed correlation header is not a reason to fail a request that is otherwise fine.
_VALID_REQUEST_ID: Final = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

# Statuses that are a real problem rather than an ordinary client mistake, and therefore
# earn a WARNING rather than an INFO. 429 means a user is hitting the run caps
# (`app.features.runs.service`) and 409 means a duplicate run was refused — both are things
# somebody eventually asks "why did this happen?" about, and neither should have to be
# reconstructed from a 200-line INFO stream.
_WARNING_STATUSES: Final[frozenset[int]] = frozenset({409, 429})

# Fly probes `/health` every 10 seconds, forever (backend/fly.toml's
# `[[http_service.checks]]`). At INFO that is ~8,600 lines a day saying nothing happened,
# which would bury the requests a person actually made. The summary is still emitted, at
# DEBUG, so `LOG_LEVEL=DEBUG` gets it back when someone is genuinely debugging the probe.
_QUIET_PATHS: Final[frozenset[str]] = frozenset({"/health"})


def _incoming_request_id(scope: Scope) -> str | None:
    """Return the caller's `X-Request-ID` if it is well-formed, else `None`."""
    candidate = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if candidate is None or not _VALID_REQUEST_ID.match(candidate):
        return None
    return candidate


def _summary_level(status_code: int, path: str) -> int:
    """Pick the level for the request summary from the status it ended with."""
    if status_code >= 500:
        return logging.ERROR
    if status_code in _WARNING_STATUSES:
        return logging.WARNING
    if path in _QUIET_PATHS:
        return logging.DEBUG
    return logging.INFO


class RequestContextMiddleware:
    """Binds a request id, echoes it, and logs one summary line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes have no request id, no status, and no duration
            # worth reporting. Passing them straight through also means the lifespan's own
            # startup logging is not wrapped in a request context it does not belong to.
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or str(uuid4())
        scope.setdefault("state", {})[_SCOPE_KEY] = request_id

        # `status_code` is read after `self.app(...)` returns, so it has to survive the
        # nested function that learns it. 500 is the pessimistic default for the one path
        # that never sends a response start at all: an exception escaping the application.
        state: dict[str, int] = {"status_code": 500}

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status_code"] = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        started = time.perf_counter()
        # `user_id_context(None)` establishes the binding this request will fill in, and —
        # more importantly — owns its reset. Without it, `bind_user_id` (which has no scope
        # of its own to unbind at) could leave one request's user id visible to the next
        # request served on the same task.
        with request_id_context(request_id), user_id_context(None):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                # The ERROR line IS the incident record — there is no error-tracking
                # service holding a second copy (ARCHITECTURE.md §3.8) — so it carries the
                # complete traceback, never a truncated one.
                self._log_summary(scope, 500, started, exc_info=True)
                raise
            self._log_summary(scope, state["status_code"], started)

    def _log_summary(
        self, scope: Scope, status_code: int, started: float, *, exc_info: bool = False
    ) -> None:
        """Emit the one-line request summary.

        The path is `scope["path"]`, deliberately without the query string: a query string
        is caller-supplied and is exactly where a token would end up if one were ever
        passed in a URL. `user_id` is not passed here at all — it is on the contextvar, and
        `JsonFormatter` merges it in with every other correlation id.
        """
        path = scope["path"]
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.log(
            _summary_level(status_code, path),
            "%s %s -> %s (%sms)",
            scope["method"],
            path,
            status_code,
            duration_ms,
            exc_info=exc_info,
            extra={
                "method": scope["method"],
                "path": path,
                "status": status_code,
                "duration_ms": duration_ms,
            },
        )


async def request_id_exception_handler(request: Request, exc: Exception) -> Response:
    """Answer an unhandled exception with a 500 that still carries `X-Request-ID`.

    Registered on the application for `Exception`, which Starlette installs inside
    `ServerErrorMiddleware` — the outermost layer of the stack, *above*
    `RequestContextMiddleware`. That placement is the whole reason this exists: when the
    application raises, `ServerErrorMiddleware` builds the 500 itself and writes it with
    the ORIGINAL `send`, bypassing the header-stamping wrapper above, so without this the
    one response a caller most needs a correlation id for would be the only one without
    one.

    It does not log. `RequestContextMiddleware` has already logged the traceback on its way
    past, and it knows the request's duration, which this does not.

    Starlette re-raises after calling this, so an unhandled exception still fails a test and
    still reaches whatever is above uvicorn.
    """
    request_id = request.scope.get("state", {}).get(_SCOPE_KEY)
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500, headers=headers)


def request_id_of(request: Request) -> str | None:
    """The id bound to `request`, for a handler that wants to put it in a response body.

    Nothing needs this yet. It exists so that the `scope["state"]` key above has exactly one
    reader outside this module rather than being spelled out at each new call site.
    """
    state: dict[str, Any] = request.scope.get("state", {})
    value = state.get(_SCOPE_KEY)
    return value if isinstance(value, str) else None
