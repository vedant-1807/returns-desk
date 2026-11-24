from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt

from returns_desk.config import Settings
from returns_desk.db import Database, Principal
from returns_desk.errors import Forbidden, NotAuthenticated

Surface = Literal["shopper", "staff"]


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
    staff queue). It deliberately carries no role. Supabase Auth takes over issuing these in
    the deploy milestone; verification below stays the same.
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


def authenticate(settings: Settings, db: Database, authorization: str | None) -> Principal:
    """Turn a bearer token into a verified principal.

    The signature proves who the user is. What they may do is then read from the database
    on every request: staff role from memberships, shopper identity from shoppers. A valid
    token for a merchant the user has no relationship with gets nothing.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise NotAuthenticated("Sign in to continue")
    token = authorization[7:].strip()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            # Expiry is real time on purpose, even when the app runs on a frozen clock.
            options={"require": ["sub", "exp", "aud"]},
        )
        user_id = UUID(claims["sub"])
        merchant_id = UUID(claims["merchant_id"])
        surface = claims["surface"]
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise NotAuthenticated("Sign in to continue") from exc

    context = db.auth_context(user_id, merchant_id)
    if context is None:
        raise Forbidden("You do not have access to this merchant")

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
