from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from returns_desk.db import Conn, Principal
from returns_desk.domain.rules import (
    CustomerTerms,
    CustomerTermsRules,
    OperationalRules,
    OperationalRulesBody,
)
from returns_desk.errors import Conflict, Forbidden, InvalidRequest, NotFound


def customer_terms_at(conn: Conn, purchased_at: datetime) -> CustomerTerms | None:
    """The customer terms in force when the order was bought.

    Returns None unless exactly one version applies. The caller then sends the case to staff
    rather than guessing between versions.
    """
    rows = conn.execute(
        """
        select pv.id, pv.version_label, pv.rules
        from policy_versions pv
        join policies p on p.id = pv.policy_id
        where p.merchant_id = app_merchant()
          and p.policy_type = 'customer_terms'
          and p.scope = 'returns'
          and pv.effective_from <= %(at)s
          and (pv.effective_to is null or %(at)s < pv.effective_to)
        """,
        {"at": purchased_at},
    ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        rules = CustomerTermsRules.model_validate(row["rules"])
    except ValidationError:
        return None
    return CustomerTerms(version_id=row["id"], version_label=row["version_label"], rules=rules)


def operational_rules_at(conn: Conn, at: datetime) -> OperationalRules | None:
    """The internal rules active at `at`, for the caller's merchant.

    Goes through app_operational_rules() because a shopper's request must be decided under
    rules the shopper is not allowed to read.
    """
    rows = conn.execute("select * from app_operational_rules(%s)", (at,)).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        rules = OperationalRulesBody.model_validate(row["rules"])
    except ValidationError:
        return None
    return OperationalRules(
        version_id=row["version_id"], version_label=row["version_label"], rules=rules
    )


def sections_for(conn: Conn, version_id: UUID, keys: list[str]) -> list[dict[str, Any]]:
    """Cited sections of one version, limited by row-level security to what the caller's
    audience may read."""
    if not keys:
        return []
    rows = conn.execute(
        """
        select ps.section_key, ps.title, ps.body, ps.audience::text as audience,
               pv.version_label, pv.id as version_id
        from policy_sections ps
        join policy_versions pv on pv.id = ps.policy_version_id
        where ps.policy_version_id = %s and ps.section_key = any(%s)
        """,
        (version_id, keys),
    ).fetchall()
    order = {key: i for i, key in enumerate(keys)}
    return sorted(rows, key=lambda r: order[r["section_key"]])


def list_policies(conn: Conn) -> list[dict[str, Any]]:
    """Policies and versions the caller may read (row-level security decides)."""
    policies = conn.execute(
        """
        select id, policy_type::text as policy_type, scope, name
        from policies order by policy_type, name
        """
    ).fetchall()
    for policy in policies:
        versions = conn.execute(
            """
            select id, version_label, effective_from, effective_to, rules, published_at
            from policy_versions where policy_id = %s order by effective_from
            """,
            (policy["id"],),
        ).fetchall()
        for version in versions:
            version["sections"] = conn.execute(
                """
                select section_key, title, body, audience::text as audience
                from policy_sections where policy_version_id = %s order by section_key
                """,
                (version["id"],),
            ).fetchall()
        policy["versions"] = versions
    return policies


def publish_version(
    conn: Conn,
    principal: Principal,
    *,
    policy_id: UUID,
    version_label: str,
    effective_from: datetime,
    rules: dict[str, Any],
    sections: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Publish a revision that takes effect now or later.

    The open version is closed at the new start time and the new one is added, in one
    transaction. Older purchases keep pointing at the version that was in force when they
    were made, because versions are chosen by purchase timestamp and never edited.
    """
    if principal.role != "supervisor":
        raise Forbidden("Only a supervisor can publish a policy version")
    if effective_from < now:
        raise InvalidRequest("A policy version cannot take effect in the past")

    policy = conn.execute(
        "select id, policy_type::text as policy_type from policies where id = %s",
        (policy_id,),
    ).fetchone()
    if policy is None:
        raise NotFound()

    model = (
        CustomerTermsRules if policy["policy_type"] == "customer_terms" else OperationalRulesBody
    )
    try:
        validated = model.model_validate(rules)
    except ValidationError as exc:
        raise InvalidRequest(
            "The rules do not match this policy type", details={"errors": exc.errors()}
        ) from exc

    current = conn.execute(
        """
        select id, effective_from from policy_versions
        where policy_id = %s and effective_to is null
        for update
        """,
        (policy_id,),
    ).fetchone()
    if current is not None:
        if effective_from <= current["effective_from"]:
            raise Conflict("The new version must start after the current version starts")
        conn.execute(
            "update policy_versions set effective_to = %s where id = %s",
            (effective_from, current["id"]),
        )

    try:
        with conn.transaction():
            version = conn.execute(
                """
                insert into policy_versions
                  (merchant_id, policy_id, version_label, effective_from, rules,
                   published_by, published_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                returning id, version_label, effective_from, effective_to
                """,
                (
                    principal.merchant_id,
                    policy_id,
                    version_label,
                    effective_from,
                    Jsonb(validated.model_dump()),
                    principal.user_id,
                    now,
                ),
            ).fetchone()
    except (psycopg.errors.ExclusionViolation, psycopg.errors.UniqueViolation) as exc:
        raise Conflict("This version overlaps or duplicates an existing version") from exc

    for section in sections:
        conn.execute(
            """
            insert into policy_sections
              (merchant_id, policy_version_id, section_key, title, audience, body)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (
                principal.merchant_id,
                version["id"],
                section["section_key"],
                section["title"],
                section["audience"],
                section["body"],
            ),
        )
    return version
