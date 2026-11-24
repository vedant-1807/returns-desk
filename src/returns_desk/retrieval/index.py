from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from returns_desk.retrieval.embedders import Embedder, to_pgvector

MAX_CHUNK_CHARS = 1200


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a section into chunks on paragraph boundaries. Policy sections are short, so
    most become a single chunk."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [text.strip()]:
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def index_policy_versions(
    conn: psycopg.Connection[Any], embedder: Embedder, version_ids: list[UUID] | None = None
) -> int:
    """Embed the sections of the given policy versions (or of every version) that have no
    chunks yet for this embedder. Safe to run again: existing chunks are left alone.

    Runs as an admin job. The caller supplies a connection that may write policy_chunks.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        sections = cur.execute(
            """
            select s.id, s.merchant_id, s.policy_version_id, s.section_key, s.title,
                   s.audience::text as audience, s.body
            from policy_sections s
            where (%(versions)s::uuid[] is null
                   or s.policy_version_id = any(%(versions)s::uuid[]))
              and not exists (
                select 1 from policy_chunks c
                where c.section_id = s.id and c.embedding_model = %(model)s
              )
            order by s.policy_version_id, s.section_key
            """,
            {"versions": version_ids, "model": embedder.model_id},
        ).fetchall()

    pending: list[tuple[dict[str, Any], int, str]] = []
    for row in sections:
        for index, chunk in enumerate(chunk_text(row["body"])):
            pending.append((row, index, chunk))
    if not pending:
        return 0

    # The title is embedded with the text so a search for "damaged" finds the section
    # called "Damaged on arrival" even when the body words differ.
    vectors = embedder.embed_documents([f"{row['title']}\n{chunk}" for row, _, chunk in pending])
    for (row, index, chunk), vector in zip(pending, vectors, strict=True):
        conn.execute(
            """
            insert into policy_chunks
              (merchant_id, policy_version_id, section_id, section_key, title, audience,
               chunk_index, content, embedding, embedding_model)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
            on conflict (section_id, chunk_index, embedding_model) do nothing
            """,
            (
                row["merchant_id"],
                row["policy_version_id"],
                row["id"],
                row["section_key"],
                row["title"],
                row["audience"],
                index,
                chunk,
                to_pgvector(vector),
                embedder.model_id,
            ),
        )
    return len(pending)
