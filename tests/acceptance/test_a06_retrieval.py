"""A06: policy search never returns text the caller may not read, and still returns k
results when the best matches are the forbidden ones.

Embeddings here come from the deterministic hash embedder, so the test can place a
forbidden passage exactly on top of the query and know it is the nearest vector.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from returns_desk.retrieval.embedders import to_pgvector
from returns_desk.retrieval.search import applicable_version_ids, normalize_question
from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

QUESTION = "How long do I have to return things?"
MARKER = "FORBIDDEN-INTERNAL-NOTE"


def plant_forbidden_chunks(world: World, merchant: str, count: int, version_key: str) -> None:
    """Put `count` supervisor-only chunks into a customer terms version, each sitting
    almost exactly on the query vector. They are the nearest neighbours of QUESTION by far."""
    embedder = world.embedder
    base = embedder.embed_query(normalize_question(QUESTION))
    merchant_id = Ids.merchant(merchant)
    version_id = Ids.policy_version(merchant, "customer_terms", version_key)
    with psycopg.connect(world.settings.admin_dsn) as conn:
        section = conn.execute(
            """
            insert into policy_sections
              (merchant_id, policy_version_id, section_key, title, audience, body)
            values (%s, %s, 'internal.escalation', 'Internal escalation note', 'supervisor', %s)
            returning id
            """,
            (merchant_id, version_id, f"{MARKER}: never show this to a shopper."),
        ).fetchone()[0]
        for index in range(count):
            # Nudge one coordinate so every chunk is its own point in the index. (pgvector
            # folds exact duplicates into one index entry, which would hide the problem.)
            nudged = list(base)
            nudged[index % len(nudged)] += 0.01
            norm = sum(x * x for x in nudged) ** 0.5
            vector = to_pgvector([x / norm for x in nudged])
            conn.execute(
                """
                insert into policy_chunks
                  (merchant_id, policy_version_id, section_id, section_key, title, audience,
                   chunk_index, content, embedding, embedding_model)
                values (%s, %s, %s, 'internal.escalation', 'Internal escalation note',
                        'supervisor', %s, %s, %s::vector, %s)
                """,
                (
                    merchant_id,
                    version_id,
                    section,
                    index,
                    f"{MARKER} #{index}: never show this to a shopper.",
                    vector,
                    embedder.model_id,
                ),
            )


def search_as(
    world: World, user: str, merchant: str, *, k: int, force_index: bool = False
) -> tuple[list[Any], str]:
    """Run the real search service as this user. Optionally force the HNSW index so the
    test exercises the index path, and return the query plan to prove it."""
    principal = world.principal(user, merchant)
    search = world.container.policy_search
    embedder = world.embedder
    with world.container.db.tenant_tx(principal) as conn:
        versions = applicable_version_ids(conn, purchased_at=None, now=world.clock.now())
        plan = ""
        if force_index:
            for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
                conn.execute(f"set local {setting} = off")
            conn.execute("set local hnsw.iterative_scan = relaxed_order")
            plan = "\n".join(
                row["QUERY PLAN"]
                for row in conn.execute(
                    """
                    explain select id from policy_chunks
                    where embedding_model = %s and policy_version_id = any(%s)
                    order by embedding <=> %s::vector limit %s
                    """,
                    (
                        embedder.model_id,
                        versions,
                        to_pgvector(embedder.embed_query(normalize_question(QUESTION))),
                        k,
                    ),
                ).fetchall()
            )
        passages = search.search(conn, principal, question=QUESTION, version_ids=versions, k=k)
    return passages, plan


def test_a06_forbidden_best_match_is_filtered_and_k_results_still_come_back(world: World):
    # 120 forbidden chunks, all nearer to the query than any permitted one. That is three
    # times what one HNSW pass looks at (ef_search is 40).
    plant_forbidden_chunks(world, "vale", 120, "14-day")

    # First, the failure this design avoids. With a plain index scan the nearest 40 are
    # all forbidden, row-level security removes them, and the shopper gets nothing.
    principal = world.principal("maya", "vale")
    vector = to_pgvector(world.embedder.embed_query(normalize_question(QUESTION)))
    with world.container.db.tenant_tx(principal) as conn:
        versions = applicable_version_ids(conn, purchased_at=None, now=world.clock.now())
        for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            conn.execute(f"set local {setting} = off")
        conn.execute("set local hnsw.iterative_scan = off")
        plain = conn.execute(
            """
            select id from policy_chunks
            where embedding_model = %s and policy_version_id = any(%s)
            order by embedding <=> %s::vector limit 2
            """,
            (world.embedder.model_id, versions, vector),
        ).fetchall()
    assert len(plain) < 2, "expected the plain HNSW scan to come back short"

    # The real search, forced down the same index path: full count, nothing forbidden.
    # In this tightly clustered case even the iterative scan cannot leave the forbidden
    # cluster, so the count is made whole by the exact fallback.
    search = world.container.policy_search
    fallbacks_before = search.stats["exact_fallbacks"]
    passages, plan = search_as(world, "maya", "vale", k=2, force_index=True)
    assert "policy_chunks_embedding" in plan, plan
    assert search.stats["exact_fallbacks"] == fallbacks_before + 1
    assert len(passages) == 2
    assert all(p.audience == "customer" for p in passages)
    assert all(MARKER not in p.text for p in passages)
    assert {p.version_label for p in passages} == {"14-day returns"}

    # And on the ordinary path too (the planner's own choice).
    passages, _ = search_as(world, "maya", "vale", k=2)
    assert len(passages) == 2 and all(MARKER not in p.text for p in passages)

    # The supervisor, who may read them, does get the forbidden-to-others passages first.
    passages, _ = search_as(world, "vera", "vale", k=3)
    assert all(MARKER in p.text for p in passages)

    # Another merchant never sees Vale's rows at all, whatever sits nearest.
    # (Northline has four applicable passages for a supervisor: two customer, two internal.)
    passages, _ = search_as(world, "sam", "northline", k=4, force_index=True)
    assert len(passages) == 4
    assert all(MARKER not in p.text for p in passages)


def test_a06_forbidden_text_reaches_no_tool_result_model_input_reply_or_trace(world: World):
    plant_forbidden_chunks(world, "vale", 60, "14-day")

    # A real conversation whose best semantic match is the forbidden note.
    case = world.say("maya", "vale", QUESTION)
    assert case["status"] == "answered"

    # The reply.
    assert MARKER not in str(case)
    # What the model was shown.
    assert world.model.requests, "the model should have been asked"
    for request in world.model.requests:
        assert MARKER not in request.system + request.user + str(request.inputs)
    answer_inputs = [r.inputs for r in world.model.requests if r.purpose == "answer"][0]
    assert len(answer_inputs["passages"]) >= 2
    # The tool result, over the real MCP endpoint, for the shopper.
    result = world.mcp("maya", "vale", "search_policy", case_id=case["id"], question=QUESTION, k=5)
    assert result.is_error is False
    passages = result.structured_content["passages"]
    assert len(passages) >= 2
    assert MARKER not in str(result.structured_content)
    assert {p["audience"] for p in passages} == {"customer"}
    # The traces a shopper or an associate can reach: messages and audit events.
    stored = world.rows("select body from case_messages") + world.rows(
        "select detail from case_events"
    )
    assert MARKER not in str(stored)
    # The associate's view of the same case.
    associate = world.call("GET", f"/cases/{case['id']}", user="alex", merchant="vale").json()
    assert MARKER not in str(associate)


def test_a06_an_associate_asking_for_a_supervisor_rule_gets_none_of_it(world: World):
    case = world.say("maya", "northline", "hello")
    question = "What is the approval limit for refunds before a supervisor is needed?"

    def passages_for(user: str) -> list[dict[str, Any]]:
        result = world.mcp(
            user, "northline", "search_policy", case_id=case["id"], question=question, k=6
        )
        assert result.is_error is False
        return result.structured_content["passages"]

    supervisor = passages_for("sam")
    assert any(p["section_key"] == "ops.approval_limit" for p in supervisor)
    assert any("$200.00" in p["text"] for p in supervisor)

    for user in ("nina", "maya"):
        seen = passages_for(user)
        assert seen, user
        assert {p["audience"] for p in seen} == {"customer"}, user
        assert not any(p["section_key"].startswith("ops.") for p in seen), user
        assert "$200.00" not in str(seen), user


def test_a06_cache_hits_and_role_revocation(world: World):
    case = world.say("maya", "northline", "hello")
    question = "What is the approval limit for refunds before a supervisor is needed?"
    search = world.container.policy_search

    def ask(user: str) -> list[dict[str, Any]]:
        result = world.mcp(
            user, "northline", "search_policy", case_id=case["id"], question=question, k=6
        )
        return result.structured_content["passages"]

    # Miss, then hit, with identical results.
    hits_before = search.stats["hits"]
    first = ask("sam")
    second = ask("sam")
    assert search.stats["hits"] == hits_before + 1
    assert first == second
    assert any(p["section_key"] == "ops.approval_limit" for p in second)

    # Different wording, same normalized question: still a hit.
    world.mcp(
        "sam",
        "northline",
        "search_policy",
        case_id=case["id"],
        question="  what is the APPROVAL limit for refunds before a supervisor is needed  ",
        k=6,
    )
    assert search.stats["hits"] == hits_before + 2

    # The associate asking the same thing never shares the supervisor's cache entry.
    assert not any(p["section_key"].startswith("ops.") for p in ask("nina"))

    # Sam is downgraded to associate. The very next search returns customer text only.
    version_before = world.rows(
        "select permission_version from merchants where id = %s", (Ids.merchant("northline"),)
    )[0]["permission_version"]
    world.rows(
        "update memberships set role = 'associate' where user_id = %s returning user_id",
        (Ids.user("sam"),),
    )
    version_after = world.rows(
        "select permission_version from merchants where id = %s", (Ids.merchant("northline"),)
    )[0]["permission_version"]
    assert version_after == version_before + 1
    after = ask("sam")
    assert after and {p["audience"] for p in after} == {"customer"}
    assert "$200.00" not in str(after)
    assert world.call("GET", "/reviews", user="sam", merchant="northline").status_code == 403


def test_a06_a_stale_cache_entry_is_rechecked_against_current_permissions(world: World):
    """Even if a cache key somehow matched after a role change, a hit is re-read under the
    caller's current claims, so the forbidden rows cannot be served from memory."""
    case = world.say("maya", "northline", "hello")
    question = "What is the approval limit for refunds before a supervisor is needed?"
    search = world.container.policy_search
    supervisor = world.principal("sam", "northline")
    associate = world.principal("nina", "northline")

    with world.container.db.tenant_tx(supervisor) as conn:
        versions = applicable_version_ids(conn, purchased_at=None, now=world.clock.now())
        cached = search.search(conn, supervisor, question=question, version_ids=versions, k=6)
    assert any(p.audience == "supervisor" for p in cached)

    # Same cache key (the supervisor's principal object), but the transaction carries an
    # associate's claims: exactly what a stale key after a downgrade would look like.
    failures_before = search.stats["revalidation_failures"]
    with world.container.db.tenant_tx(associate) as conn:
        served = search.search(conn, supervisor, question=question, version_ids=versions, k=6)
    assert search.stats["revalidation_failures"] == failures_before + 1
    assert served and all(p.audience == "customer" for p in served)
    assert case["id"]


def test_a06_search_uses_the_policy_version_from_the_purchase_date(world: World):
    # A case about the jacket order (bought under 30-day terms) searches that version.
    case = world.say("maya", "northline", "hello", order="1001")
    result = world.mcp("maya", "northline", "search_policy", case_id=case["id"], question=QUESTION)
    labels = {p["version_label"] for p in result.structured_content["passages"]}
    assert labels == {"30-day returns"}

    # A case with no order yet searches today's terms.
    general = world.say("maya", "northline", "hello again")
    result = world.mcp(
        "maya", "northline", "search_policy", case_id=general["id"], question=QUESTION
    )
    labels = {p["version_label"] for p in result.structured_content["passages"]}
    assert labels == {"14-day returns"}
