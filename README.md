---
title: RAG PSYCH
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Hybrid RAG over MTSamples + PubMed + ICD-11 with citations
---

# rag-psych

A local clinical retrieval-augmented generation pipeline over public
psychiatry reference material. Portfolio artefact for a healthcare AI
engineering role.

**Stack:** PostgreSQL 16 + pgvector · `pritamdeka/S-PubMedBert-MS-MARCO`
embeddings · hybrid retrieval (dense + BM25 + rare-token lexical,
per-source) · RRF fusion · `ms-marco-MiniLM-L-12-v2` cross-encoder
rerank · custom clinical-negation filter · Anthropic API for grounded
generation · FastAPI + HTMX + Three.js/GSAP UI · Docker Compose.

## What it does

User asks a clinical question → three retrievers fetch top-K candidates
from each source in parallel → RRF-fused candidate pool → cross-encoder
reranks → NegEx-style filter drops passages where the queried concept
is negated → top-k go to the LLM with a strict system prompt → citations
are parsed and any `[chunk_id]` not in the retrieved set is flagged as a
hallucination. Off-topic queries refuse cleanly. Full story at `/help`
once the server is up.

```
     query
       │
       ▼
┌──────────────────────────────────────────────────────┐
│  per-source retrieval (mtsamples · pubmed · icd11)   │
│   dense cosine · Postgres ts_rank · rare-token ILIKE │
└──────────────────────┬───────────────────────────────┘
                       ▼  RRF (k=60) + text-dedupe
                cross-encoder rerank
                       ▼
              rule-based NegEx filter
                       ▼
                     top-5 ─────────────► Claude (forced citations)
                                             │
                                             ▼
                                 citation-validity audit
                                             │
                                             ▼
                                         answer
```

## Quick start

Requires Docker Desktop and Python 3.11. The first ingest pulls ~500 MB
of model weights and hits the PubMed and ICD-11 APIs; everything after
is local.

```bash
# 1. Secrets
cp .env.example .env
# Edit .env:
#   - ANTHROPIC_API_KEY  → your key from console.anthropic.com
#   - POSTGRES_PASSWORD  → run: python -c "import secrets; print(secrets.token_urlsafe(24))"
#                           paste into both POSTGRES_PASSWORD and DATABASE_URL
#   - NCBI_EMAIL         → a real address (NCBI requires it for bulk PubMed)
#   - ICD_CLIENT_ID      → register at https://icd.who.int/icdapi
#   - ICD_CLIENT_SECRET

# 2. Python env (host-side, needed for the ingest CLI)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Data — MTSamples CSV
# Download from https://www.kaggle.com/datasets/tboyle10/medicaltranscriptions
# and place at data/mtsamples.csv  (gitignored)

# 4. Postgres — brings up pgvector/pgvector:pg16 with the schema loaded
docker compose up -d postgres

# 5. Ingest all three public sources
.venv/bin/python ingest/run.py --sources all
#   → ~812 mtsamples docs · ~10K pubmed abstracts · ~685 icd11 entities
#   First run downloads the embedder (~400 MB) and caches API fetches to
#   data/cache/ — subsequent runs are mostly cache hits.

# 6. Bring up the full stack (API + UI served by FastAPI)
docker compose up --build api

# 7. Open the UI
#   http://localhost:8000/ui     — search
#   http://localhost:8000/help   — what it does, examples, limits
```

### Host-only dev loop (no container rebuild on code edits)

```bash
docker compose up -d postgres
.venv/bin/uvicorn api.main:app --reload --port 8000
```

### Evaluation

```bash
.venv/bin/python eval/run_eval.py
```

Runs the 16-query hand-labelled set in `eval/test_queries.yaml` against
the live pipeline + Postgres + Anthropic API. Prints a markdown report
and writes `eval/results/{ISO timestamp}.json` for diffing across runs.
Current headline numbers (after Phase 6.5 per-source retrieval):

| | |
|---|---|
| Source-routing top-1 | 79% |
| Source-recall@5 | 69% |
| Keyword-recall | 94% |
| **Citation validity** | **100%** (no hallucinated citations) |
| **Off-topic refusal rate** | **100%** |
| **Negation-filter pass rate** | **100%** |
| Mean total latency | ~5.8 s per query |

Cost at demo scale is ~$0.003–$0.005 per query on `claude-haiku-4-5`.

## Repo layout

```
rag-psych/
├── api/                 FastAPI service (routes, retrieval, generation, UI)
│   ├── main.py          /query (JSON) · /ui (HTMX) · /ui/query · /health · /help
│   ├── rag.py           retrieve_vector · retrieve_bm25 · retrieve_lexical
│   ├── hybrid.py        RRF + rerank + NegEx + refusal threshold
│   ├── generate.py      Claude call with forced citations + validation
│   ├── negation.py      rule-based clinical negation detector (NegEx-style)
│   ├── templates/       index.html · _results.html (HTMX partial) · help.html
│   └── static/          Three.js neural-particle scene, GSAP animations
├── Dockerfile           python:3.11-slim · non-root uid 10001 · models baked
├── ingest/
│   ├── run.py           `--sources all | mtsamples | pubmed | icd11`
│   ├── schema.sql       pgvector + HNSW + GIN indexes + trigger
│   └── sources/         one pluggable module per source
├── eval/
│   ├── test_queries.yaml    16 hand-labelled queries
│   └── run_eval.py          metrics harness
├── docs/
│   ├── roadmap.md               phase-by-phase build log (every decision, every number)
│   ├── architecture.md          design rationale
│   ├── security-checklist.md    pre-commit + pre-deploy checks
│   ├── deploy-hf-spaces.md      production deploy walkthrough (HF Spaces + Neon)
│   └── interview-talking-points.md
└── docker-compose.yml   postgres + api (UI is served by api, no separate service)
```

See [CLAUDE.md](CLAUDE.md) for working conventions and layout map.

## Design highlights

- **Per-source retrieval.** Each retriever runs once per source with a
  source filter, so a volume-heavy source (PubMed's 10K abstracts)
  can't crowd mtsamples and icd11 out of the candidate pool.
- **Three retrievers in parallel.** Dense for semantic similarity, BM25
  (OR-of-tokens) for keyword overlap, literal-substring for rare drug
  names / ICD codes that both other paths bury.
- **Custom rule-based negation filter.** `scispacy` + `negspacy` was
  evaluated first and rejected (~30 % false-positive rate on real
  clinical chunks because NegEx scope detection leaks across
  conjunctions). The replacement is a pure-Python terminator-aware
  matcher — 11/11 on a hand-built test grid, ~0.1 ms per chunk.
- **Defense-in-depth on negation.** Filter at retrieval, plus a
  polarity-check rule in the generator's system prompt.
- **Forced citations + server-side validation.** Every `[chunk_id]` the
  model emits is parsed and checked against the retrieved set.
  Hallucinated IDs are surfaced both in the JSON response
  (`invalid_cited_ids`) and in the UI (rose-coloured citation chip +
  warning banner).
- **Audit logging.** Every `/query` and `/ui/query` call logs
  `query_hash` (16-char SHA-256), k, retrieved_count, cited_count,
  invalid_cited_count, refused, model, and per-stage latencies.
  Raw query text and chunk text never touch the log stream.
- **Refusal short-circuit.** When the retrieval layer's confidence
  threshold trips, the UI renders the canonical refusal card with
  `generation_ms = 0` — no LLM call is made.

Detailed rationale in [docs/architecture.md](docs/architecture.md);
full build log with eval numbers at every step in
[docs/roadmap.md](docs/roadmap.md).

## Data note

Sources are **MTSamples** (public, de-identified clinical
transcriptions), **PubMed** abstracts (NLM-licensed for unrestricted
reuse of bibliographic metadata and abstracts), and **ICD-11 Chapter 06**
(WHO ICD-API, non-commercial use with attribution). No real patient
data, no PHI, no HIPAA-covered material is ever processed. Security
practices (parameterised SQL, audit logging, container hardening,
non-root runtime) are built to production standards regardless —
clinical habits need to exist before real data shows up.

## License

MIT for the code in this repo. Each upstream data source has its own
terms; consult each one before redistribution. Model weights used for
inference only; no fine-tuning.
