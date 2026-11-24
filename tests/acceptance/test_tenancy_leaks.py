"""Leak tests for the tenancy layer.

These bypass the API on purpose and talk to Postgres as the API's own database role. They
prove that isolation holds in the database itself, so a missing WHERE clause in application
code cannot expose another merchant's rows, another shopper's order or an internal rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from returns_desk.seeds import Ids
from tests.conftest import World

TENANT_TABLES = [
    "merchants",
    "memberships",
    "shoppers",
    "orders",
    "order_lines",
    "policies",
    "policy_versions",
    "policy_sections",
    "cases",
    "case_messages",
    "case_events",
    "proposals",
    "review_records",
    "approvals",
    "resolution_actions",
    "action_attempts",
    "ledger_entries",
]


@contextmanager
def as_app(
    world: World,
    *,
    merchant: str | None = None,
    user: str | None = None,
    role: str | None = None,
    shopper: bool = False,
) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A transaction as rd_app with the given claims, rolled back at the end."""
    with psycopg.connect(world.settings.app_dsn, row_factory=dict_row) as conn:
        conn.execute(
            """
            select set_config('app.merchant_id', %s, true), set_config('app.user_id', %s, true),
                   set_config('app.role', %s, true), set_config('app.shopper_id', %s, true)
            """,
            (
                str(Ids.merchant(merchant)) if merchant else "",
                str(Ids.user(user)) if user else "",
                role or "",
                str(Ids.shopper(merchant, user)) if shopper and merchant and user else "",
            ),
        )
        try:
            yield conn
        finally:
            conn.rollback()


@pytest.fixture
def busy_world(world: World) -> World:
    """Both merchants with real activity: a refunded case at Northline and a case waiting
    for review at Vale, so every table has rows on both sides."""
    world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    )
    world.call(
        "POST", "/cases", user="maya", merchant="vale", json_body=world.refund_request("vale")
    )
    world.call(
        "POST",
        "/cases",
        user="omar",
        merchant="vale",
        json_body=world.refund_request("vale", number="2001", line="tote"),
    )
    return world


def test_without_claims_every_table_is_empty(busy_world: World):
    with as_app(busy_world) as conn:
        for table in TENANT_TABLES:
            assert conn.execute(f"select count(*) as n from {table}").fetchone()["n"] == 0, table


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_staff_never_see_another_merchants_rows(busy_world: World, table: str):
    column = "id" if table == "merchants" else "merchant_id"
    with as_app(busy_world, merchant="northline", user="sam", role="supervisor") as conn:
        rows = conn.execute(f"select {column} as merchant from {table}").fetchall()
    assert {r["merchant"] for r in rows} <= {Ids.merchant("northline")}, table


def test_both_sides_actually_have_rows(busy_world: World):
    # Guards the test above against passing because a table is simply empty.
    for table in ("orders", "order_lines", "cases", "proposals", "policy_versions"):
        by_merchant = busy_world.rows(
            f"select merchant_id, count(*) as n from {table} group by merchant_id"
        )
        assert len(by_merchant) == 2, table
    with as_app(busy_world, merchant="northline", user="sam", role="supervisor") as conn:
        assert conn.execute("select count(*) as n from cases").fetchone()["n"] == 1
        assert conn.execute("select count(*) as n from ledger_entries").fetchone()["n"] == 1
    with as_app(busy_world, merchant="vale", user="vera", role="supervisor") as conn:
        assert conn.execute("select count(*) as n from cases").fetchone()["n"] == 2
        assert conn.execute("select count(*) as n from review_records").fetchone()["n"] == 1


def test_a_shopper_sees_only_their_own_orders_and_cases(busy_world: World):
    with as_app(busy_world, merchant="vale", user="maya", role="shopper", shopper=True) as conn:
        orders = conn.execute("select shopper_id, order_number from orders").fetchall()
        assert [o["order_number"] for o in orders] == ["VS-1001"]
        lines = conn.execute("select description from order_lines").fetchall()
        assert {line["description"] for line in lines} == {"Trail Jacket"}
        cases = conn.execute("select shopper_id from cases").fetchall()
        assert {c["shopper_id"] for c in cases} == {Ids.shopper("vale", "maya")}
        assert conn.execute("select count(*) as n from proposals").fetchone()["n"] == 1
        # Never the other shopper's rows by ID either.
        assert (
            conn.execute(
                "select 1 from orders where id = %s", (Ids.order("vale", "2001"),)
            ).fetchone()
            is None
        )


def test_a_shopper_role_without_a_shopper_id_sees_no_orders(busy_world: World):
    with as_app(busy_world, merchant="vale", user="maya", role="shopper") as conn:
        assert conn.execute("select count(*) as n from orders").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from cases").fetchone()["n"] == 0


def test_internal_rules_and_review_records_are_supervisor_only(busy_world: World):
    for user, role, shopper in (("maya", "shopper", True), ("alex", "associate", False)):
        with as_app(busy_world, merchant="vale", user=user, role=role, shopper=shopper) as conn:
            types = conn.execute("select distinct policy_type::text as t from policies").fetchall()
            assert [t["t"] for t in types] == ["customer_terms"], role
            audiences = conn.execute(
                "select distinct audience::text as a from policy_sections"
            ).fetchall()
            assert [a["a"] for a in audiences] == ["customer"], role
            assert conn.execute("select count(*) as n from review_records").fetchone()["n"] == 0
            rules = conn.execute("select rules from policy_versions").fetchall()
            assert all("auto_approve_limit_cents" not in r["rules"] for r in rules), role

    with as_app(busy_world, merchant="vale", user="vera", role="supervisor") as conn:
        assert conn.execute("select count(*) as n from review_records").fetchone()["n"] == 1
        assert (
            conn.execute(
                "select count(*) as n from policy_sections where audience = 'supervisor'"
            ).fetchone()["n"]
            == 2
        )

    # Shoppers do not get the audit trail or the decisions.
    with as_app(busy_world, merchant="vale", user="maya", role="shopper", shopper=True) as conn:
        assert conn.execute("select count(*) as n from case_events").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from approvals").fetchone()["n"] == 0


def test_rows_cannot_be_written_into_another_merchant(busy_world: World):
    now = busy_world.clock.now()
    # A Northline shopper tries to open a case that belongs to Vale.
    with (
        as_app(busy_world, merchant="northline", user="maya", role="shopper", shopper=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(
            """
            insert into cases
              (merchant_id, shopper_id, client_request_id, order_id, order_line_id, quantity,
               requested_action, reason_code, request_received_at, created_at, updated_at)
            values (%s, %s, 'x', %s, %s, 1, 'refund', 'damaged', %s, %s, %s)
            """,
            (
                Ids.merchant("vale"),
                Ids.shopper("vale", "maya"),
                Ids.order("vale", "1001"),
                Ids.line("vale", "1001", "blue"),
                now,
                now,
                now,
            ),
        )
    # Updating another merchant's line matches no rows.
    with as_app(busy_world, merchant="northline", user="sam", role="supervisor") as conn:
        updated = conn.execute(
            "update order_lines set reserved_quantity = 1 where id = %s",
            (Ids.line("vale", "1001", "green"),),
        )
        assert updated.rowcount == 0


def test_an_associate_cannot_record_a_decision_in_the_database(busy_world: World):
    proposal = busy_world.rows(
        "select id, version from proposals where status = 'pending_review' limit 1"
    )[0]
    for user, role in (("alex", "associate"), ("vera", "supervisor")):
        with as_app(busy_world, merchant="vale", user=user, role=role) as conn:
            statement = (
                """
                insert into approvals
                  (merchant_id, proposal_id, proposal_version, reviewer_user_id, decision,
                   decided_at)
                values (%s, %s, %s, %s, 'approved', %s)
                """,
                (
                    Ids.merchant("vale"),
                    proposal["id"],
                    proposal["version"],
                    # Even a supervisor cannot sign as somebody else.
                    Ids.user("alex"),
                    busy_world.clock.now(),
                ),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(*statement)


def test_the_api_role_cannot_settle_money_or_reach_the_processor(busy_world: World):
    attempts = [
        "update resolution_actions set status = 'succeeded'",
        "insert into ledger_entries (merchant_id, action_id, order_id, order_line_id, kind, "
        "quantity, amount_cents, provider_refund_id, recorded_at) "
        "select merchant_id, id, order_id, order_line_id, 'refund', 1, 1, 'x', now() "
        "from resolution_actions",
        "update order_lines set refunded_cents = 0",
        "update order_lines set unit_paid_cents = 1",
        "delete from cases",
        "select * from sandbox_processor.refunds",
        "insert into sandbox_processor.faults (kind, remaining) values ('reject', 1)",
        "select * from schema_migrations",
    ]
    for statement in attempts:
        with (
            as_app(busy_world, merchant="northline", user="sam", role="supervisor") as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(statement)


def test_the_processor_role_cannot_read_application_tables(busy_world: World):
    with psycopg.connect(busy_world.settings.processor_dsn) as conn:
        for table in ("orders", "cases", "resolution_actions", "ledger_entries", "app_users"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"select * from {table}")
            conn.rollback()


def test_the_database_itself_refuses_to_over_refund_a_line(world: World):
    line_id = Ids.line("northline", "1001", "blue")
    with psycopg.connect(world.settings.admin_dsn) as conn:
        for statement in (
            "update order_lines set refunded_quantity = 2 where id = %s",
            "update order_lines set reserved_quantity = 1, refunded_quantity = 1 where id = %s",
            "update order_lines set refunded_cents = 18001 where id = %s",
            "update order_lines set reserved_cents = 1, refunded_cents = 18000 where id = %s",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, (line_id,))
            conn.rollback()


def test_policy_versions_cannot_overlap_or_be_edited(world: World):
    policy_id = Ids.policy("northline", "customer_terms")
    merchant_id = Ids.merchant("northline")
    now = world.clock.now()
    with psycopg.connect(world.settings.admin_dsn) as conn:
        # A version that starts inside an existing version's interval.
        with pytest.raises(psycopg.errors.ExclusionViolation):
            conn.execute(
                """
                insert into policy_versions
                  (merchant_id, policy_id, version_label, effective_from, effective_to, rules)
                values (%s, %s, 'overlap', %s, %s, '{}')
                """,
                (merchant_id, policy_id, now - timedelta(days=50), now - timedelta(days=40)),
            )
        conn.rollback()
        # Published terms are immutable, even for the admin role.
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "update policy_versions set rules = '{}' where policy_id = %s", (policy_id,)
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("delete from policy_versions where policy_id = %s", (policy_id,))
        conn.rollback()


def test_claims_do_not_leak_between_requests_on_a_pooled_connection(world: World):
    # Drive many requests for both merchants through the small shared pool.
    for _ in range(6):
        north = world.call("GET", "/orders", user="maya", merchant="northline").json()["orders"]
        vale = world.call("GET", "/orders", user="omar", merchant="vale").json()["orders"]
        assert [o["order_number"] for o in north] == ["NL-1001"]
        assert [o["order_number"] for o in vale] == ["VS-2001"]
    # A connection handed back to the pool carries no claims.
    with world.container.db._app.connection() as conn:
        row = conn.execute(
            "select app_merchant() as m, app_user() as u, app_role() as r, app_shopper() as s"
        ).fetchone()
    assert row == {"m": None, "u": None, "r": None, "s": None}
