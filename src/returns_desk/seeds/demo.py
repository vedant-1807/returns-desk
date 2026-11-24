"""Named, reproducible fixtures.

Every ID is derived from a name, and every timestamp is an offset from `now`, so a clean
seed always produces the same world: two merchants holding the same two-jacket order.

    two_jackets      the reference case (acceptance tests A01, A02, A03, A05, A07, A14)
    return_history   two_jackets plus three completed returns in the last 30 days and a
                     $40 order, all for the same Northline shopper (A10)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from returns_desk.processor.sandbox import SandboxProcessor
from returns_desk.retrieval.embedders import Embedder, HashEmbedder
from returns_desk.retrieval.index import index_policy_versions

FIXTURES = ("two_jackets", "return_history")

_NAMESPACE = UUID("6f1d3c52-8a0e-4b7e-9c55-0d2e7a1b4c10")


def _id(name: str) -> UUID:
    return uuid.uuid5(_NAMESPACE, name)


MERCHANTS: dict[str, dict[str, Any]] = {
    "northline": {
        "name": "Northline Supply",
        "timezone": "America/Los_Angeles",
        "prefix": "NL",
        # Auto-resolves up to and including $200.
        "auto_approve_limit_cents": 20000,
        "limit_text": "Refunds up to and including $200.00 are issued automatically. "
        "Anything above that needs a supervisor.",
    },
    "vale": {
        "name": "Vale Studio",
        "timezone": "America/New_York",
        "prefix": "VS",
        # Requires review above $150.
        "auto_approve_limit_cents": 15000,
        "limit_text": "Refunds above $150.00 require supervisor review before any payment.",
    },
}

USERS: dict[str, tuple[str, str]] = {
    "maya": ("maya@example.test", "Maya Okafor"),
    "omar": ("omar@example.test", "Omar Haddad"),
    "nina": ("nina.associate@northline.test", "Nina Park"),
    "sam": ("sam.supervisor@northline.test", "Sam Rivera"),
    "lee": ("lee.supervisor@northline.test", "Lee Tanaka"),
    "alex": ("alex.associate@vale.test", "Alex Moreau"),
    "vera": ("vera.supervisor@vale.test", "Vera Lindqvist"),
}

STAFF: dict[str, tuple[str, str]] = {
    "nina": ("northline", "associate"),
    "sam": ("northline", "supervisor"),
    "lee": ("northline", "supervisor"),
    "alex": ("vale", "associate"),
    "vera": ("vale", "supervisor"),
}

SHOPPERS = ("maya", "omar")


class Ids:
    """Stable IDs for everything a fixture creates. Tests and the demo use these names."""

    @staticmethod
    def merchant(slug: str) -> UUID:
        return _id(f"merchant/{slug}")

    @staticmethod
    def user(key: str) -> UUID:
        return _id(f"user/{key}")

    @staticmethod
    def shopper(merchant: str, user: str) -> UUID:
        return _id(f"shopper/{merchant}/{user}")

    @staticmethod
    def order(merchant: str, number: str) -> UUID:
        return _id(f"order/{merchant}/{number}")

    @staticmethod
    def line(merchant: str, number: str, key: str) -> UUID:
        return _id(f"line/{merchant}/{number}/{key}")

    @staticmethod
    def policy(merchant: str, policy_type: str) -> UUID:
        return _id(f"policy/{merchant}/{policy_type}")

    @staticmethod
    def policy_version(merchant: str, policy_type: str, key: str) -> UUID:
        return _id(f"policy-version/{merchant}/{policy_type}/{key}")

    @staticmethod
    def order_number(merchant: str, number: str) -> str:
        return f"{MERCHANTS[merchant]['prefix']}-{number}"

    @staticmethod
    def payment_ref(merchant: str, number: str) -> str:
        return f"pay_{merchant}_{number}"


def reset(admin_dsn: str, processor: SandboxProcessor) -> None:
    """Empty every application table and the sandbox processor's records."""
    with psycopg.connect(admin_dsn) as conn:
        # trace_exports points at audit rows by ID. Those IDs start again after a reset,
        # so the record of what was already sent has to go with them.
        conn.execute("truncate table merchants, app_users, trace_exports restart identity cascade")
        # Workflow checkpoints belong to cases that no longer exist.
        conn.execute(
            "truncate table workflow.checkpoints, workflow.checkpoint_blobs, "
            "workflow.checkpoint_writes"
        )
    processor.reset()


def seed(
    admin_dsn: str,
    processor: SandboxProcessor,
    *,
    fixture: str,
    now: datetime,
    embedder: Embedder | None = None,
) -> None:
    """Reset, then load one named fixture relative to `now`. Policy text is embedded with
    `embedder` (the offline hash embedder unless told otherwise) so it is searchable."""
    if fixture not in FIXTURES:
        raise ValueError(f"unknown fixture {fixture!r}; choose one of {', '.join(FIXTURES)}")
    embedder = embedder or HashEmbedder()
    # Check the embedder before touching any data, so a model server that is down cannot
    # leave the database emptied and half seeded.
    embedder.embed_query("ready")
    reset(admin_dsn, processor)
    with psycopg.connect(admin_dsn) as conn:
        _people(conn)
        for slug in MERCHANTS:
            _policies(conn, slug, now)
            # The reference order: two jackets, bought 25 days ago, delivered 20 days ago.
            _order(
                conn,
                processor,
                merchant=slug,
                shopper="maya",
                number="1001",
                purchased_at=now - timedelta(days=25),
                delivered_at=now - timedelta(days=20),
                lines=[
                    ("blue", "JKT-TRAIL", "Blue / M", "Trail Jacket", 1, 18000),
                    ("green", "JKT-TRAIL", "Green / M", "Trail Jacket", 1, 12000),
                ],
            )
            # Another shopper's order at the same merchant, for ownership tests.
            _order(
                conn,
                processor,
                merchant=slug,
                shopper="omar",
                number="2001",
                purchased_at=now - timedelta(days=9),
                delivered_at=now - timedelta(days=5),
                lines=[("tote", "TOTE-CNV", "Natural", "Canvas Tote", 1, 6000)],
            )
        if fixture == "return_history":
            _return_history(conn, processor, now)
        index_policy_versions(conn, embedder)
    # Seeding is not application traffic: leave the processor's call log empty.
    processor.clear_request_log()


# --- builders -------------------------------------------------------------------------


def _people(conn: psycopg.Connection) -> None:
    for slug, merchant in MERCHANTS.items():
        conn.execute(
            "insert into merchants (id, slug, name, timezone) values (%s, %s, %s, %s)",
            (Ids.merchant(slug), slug, merchant["name"], merchant["timezone"]),
        )
    for key, (email, name) in USERS.items():
        conn.execute(
            "insert into app_users (id, email, display_name) values (%s, %s, %s)",
            (Ids.user(key), email, name),
        )
    for key, (slug, role) in STAFF.items():
        conn.execute(
            "insert into memberships (merchant_id, user_id, role) values (%s, %s, %s)",
            (Ids.merchant(slug), Ids.user(key), role),
        )
    for slug in MERCHANTS:
        for key in SHOPPERS:
            conn.execute(
                """
                insert into shoppers (id, merchant_id, user_id, display_name)
                values (%s, %s, %s, %s)
                """,
                (Ids.shopper(slug, key), Ids.merchant(slug), Ids.user(key), USERS[key][1]),
            )


def _policies(conn: psycopg.Connection, slug: str, now: datetime) -> None:
    merchant_id = Ids.merchant(slug)
    merchant = MERCHANTS[slug]
    start = now - timedelta(days=300)
    revised = now - timedelta(days=10)

    terms_id = Ids.policy(slug, "customer_terms")
    conn.execute(
        """
        insert into policies (id, merchant_id, policy_type, scope, name)
        values (%s, %s, 'customer_terms', 'returns', 'Return policy')
        """,
        (terms_id, merchant_id),
    )
    # The 30-day terms were in force at purchase. A 14-day version was published later.
    for key, days, effective_from, effective_to in (
        ("30-day", 30, start, revised),
        ("14-day", 14, revised, None),
    ):
        version_id = Ids.policy_version(slug, "customer_terms", key)
        conn.execute(
            """
            insert into policy_versions
              (id, merchant_id, policy_id, version_label, effective_from, effective_to,
               rules, published_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version_id,
                merchant_id,
                terms_id,
                f"{key} returns",
                effective_from,
                effective_to,
                Jsonb(
                    {
                        "return_window_days": days,
                        "immediate_refund_reasons": ["damaged"],
                        "window_section": "returns.window",
                        "reason_sections": {"damaged": "returns.damaged"},
                    }
                ),
                effective_from,
            ),
        )
        _section(
            conn,
            merchant_id,
            version_id,
            "returns.window",
            "Return window",
            "customer",
            f"You can return an item within {days} days of delivery. The delivery day is "
            f"day 0 and day {days} is included.",
        )
        _section(
            conn,
            merchant_id,
            version_id,
            "returns.damaged",
            "Damaged on arrival",
            "customer",
            "If an item arrives damaged, we refund what you paid for that item to your "
            "original payment method. You do not need to send it back.",
        )

    ops_id = Ids.policy(slug, "operational")
    conn.execute(
        """
        insert into policies (id, merchant_id, policy_type, scope, name)
        values (%s, %s, 'operational', 'returns', 'Approval rules')
        """,
        (ops_id, merchant_id),
    )
    ops_version = Ids.policy_version(slug, "operational", "v1")
    conn.execute(
        """
        insert into policy_versions
          (id, merchant_id, policy_id, version_label, effective_from, rules, published_at)
        values (%s, %s, %s, 'Approval rules v1', %s, %s, %s)
        """,
        (
            ops_version,
            merchant_id,
            ops_id,
            start,
            Jsonb(
                {
                    "auto_approve_limit_cents": merchant["auto_approve_limit_cents"],
                    "return_history_threshold": 3,
                    "return_history_window_days": 30,
                }
            ),
            start,
        ),
    )
    _section(
        conn,
        merchant_id,
        ops_version,
        "ops.approval_limit",
        "Approval limit",
        "supervisor",
        merchant["limit_text"],
    )
    _section(
        conn,
        merchant_id,
        ops_version,
        "ops.return_history",
        "Return history",
        "supervisor",
        "Three or more completed returns by the same shopper in the preceding 30 days "
        "send the next request to review.",
    )


def _section(
    conn: psycopg.Connection,
    merchant_id: UUID,
    version_id: UUID,
    key: str,
    title: str,
    audience: str,
    body: str,
) -> None:
    conn.execute(
        """
        insert into policy_sections
          (merchant_id, policy_version_id, section_key, title, audience, body)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (merchant_id, version_id, key, title, audience, body),
    )


def _order(
    conn: psycopg.Connection,
    processor: SandboxProcessor,
    *,
    merchant: str,
    shopper: str,
    number: str,
    purchased_at: datetime,
    delivered_at: datetime | None,
    lines: list[tuple[str, str, str, str, int, int]],
) -> None:
    merchant_id = Ids.merchant(merchant)
    order_id = Ids.order(merchant, number)
    payment_ref = Ids.payment_ref(merchant, number)
    conn.execute(
        """
        insert into orders
          (id, merchant_id, shopper_id, order_number, purchased_at, delivered_at,
           provider_payment_ref)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            order_id,
            merchant_id,
            Ids.shopper(merchant, shopper),
            Ids.order_number(merchant, number),
            purchased_at,
            delivered_at,
            payment_ref,
        ),
    )
    total = 0
    for key, sku, variant, description, quantity, unit_paid_cents in lines:
        conn.execute(
            """
            insert into order_lines
              (id, merchant_id, order_id, sku, variant, description, quantity, unit_paid_cents)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                Ids.line(merchant, number, key),
                merchant_id,
                order_id,
                sku,
                variant,
                description,
                quantity,
                unit_paid_cents,
            ),
        )
        total += quantity * unit_paid_cents
    # The processor holds its own record of the original payment.
    processor.create_payment(payment_ref, merchant, total)


def _return_history(conn: psycopg.Connection, processor: SandboxProcessor, now: datetime) -> None:
    """Three completed $25 returns for Maya at Northline in the last 30 days, each consistent
    on both sides (app ledger and processor), plus a fresh $40 order to ask about."""
    merchant = "northline"
    merchant_id = Ids.merchant(merchant)
    shopper_id = Ids.shopper(merchant, "maya")
    terms_version = Ids.policy_version(merchant, "customer_terms", "30-day")
    ops_version = Ids.policy_version(merchant, "operational", "v1")

    for index, days_ago in enumerate((12, 8, 3), start=1):
        number = f"110{index}"
        resolved_at = now - timedelta(days=days_ago)
        requested_at = resolved_at - timedelta(minutes=5)
        _order(
            conn,
            processor,
            merchant=merchant,
            shopper="maya",
            number=number,
            purchased_at=now - timedelta(days=28),
            delivered_at=now - timedelta(days=24),
            lines=[("socks", "SOCK-WOOL", "Charcoal / M", "Wool Socks", 1, 2500)],
        )
        order_id = Ids.order(merchant, number)
        line_id = Ids.line(merchant, number, "socks")
        case_id = _id(f"case/{merchant}/history/{index}")
        proposal_id = _id(f"proposal/{merchant}/history/{index}")
        action_id = _id(f"action/{merchant}/history/{index}")
        key = f"rd-refund-{action_id}"
        receipt = processor.create_refund(
            payment_ref=Ids.payment_ref(merchant, number),
            amount_cents=2500,
            idempotency_key=key,
            metadata={"action_id": str(action_id), "case_id": str(case_id)},
        )
        conn.execute(
            "update order_lines set refunded_quantity = 1, refunded_cents = 2500 where id = %s",
            (line_id,),
        )
        conn.execute(
            """
            insert into cases
              (id, merchant_id, shopper_id, client_request_id, order_id, order_line_id,
               quantity, requested_action, reason_code, state, outcome_type,
               request_received_at, resolved_at, created_at, updated_at)
            values (%s, %s, %s, %s, %s, %s, 1, 'refund', 'damaged', 'resolved', 'refund',
                    %s, %s, %s, %s)
            """,
            (
                case_id,
                merchant_id,
                shopper_id,
                f"seed-history-{index}",
                order_id,
                line_id,
                requested_at,
                resolved_at,
                requested_at,
                resolved_at,
            ),
        )
        conn.execute(
            """
            insert into proposals
              (id, merchant_id, case_id, version, action_id, action_kind, order_line_id,
               quantity, amount_cents, decision, customer_policy_version_id, cited_sections,
               operational_policy_version_id, status, consented_at, created_at)
            values (%s, %s, %s, 1, %s, 'refund', %s, 1, 2500, 'auto', %s, %s, %s,
                    'submitted', %s, %s)
            """,
            (
                proposal_id,
                merchant_id,
                case_id,
                action_id,
                line_id,
                terms_version,
                ["returns.window", "returns.damaged"],
                ops_version,
                requested_at,
                requested_at,
            ),
        )
        conn.execute(
            """
            insert into resolution_actions
              (id, merchant_id, case_id, proposal_id, kind, order_id, order_line_id, quantity,
               amount_cents, provider_payment_ref, idempotency_key, provider_refund_id,
               status, attempt_count, operational_policy_version_id, created_at, updated_at,
               completed_at)
            values (%s, %s, %s, %s, 'refund', %s, %s, 1, 2500, %s, %s, %s, 'succeeded', 1,
                    %s, %s, %s, %s)
            """,
            (
                action_id,
                merchant_id,
                case_id,
                proposal_id,
                order_id,
                line_id,
                Ids.payment_ref(merchant, number),
                key,
                receipt.id,
                ops_version,
                requested_at,
                resolved_at,
                resolved_at,
            ),
        )
        conn.execute(
            """
            insert into outbox (merchant_id, action_id, available_at, attempts, done_at, created_at)
            values (%s, %s, %s, 1, %s, %s)
            """,
            (merchant_id, action_id, requested_at, resolved_at, requested_at),
        )
        conn.execute(
            """
            insert into ledger_entries
              (merchant_id, action_id, order_id, order_line_id, kind, quantity, amount_cents,
               provider_refund_id, recorded_at)
            values (%s, %s, %s, %s, 'refund', 1, 2500, %s, %s)
            """,
            (merchant_id, action_id, order_id, line_id, receipt.id, resolved_at),
        )

    # Otherwise eligible: $40, bought under the 30-day terms, delivered 6 days ago.
    _order(
        conn,
        processor,
        merchant=merchant,
        shopper="maya",
        number="1200",
        purchased_at=now - timedelta(days=12),
        delivered_at=now - timedelta(days=6),
        lines=[("beanie", "BNE-MERINO", "Oat", "Merino Beanie", 1, 4000)],
    )
