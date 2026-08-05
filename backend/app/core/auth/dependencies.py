"""FastAPI dependencies that turn a Bearer token into a verified Supabase user id.

**The ordered verification recipe**, and why the order is what it is:

1. `HTTPBearer(auto_error=False)` extracts the credentials from `Authorization`.
   `credentials is None` covers both "no header at all" and "a header with the wrong
   scheme" — FastAPI's `HTTPBearer` cannot tell those apart, and neither do we; both fail
   the same way.
2. `jwt.get_unverified_header(token)`, wrapped in `try/except PyJWTError`. A malformed
   token (garbage, `a.b`, valid-base64-but-not-JSON, a non-string `kid`) raises here — this
   is the **"never a 500" guarantee**. Unwrapped, any of those becomes an unhandled
   exception and a stack trace in the response instead of a 401.
3. The unverified `alg` is checked against the hardcoded allowlist **before** any key
   lookup and **before** `jwt.decode` is ever called. This kills `alg: none` and an
   HS256-confusion attempt while the request has touched nothing but a base64 decode — an
   `alg: none` token never even reaches the JWKS cache.
4. The unverified `kid` is checked for presence **before** any network work. Ordering this
   after the key lookup would turn a flood of kid-less tokens into an unauthenticated
   outbound-traffic amplifier against Supabase's JWKS endpoint.
5. `cache.get_key(kid)` — the only step that can perform I/O, and it is rate-limited inside
   `jwks.JwksCache` itself (see that module).
6. `jwt.decode(token, key, algorithms=_ALLOWED_ALGORITHMS, audience=_EXPECTED_AUDIENCE,
   options={"require": [...]})`, wrapped in `try/except PyJWTError`. Verifies the
   signature, `exp`, and `aud` in one call. `algorithms` is always the module-level
   constant — **never** the token's own unverified `alg` from step 3. `iss` is
   deliberately not verified: this ticket scopes verification to signature/`exp`/`aud`,
   and a custom-domain Supabase project's `iss` need not equal `SUPABASE_URL`.
7. Only once `jwt.decode` has succeeded is any claim trustworthy. `sub` is read and
   narrowed to a non-empty string — the security check and the type-narrowing mypy needs
   are the same `isinstance` line.
8. The `sub` string is returned as the user id.

**Reorderings that would be vulnerabilities** — each of these has a numbered step above
that exists specifically to prevent it:

1. Passing `algorithms=[header["alg"]]`, or omitting `algorithms` entirely — the classic
   JWT bug. The unverified header may SELECT which key to try (step 4) and may FAIL the
   request (step 3), but it must never select the verification algorithm.
2. Skipping step 3 because "`jwt.decode`'s `algorithms=` argument checks this anyway" — it
   does, and step 3 is belt-and-braces on top of it, not a substitute. Step 3 additionally
   guarantees an `alg: none` token is rejected before the JWKS cache is ever consulted, so
   it cannot be turned into a refetch trigger.
3. Moving step 4 after step 5 — turns a malformed, kid-less token into an outbound JWKS
   request.
4. Dropping `options={"require": [...]}` — measured against this version of PyJWT: it only
   verifies `exp` if the claim is present at all, so a token that omits `exp` verifies
   successfully and never expires. This is the least visible failure mode of the set,
   because every normally-issued token has an `exp`.
5. Reading `sub` before step 6 succeeds, or decoding with
   `options={"verify_signature": False}` — a total authentication bypass.
6. Passing `key.key` (the raw cryptography key) to `jwt.decode` instead of the `PyJWK`
   object itself. With a `PyJWK`, PyJWT determines the verification algorithm from the key
   material (`PyJWK.Algorithm`); with a raw key it falls back to resolving the algorithm
   from the attacker-controlled header. Passing the `PyJWK` makes algorithm confusion
   impossible by construction, at zero extra cost — so that's what step 6 does.
7. Returning a different status code or body per failure reason. Every failure below funnels
   through `_reject()`, which raises one exception with one body — see below.

**Why `HTTPBearer(auto_error=False)`.** It enforces the `Bearer` scheme, returns `None`
rather than raising on an absent or malformed `Authorization` header (so it is our own
generic 401 the client sees, never FastAPI's differently-worded default one — see the next
paragraph), and it documents the scheme in the OpenAPI schema and the Swagger "Authorize"
button for free.

**Why every failure funnels through one helper.** `_reject()` is the only place that raises
the 401. If three call sites each independently wrote `raise HTTPException(401, "...")`,
three authors typing the same string would make the responses identical by coincidence.
Routing every failure through one function makes them identical by construction, which is
what stops a client from fingerprinting *why* a token was rejected (expired vs. wrong
signature vs. unknown kid vs. no header at all) from the response alone. The specific reason
is logged server-side at WARNING — never sent to the client, and never a value that could
itself be a credential (see `_reject`'s docstring).

**`OptionalUser` semantics** (used by endpoints that behave differently for signed-in vs.
anonymous callers, once any exist — none do yet):

- No `Authorization` header at all -> `None` (anonymous; this is expected and unremarkable).
- A header is present but the token fails verification for ANY reason (malformed, expired,
  wrong signature, unknown kid, disallowed alg, wrong audience, ...) -> **401, not `None`.**
  A client that sent a token expects it to be honoured. Silently downgrading a bad token to
  "anonymous" would hide an expired session behind a response that looks like success, and
  make "why does the server think I'm signed out?" unanswerable from the client side.

That second rule needs its own header check rather than trusting `credentials is None`:
`HTTPBearer(auto_error=False)` returns `None` for BOTH "no header" and "wrong scheme" (see
step 1), so `credentials is None` alone cannot distinguish "the client sent nothing" from
"the client sent `Authorization: Basic ...`". Without the raw `request.headers` check,
`get_optional_user_id` would silently treat a non-Bearer `Authorization` header as
anonymous instead of rejecting it — precisely the downgrade this semantics forbids.

**Why a `core` module imports FastAPI.** Pre-empting the obvious review question:
ARCHITECTURE.md §3.1 constrains the import direction between THIS PROJECT's layers
(`core` must not import from `api`, `features`, or `infrastructure`) — it says nothing
about third-party libraries. `app.core.settings` already imports pydantic on the same
footing, and `app.infrastructure.db.pool` imports asyncpg. What §3.1 actually forbids is
`core` reaching UP into `app.api`, which this module does not do — `app.api.deps` imports
FROM `app.core.auth`, never the reverse.
"""

import logging
from typing import Annotated, Any, Final, NoReturn
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWTError

from app.core.auth.jwks import JwksCache, get_jwks_cache


logger = logging.getLogger(__name__)

# ES256 first because that is what Supabase actually signs with: a real sign-in against
# the local stack returns a header of {"alg": "ES256", "kid": "<uuid>", "typ": "JWT"} over
# a single EC P-256 JWKS key (measured against the running local stack). RS256 is
# Supabase's other supported asymmetric algorithm. HS256 is deliberately absent — a
# symmetric key published in a JWKS document is a public signing secret, not something
# that can safely gate authentication. `none` is deliberately absent — the classic bypass.
# A tuple, not a list, so it cannot be mutated at runtime by anything that imports it.
_ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("ES256", "RS256")

# A Supabase service-role key is a JWT from the same issuer, signed by the same keys, with
# a different `aud`. Verifying `aud` is the only thing standing between a leaked
# service-role key and it being accepted here as an ordinary user token.
_EXPECTED_AUDIENCE: Final = "authenticated"

# Measured: without `options={"require": [...]}`, PyJWT verifies `exp` only if the claim
# happens to be present — a token that omits it decodes successfully and never expires.
# "Absent" must not be allowed to read as "passed".
_REQUIRED_CLAIMS: Final[tuple[str, ...]] = ("exp", "aud", "sub")

# One string, so every 401 body is byte-identical by construction — see the module
# docstring's "why every failure funnels through one helper".
_UNAUTHENTICATED_DETAIL: Final = "Invalid or missing authentication credentials"

# Lowercase because Starlette's Headers mapping is case-insensitive but iterates/compares
# on whatever case was sent; `in request.headers` handles the case-insensitivity, this is
# just the literal being tested against.
_AUTHORIZATION_HEADER: Final = "authorization"

# auto_error=False: FastAPI's own built-in 401 for a missing/malformed Authorization header
# has a different body from _UNAUTHENTICATED_DETAIL, and two differently-worded 401s coming
# from one endpoint is itself a fingerprint. Returning None here lets _reject() be the only
# thing that ever produces a 401 from this dependency.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Supabase access token, verified locally against the project's JWKS.",
)

JwksCacheDep = Annotated[JwksCache, Depends(get_jwks_cache)]
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]


def _reject(reason: str) -> NoReturn:
    """Log `reason` server-side and raise the one 401 every failure path shares.

    `reason` must always be a literal string, at most decorated with an exception TYPE
    NAME (e.g. `type(error).__name__`) — **never** the token, the raw `Authorization`
    header value, or any claim from the token, all of which are credentials under
    ARCHITECTURE.md §9.1/§9.4 and must never appear in a log line.

    Typed `-> NoReturn` so mypy sees control leave at every call site, and so a future edit
    that accidentally makes this function sometimes return is caught by the type checker
    instead of silently letting a caller fall through past a rejected token.
    """
    logger.warning("Rejected an authentication attempt: %s", reason)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHENTICATED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _verify_bearer_token(token: str, cache: JwksCache) -> str:
    """Run the ordered verification recipe from the module docstring; return the user id."""
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as error:
        _reject(f"unreadable token header ({type(error).__name__})")

    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in _ALLOWED_ALGORITHMS:
        _reject("token header carries a disallowed alg")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        _reject("token header carries no kid")

    key = await cache.get_key(kid)
    if key is None:
        _reject("no signing key for the token's kid")

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience=_EXPECTED_AUDIENCE,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except PyJWTError as error:
        _reject(f"token rejected during verification ({type(error).__name__})")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        _reject("verified token has no usable sub")

    return sub


async def get_current_user_id(credentials: BearerCredentials, cache: JwksCacheDep) -> str:
    """Require a valid Supabase access token; return its `sub` claim as the user id.

    Raises `HTTPException(401)` (via `_reject`) if the header is missing, malformed, or the
    token fails verification for any reason. See the module docstring for the full ordered
    recipe and why each step happens where it does.
    """
    if credentials is None:
        _reject("no Bearer credentials on the request")
    return await _verify_bearer_token(credentials.credentials, cache)


async def get_optional_user_id(
    request: Request, credentials: BearerCredentials, cache: JwksCacheDep
) -> str | None:
    """Return the caller's user id if authenticated, `None` if anonymous.

    "Anonymous" means no `Authorization` header was sent at all. A header that IS present
    but does not verify is rejected with 401, never silently downgraded to `None` — see the
    module docstring's "OptionalUser semantics" for why. That is also why this checks
    `request.headers` directly rather than trusting `credentials is None`:
    `HTTPBearer(auto_error=False)` returns `None` for both "no header" and "wrong scheme"
    (see the module docstring's step 1), so `credentials is None` cannot by itself tell
    "nothing sent" apart from "something non-Bearer was sent" — only the raw header check
    can, and without it an `Authorization: Basic ...` header would be silently treated as
    anonymous instead of rejected.
    """
    if _AUTHORIZATION_HEADER not in request.headers:
        return None
    return await get_current_user_id(credentials, cache)


CurrentUser = Annotated[str, Depends(get_current_user_id)]
OptionalUser = Annotated[str | None, Depends(get_optional_user_id)]


async def get_current_user_uuid(user_id: CurrentUser) -> UUID:
    """Require a valid token and return its `sub` claim as a `UUID`.

    A thin adapter over `get_current_user_id`, not a second verification path: by the time
    this runs the token has already been through the full ordered recipe in the module
    docstring, and all this adds is parsing the `sub` that recipe produced.

    **Why routes want a `UUID` rather than the raw `str`.** Every owner value this
    application compares a `sub` against comes out of Postgres as a `uuid.UUID` —
    `websites.user_id` is a `uuid` column (db/schema.prisma) and asyncpg decodes it as one.
    A `str` compared against a `UUID` is never equal, so an ownership check written against
    the raw claim would answer `403` for the resource's actual owner, on every request,
    with no error anywhere. Parsing once here, in the dependency, is what keeps that bug
    from having to be re-avoided in every feature.

    **Why a non-UUID `sub` is a 401.** Supabase issues UUID `sub` claims. A token carrying
    anything else was not minted by the identity provider this API trusts, whatever else is
    true of its signature — it cannot own a row and cannot create one, so there is no
    request it could legitimately make. Routing it through `_reject` gives it the same body
    as every other authentication failure, rather than leaking through a distinct status or
    message that the signature verified but the subject did not fit.
    """
    try:
        return UUID(user_id)
    except ValueError:
        # A description, never the claim's value: `sub` comes out of a credential, and this
        # repository's logs are public (ARCHITECTURE.md §9.4, and `_reject`'s docstring).
        _reject("verified token's sub is not a UUID")


CurrentUserId = Annotated[UUID, Depends(get_current_user_uuid)]
