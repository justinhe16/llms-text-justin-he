"""The JWKS cache: fetched once at startup, refetched on an unknown `kid`.

**Why JWKS instead of a shared HS256 secret.** Supabase signs access tokens asymmetrically
(ES256 today, RS256 also supported — see `dependencies.py`) and publishes the *public* half
of that key pair at `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`. Verifying against those
public keys means this service never holds a secret that could forge a token, and a key
rotation on Supabase's side is something this cache recovers from on its own (see below),
never a value someone has to copy into `fly secrets set`.

**Why cache-at-startup plus refetch-on-unknown-kid, and not the alternatives.** A per-request
fetch would mean a network round trip — and an outbound request to Supabase — on every single
authenticated call. Never refetching would mean a Supabase key rotation locks every user out
until the next deploy. Caching the whole document at startup and refetching only when a
request's `kid` is not in the cache gets the common case (a known, unrotated key) down to a
dict lookup, while still recovering from a rotation within one request's latency.

**Why the refetch is rate-limited.** Refetching on every unknown `kid` without a limit turns a
flood of requests carrying bogus, never-valid `kid`s into one outbound HTTP request per
inbound request — an unauthenticated caller could use this service to hammer Supabase's JWKS
endpoint at whatever rate it can open connections. The 60-second window below is an internal
circuit breaker on our OWN outbound calls. This is a different thing from
ARCHITECTURE.md §11's "rate limiting is out of scope" — that clause is about product rate
limiting of *users* (quotas, billing), a decision nobody has made yet. This window exists
because the ticket names the DoS shape explicitly, and it protects Supabase's endpoint and
this process's own outbound connection budget, not any per-user quota.

**Why a failed startup fetch degrades instead of crashing, unlike `open_pool()`.** A bad
`DATABASE_URL` breaks every request for the life of the process — refusing to boot is the
honest response, because there is no path to recovery without a restart. An unreachable JWKS
endpoint is different: it is recoverable on the very next request, because `get_key()` below
refetches when it sees a `kid` it does not recognize. Refusing to boot over a transient
Supabase blip would turn that blip into a self-inflicted outage. `open_jwks_cache()` therefore
logs loudly and starts with an empty key cache — every authenticated request 401s until a
refetch succeeds, but the process itself is alive and `/health` still answers.

**Why the startup fetch does NOT start the rate-limit window.** `refresh()` (used by both the
startup path and tests that prime the cache) deliberately never touches `_last_refetch_at`.
Only the request-path refetch inside `get_key()` sets it. This is the detail that makes "a
transient blip should not prevent boot" literally true rather than true-with-an-asterisk: if
`refresh()` set the timestamp on a failed startup fetch, the very first request after boot
would find the window already open and refuse to refetch for 60 seconds, turning "recovers on
the next request" into "recovers on the next request, unless that request arrives within a
minute of boot." A future reader simplifying `get_key()` to call `refresh()` (which would
reintroduce exactly that bug) is the mistake this comment exists to prevent.

Rejected alternatives:

- `jwt.PyJWKClient` — synchronous under the hood (it uses `urllib.request`), so calling it
  from an async request handler would stall the event loop for every other in-flight request
  during the fetch. Its `lifespan`/`max_cached_keys` knobs cannot express "refetch at most
  once per 60 seconds on an unknown kid", and it gives tests no seam to count real HTTP
  requests, which is how this ticket's two measured acceptance criteria are measured.
- `jwt.PyJWKSet` — silently skips unusable key entries with no log line, and raises on an
  empty key set, conflating "the endpoint returned nothing" with "the document is malformed"
  into a single unlabelled failure.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Final, cast

import httpx
from jwt import PyJWK
from jwt.exceptions import PyJWTError

from app.core.settings import Settings


logger = logging.getLogger(__name__)

# Fixed by Supabase's API surface, not something an operator configures.
_JWKS_PATH: Final = "/auth/v1/.well-known/jwks.json"

# Not operator-tunable, on purpose: a value someone can raise is a value someone raises to
# "fix" a rotation instead of fixing the rotation. See the module docstring for why 60s
# exists at all — it bounds how hard an unauthenticated caller can drive our own outbound
# traffic against Supabase's JWKS endpoint, not a per-user product limit (out of scope,
# ARCHITECTURE.md §11).
_REFETCH_INTERVAL_SECONDS: Final = 60.0

# Bounds a single request-path refetch inside get_key().
_FETCH_TIMEOUT_SECONDS: Final = 5.0

# Deliberately larger than _FETCH_TIMEOUT_SECONDS: httpx's own connect timeout is not
# always enough to bound DNS resolution (some resolvers are not interruptible by it), so
# this is a second, outer backstop around the whole startup fetch via asyncio.wait_for().
# Fly's build-check polls /health for roughly 30s after boot; this must leave boot enough
# of that budget to still finish and start answering.
_STARTUP_FETCH_TIMEOUT_SECONDS: Final = 10.0

# A JWKS `kty` of `oct` is a symmetric (shared-secret) key. Publishing one in a JWKS
# document would mean anyone who can read the document could mint a valid token with it —
# so it is never eligible to be cached, no matter what the document says. This is the
# second of two independent guards against that (the algorithm allowlist in
# dependencies.py is the first); keeping it here means "the cache holds public keys only"
# is true locally, without relying on the caller to also get it right.
_ASYMMETRIC_KEY_TYPES: Final = frozenset({"EC", "RSA"})


def _build_jwks_url(supabase_url: str) -> str:
    """Build the JWKS document URL from a project's base Supabase URL.

    Strips a trailing slash before appending `_JWKS_PATH`. A trailing slash on
    `SUPABASE_URL` is a realistic dashboard copy-paste, and without stripping it the result
    would carry a doubled slash (`//auth/v1/...`), which some gateways treat as a 404
    rather than normalizing it.
    """
    return f"{supabase_url.rstrip('/')}{_JWKS_PATH}"


class JwksCache:
    """Holds the current JWKS key set, keyed by `kid`, with a rate-limited refetch.

    The `httpx.AsyncClient` is **injected, never constructed here** — the owning app builds
    one client per process and controls its lifecycle (`create_jwks_client()` /
    `close_jwks_cache()` below), the same shape as the asyncpg pool in
    `app.infrastructure.db.pool`. Injecting it is also what lets the test suite put an
    `httpx.MockTransport` under a REAL `AsyncClient` and count genuine HTTP requests — the
    only way this ticket's two "must be measured" acceptance criteria (no I/O on a cache
    hit; at most one refetch per window) are measured at the HTTP layer instead of inferred
    from reading this module's source.

    `clock` is injected too, defaulting to `time.monotonic`, so tests can advance a fake
    clock past the rate-limit window instead of sleeping 60 real seconds.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        jwks_url: str,
        *,
        refetch_interval_seconds: float = _REFETCH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._jwks_url = jwks_url
        self._refetch_interval_seconds = refetch_interval_seconds
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._last_refetch_at: float | None = None
        self._lock = asyncio.Lock()

    def key_count(self) -> int:
        """Return how many usable keys the cache currently holds."""
        return len(self._keys)

    async def refresh(self) -> None:
        """Replace the cached key set with a fresh fetch. Raises on failure.

        Used by the startup path (`open_jwks_cache` below) and by tests priming the cache.
        **Deliberately does not touch `_last_refetch_at`** — see the module docstring's
        "why the startup fetch does NOT start the rate-limit window" section. A future
        change that makes this set the timestamp would silently reintroduce the bug that
        section describes; if you are tempted to "simplify" `get_key()` to call this
        instead of `_fetch_keys()` directly, that is the tell.
        """
        self._keys = await self._fetch_keys()

    async def get_key(self, kid: str) -> PyJWK | None:
        """Return the signing key for `kid`, refetching at most once per rate-limit window.

        Never raises — a fetch failure or an unknown `kid` both return `None`. This module
        knows nothing about HTTP status codes; mapping "no key" to a 401 is
        `dependencies.py`'s job.
        """
        key = self._keys.get(kid)
        if key is not None:
            # The hot path: a known kid never touches the lock, the clock, or the network.
            # This is what makes "verification performs no network I/O when the kid is
            # cached" true for every request in normal operation.
            return key

        async with self._lock:
            # Re-check under the lock (double-checked locking). Without this, N concurrent
            # requests carrying the same unknown kid would all pass the "key is missing"
            # check above before any of them updates the cache, and all N would refetch —
            # correct under a naive sequential test, wrong under real concurrency. It also
            # means a caller queued behind a just-finished rotation refetch finds the new
            # key here instead of being told "unknown" and refetching again itself.
            key = self._keys.get(kid)
            if key is not None:
                return key

            if not self._may_refetch():
                logger.warning(
                    "Suppressed a JWKS refetch for an unknown kid: rate-limited to once per %.0fs",
                    self._refetch_interval_seconds,
                )
                return None

            # Set BEFORE the await, and unconditionally — regardless of whether the fetch
            # that follows succeeds. What is rate-limited is the ATTEMPT, not successful
            # attempts only: a persistently broken JWKS endpoint must be hit at most once
            # per window too, or it is hit once per request, which is exactly the
            # self-inflicted DoS this window exists to prevent.
            self._last_refetch_at = self._clock()
            try:
                self._keys = await self._fetch_keys()
            except Exception as error:
                logger.error("JWKS refetch failed for an unknown kid (%s)", type(error).__name__)
                return None

            return self._keys.get(kid)

    def _may_refetch(self) -> bool:
        """Return whether enough time has passed since the last refetch attempt."""
        if self._last_refetch_at is None:
            return True
        return self._clock() - self._last_refetch_at >= self._refetch_interval_seconds

    async def _fetch_keys(self) -> dict[str, PyJWK]:
        """Fetch and parse the JWKS document. Raises on any failure; never half-parses it.

        A malformed or unreachable document is a **failed fetch** — this either returns a
        complete `dict[str, PyJWK]` or raises, so callers never observe a partially-updated
        cache.
        """
        response = await self._client.get(self._jwks_url, timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        document: Any = response.json()

        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise ValueError("JWKS document is not an object with a 'keys' list")

        keys: dict[str, PyJWK] = {}
        skipped = 0
        for raw_entry in document["keys"]:
            if not isinstance(raw_entry, dict):
                skipped += 1
                continue
            # mypy only knows `dict[Any, Any]` from the isinstance check above; JSON
            # object keys are always strings, so this describes actual runtime shape
            # rather than working around a real type mismatch (same idiom as
            # transaction.py's `cast` for PoolConnectionProxy -> Connection).
            entry = cast(dict[str, Any], raw_entry)

            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                skipped += 1
                continue
            if entry.get("kty") not in _ASYMMETRIC_KEY_TYPES:
                skipped += 1
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except (PyJWTError, ValueError, TypeError):
                skipped += 1
                continue

        if skipped:
            # Count only — never the entry itself, which could carry key material.
            logger.warning("Skipped %d unusable JWKS entries", skipped)

        return keys


# The process-wide cache. Private: read it through get_jwks_cache(), never this name
# directly, mirroring app.infrastructure.db.pool's `_pool`. Declared here rather than
# beside the other module constants above because it names `JwksCache`, defined just
# above it — an unquoted forward reference to a not-yet-defined name at module scope
# raises `NameError` at import time (unlike inside a function signature).
_cache: JwksCache | None = None


def create_jwks_client() -> httpx.AsyncClient:
    """Build the `httpx.AsyncClient` the JWKS cache fetches through.

    A pure factory with no module state — the direct analogue of
    `app.infrastructure.db.pool.create_pool()`. `app.main`'s lifespan calls this once, hands
    the client to `open_jwks_cache()`, and closes it on shutdown, so every entrypoint that
    ever needs a JWKS-fetching client builds one identically.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(_FETCH_TIMEOUT_SECONDS))


async def open_jwks_cache(settings: Settings, client: httpx.AsyncClient) -> JwksCache:
    """Create the process-wide JWKS cache and prime it. Called once, from the lifespan.

    **The JWKS fetch itself can never raise out of this function — only misuse can.** A
    startup fetch failure of any kind (unreachable host, DNS failure, malformed document,
    unusable key material, or the function's own timeout) is caught, logged loudly at
    ERROR, and left as an empty cache: every authenticated request 401s until a refetch
    succeeds, but the process boots and `/health` answers. See the module docstring for why
    this is the deliberate opposite of `open_pool()`'s fail-fast.

    Raises:
        RuntimeError: if a cache is already open — a caller bug (e.g. the lifespan running
            twice), never a fetch failure, so it is not swallowed by the `except` below.
    """
    global _cache
    if _cache is not None:
        raise RuntimeError(
            "The JWKS cache is already open. open_jwks_cache() is called once per process "
            "(see app.main.lifespan); call close_jwks_cache() before opening another."
        )

    cache = JwksCache(client, _build_jwks_url(settings.supabase_url))
    try:
        await asyncio.wait_for(cache.refresh(), timeout=_STARTUP_FETCH_TIMEOUT_SECONDS)
        logger.info("JWKS cache primed with %d signing key(s)", cache.key_count())
    except Exception as error:
        # Broad on purpose: httpx.ConnectError/TimeoutException, OSError/socket.gaierror
        # from DNS, json.JSONDecodeError, ValueError from a malformed document, PyJWTError
        # from unusable key material, and TimeoutError from the wait_for backstop above all
        # land here. A narrower `except httpx.HTTPError` would let a DNS failure crash
        # boot — exactly the path CI's build-check exercises on EVERY run, since its
        # SUPABASE_URL does not resolve. Never interpolate the URL or the exception's
        # message (ARCHITECTURE.md §9.4) — either could carry it. Type name only.
        logger.error(
            "Could not fetch the Supabase JWKS at startup (%s). Continuing with an EMPTY "
            "key cache: the first request carrying an unknown kid triggers a refetch, so a "
            "transient Supabase blip degrades authentication instead of preventing boot. "
            "Until that refetch succeeds, every authenticated request is rejected with 401.",
            type(error).__name__,
        )

    _cache = cache
    return cache


def get_jwks_cache() -> JwksCache:
    """Return the process-wide cache opened by `open_jwks_cache()`.

    Doubles as the FastAPI dependency callable (`Depends(get_jwks_cache)` in
    `dependencies.py`) — tests override authentication by substituting this callable with
    `app.dependency_overrides[get_jwks_cache]`, the same pattern `app.api.deps.get_db_pool`
    uses for the database pool.

    Raises:
        RuntimeError: if called before `open_jwks_cache()` has run, in `open_pool()` /
            `get_pool()`'s exact shape — a clear failure at the call site rather than an
            `AttributeError` deep inside a dependency.
    """
    if _cache is None:
        raise RuntimeError(
            "The JWKS cache has not been opened yet. Call open_jwks_cache(settings, client) "
            "during application startup (see app.main.lifespan) before get_jwks_cache() is "
            "used."
        )
    return _cache


async def close_jwks_cache() -> None:
    """Clear the process-wide cache. Safe to call more than once.

    Deliberately does NOT close the `httpx.AsyncClient` — the lifespan created it (via
    `create_jwks_client()`) and is responsible for closing it, staying symmetric with
    `open_jwks_cache()` not constructing one either.
    """
    global _cache
    _cache = None
