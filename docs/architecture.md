# Architecture

## System overview

```
┌────────────────────────────────┐      ┌─────────────────┐
│         FastAPI container      │─────▶│  PostgreSQL +   │
│                                │◀─────│    pgvector     │
│  /ui (HTMX page)               │      └─────────────────┘
│  /ui/query  /query  /health    │
│  /help      /static             │
└──────────────┬─────────────────┘
               │ (server-side)
               ▼
       ┌───────────────┐
       │  Anthropic    │
       │     API       │
       └───────────────┘
```

Two containers in Docker Compose: `postgres` and `api`. The UI is
server-rendered Jinja + HTMX by the same FastAPI process that serves
the JSON API — no separate UI container, no frontend framework, no
browser-side LLM key. The `X-Anthropic-API-Key` only ever leaves the
host via the `api` container's outbound call to `api.anthropic.com`.

## Data flow

### Ingestion (one-time, offline)

```
MTSamples CSV
    │
    ▼
Filter: medical_specialty contains "Psychiatry"
    │
    ▼
Section-aware chunker (regex on SECTION HEADERS: pattern)
    │  (fallback: recursive character splitter for atypical notes)
    ▼
Batched embedding via S-PubMedBert-MS-MARCO (local CPU, ~1-3 min)
    │
    ▼
INSERT INTO chunks (note_id, specialty, section, chunk_text, embedding, tsvector)
    │  (parameterized — no f-strings near SQL, ever)
    ▼
HNSW index on embedding, GIN index on tsvector
```

### Query-time

```
User query
    │
    ├──▶ Embed with same model ──▶ Vector search (cosine via <=> operator)
    │                                     │
    ├──▶ Full-text search (ts_rank) ──────┤
    │                                     │
    │                                     ▼
    │                            Weighted fusion (normalized scores)
    │                                     │
    │                                     ▼
    │                                Top-k chunks
    │                                     │
    └────────────────┬────────────────────┘
                     ▼
          System prompt + context + query
                     │
                     ▼
             Anthropic API call
                     │
                     ▼
        Post-generation citation validation
                     │
                     ▼
        Response with answer + chunks + citations + latency
```

## Key design decisions

### Why pgvector and not a dedicated vector DB?

You already know Postgres. One image, one connection string, one query
language. Transactional guarantees come for free. Metadata filtering and
vector search compose naturally in SQL (`WHERE specialty = 'Psychiatry'
ORDER BY embedding <=> $1 LIMIT 5`). For a portfolio piece demonstrating
production sensibility, this is a stronger choice than dragging in Pinecone
for a dataset that fits on a laptop.

At scale, the conversation changes — for multi-million-chunk corpora, a
managed service may win on operational overhead. That's an interview
talking point, not a reason to complicate the demo.

### Why S-PubMedBert-MS-MARCO over general embeddings?

Medical synonyms are the core retrieval problem. "MI" and "myocardial
infarction" and "heart attack" must cluster. A general embedder like
`all-MiniLM-L6-v2` treats these as unrelated tokens. PubMedBERT was
pretrained on biomedical text, and the MS-MARCO fine-tune made it a
sentence embedder. 768 dimensions is a reasonable tradeoff — larger
embeddings (1024+) marginally improve quality but double storage.

### Why hybrid search (BM25 + vector)?

Clinical queries often contain exact strings that semantic search handles
poorly: drug names ("sertraline"), codes ("F32.1"), specific dosages.
Pure vector search can miss these; pure BM25 misses paraphrases. A
weighted sum captures both. `HYBRID_VECTOR_WEIGHT=0.7, HYBRID_BM25_WEIGHT=0.3`
is a reasonable default; we'd tune this on the eval set in a real project.

### Why section-aware chunking?

Clinical notes follow templates. An "ASSESSMENT" chunk and a "PAST MEDICAL
HISTORY" chunk are about different things even if they share vocabulary.
Splitting by section keeps each chunk semantically coherent and lets us
filter by section type at query time (e.g., "show me only Plan sections").
Fixed-size chunking would bleed content across these boundaries.

### Why force citations in the system prompt?

Two reasons:
1. **Audit trail.** Clinicians need to verify where a claim came from.
   A claim without a citation is not clinically useful.
2. **Hallucination detection.** If the model cites `[chunk_id_47]` but
   that ID isn't in our retrieved set, the model invented it — and
   probably invented the claim too. Post-generation validation catches
   this.

### Why the exact "I don't know" string?

Soft refusals ("I'm not sure but...", "It seems like...") still hallucinate.
The exact-string approach lets us detect refusal reliably in evaluation
and downstream tooling. The system prompt specifies the exact string;
our eval harness checks for it.

## The neural network layer (for interview discussion)

The embedding model (`S-PubMedBert-MS-MARCO`) is a BERT-family bi-directional
encoder. It takes a chunk of text and produces a single 768-dimensional
dense vector via mean-pooling over the token embeddings from the last
hidden layer. Contrastive training (originally on PubMed abstracts, then
on MS-MARCO query-passage pairs) shaped the vector space so that
semantically similar texts end up close in cosine distance.

The generator (Claude Haiku 4.5 / Sonnet 4.5) is an autoregressive decoder.
Its self-attention mechanism is O(n²) in sequence length, which is the
architectural reason we retrieve only the top-k chunks rather than
stuffing the whole corpus. The "lost in the middle" phenomenon (models
pay most attention to the start and end of their context) is why we
order retrieved chunks by score with the most relevant chunk at the end
of the context.

Neither model is trained or fine-tuned in this project. Both are used
for inference with pre-trained weights.

## Failure modes and mitigations

| Failure mode | Mitigation |
|---|---|
| Retrieval returns irrelevant chunks | Hybrid search + evaluation harness with precision@k tracking |
| LLM hallucinates outside retrieved context | Forced citations + post-generation validation; eval faithfulness metric |
| LLM hallucinates a citation ID | Parse cited IDs, verify against retrieved set, flag if invalid |
| Off-topic query gets a fabricated answer | Exact-string refusal instruction; eval refusal rate |
| Vector DB down | `/health` returns degraded; UI shows user-friendly error |
| Anthropic API down or rate-limited | Graceful error in API layer; no retry storms |
| Secret leakage via logs | Hashed query logging; no raw chunk text in INFO logs |
| Oversized query exhausts context | Pydantic `max_length=2000` on input |
| SQL injection via query text | Parameterized queries everywhere; query text only embedded, never concatenated into SQL |
| API key committed to git | `.gitignore` + pre-commit grep; spend limit on the account as defense in depth |

## What a real healthcare deployment would add (interview talking points)

- BAA with Anthropic before touching any PHI
- Self-hosted LLM option (Llama, Mistral via vLLM) for PHI-sensitive workloads
- De-identification step in ingestion (NER for PHI entities)
- Full audit logging to immutable storage (WORM buckets)
- SSO with role-based access, signed API tokens, short-lived credentials
- Encryption at rest for the vector DB volume
- Network segmentation — API layer in a VPC with no direct internet egress
  except through an egress proxy that logs Anthropic calls
- RAGAS or custom faithfulness scoring on every production response, with
  low-confidence responses routed to human review
- Dashboards on Grafana/Datadog for retrieval quality drift over time
