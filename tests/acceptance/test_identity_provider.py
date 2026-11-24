"""Sign-in through an identity provider (Supabase Auth in the deployment).

A provider's token proves who the user is and nothing else. The merchant and the side of
the desk come with the request as two headers, and are only ever a choice among
relationships the database says the user has. These tests use tokens shaped like
Supabase's, signed both ways Supabase can sign them: with the shared secret (HS256) and
with an asymmetric key published as a JWKS (ES256).
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from returns_desk import auth
from returns_desk.deploy import create_supabase_users, reconcile_cron_sql
from returns_desk.seeds import Ids
from tests.conftest import World


def provider_token(world: World, user: str, **overrides: Any) -> str:
    """A token as Supabase Auth issues it: who the user is, and no merchant or role."""
    claims = {
        "sub": str(Ids.user(user)),
        "aud": "authenticated",
        "role": "authenticated",
        "email": f"{user}@example.test",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        **overrides,
    }
    return jwt.encode(claims, world.settings.jwt_secret, algorithm="HS256")


def headers(token: str, merchant: str | None = None, surface: str | None = None) -> dict[str, str]:
    out = {"Authorization": f"Bearer {token}"}
    if merchant:
        out["X-RD-Merchant"] = merchant
    if surface:
        out["X-RD-Surface"] = surface
    return out


def test_a_provider_token_lists_where_the_user_can_go(world: World):
    maya = world.call("GET", "/me/merchants", headers=headers(provider_token(world, "maya")))
    nina = world.call("GET", "/me/merchants", headers=headers(provider_token(world, "nina")))

    assert maya.json()["merchants"] == [
        {"merchant_slug": "northline", "merchant_name": "Northline Supply", "role": "shopper"},
        {"merchant_slug": "vale", "merchant_name": "Vale Studio", "role": "shopper"},
    ]
    assert nina.json()["merchants"] == [
        {"merchant_slug": "northline", "merchant_name": "Northline Supply", "role": "associate"}
    ]
    assert world.call("GET", "/me/merchants").status_code == 401


def test_the_merchant_and_surface_come_with_the_request_and_are_checked(world: World):
    token = provider_token(world, "maya")

    mine = world.call("GET", "/orders", headers=headers(token, "northline", "shopper"))
    assert mine.status_code == 200
    assert [o["order_number"] for o in mine.json()["orders"]] == ["NL-1001"]
    me = world.call("GET", "/me", headers=headers(token, "vale", "shopper")).json()
    assert (me["merchant"], me["role"]) == ("vale", "shopper")

    # No choice made yet.
    assert world.call("GET", "/orders", headers=headers(token)).status_code == 401
    assert world.call("GET", "/orders", headers=headers(token, "northline")).status_code == 401


def test_the_headers_choose_among_real_relationships_and_grant_nothing(world: World):
    maya = provider_token(world, "maya")
    nina = provider_token(world, "nina")

    # A shopper asking for the staff side, staff asking for another merchant, a merchant
    # that does not exist, and a surface that does not exist.
    refused = [
        headers(maya, "northline", "staff"),
        headers(nina, "vale", "staff"),
        headers(nina, "northline", "shopper"),
        headers(maya, "no-such-store", "shopper"),
    ]
    assert [world.call("GET", "/cases", headers=h).status_code for h in refused] == [403] * 4
    assert (
        world.call("GET", "/cases", headers=headers(maya, "northline", "admin")).status_code == 401
    )

    # The role is the database's word, whatever the request says.
    staff = world.call("GET", "/me", headers=headers(nina, "northline", "staff")).json()
    assert staff["role"] == "associate"
    assert (
        world.call("GET", "/reviews", headers=headers(nina, "northline", "staff")).status_code
        == 403
    )


def test_a_whole_conversation_works_on_a_provider_token(world: World):
    token = provider_token(world, "maya")

    response = world.call(
        "POST",
        "/cases",
        headers=headers(token, "northline", "shopper"),
        json_body={
            "client_request_id": "idp-1",
            "message": "The blue jacket arrived damaged. Please refund just that one.",
        },
    )

    assert response.status_code == 200
    assert response.json()["case"]["status"] == "refunded"


def test_tokens_that_do_not_check_out_are_refused(world: World):
    good = provider_token(world, "maya")
    bad = [
        provider_token(world, "maya", exp=int(time.time()) - 10),
        provider_token(world, "maya", aud="someone-else"),
        jwt.encode({"sub": str(Ids.user("maya")), "aud": "authenticated"}, "w" * 40, "HS256"),
        provider_token(world, "maya", sub="not-a-uuid"),
        good[:-3] + "abc",
        "",
    ]
    for token in bad:
        response = world.call("GET", "/orders", headers=headers(token, "northline", "shopper"))
        assert response.status_code == 401, token
        assert world.call("GET", "/me/merchants", headers=headers(token)).status_code == 401


class OneKey:
    """Stands in for the provider's JWKS endpoint: one published public key."""

    def __init__(self, public_key: Any) -> None:
        self.key = public_key

    def get_signing_key_from_jwt(self, token: str) -> OneKey:
        return self


def test_asymmetric_tokens_are_checked_against_the_published_key(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    private = ec.generate_private_key(ec.SECP256R1())
    stranger = ec.generate_private_key(ec.SECP256R1())
    jwks_url = "https://project.supabase.test/auth/v1/.well-known/jwks.json"
    monkeypatch.setattr(world.settings, "jwt_jwks_url", jwks_url)
    monkeypatch.setitem(auth._jwks_clients, jwks_url, OneKey(private.public_key()))
    claims = {
        "sub": str(Ids.user("maya")),
        "aud": "authenticated",
        "exp": int(time.time()) + 600,
    }

    signed = jwt.encode(claims, private, algorithm="ES256")
    forged = jwt.encode(claims, stranger, algorithm="ES256")
    unsigned = jwt.encode(claims, None, algorithm="none")

    assert (
        world.call("GET", "/orders", headers=headers(signed, "northline", "shopper")).status_code
        == 200
    )
    for token in (forged, unsigned):
        response = world.call("GET", "/orders", headers=headers(token, "northline", "shopper"))
        assert response.status_code == 401

    # Without a JWKS address, an asymmetric token has nothing to be checked against.
    monkeypatch.setattr(world.settings, "jwt_jwks_url", "")
    assert (
        world.call("GET", "/orders", headers=headers(signed, "northline", "shopper")).status_code
        == 401
    )


def test_the_tool_endpoint_takes_the_same_headers(world: World):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    case = world.say("maya", "northline", "Can I return the blue jacket? It arrived damaged.")
    app = world.client.app

    async def status(request_headers: dict[str, str]) -> Any:
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://testserver",
                headers=request_headers,
            ) as http,
            Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client,
        ):
            return await client.call_tool("get_resolution_status", {"case_id": case["id"]})

    token = provider_token(world, "maya")
    allowed = world.client.portal.call(status, headers(token, "northline", "shopper"))
    elsewhere = world.client.portal.call(status, headers(token, "vale", "shopper"))
    unchosen = world.client.portal.call(status, headers(token))

    assert allowed.structured_content["state"] == "awaiting_customer"
    # Her own case at another merchant looks like no case at all.
    assert elsewhere.is_error and "not_found" in elsewhere.content[0].text
    assert unchosen.is_error


# --- deployment helpers -------------------------------------------------------------------


def test_demo_people_are_created_in_supabase_auth_under_their_seeded_ids():
    seen: list[tuple[str, str, dict[str, Any]]] = []
    existing = {str(Ids.user("maya"))}

    def supabase(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["apikey"] == "service-role-key"
        assert request.headers["authorization"] == "Bearer service-role-key"
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        if request.method == "POST":
            if body["id"] in existing:
                return httpx2.Response(422, json={"msg": "User already registered"})
            return httpx2.Response(200, json={"id": body["id"], "email": body["email"]})
        return httpx2.Response(200, json={"id": request.url.path.rsplit("/", 1)[1]})

    results = create_supabase_users(
        "https://project.supabase.test",
        "service-role-key",
        "a-long-demo-password",
        transport=httpx2.MockTransport(supabase),
    )

    by_user = {row["user"]: row["result"] for row in results}
    assert by_user["maya"] == "updated" and by_user["sam"] == "created"
    assert len(results) == 7
    creates = [body for method, _, body in seen if method == "POST"]
    assert {body["id"] for body in creates} == {
        str(Ids.user(key)) for key in ("maya", "omar", "nina", "sam", "lee", "alex", "vera")
    }
    assert all(body["email_confirm"] is True for body in creates)
    with pytest.raises(ValueError, match="at least 12"):
        create_supabase_users("https://project.supabase.test", "k", "short")


def test_supabase_refusing_a_custom_id_is_caught():
    def supabase(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "11111111-1111-1111-1111-111111111111"})

    with pytest.raises(RuntimeError, match="not the seeded ID"):
        create_supabase_users(
            "https://project.supabase.test",
            "service-role-key",
            "a-long-demo-password",
            transport=httpx2.MockTransport(supabase),
        )


def test_the_scheduler_sql_calls_reconcile_with_the_internal_token():
    sql = reconcile_cron_sql("https://returns-desk.onrender.test/", "internal-token-value")

    assert "'https://returns-desk.onrender.test/internal/reconcile'" in sql
    assert "'Bearer internal-token-value'" in sql
    assert "'* * * * *'" in sql and "cron.unschedule" in sql
    with pytest.raises(ValueError):
        reconcile_cron_sql("https://x.test", "it's-quoted")
