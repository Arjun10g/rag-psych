"""
FastAPI service: POST /query (RAG pipeline) and GET /health.

Wires together api/hybrid.py (retrieval) and api/generate.py (generation)
behind a Pydantic-validated HTTP surface with audit logging, rate limiting,
and CORS locked down to the Streamlit demo origin only.

Security boundaries enforced here (per CLAUDE.md):
  - Rule 5: query text and chunk text never appear in logs; we log a
    16-char query hash plus latency/k/model metrics only.
  - Rule 7: query length capped at 2000 chars by Pydantic; oversize
    requests return 400 with a generic error (no schema details leaked).
  - Rule 8: /health returns only `{"status": "ok"|"degraded"}` with no
    stack traces, version strings, or schema details.

Run locally:  uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .generate import generate
from .hybrid import retrieve_hybrid
from .logging_config import configure_logging, hash_query

load_dotenv()
audit = configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGIN", "http://localhost:8000").split(",")
    if o.strip()
]
DATABASE_URL = os.environ["DATABASE_URL"]

_HERE = Path(__file__).resolve().parent
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="rag-psych", version="0.1.0", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
templates = Jinja2Templates(directory=_HERE / "templates")
_CITATION_RE = re.compile(r"\[(\d+)\]")

_EVAL_RESULTS_DIR = _HERE.parent / "eval" / "results"
_basic_auth = HTTPBasic(auto_error=False)


def _require_eval_password(
    credentials: HTTPBasicCredentials | None = Depends(_basic_auth),
) -> None:
    """HTTP Basic auth gate for /eval routes.

    The username field is accepted but ignored; only the password is
    checked against the EVAL_PASSWORD env var. `secrets.compare_digest`
    gives us constant-time comparison so password-guessing attempts
    can't be timed. If EVAL_PASSWORD is unset the route is sealed off
    (no accidental wide-open dashboard).
    """
    expected = os.environ.get("EVAL_PASSWORD", "")
    supplied = credentials.password if credentials else ""
    ok = bool(expected) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="rag-psych eval"'},
        )

SourceType = Literal["mtsamples", "pubmed", "icd11", "icd12"]


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=20)
    source_types: list[SourceType] | None = None


class ChunkSummary(BaseModel):
    chunk_id: int
    source_type: str
    section: str | None
    title: str | None
    chunk_text: str
    rerank_score: float


class Latencies(BaseModel):
    retrieval_ms: float
    generation_ms: float
    total_ms: float


class QueryResponse(BaseModel):
    answer: str
    cited_ids: list[int]
    invalid_cited_ids: list[int]
    refused: bool
    retrieved_chunks: list[ChunkSummary]
    model: str
    latency: Latencies


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Normalize Pydantic validation failures to 400 + generic error.

    Returning the raw `exc.errors()` would leak field-level schema hints
    into the response body, which CLAUDE.md rule 8 forbids.
    """
    return JSONResponse(status_code=400, content={"error": "invalid_request"})


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Bare Space URL → /ui. Also catches HF Spaces' platform healthcheck."""
    return RedirectResponse(url="/ui", status_code=307)


@app.get("/health")
def health() -> JSONResponse:
    """Liveness + DB reachability. No internals leaked on failure."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception:
        return JSONResponse({"status": "degraded"}, status_code=503)


@app.post("/query", response_model=QueryResponse)
@limiter.limit("30/minute")
def query(request: Request, body: QueryRequest) -> QueryResponse:
    """Run the RAG pipeline end-to-end. See module docstring for guarantees."""
    qhash = hash_query(body.query)
    audit.info("query_received", extra={"audit": {
        "query_hash": qhash, "k": body.k,
        "source_types": body.source_types, "client": get_remote_address(request),
    }})

    t0 = time.perf_counter()
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            t_retrieve_start = time.perf_counter()
            hits = retrieve_hybrid(conn, body.query, k=body.k, source_types=body.source_types)
            retrieval_ms = (time.perf_counter() - t_retrieve_start) * 1000
            gen = generate(body.query, hits)
    except Exception:
        audit.exception("query_failed", extra={"audit": {"query_hash": qhash}})
        raise HTTPException(status_code=500, detail="internal_error")

    total_ms = (time.perf_counter() - t0) * 1000
    audit.info("query_completed", extra={"audit": {
        "query_hash": qhash,
        "k": body.k,
        "retrieved_count": len(hits),
        "cited_count": len(gen.cited_ids),
        "invalid_cited_count": len(gen.invalid_cited_ids),
        "refused": gen.refused,
        "model": gen.model,
        "retrieval_ms": round(retrieval_ms, 1),
        "generation_ms": round(gen.latency_ms, 1),
        "total_ms": round(total_ms, 1),
    }})

    return QueryResponse(
        answer=gen.answer,
        cited_ids=gen.cited_ids,
        invalid_cited_ids=gen.invalid_cited_ids,
        refused=gen.refused,
        retrieved_chunks=[
            ChunkSummary(
                chunk_id=h.hit.chunk_id,
                source_type=h.hit.source_type,
                section=h.hit.section,
                title=h.hit.title,
                chunk_text=h.hit.chunk_text,
                rerank_score=h.rerank_score,
            )
            for h in hits
        ],
        model=gen.model,
        latency=Latencies(
            retrieval_ms=round(retrieval_ms, 1),
            generation_ms=round(gen.latency_ms, 1),
            total_ms=round(total_ms, 1),
        ),
    )


# ─── HTMX-served UI ────────────────────────────────────────────────────────


@app.get("/ui", response_class=HTMLResponse)
def ui_index(request: Request) -> HTMLResponse:
    """Render the main page. Empty results section; HTMX swaps it in."""
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/help", response_class=HTMLResponse)
def ui_help(request: Request) -> HTMLResponse:
    """Static help page: what the system offers, examples, limits, pipeline."""
    return templates.TemplateResponse(request, "help.html", {})


# ─── Password-gated /eval dashboard ────────────────────────────────────────


@app.get(
    "/eval",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_eval_password)],
)
def eval_dashboard(request: Request) -> HTMLResponse:
    """Password-protected eval visualization dashboard."""
    return templates.TemplateResponse(request, "eval.html", {})


@app.get(
    "/eval/data",
    dependencies=[Depends(_require_eval_password)],
)
def eval_data() -> JSONResponse:
    """JSON feed for the dashboard: run history + live corpus stats."""
    return JSONResponse({
        "runs": _load_eval_runs(),
        "corpus": _corpus_stats(),
    })


def _load_eval_runs() -> list[dict[str, Any]]:
    """All eval/results/*.json files, oldest first. Empty list if the
    directory hasn't been populated yet (fresh clone, Docker without
    the volume mount)."""
    if not _EVAL_RESULTS_DIR.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for path in sorted(_EVAL_RESULTS_DIR.glob("*.json")):
        try:
            runs.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return runs


def _corpus_stats() -> dict[str, Any]:
    """Live Postgres counts by source, plus top sections per source."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_type, COUNT(*) FROM documents GROUP BY 1 ORDER BY 1"
                )
                docs = {row[0]: row[1] for row in cur.fetchall()}
                cur.execute(
                    "SELECT source_type, COUNT(*) FROM chunks_with_source "
                    "GROUP BY 1 ORDER BY 1"
                )
                chunks = {row[0]: row[1] for row in cur.fetchall()}
                cur.execute(
                    "SELECT source_type, section, COUNT(*) AS n "
                    "FROM chunks_with_source WHERE section IS NOT NULL "
                    "GROUP BY 1, 2 ORDER BY n DESC LIMIT 40"
                )
                sections = [
                    {"source_type": r[0], "section": r[1], "n": r[2]}
                    for r in cur.fetchall()
                ]
        return {"docs": docs, "chunks": chunks, "sections": sections}
    except Exception:
        return {"docs": {}, "chunks": {}, "sections": []}


@app.post("/ui/query", response_class=HTMLResponse)
@limiter.limit("30/minute")
def ui_query(
    request: Request,
    query: str = Form(..., min_length=1, max_length=2000),
    k: int = Form(5, ge=1, le=20),
) -> HTMLResponse:
    """HTMX endpoint: returns rendered _results.html fragment for swap-in."""
    qhash = hash_query(query)
    audit.info("ui_query_received", extra={"audit": {
        "query_hash": qhash, "k": k, "client": get_remote_address(request),
    }})
    t0 = time.perf_counter()
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            t_r = time.perf_counter()
            hits = retrieve_hybrid(conn, query, k=k)
            retrieval_ms = (time.perf_counter() - t_r) * 1000
            gen = generate(query, hits)
    except Exception:
        audit.exception("ui_query_failed", extra={"audit": {"query_hash": qhash}})
        return templates.TemplateResponse(
            request, "_error.html", {"message": "Something went wrong. Please try again."},
            status_code=500,
        )
    total_ms = (time.perf_counter() - t0) * 1000
    audit.info("ui_query_completed", extra={"audit": {
        "query_hash": qhash, "k": k, "retrieved_count": len(hits),
        "cited_count": len(gen.cited_ids), "invalid_cited_count": len(gen.invalid_cited_ids),
        "refused": gen.refused, "model": gen.model,
        "retrieval_ms": round(retrieval_ms, 1),
        "generation_ms": round(gen.latency_ms, 1),
        "total_ms": round(total_ms, 1),
    }})

    answer_html = _render_citations(gen.answer)
    return templates.TemplateResponse(request, "_results.html", {
        "answer_html": answer_html,
        "cited_ids": gen.cited_ids,
        "invalid_cited_ids": gen.invalid_cited_ids,
        "refused": gen.refused,
        "hits": hits,
        "model": gen.model,
        "retrieval_ms": round(retrieval_ms, 0),
        "generation_ms": round(gen.latency_ms, 0),
        "total_ms": round(total_ms, 0),
    })


def _render_citations(answer: str) -> str:
    """Wrap each [chunk_id] in a clickable span GSAP/JS hooks into.

    Escapes the text first; chunk IDs are integers from our DB so they're
    safe to interpolate, but the surrounding answer is LLM output and must
    be HTML-escaped before injecting our spans.
    """
    from html import escape
    safe = escape(answer)

    def _wrap(m: re.Match) -> str:
        cid = m.group(1)
        return f'<span class="citation" data-chunk="{cid}" tabindex="0">[{cid}]</span>'
    return _CITATION_RE.sub(_wrap, safe)
