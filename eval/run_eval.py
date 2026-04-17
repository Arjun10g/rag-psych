"""
Evaluation harness for the rag-psych retrieval + generation pipeline.

Loads `eval/test_queries.yaml`, runs each query end-to-end through
`retrieve_hybrid` + `generate`, scores it against the hand-labeled
expectations, and prints a markdown table to stdout. A full JSON dump
(per-query and aggregate) is also written to
`eval/results/{ISO timestamp}.json` so we can diff runs over time as
the pipeline evolves.

Metrics:
  - source_routing_top1   — did the rank-1 chunk match an expected source?
  - source_recall_top5    — fraction of top-5 from any expected source
  - keyword_recall        — fraction of expected_keywords that appear at
                            least once in the top-5 chunk_text (case-insensitive)
  - refusal_correct       — for off_topic queries, did the system refuse?
                            (refusal is either retrieve_hybrid → [] or the
                            generator returning the canonical refusal string)
  - citation_validity     — fraction of cited chunk_ids that are in the
                            retrieved set (1.0 means no hallucinated citations)
  - negation_held         — for queries with `negation:`, none of the
                            forbidden patterns appear in top-5 chunk_text
  - retrieval_ms / generation_ms / total_ms — wall-clock per query

Aggregates:
  - means of all numeric per-query metrics
  - off_topic refusal rate (must be 100% to pass)
  - any non-empty `invalid_cited_ids` is a hallucination flag

Run:  .venv/bin/python eval/run_eval.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import psycopg
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.generate import REFUSAL_STRING, generate  # noqa: E402
from api.hybrid import retrieve_hybrid  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
QUERIES_PATH = EVAL_DIR / "test_queries.yaml"
RESULTS_DIR = EVAL_DIR / "results"


def main() -> None:
    load_dotenv()
    queries = yaml.safe_load(QUERIES_PATH.read_text())["queries"]
    print(f"loaded {len(queries)} queries from {QUERIES_PATH.name}\n")

    results: list[dict[str, Any]] = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for q in queries:
            results.append(_run_one(conn, q))
            print(f"  {q['id']:30s} done")

    aggregate = _aggregate(results)
    report_md = _format_markdown(results, aggregate)
    print("\n" + report_md)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"{ts}.json"
    out_path.write_text(json.dumps({
        "timestamp": ts,
        "n_queries": len(results),
        "aggregate": aggregate,
        "per_query": results,
    }, indent=2))
    print(f"\nresults saved to {out_path.relative_to(EVAL_DIR.parent)}")


def _run_one(conn: psycopg.Connection, q: dict[str, Any]) -> dict[str, Any]:
    qid = q["id"]
    query = q["query"]
    off_topic = bool(q.get("off_topic", False))
    expected_sources = set(q.get("expected_sources") or [])
    expected_keywords = q.get("expected_keywords") or []
    negation = q.get("negation")

    t0 = time.perf_counter()
    t_r = time.perf_counter()
    hits = retrieve_hybrid(conn, query, k=5)
    retrieval_ms = (time.perf_counter() - t_r) * 1000
    gen = generate(query, hits)
    total_ms = (time.perf_counter() - t0) * 1000

    sources = [h.hit.source_type for h in hits]
    blob = " \n ".join((h.hit.chunk_text or "") for h in hits).lower()

    source_routing_top1 = (
        bool(hits) and bool(expected_sources)
        and sources[0] in expected_sources
    )
    source_recall_top5 = (
        sum(1 for s in sources if s in expected_sources) / 5
        if expected_sources else None
    )
    keyword_recall = (
        sum(1 for kw in expected_keywords if kw.lower() in blob)
        / len(expected_keywords)
        if expected_keywords else None
    )
    refusal_correct = (
        gen.refused if off_topic else (not gen.refused or len(hits) == 0)
    )
    # Off-topic queries are correct when refused. Non-off-topic queries are
    # correct when not refused (or when retrieval refused — also acceptable).
    if off_topic:
        refusal_correct = gen.refused
    else:
        refusal_correct = True  # not penalized; we measure quality elsewhere

    citation_validity = (
        1.0 if not gen.cited_ids
        else (1 - len(gen.invalid_cited_ids) / len(gen.cited_ids))
    )

    negation_held: bool | None = None
    forbidden_hits: list[str] = []
    if negation:
        for pat in negation.get("forbidden_patterns", []):
            if pat.lower() in blob:
                forbidden_hits.append(pat)
        negation_held = (len(forbidden_hits) == 0)

    return {
        "id": qid,
        "query": query,
        "off_topic": off_topic,
        "n_hits": len(hits),
        "sources_top5": sources,
        "expected_sources": sorted(expected_sources),
        "source_routing_top1": source_routing_top1,
        "source_recall_top5": source_recall_top5,
        "keyword_recall": keyword_recall,
        "refused": gen.refused,
        "off_topic_refusal_correct": (gen.refused == off_topic) if off_topic else None,
        "cited_ids": gen.cited_ids,
        "invalid_cited_ids": gen.invalid_cited_ids,
        "citation_validity": citation_validity,
        "negation_held": negation_held,
        "negation_forbidden_hits": forbidden_hits,
        "answer_first_120": (gen.answer or "")[:120],
        "model": gen.model,
        "retrieval_ms": round(retrieval_ms, 1),
        "generation_ms": round(gen.latency_ms, 1),
        "total_ms": round(total_ms, 1),
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    def mean_of(key: str, predicate=lambda r: True) -> float | None:
        values = [r[key] for r in results if predicate(r) and r[key] is not None]
        return round(mean(values), 3) if values else None

    on_topic = lambda r: not r["off_topic"]
    off_topic = lambda r: r["off_topic"]
    has_neg = lambda r: r["negation_held"] is not None

    off_topic_results = [r for r in results if r["off_topic"]]
    off_topic_refusal_rate = (
        sum(1 for r in off_topic_results if r["refused"]) / len(off_topic_results)
        if off_topic_results else None
    )
    any_invalid = any(r["invalid_cited_ids"] for r in results)
    neg_pass_rate = (
        sum(1 for r in results if r["negation_held"]) / sum(1 for r in results if has_neg(r))
        if any(has_neg(r) for r in results) else None
    )

    return {
        "n_queries": len(results),
        "n_on_topic": sum(1 for r in results if not r["off_topic"]),
        "n_off_topic": len(off_topic_results),
        "source_routing_top1_rate":
            round(sum(1 for r in results if r["source_routing_top1"]) / sum(1 for r in results if on_topic(r)), 3)
            if any(on_topic(r) for r in results) else None,
        "mean_source_recall_top5": mean_of("source_recall_top5", on_topic),
        "mean_keyword_recall":     mean_of("keyword_recall", on_topic),
        "mean_citation_validity":  mean_of("citation_validity", on_topic),
        "any_hallucinated_citation": any_invalid,
        "off_topic_refusal_rate":  off_topic_refusal_rate,
        "negation_pass_rate":      neg_pass_rate,
        "mean_retrieval_ms":       mean_of("retrieval_ms"),
        "mean_generation_ms":      mean_of("generation_ms"),
        "mean_total_ms":           mean_of("total_ms"),
    }


def _format_markdown(results: list[dict[str, Any]], agg: dict[str, Any]) -> str:
    """Render a compact two-table report: per-query rows + aggregate rollup."""
    rows = ["| id | sources@5 | route✓ | kw rec | cite✓ | refused | t_total |",
            "|---|---|---|---|---|---|---|"]
    for r in results:
        kw = "—" if r["keyword_recall"] is None else f"{r['keyword_recall']:.0%}"
        sr = "—" if r["source_recall_top5"] is None else f"{r['source_recall_top5']:.0%}"
        cite = f"{r['citation_validity']:.0%}"
        route = "—" if r["off_topic"] else ("✓" if r["source_routing_top1"] else "✗")
        refused = "yes" if r["refused"] else "no"
        rows.append(f"| {r['id']} | {','.join(r['sources_top5'])[:24] or '—'} ({sr}) | {route} | {kw} | {cite} | {refused} | {int(r['total_ms'])}ms |")

    summary = [
        "",
        "### Aggregate",
        "",
        f"- queries: {agg['n_queries']} ({agg['n_on_topic']} on-topic, {agg['n_off_topic']} off-topic)",
        f"- source-routing top-1: **{(agg['source_routing_top1_rate'] or 0) * 100:.0f}%**",
        f"- mean source-recall@5: **{(agg['mean_source_recall_top5'] or 0) * 100:.0f}%**",
        f"- mean keyword-recall: **{(agg['mean_keyword_recall'] or 0) * 100:.0f}%**",
        f"- mean citation-validity: **{(agg['mean_citation_validity'] or 0) * 100:.0f}%** "
        f"({'hallucinated citations DETECTED' if agg['any_hallucinated_citation'] else 'no hallucinated citations'})",
        f"- off-topic refusal rate: **{(agg['off_topic_refusal_rate'] or 0) * 100:.0f}%** "
        f"(target 100%)",
        f"- negation pass rate: **{(agg['negation_pass_rate'] or 0) * 100:.0f}%** "
        f"(target 100%)" if agg['negation_pass_rate'] is not None else "- negation: not measured",
        f"- mean retrieval: {int(agg['mean_retrieval_ms'])} ms · "
        f"mean generation: {int(agg['mean_generation_ms'])} ms · "
        f"mean total: {int(agg['mean_total_ms'])} ms",
    ]
    return "\n".join(rows + summary)


if __name__ == "__main__":
    main()
