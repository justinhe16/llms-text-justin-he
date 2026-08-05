"""Tests for app.core.auth.dependencies: CurrentUser and OptionalUser.

Every test drives a real HTTP request through a throwaway FastAPI app (`auth_app` below) —
never `get_current_user_id`/`get_optional_user_id` called directly — so what is under test
includes the real `HTTPBearer` header parsing, FastAPI's dependency solver, and the real
`HTTPException` -> `JSONResponse` conversion, not just this module's own Python logic.

`CurrentUser`/`OptionalUser` are imported from `app.api.deps` (not from
`app.core.auth.dependencies`), the same import path a real router uses, so the suite
exercises — and proves live — the re-export `app.api.deps` performs.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from conftest import _FakeClock, _JwksServer, _SigningKey, user_claims
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import deps
from app.api.deps import CurrentUser, OptionalUser
from app.core.auth import dependencies as auth_dependencies
from app.core.auth.jwks import JwksCache, get_jwks_cache


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding, the encoding every JWT segment uses."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unsigned_token(header: dict[str, Any], claims: dict[str, Any]) -> str:
    """Build a JWT with an arbitrary header and a signature segment that is never actually
    checked against anything, because the rejections this helper exists to test — a
    disallowed alg, a missing alg, a non-string kid — all happen before signature
    verification is ever reached.
    """
    header_b64 = _b64url(json.dumps(header).encode())
    payload_b64 = _b64url(json.dumps(claims).encode())
    signature_b64 = _b64url(b"not-a-real-signature")
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def _hs256_token_signed_with_the_public_jwk(
    claims: dict[str, Any], kid: str, jwk: dict[str, Any]
) -> str:
    """Hand-build the classic algorithm-confusion token: an HS256-signed JWT whose "secret"
    is the EC public key material published in the JWKS document — material anyone who can
    read the JWKS endpoint already has.

    Built by hand rather than with `jwt.encode(..., algorithm="HS256")` because PyJWT
    refuses that call outright: passing an asymmetric key to the HS256 encoder raises
    `InvalidKeyError` (measured), which would make the attack token impossible to
    construct through the library's own encode path and prove nothing about our decode
    path.
    """
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    header_b64 = _b64url(json.dumps(header).encode())
    payload_b64 = _b64url(json.dumps(claims).encode())
    secret = json.dumps(jwk, sort_keys=True).encode()
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _detached_payload_token(key: _SigningKey) -> str:
    """A JWT with an empty (detached) payload segment: `header..signature`."""
    header_b64 = _b64url(json.dumps({"alg": key.algorithm, "typ": "JWT", "kid": key.kid}).encode())
    return f"{header_b64}..{_b64url(b'sig')}"


@pytest.fixture
def auth_app(jwks_cache: JwksCache) -> FastAPI:
    """A throwaway app carrying the two dependencies under test.

    The production app (`app.main.app`) is never touched: no route is added to it and its
    `dependency_overrides` are never mutated, so nothing here can leak into another test
    module or into the shipped OpenAPI document. What IS real is everything between the
    socket and the dependency — Starlette routing, FastAPI's dependency solver, the real
    `HTTPBearer` instance parsing a real `Authorization` header, and the real
    `HTTPException` -> `JSONResponse` conversion. Calling `get_current_user_id` directly
    would assert nothing about the 401 body or the `WWW-Authenticate` header, which is
    precisely what `test_every_authentication_failure_returns_an_identical_401` measures.
    """
    api = FastAPI()

    @api.get("/protected")
    async def protected(user_id: CurrentUser) -> dict[str, str]:
        return {"user_id": user_id}

    @api.get("/optional")
    async def optional(user_id: OptionalUser) -> dict[str, str | None]:
        return {"user_id": user_id}

    # Keyed on the callable itself: JwksCacheDep is Annotated[JwksCache,
    # Depends(get_jwks_cache)], so overriding get_jwks_cache substitutes the cache for
    # both dependencies at once, without the module singleton (app.core.auth.jwks._cache)
    # ever being opened. The cache wraps a real httpx.AsyncClient over an
    # httpx.MockTransport, so the client code path under test — URL construction, status
    # handling, JSON parsing — all still runs for real.
    api.dependency_overrides[get_jwks_cache] = lambda: jwks_cache
    return api


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=auth_app), base_url="http://test") as ac:
        yield ac


async def test_a_valid_es256_token_returns_the_sub_claim_as_the_user_id(
    auth_client: AsyncClient, es256_key: _SigningKey
) -> None:
    claims = user_claims()
    token = es256_key.sign(claims)

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"user_id": claims["sub"]}


async def test_a_valid_rs256_token_is_accepted_too(
    auth_client: AsyncClient, rs256_key: _SigningKey
) -> None:
    """The allowlist's second member, exercised end to end."""
    claims = user_claims()
    token = rs256_key.sign(claims)

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"user_id": claims["sub"]}


async def test_a_missing_authorization_header_is_401(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/protected")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header_template",
    ["{token}", "token {token}", "Basic dXNlcjpwdw==", "Bearer", "Bearer "],
)
async def test_a_header_without_a_usable_bearer_prefix_is_401(
    auth_client: AsyncClient, es256_key: _SigningKey, header_template: str
) -> None:
    """`HTTPBearer(auto_error=False)` returns `None` for every one of these — see the
    module docstring's note on why `credentials is None` cannot distinguish them, which
    is exactly why they must ALL end up 401 rather than some being silently accepted.
    """
    token = es256_key.sign(user_claims())
    header_value = header_template.format(token=token)

    response = await auth_client.get("/protected", headers={"Authorization": header_value})

    assert response.status_code == 401


async def test_an_expired_token_is_401(auth_client: AsyncClient, es256_key: _SigningKey) -> None:
    token = es256_key.sign(user_claims(expires_in=-60))

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_a_token_signed_by_a_different_key_is_401(
    auth_client: AsyncClient, foreign_key: _SigningKey, jwks_server: _JwksServer
) -> None:
    """`foreign_key` shares its `kid` with the cached `es256_key` but is a different
    private key entirely. The `kid` must resolve to a real, cached key — so no refetch —
    while the signature itself still fails.
    """
    baseline = jwks_server.request_count
    token = foreign_key.sign(user_claims())

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert jwks_server.request_count == baseline


async def test_an_alg_none_token_is_401(
    auth_client: AsyncClient, es256_key: _SigningKey, jwks_server: _JwksServer
) -> None:
    """The classic bypass. Rejected at the pre-decode allowlist check (step 3), before the
    kid is ever looked up — asserted here by the refetch count staying flat.
    """
    baseline = jwks_server.request_count
    token = jwt.encode(user_claims(), key="", algorithm="none", headers={"kid": es256_key.kid})

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert jwks_server.request_count == baseline


async def test_an_hs256_token_signed_with_the_published_public_key_is_401(
    auth_client: AsyncClient, es256_key: _SigningKey
) -> None:
    """The literal algorithm-confusion attack: HMAC the token with the EC public key's own
    published JWK material and present it under a real, cached kid. See
    `_hs256_token_signed_with_the_public_jwk` for why this is hand-built rather than
    produced with `jwt.encode`.
    """
    token = _hs256_token_signed_with_the_public_jwk(user_claims(), es256_key.kid, es256_key.jwk)

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


@pytest.mark.parametrize("alg", ["ES384", "EdDSA", "RS512", "", 123])
async def test_an_algorithm_outside_the_allowlist_is_401(
    auth_client: AsyncClient, es256_key: _SigningKey, alg: Any
) -> None:
    token = _unsigned_token({"alg": alg, "typ": "JWT", "kid": es256_key.kid}, user_claims())

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_a_token_with_no_kid_is_401_and_triggers_no_refetch(
    auth_client: AsyncClient, es256_key: _SigningKey, jwks_server: _JwksServer
) -> None:
    """Proves the step-4-before-step-5 ordering: a kid-less token must never reach the
    cache, or a flood of them becomes an unauthenticated outbound-traffic amplifier.
    """
    baseline = jwks_server.request_count
    token = jwt.encode(user_claims(), es256_key.private_key, algorithm="ES256")  # no kid header

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert jwks_server.request_count == baseline


async def test_an_unknown_kid_triggers_exactly_one_refetch_and_then_401(
    auth_client: AsyncClient, es256_key: _SigningKey, jwks_server: _JwksServer
) -> None:
    baseline = jwks_server.request_count
    token = es256_key.sign(user_claims(), headers={"kid": "totally-unknown-kid"})

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert jwks_server.request_count == baseline + 1


async def test_repeated_unknown_kids_are_rate_limited_to_one_refetch_per_window(
    auth_client: AsyncClient,
    es256_key: _SigningKey,
    jwks_server: _JwksServer,
    fake_clock: _FakeClock,
) -> None:
    """MEASURED (HTTP level): the same rate-limit property `test_jwks.py` measures at the
    cache level, this time driven through real HTTP requests against the dependency —
    dozens sequential plus a concurrent burst, then a clock advance past the window.
    """
    baseline = jwks_server.request_count

    def _token(n: int) -> str:
        return es256_key.sign(user_claims(), headers={"kid": f"unknown-{n}"})

    for n in range(30):
        response = await auth_client.get(
            "/protected", headers={"Authorization": f"Bearer {_token(n)}"}
        )
        assert response.status_code == 401

    burst_responses = await asyncio.gather(
        *[
            auth_client.get("/protected", headers={"Authorization": f"Bearer {_token(30 + n)}"})
            for n in range(20)
        ]
    )
    assert all(response.status_code == 401 for response in burst_responses)
    assert jwks_server.request_count == baseline + 1

    fake_clock.advance(61.0)
    response = await auth_client.get(
        "/protected", headers={"Authorization": f"Bearer {_token(999)}"}
    )
    assert response.status_code == 401
    assert jwks_server.request_count == baseline + 2


async def test_a_rotated_kid_is_picked_up_by_a_single_refetch(
    auth_client: AsyncClient, rotated_key: _SigningKey, jwks_server: _JwksServer
) -> None:
    baseline = jwks_server.request_count
    jwks_server.keys.append(rotated_key.jwk)  # the document has rotated; the cache has not
    token = rotated_key.sign(user_claims())

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert jwks_server.request_count == baseline + 1


async def test_verification_makes_no_http_request_when_the_kid_is_cached(
    auth_client: AsyncClient, es256_key: _SigningKey, jwks_server: _JwksServer
) -> None:
    """MEASURED (HTTP level): the counterpart to `test_jwks.py`'s cache-level version,
    driven through real HTTP requests against the dependency instead of the cache
    directly.
    """
    baseline = jwks_server.request_count
    token = es256_key.sign(user_claims())

    for _ in range(25):
        response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    assert jwks_server.request_count == baseline


async def test_a_service_role_shaped_token_is_rejected(
    auth_client: AsyncClient, es256_key: _SigningKey
) -> None:
    """A service-role key is a JWT from the same issuer, signed by the same keys, with a
    different `aud`. Verifying `aud` is the only thing standing between a leaked
    service-role key and it being accepted here as an ordinary user token.
    """
    token = es256_key.sign(user_claims(aud="service_role"))

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_a_token_without_an_exp_claim_is_rejected(
    auth_client: AsyncClient, es256_key: _SigningKey
) -> None:
    """Measured against this PyJWT version: without `options={"require": [...]}`, a token
    with no `exp` at all decodes successfully and never expires. This is the least visible
    failure mode possible, because every normally-issued token carries `exp`.
    """
    claims = user_claims()
    del claims["exp"]
    token = es256_key.sign(claims)

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


@pytest.mark.parametrize("sub", [None, "", "   ", 12345])
async def test_a_token_without_a_usable_sub_is_rejected(
    auth_client: AsyncClient, es256_key: _SigningKey, sub: Any
) -> None:
    claims = user_claims()
    if sub is None:
        del claims["sub"]  # options={"require": [...]} rejects this before it is even read
    else:
        claims["sub"] = sub
    token = es256_key.sign(claims)

    response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_optional_user_is_none_when_there_is_no_authorization_header(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.get("/optional")

    assert response.status_code == 200
    assert response.json() == {"user_id": None}


async def test_optional_user_returns_the_user_id_for_a_valid_token(
    auth_client: AsyncClient, es256_key: _SigningKey
) -> None:
    claims = user_claims()
    token = es256_key.sign(claims)

    response = await auth_client.get("/optional", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"user_id": claims["sub"]}


async def test_optional_user_rejects_a_bad_token_instead_of_downgrading_to_anonymous(
    auth_client: AsyncClient, es256_key: _SigningKey, foreign_key: _SigningKey
) -> None:
    """Decision 8: a client that sent a token expects it honoured. Silently downgrading a
    bad token to "anonymous" would hide an expired session behind a response that looks
    like success. The `Basic` case only 401s because the dependency inspects the raw
    header — see its docstring.
    """
    bad_headers = [
        f"Bearer {es256_key.sign(user_claims(expires_in=-60))}",
        f"Bearer {foreign_key.sign(user_claims())}",
        f"Bearer {es256_key.sign(user_claims(), headers={'kid': 'totally-unknown'})}",
        "Basic dXNlcjpwdw==",
        "Bearer "
        + jwt.encode(user_claims(), key="", algorithm="none", headers={"kid": es256_key.kid}),
    ]

    for header_value in bad_headers:
        response = await auth_client.get("/optional", headers={"Authorization": header_value})
        assert response.status_code == 401, header_value


async def test_every_authentication_failure_returns_an_identical_401(
    auth_client: AsyncClient,
    es256_key: _SigningKey,
    foreign_key: _SigningKey,
    rotated_key: _SigningKey,
) -> None:
    """A client must not be able to fingerprint WHY a token was rejected — expired,
    forged, unknown kid, wrong audience, and outright malformed must all look identical.
    Collects (status, body, WWW-Authenticate, content-type) for every case through both
    routes and asserts they land in a single bucket.
    """
    always_bad_headers = [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwdw==",
        f"Bearer {es256_key.sign(user_claims(expires_in=-60))}",
        f"Bearer {foreign_key.sign(user_claims())}",
        f"Bearer {es256_key.sign(user_claims(), headers={'kid': 'unknown-kid-xyz'})}",
        "Bearer "
        + jwt.encode(user_claims(), key="", algorithm="none", headers={"kid": es256_key.kid}),
        f"Bearer {es256_key.sign(user_claims(aud='service_role'))}",
        f"Bearer {rotated_key.sign(user_claims())}",  # not yet published in the document
        "garbage-not-even-a-jwt",
    ]

    observed: set[tuple[int, bytes, str | None, str | None]] = set()

    def _record(response: httpx.Response) -> None:
        observed.add(
            (
                response.status_code,
                response.content,
                response.headers.get("www-authenticate"),
                response.headers.get("content-type"),
            )
        )

    # No Authorization header at all is a failure only for /protected — /optional treats
    # it as anonymous by design (Decision 8), so it is deliberately excluded below.
    _record(await auth_client.get("/protected"))

    for header_value in always_bad_headers:
        for path in ("/protected", "/optional"):
            _record(await auth_client.get(path, headers={"Authorization": header_value}))

    assert len(observed) == 1
    status_code = next(iter(observed))[0]
    assert status_code == 401


async def test_no_invalid_token_ever_produces_a_500(
    auth_app: FastAPI, es256_key: _SigningKey
) -> None:
    """Its own client, with `raise_app_exceptions=False`. Every other test in this file
    uses the default (`True`), so a genuine bug in the dependency surfaces as a traceback
    in the test run rather than a misleading assertion failure. This test instead lets any
    such bug become a real 500 HTTP response, so `assert status_code == 401` actually
    NAMES the failure it guards against.
    """
    hostile_tokens = [
        "",
        ".",
        "a.b",
        "a.b.c.d",
        # ASCII-only garbage — a raw Authorization header value cannot carry non-ASCII at
        # all (httpx itself rejects one with UnicodeEncodeError before a request is even
        # sent), so this covers "not valid base64, not even close" instead.
        "not!!!valid???base64",
        base64.urlsafe_b64encode(b"x" * 65536).decode(),  # a 64 KB base64 blob, no dots
        "null",
        json.dumps({"alg": "ES256"}),  # a bare JSON object, not a JWT at all
        _b64url(b"not valid json") + "." + _b64url(b"{}") + "." + _b64url(b"sig"),
        _unsigned_token({"alg": "ES256", "typ": "JWT", "kid": 12345}, user_claims()),
        _unsigned_token({"typ": "JWT", "kid": es256_key.kid}, user_claims()),  # no alg
        _detached_payload_token(es256_key),
        _unsigned_token(
            {"alg": "ES256", "typ": "JWT", "kid": es256_key.kid, "b64": False}, user_claims()
        ),
    ]

    async with AsyncClient(
        transport=ASGITransport(app=auth_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for token in hostile_tokens:
            response = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401, f"token={token!r} produced {response.status_code}"


async def test_the_failure_reason_is_logged_but_never_the_token_or_the_claims(
    auth_client: AsyncClient, es256_key: _SigningKey, caplog: pytest.LogCaptureFixture
) -> None:
    claims = user_claims(sub="super-secret-looking-subject-id")
    token = es256_key.sign(claims, headers={"kid": "an-unknown-kid-for-this-test"})

    with caplog.at_level(logging.WARNING):
        response = await auth_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) >= 1
    for record in warnings:
        message = record.getMessage()
        assert token not in message
        assert claims["sub"] not in message
        assert "eyJ" not in message  # base64url prefix shared by every JWT header/payload


def test_the_annotated_aliases_are_re_exported_from_app_api_deps() -> None:
    """Guards against a re-export that redefines the alias and creates a second,
    un-overridable `Depends` — which would make `dependency_overrides[get_jwks_cache]`
    silently stop reaching routes that imported from `app.api.deps`.
    """
    assert deps.CurrentUser is auth_dependencies.CurrentUser
    assert deps.OptionalUser is auth_dependencies.OptionalUser


def test_the_bearer_scheme_is_advertised_in_the_openapi_document(auth_app: FastAPI) -> None:
    schema = auth_app.openapi()
    security_schemes = schema["components"]["securitySchemes"]
    assert any(
        scheme.get("type") == "http" and scheme.get("scheme") == "bearer"
        for scheme in security_schemes.values()
    )
    protected_operation = schema["paths"]["/protected"]["get"]
    assert "security" in protected_operation
