# rag-psych — Clinical RAG Pipeline

A one-to-two-day local build of a Retrieval-Augmented Generation system over
MTSamples psychiatry transcriptions. Built as an interview portfolio artifact
for a healthcare AI engineering role.

## What this project is — and isn't

This is a **RAG integration project**, not an ML training project. Every
neural network is loaded with pre-trained weights and used for inference only.

**In scope:**
- Multi-source ingestion via a pluggable `ingest/sources/` architecture
  - MTSamples — de-identified clinical notes (local CSV)
  - PubMed — psychiatry/psychology abstracts (NCBI E-utilities API)
  - ICD-11 — WHO mental disorders chapter (OAuth2 REST API)
- Source-aware chunking strategies (one per source)
- Unified schema with document-level provenance (source_type, license, metadata)
- Embed chunks with a pre-trained clinical model (Bio_ClinicalBERT family)
- Store vectors in pgvector; retrieve with hybrid search (cosine + BM25)
- Optional filtering by source_type at retrieval time
- Generate grounded answers via the Anthropic API with forced citations
- Serve the API and the HTMX-rendered demo UI from the same FastAPI process;
  orchestrate via Docker Compose
- Evaluate with a small hand-labeled set (precision@k, faithfulness)

**Out of scope:**
- Training or fine-tuning any transformer
- Implementing attention, transformer blocks, or positional encodings
- Domain-adaptive pretraining or contrastive fine-tuning of the embedder
- Any use of real PHI — all data is public or de-identified

## Tech stack (pinned, do not substitute without asking)

- Python 3.11
- PostgreSQL 16 with pgvector extension (image: `pgvector/pgvector:pg16`)
- `sentence-transformers` with `pritamdeka/S-PubMedBert-MS-MARCO` (768-dim)
- Anthropic Python SDK, model `claude-haiku-4-5` for dev, `claude-sonnet-4-5` for demo
- FastAPI + uvicorn for the API service
- HTMX + Jinja2 templates (served by FastAPI) for the demo UI — no separate
  UI framework; Three.js + GSAP via CDN for visual polish
- `psycopg[binary]` for Postgres (v3, not psycopg2)
- Docker Compose for orchestration

## Critical security rules — enforce without exception

1. **No secrets in code.** Anthropic API key lives only in `.env`, which is
   gitignored. Never hardcode keys, never log them, never echo them in errors.
2. **No PHI, ever.** The only data in this repo is MTSamples (public,
   de-identified). Do not add real patient data, do not synthesize realistic
   PHI, do not commit any `.csv` larger than a tiny sample fixture.
3. **The `.env` file is sacred.** It must appear in `.gitignore` on day one.
   Before any `git add`, confirm `.env` is not staged.
4. **Parameterized SQL only.** All database queries use parameter binding via
   psycopg. Never f-string or concatenate user input into SQL — even for
   "internal" code.
5. **Audit-style logging.** Every `/query` call logs: timestamp, query hash
   (not the query itself — queries may contain sensitive context), number of
   chunks retrieved, latency, model used. Never log full chunk text or raw
   queries in production-mode logs.
6. **Pin image digests in production.** For the demo, `pgvector/pgvector:pg16`
   is fine. In any README code the user might copy into a real deployment,
   recommend pinning to a specific digest.
7. **Validate all API inputs.** FastAPI request models use Pydantic with
   strict types and `max_length` constraints on query strings (cap at 2000
   chars). Reject anything larger with a 400.
8. **Health check isolation.** `/health` must not reveal stack traces, package
   versions, or database schema. Return `{"status": "ok" | "degraded"}` only.
9. **Docker containers run as non-root.** Every Dockerfile creates and
   switches to a non-root user before copying application code.
10. **No external calls from the ingest script except the embedding model
    download.** Ingest runs entirely against local data + local Postgres.
11. **Database credentials live in `.env`, not in `docker-compose.yml`.** The
    compose file references them via `${POSTGRES_PASSWORD}` etc. Never
    hardcode database passwords, usernames, or database names in any
    committed file. Generate real passwords with:
    `python -c "import secrets; print(secrets.token_urlsafe(24))"`

## Project layout

```
rag-psych/
├── CLAUDE.md                    # this file — loaded every session
├── .env.example                 # committed — template for users
├── .env                         # GITIGNORED — real secrets
├── .gitignore
├── .dockerignore
├── docker-compose.yml
├── requirements.txt
├── README.md
├── docs/                        # pulled in on demand via @docs/filename.md
│   ├── architecture.md          # detailed system design + diagrams
│   ├── security-checklist.md    # pre-commit + pre-deploy checks
│   ├── interview-talking-points.md
│   └── roadmap.md               # phased build plan with checkboxes
├── data/
│   ├── .gitkeep                 # mtsamples.csv + cache/ are GITIGNORED
│   └── cache/                   # per-source API response caches
├── ingest/
│   ├── schema.sql               # documents + chunks tables, HNSW/GIN indexes
│   ├── run.py                   # top-level runner: --sources flag
│   └── sources/                 # pluggable source modules
│       ├── __init__.py          # RawDocument, Chunk, Source protocol
│       ├── mtsamples.py
│       ├── pubmed.py
│       └── icd11.py
├── api/
│   ├── main.py                  # FastAPI app (JSON /query + HTMX /ui + /help + /health)
│   ├── rag.py                   # retrieve_vector + retrieve_bm25 + retrieve_lexical
│   ├── hybrid.py                # per-source RRF + cross-encoder rerank + NegEx filter
│   ├── generate.py              # Claude call + forced citations + citation audit
│   ├── negation.py              # custom rule-based clinical negation detector
│   ├── logging_config.py        # audit-style JSON logging (query_hash only)
│   ├── templates/               # index.html · _results.html · help.html · _error.html
│   └── static/                  # app.js (Three.js) · animations.js (GSAP) · styles.css
├── Dockerfile                   # root Dockerfile — used by compose, HF Spaces, Fly
└── eval/
    ├── test_queries.yaml        # hand-labeled eval set
    ├── run_eval.py              # precision@k, faithfulness
    └── results/                 # GITIGNORED
```

## How to work in this repo

- Before making changes, read `@docs/roadmap.md` to see current phase and
  checked-off items.
- For architecture questions, read `@docs/architecture.md`.
- Before committing, run through `@docs/security-checklist.md`.
- When adding a new feature, update the roadmap checkboxes in the same commit.
- Keep functions under ~40 lines where reasonable; this is a portfolio piece
  and should read cleanly.
- Prefer explicit over clever. Interviewers will read this code.

## Code style

- Type hints on all function signatures. `from __future__ import annotations`
  at the top of every `.py` file.
- Docstrings on public functions — one-liner summary, then explain the *why*
  if the function makes a non-obvious choice.
- Keep inline comments rare. The code should explain what; docstrings and
  `docs/` explain why.
- Use `pathlib.Path`, not `os.path`.
- Use `psycopg` v3 (`import psycopg`), not `psycopg2`.
- Format with `ruff format` before commit; lint with `ruff check`.

## Testing strategy

- Unit tests for chunking logic (`ingest/sources/mtsamples.py`,
  `ingest/sources/dsm.py`) — run on a fixture of a few synthetic notes.
- Integration test for retrieval — ingest a tiny fixture, confirm a known
  query returns the expected chunk in top-3.
- Eval harness (`eval/run_eval.py`) is the headline quality signal; it's not
  a unit test but should be runnable with one command.

## Things Claude Code should NOT do

- Do not add real patient data or realistic synthetic PHI "for better testing."
- Do not suggest committing `data/mtsamples.csv` — it must stay gitignored.
- Do not add a fine-tuning step to the ingest pipeline.
- Do not switch from pgvector to a managed vector DB (Pinecone, etc.) without
  being asked — the pgvector choice is deliberate.
- Do not add `psycopg2` — we use psycopg v3.
- Do not introduce LangChain as a core dependency. If a specific LangChain
  utility is genuinely the cleanest option for one task, discuss it first.
- Do not log raw queries, raw chunks, or the API key — ever.
- Do not generate long inline comments or docstrings that restate what the
  code obviously does.
- Do not hardcode Postgres credentials in `docker-compose.yml`. They must
  be referenced as `${POSTGRES_PASSWORD}` etc. and defined in `.env`.
- Do not commit any API response cache (`data/cache/**`). The `data/*`
  rule in `.gitignore` covers this — verify before commit.
- Do not log ICD-11 client_id, client_secret, access tokens, or NCBI
  API keys at any log level.
- Do not add a fourth source without discussion. The pluggable design
  supports it, but scope creep kills portfolio projects. Ship three
  sources well before adding a fourth.
- Do not fetch PubMed full-text from PMC. Stick to abstracts — PMC has
  different licensing per article.

## Quick commands

```bash
# First-time setup
cp .env.example .env          # then fill in ANTHROPIC_API_KEY
docker compose up -d postgres
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Ingest data (once — or re-run when adding a new source)
python ingest/run.py --sources all

# Or just one source at a time while debugging
python ingest/run.py --sources mtsamples

# Run the full stack
docker compose up --build

# Run evaluation
python eval/run_eval.py

# Lint & format
ruff format . && ruff check .
```
