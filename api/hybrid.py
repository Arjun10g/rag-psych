"""
Hybrid retrieval: BM25 + dense fused via Reciprocal Rank Fusion, then
re-ranked by a cross-encoder.

Why RRF instead of weighted-sum normalization: BM25 and cosine live on
incomparable scales, and the optimal α between them shifts across query
types. RRF (Cormack et al. 2009) drops magnitudes and aggregates ranks,
which is what production systems (OpenSearch, Vespa, Elastic, MedRAG)
have converged on.

Why a cross-encoder rerank on top: Anthropic's contextual retrieval
benchmark shows reranking adds roughly the same lift as going from
dense-only to hybrid. Cross-encoders score (query, chunk) jointly with
full attention, fixing the "bag of similar embeddings" failure mode.

Why a score-threshold refusal: clinical RAGs need to say "I don't know"
when the corpus doesn't contain the answer. A min cross-encoder score
on the best post-rerank result is a cheap, well-calibrated signal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

import psycopg
from sentence_transformers import CrossEncoder

from .negation import is_negated, salient_query_terms
from .rag import Hit, retrieve_bm25, retrieve_lexical, retrieve_vector

DEFAULT_RRF_K = 60
DEFAULT_CANDIDATE_K = 50
DEFAULT_FINAL_K = 5
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-12-v2"
DEFAULT_RERANK_MIN_SCORE = -5.0
# Run negation filter on this many top-reranked candidates; we drop the
# negated ones, then take the final k. Bigger window = more headroom for
# refusal-after-filter, at proportional NegEx cost (~17 ms/chunk).
DEFAULT_NEGATION_WINDOW = 15
# Per-source retrieval: each retriever runs once per source_type with a
# source filter, so no single source can crowd out the others in the
# candidate pool. Each sub-query returns DEFAULT_PER_SOURCE_K rows; RRF
# unions them into the final candidate list.
DEFAULT_PER_SOURCE_K = 20
SOURCE_TYPES: tuple[str, ...] = ("mtsamples", "pubmed", "icd11", "icd12")

_reranker: CrossEncoder | None = None


@dataclass(frozen=True)
class RerankedHit:
    hit: Hit
    rerank_score: float


def retrieve_hybrid(
    conn: psycopg.Connection,
    query: str,
    k: int = DEFAULT_FINAL_K,
    candidate_k: int | None = None,
    source_types: Sequence[str] | None = None,
    min_score: float | None = None,
) -> list[RerankedHit]:
    """RRF-fused, cross-encoder-reranked retrieval with refusal threshold.

    Returns up to `k` hits sorted by reranker score (descending).
    Returns [] when no candidates survive the threshold — the generation
    layer interprets that as the trigger for the canonical refusal string.
    """
    candidate_k = candidate_k or int(os.environ.get("RETRIEVAL_CANDIDATE_K", DEFAULT_CANDIDATE_K))
    per_source_k = int(os.environ.get("PER_SOURCE_K", DEFAULT_PER_SOURCE_K))
    rrf_k = int(os.environ.get("HYBRID_RRF_K", DEFAULT_RRF_K))
    threshold = (
        min_score if min_score is not None
        else float(os.environ.get("RERANK_MIN_SCORE", DEFAULT_RERANK_MIN_SCORE))
    )

    # Per-source retrieval. For each source we run all three retrievers
    # with that source as a filter, producing up to 3 × len(sources)
    # ranked lists. RRF then fuses them all. This keeps the candidate
    # pool balanced across sources even when one source dominates by
    # volume (e.g., pubmed with 10K docs).
    sources = list(source_types) if source_types else list(SOURCE_TYPES)
    rankings: list[list[Hit]] = []
    for src in sources:
        rankings.append(retrieve_vector(conn, query, k=per_source_k, source_types=[src]))
        rankings.append(retrieve_bm25(conn, query, k=per_source_k, source_types=[src]))
        rankings.append(retrieve_lexical(conn, query, k=per_source_k, source_types=[src]))
    fused = _rrf_fuse(rankings, k_rrf=rrf_k)
    if not fused:
        return []

    candidates = fused[:candidate_k]
    scored = _rerank(query, candidates)
    scored.sort(key=lambda r: r.rerank_score, reverse=True)
    if not scored or scored[0].rerank_score < threshold:
        return []
    return _drop_negated(query, scored, k=k)


def _drop_negated(
    query: str, scored: list[RerankedHit], k: int
) -> list[RerankedHit]:
    """Apply NegEx filter on the top window, then take the final top-k.

    Chunks where the query's salient clinical terms appear in a negated
    span (e.g. "patient denies suicidal ideation" for a query about
    suicidal ideation) are dropped. NegEx is rule-based and clinical-
    specific; latency is ~17 ms per chunk so we cap the window.
    """
    terms = salient_query_terms(query)
    if not terms:
        return scored[:k]
    window = scored[:DEFAULT_NEGATION_WINDOW]
    kept = [r for r in window if not is_negated(r.hit.chunk_text, terms)]
    if not kept:
        return []
    return kept[:k]


def _rrf_fuse(rankings: list[list[Hit]], k_rrf: int) -> list[Hit]:
    """Aggregate RRF scores across retrievers; return hits sorted by total.

    Deduplicates by `chunk_text` after fusion: the MTSamples CSV contains
    literal duplicate rows (same transcription submitted twice) and they
    produce distinct chunk_ids that would otherwise crowd the top-k with
    repeats. We keep the highest-scoring representative of each text.
    """
    scores: dict[int, float] = {}
    keep: dict[int, Hit] = {}
    for hits in rankings:
        for rank, hit in enumerate(hits):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k_rrf + rank + 1)
            keep.setdefault(hit.chunk_id, hit)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    seen_text: set[str] = set()
    deduped: list[Hit] = []
    for chunk_id, _ in ordered:
        hit = keep[chunk_id]
        text_key = (hit.chunk_text or "").strip()
        if text_key in seen_text:
            continue
        seen_text.add(text_key)
        deduped.append(hit)
    return deduped


def _rerank(query: str, candidates: list[Hit]) -> list[RerankedHit]:
    if not candidates:
        return []
    model = _get_reranker()
    pairs = [(query, h.chunk_text) for h in candidates]
    scores = model.predict(pairs, show_progress_bar=False)
    return [RerankedHit(hit=h, rerank_score=float(s)) for h, s in zip(candidates, scores)]


def _get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        name = os.environ.get("RERANK_MODEL", DEFAULT_RERANK_MODEL)
        _reranker = CrossEncoder(name)
    return _reranker
