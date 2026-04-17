"""
Retrieval primitives: vector (cosine via pgvector) and BM25 (Postgres ts_rank).

These are the two retrievers that get fused in api/hybrid.py. Each returns
a ranked list of `Hit` records pulled from the `chunks_with_source` view,
oldest-rank-first (rank 0 = best). Both share the same row shape so the
fusion layer doesn't need to special-case either one.

The embedding model is cached as a module-level singleton so repeated calls
in the same process reuse the loaded weights. Cold-load is ~3-5 s on CPU.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Sequence

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

DEFAULT_EMBEDDING_MODEL = "pritamdeka/S-PubMedBert-MS-MARCO"
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Tokens that are "long" by length but too generic to count as rare clinical
# entities — they appear in nearly every clinical/research chunk and would
# drown the lexical retriever in noise.
_GENERIC_LONG_TOKENS = frozenset({
    "patient", "patients", "clinical", "disorder", "disorders", "depression",
    "depressive", "anxiety", "criteria", "diagnosis", "treatment", "symptoms",
    "research", "adolescents", "adolescent", "generalized", "augmentation",
    "disease", "therapy", "results", "study", "studies", "moderate", "severe",
    "history", "currently", "recommend", "recommended", "negative", "positive",
    "psychiatric", "psychological", "medication", "medications",
})

_embedding_model: SentenceTransformer | None = None


@dataclass(frozen=True)
class Hit:
    """One retrieval hit — fields cover both retriever paths and rerank later."""
    chunk_id: int
    document_id: int
    source_type: str
    source_uri: str | None
    section: str | None
    title: str | None
    chunk_text: str
    score: float          # cosine similarity (vector) or ts_rank (bm25)


def get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        name = os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
        _embedding_model = SentenceTransformer(name)
    return _embedding_model


def retrieve_vector(
    conn: psycopg.Connection,
    query: str,
    k: int = 50,
    source_types: Sequence[str] | None = None,
) -> list[Hit]:
    """Top-k by cosine similarity against the chunk embeddings.

    Uses the `<=>` cosine-distance operator backed by the HNSW index.
    Score returned is `1 - distance` so higher = better, matching the
    intuitive direction expected by the fusion layer.
    """
    register_vector(conn)
    embedding = get_embedding_model().encode(query, normalize_embeddings=True)
    sql, params = _build_vector_sql(embedding, k, source_types)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_row_to_hit(row) for row in cur.fetchall()]


def retrieve_bm25(
    conn: psycopg.Connection,
    query: str,
    k: int = 50,
    source_types: Sequence[str] | None = None,
) -> list[Hit]:
    """Top-k by Postgres `ts_rank` over the auto-populated `tsv` column.

    Tokens are extracted with a strict alphanumeric regex and joined with
    OR semantics — `plainto_tsquery`'s implicit AND is too brittle for
    natural-language clinical queries (e.g. "sertraline 50mg for MDD"
    requires every literal token in one chunk, which usually fails).
    OR keeps any token-overlap candidates flowing into RRF, which then
    ranks them. The regex also keeps user input safely outside the
    `to_tsquery` parser, which is strict about punctuation.
    """
    ts_query = _to_or_tsquery(query)
    if not ts_query:
        return []
    sql, params = _build_bm25_sql(ts_query, k, source_types)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_row_to_hit(row) for row in cur.fetchall()]


def _to_or_tsquery(query: str) -> str:
    tokens = {t.lower() for t in _TOKEN_RE.findall(query) if len(t) > 1}
    return " | ".join(sorted(tokens))


def retrieve_lexical(
    conn: psycopg.Connection,
    query: str,
    k: int = 50,
    source_types: Sequence[str] | None = None,
) -> list[Hit]:
    """Top-k by literal-substring matching on rare query tokens.

    Third RRF input alongside vector + BM25. Targets the failure mode where
    a chunk literally contains a rare clinical entity (drug name, ICD code,
    acronym) but the surrounding context buries it for both dense and
    `ts_rank` retrievers.

    Score = sum of matched-token lengths — gives longer/more-specific
    tokens proportionally more weight than short noisy ones like "50mg".
    Returns [] when the query has no tokens passing the rarity heuristic
    (the other two retrievers handle that case fine).
    """
    rare = rare_query_tokens(query)
    if not rare:
        return []
    patterns = [f"%{t}%" for t in rare]
    score_expr = " + ".join(
        f"(CASE WHEN chunk_text ILIKE %s THEN {len(t)} ELSE 0 END)" for t in rare
    )
    where_any = " OR ".join("chunk_text ILIKE %s" for _ in rare)
    src_clause, src_params = "", ()
    if source_types:
        src_clause = " AND source_type = ANY(%s)"
        src_params = (list(source_types),)
    sql = (
        "SELECT chunk_id, document_id, source_type, source_uri, section, "
        "       title, chunk_text, "
        f"       ({score_expr})::float AS score "
        "FROM chunks_with_source "
        f"WHERE ({where_any}){src_clause} "
        "ORDER BY score DESC, chunk_id ASC "
        "LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (*patterns, *patterns, *src_params, k))
        return [_row_to_hit(row) for row in cur.fetchall()]


def rare_query_tokens(query: str) -> list[str]:
    """Extract tokens worth literal-matching: long alphabetic, acronyms, codes.

    Three rules combined:
      - alphabetic and len > 7, not in the generic-medical stoplist
        (catches drug names like sertraline, paroxetine, fluoxetine)
      - all-uppercase and len >= 3 (catches acronyms: OCD, SSRI, MDD, TRD)
      - mixed letter+digit and len >= 3 (catches ICD codes like F41 / 6A20)
    """
    rare: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(query):
        low = raw.lower()
        if low in seen:
            continue
        has_digit = any(c.isdigit() for c in raw)
        has_alpha = any(c.isalpha() for c in raw)
        is_upper = raw.isupper() and len(raw) >= 3 and not has_digit
        is_long = len(raw) > 7 and not has_digit and low not in _GENERIC_LONG_TOKENS
        is_codeish = has_digit and has_alpha and len(raw) >= 3
        if is_long or is_upper or is_codeish:
            rare.append(low)
            seen.add(low)
    return rare


def _build_vector_sql(
    embedding, k: int, source_types: Sequence[str] | None
) -> tuple[str, tuple]:
    where, params_pre = _source_filter(source_types)
    sql = (
        "SELECT chunk_id, document_id, source_type, source_uri, section, "
        "       title, chunk_text, 1 - (embedding <=> %s) AS score "
        "FROM chunks_with_source"
        f"{where} "
        "ORDER BY embedding <=> %s "
        "LIMIT %s"
    )
    # Placeholder order: SELECT embedding, optional WHERE source_type array,
    # ORDER BY embedding, LIMIT.
    return sql, (embedding, *params_pre, embedding, k)


def _build_bm25_sql(
    ts_query: str, k: int, source_types: Sequence[str] | None
) -> tuple[str, tuple]:
    where, params_pre = _source_filter(source_types, leading_where=False)
    base_where = "tsv @@ to_tsquery('english', %s)"
    full_where = f"WHERE {base_where}" + (f" AND {where}" if where else "")
    sql = (
        "SELECT chunk_id, document_id, source_type, source_uri, section, "
        "       title, chunk_text, ts_rank(tsv, to_tsquery('english', %s)) AS score "
        "FROM chunks_with_source "
        f"{full_where} "
        "ORDER BY ts_rank(tsv, to_tsquery('english', %s)) DESC "
        "LIMIT %s"
    )
    return sql, (ts_query, ts_query, *params_pre, ts_query, k)


def _source_filter(
    source_types: Sequence[str] | None, *, leading_where: bool = True
) -> tuple[str, tuple]:
    if not source_types:
        return ("", ())
    clause = "source_type = ANY(%s)"
    return (f" WHERE {clause}" if leading_where else clause, (list(source_types),))


def _row_to_hit(row) -> Hit:
    return Hit(
        chunk_id=row[0],
        document_id=row[1],
        source_type=row[2],
        source_uri=row[3],
        section=row[4],
        title=row[5],
        chunk_text=row[6],
        score=float(row[7]),
    )
