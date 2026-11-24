from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt

from returns_desk.config import Settings
from returns_desk.db import Database, Principal
from returns_desk.errors import Forbidden, NotAuthenticated

Surface = Literal["shopper", "staff"]

# Asymmetric algorithms an identity provider may sign with. Their public keys are fetched
# from the provider's JWKS endpoint. HS256 uses the shared secret instead.
_ASYMMETRIC = ("ES256", "RS256", "EdDSA")
_jwks_clients: dict[str, jwt.PyJWKClient] = {}
_jwks_lock = threading.Lock()


def mint_token(
    settings: Settings,
    *,
    user_id: UUID,
    merchant_id: UUID,
    surface: Surface,
    now: datetime,
    ttl: timedelta = timedelta(hours=8),
) -> str:
    """Issue an access token for local development and tests.

    The token says who signed in, to which merchant, and on which surface (shopper chat or
    staff queue). It deliberately carries no role. In a deployment Supabase Auth issues the
    tokens instead; those say only who the user is, and the merchant and surface come with
    each request (see `authenticate`).
    """
    return jwt.encode(
        {
            "sub": str(user_id),
            "merchant_id": str(merchant_id),
            "surface": surface,
            "aud": settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def _signing_key(settings: Settings, token: str) -> tuple[Any, list[str]]:
    """The key to check this token with, and the algorithms that key may be used for."""
    algorithm = jwt.get_unverified_header(token).get("alg")
    if algorithm == "HS256":
        return settings.jwt_secret, ["HS256"]
    if algorithm in _ASYMMETRIC and settings.jwt_jwks_url:
        with _jwks_lock:
            client = _jwks_clients.get(settings.jwt_jwks_url)
            if client is None:
                client = jwt.PyJWKClient(settings.jwt_jwks_url, cache_keys=True, lifespan=3600)
                _jwks_clients[settings.jwt_jwks_url] = client
        return client.get_signing_key_from_jwt(token).key, [algorithm]
    raise jwt.InvalidAlgorithmError(f"tokens signed with {algorithm!r} are not accepted")


def verify_token(settings: Settings, authorization: str | None) -> dict[str, Any]:
    """Check a bearer token's signature, audience and expiry. Returns its claims."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise NotAuthenticated("Sign in to continue")
    token = authorization[7:].strip()
    try:
        key, algorithms = _signing_key(settings, token)
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.jwt_audience,
            # Expiry is real time on purpose, even when the app runs on a frozen clock.
            options={"require": ["sub", "exp", "aud"]},
        )
        UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise NotAuthenticated("Sign in to continue") from exc
    return claims


def authenticate(
    settings: Settings,
    db: Database,
    authorization: str | None,
    *,
    merchant: str | None = None,
    surface: str | None = None,
) -> Principal:
    """Turn a bearer token into a verified principal.

    The signature proves who the user is. Which merchant and which surface they are using
    is read from the token when it carries them (local development tokens do) and
    otherwise from the request (`merchant` is the merchant's slug). Either way that is only
    a choice among relationships the user really has: what they may do is read from the
    database on every request, staff role from memberships and shopper identity from
    shoppers. A valid token for a merchant the user has no relationship with gets nothing.
    """
    claims = verify_token(settings, authorization)
    user_id = UUID(claims["sub"])
    try:
        if "merchant_id" in claims:
            merchant_id: UUID | None = UUID(claims["merchant_id"])
            surface = claims["surface"]
        else:
            if not merchant or surface not in ("shopper", "staff"):
                raise NotAuthenticated("Choose a merchant to continue")
            merchant_id = db.merchant_id(merchant)
    except (KeyError, ValueError, TypeError) as exc:
        raise NotAuthenticated("Sign in to continue") from exc

    context = db.auth_context(user_id, merchant_id) if merchant_id else None
    if context is None:
        raise Forbidden("You do not have access to this merchant")
    assert merchant_id is not None

    if surface == "staff" and context["staff_role"] in ("associate", "supervisor"):
        return Principal(
            user_id=user_id,
            merchant_id=merchant_id,
            role=context["staff_role"],
            shopper_id=None,
            merchant_timezone=context["merchant_timezone"],
            merchant_slug=context["merchant_slug"],
            permission_version=context["permission_version"],
        )
    if surface == "shopper" and context["shopper_id"] is not None:
        return Principal(
            user_id=user_id,
            merchant_id=merchant_id,
            role="shopper",
            shopper_id=context["shopper_id"],
            merchant_timezone=context["merchant_timezone"],
            merchant_slug=context["merchant_slug"],
            permission_version=context["permission_version"],
        )
    raise Forbidden("You do not have access to this merchant")
