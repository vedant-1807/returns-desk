"""Permission-filtered policy search.

The search runs as the API's row-level-security role with the caller's claims, so tenant
and audience filtering happen inside Postgres. A passage the caller may not read is never
returned by the query, which means it can never reach a model, a reply or a trace.

Two things here are easy to get wrong:

1. An HNSW index returns its nearest vectors first and row-level security filters them
   afterwards. If the nearest ones are all forbidden, a plain index scan comes back with
   fewer than k rows, or none. pgvector's iterative scan keeps reading the index until the
   limit is met, so the search turns it on for every query. That still is not a guarantee:
   when forbidden rows form a tight cluster around the query, the index graph may not
   lead out of it. So a short result is always followed by an exact scan of the permitted
   rows. Filtering can therefore never silently drop a valid result.

2. Caching search results is only safe if a cache hit is checked against current
   permissions. The cache therefore stores chunk IDs, never text, and a hit is re-read from
   the table under the caller's current claims before anything is served.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from returns_desk.clock import Clock
from returns_desk.db import Conn, Principal
from returns_desk.retrieval.embedders import Embedder, to_pgvector


@dataclass(frozen=True)
class Passage:
    chunk_id: UUID
    version_id: UUID
    version_label: str
    section_key: str
    title: str
    audience: str
    text: str
    effective_from: datetime
    effective_to: datetime | None
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "passage_id": str(self.chunk_id),
            "version_id": str(self.version_id),
            "version_label": self.version_label,
            "section_key": self.section_key,
            "title": self.title,
            "audience": self.audience,
            "text": self.text,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "score": round(self.score, 4),
        }


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower()).strip(" ?!.")


class _TtlCache:
    """A small thread-safe LRU with expiry."""

    def __init__(self, max_entries: int, ttl_seconds: float, clock: Clock) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any | None:
        now = self._clock.now().timestamp()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            stored_at, value = item
            if now - stored_at > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock.now().timestamp(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def drop(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)


class PolicySearch:
    def __init__(
        self,
        embedder: Embedder,
        clock: Clock,
        *,
        cache_entries: int = 512,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._embedder = embedder
        self._results = _TtlCache(cache_entries, cache_ttl_seconds, clock)
        self._vectors = _TtlCache(cache_entries, 3600.0, clock)
        # Counters the tests and the operations view read.
        self.stats = {"hits": 0, "misses": 0, "revalidation_failures": 0, "exact_fallbacks": 0}

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    def search(
        self,
        conn: Conn,
        principal: Principal,
        *,
        question: str,
        version_ids: list[UUID],
        k: int = 5,
    ) -> list[Passage]:
        """Top-k passages from the given policy versions that this caller may read."""
        normalized = normalize_question(question)
        if not version_ids or not normalized:
            return []

        # Scoped to merchant, audience, permission version, policy versions and the
        # normalized question. A role or policy change produces a different key.
        key = (
            principal.merchant_id,
            principal.role,
            principal.permission_version,
            tuple(sorted(str(v) for v in version_ids)),
            self._embedder.model_id,
            normalized,
            k,
        )
        cached = self._results.get(key)
        if cached is not None:
            passages = self._revalidate(conn, cached)
            if passages is not None:
                self.stats["hits"] += 1
                return passages
            # Something the entry pointed at is gone or no longer readable. Recompute.
            self.stats["revalidation_failures"] += 1
            self._results.drop(key)

        self.stats["misses"] += 1
        passages = self._query(conn, normalized, version_ids, k)
        self._results.put(key, [(p.chunk_id, p.score) for p in passages])
        return passages

    def _query_vector(self, normalized: str) -> list[float]:
        key = (self._embedder.model_id, normalized)
        vector = self._vectors.get(key)
        if vector is None:
            vector = self._embedder.embed_query(normalized)
            self._vectors.put(key, vector)
        return vector

    def _query(self, conn: Conn, normalized: str, version_ids: list[UUID], k: int) -> list[Passage]:
        vector = to_pgvector(self._query_vector(normalized))
        params = {"q": vector, "model": self._embedder.model_id, "versions": version_ids, "k": k}
        # Keep scanning the index until k permitted rows are found (pgvector 0.8+). The
        # setting lasts for this transaction only.
        conn.execute("set local hnsw.iterative_scan = relaxed_order")
        rows = conn.execute(
            _SEARCH_SQL.format(order="c.embedding <=> %(q)s::vector"), params
        ).fetchall()
        if len(rows) < k:
            # The index came back short. That happens when the graph cannot reach the
            # permitted rows from the forbidden ones around the query, which iterative
            # scanning does not fix. Scan the permitted rows exactly instead: the result is
            # then complete by construction, and the set is small because it is already
            # limited to the applicable policy versions. ("+ 0" keeps the planner off the
            # vector index.)
            self.stats["exact_fallbacks"] += 1
            rows = conn.execute(
                _SEARCH_SQL.format(order="(c.embedding <=> %(q)s::vector) + 0"), params
            ).fetchall()
        return [_passage(row, 1.0 - float(row["distance"])) for row in rows]

    def _revalidate(self, conn: Conn, cached: list[tuple[UUID, float]]) -> list[Passage] | None:
        """Re-read cached chunks under the caller's current claims. Returns None unless
        every one of them is still readable."""
        if not cached:
            return []
        ids = [chunk_id for chunk_id, _ in cached]
        rows = conn.execute(
            """
            select c.id, c.policy_version_id, c.section_key, c.title,
                   c.audience::text as audience, c.content,
                   pv.version_label, pv.effective_from, pv.effective_to
            from policy_chunks c
            join policy_versions pv on pv.id = c.policy_version_id
            where c.id = any(%s)
            """,
            (ids,),
        ).fetchall()
        if len(rows) != len(ids):
            return None
        by_id = {row["id"]: row for row in rows}
        return [_passage(by_id[chunk_id], score) for chunk_id, score in cached]


_SEARCH_SQL = """
    with nearest as materialized (
      select c.id, c.policy_version_id, c.section_key, c.title,
             c.audience::text as audience, c.content,
             c.embedding <=> %(q)s::vector as distance
      from policy_chunks c
      where c.embedding_model = %(model)s
        and c.policy_version_id = any(%(versions)s)
      order by {order}
      limit %(k)s
    )
    select n.*, pv.version_label, pv.effective_from, pv.effective_to
    from nearest n
    join policy_versions pv on pv.id = n.policy_version_id
    order by n.distance, n.id
"""


def _passage(row: dict[str, Any], score: float) -> Passage:
    return Passage(
        chunk_id=row["id"],
        version_id=row["policy_version_id"],
        version_label=row["version_label"],
        section_key=row["section_key"],
        title=row["title"],
        audience=row["audience"],
        text=row["content"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        score=score,
    )


def applicable_version_ids(
    conn: Conn, *, purchased_at: datetime | None, now: datetime
) -> list[UUID]:
    """Which policy versions a search should cover.

    Customer terms: the version in force when the order was bought, or today's version when
    no order is known yet. Operational rules: the version active now. Row-level security
    drops the operational version for anyone who is not a supervisor, so this list is
    already limited to what the caller may read.
    """
    rows = conn.execute(
        """
        select pv.id
        from policy_versions pv
        join policies p on p.id = pv.policy_id
        where p.scope = 'returns'
          and pv.effective_from <= case when p.policy_type = 'customer_terms'
                                        then %(terms_at)s else %(now)s end
          and (pv.effective_to is null
               or pv.effective_to > case when p.policy_type = 'customer_terms'
                                         then %(terms_at)s else %(now)s end)
        order by pv.id
        """,
        {"terms_at": purchased_at or now, "now": now},
    ).fetchall()
    return [row["id"] for row in rows]
