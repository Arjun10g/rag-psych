# Interview Talking Points

Rehearsed answers mapped to likely questions. Each answer is anchored in a
concrete decision you made in this codebase, so you can always point at code
when pressed for specifics.

## "Walk me through your RAG pipeline."

Ingestion is a three-step transform. I pull MTSamples psychiatry notes from
a CSV, chunk them with a section-aware regex that recognizes clinical headers
like HPI, ASSESSMENT, and PLAN — with a recursive character splitter as a
fallback for notes that don't follow the template. Each chunk gets embedded
with S-PubMedBert-MS-MARCO, a BERT-family encoder pretrained on biomedical
text, producing a 768-dimensional vector. Chunks, metadata, and vectors land
in a Postgres table with an HNSW index for approximate nearest neighbor
search and a GIN index on a tsvector column for BM25.

At query time, I embed the user query with the same model, run hybrid search
— cosine similarity plus BM25, weighted 70/30 — and take the top 5 chunks.
Those get formatted into a prompt with a strict system instruction: only use
the provided context, cite every claim with `[chunk_id]`, and refuse with a
specific string if the answer isn't in the context. After generation, I
validate that every cited chunk_id actually appears in the retrieved set —
if one doesn't, the model hallucinated a citation, and we flag the response.

FastAPI serves this via a `/query` endpoint with Pydantic validation and
audit-style logging, Streamlit demos it with a side-by-side view of the
answer and retrieved chunks, and Docker Compose ties it all together.

## "How do you prevent hallucinations?"

Three layers of defense.

Layer one is retrieval quality. Better chunks mean less hallucination
pressure on the LLM. I use a domain-specific embedder, hybrid search to
catch both semantic matches and exact strings, and I evaluate precision@5
on a hand-labeled set so I know when retrieval is failing.

Layer two is prompt engineering. The system prompt forces citations for
every claim and specifies an exact refusal string when the context doesn't
support an answer. Exact-string refusal is deliberate — soft refusals like
"I'm not sure but..." still hallucinate. An exact string is machine-checkable.

Layer three is post-generation validation. I parse the citations out of the
response and verify each cited chunk_id appears in the retrieved set. An
invalid citation is a strong signal the model fabricated content. In
production I'd extend this to faithfulness scoring — checking whether each
claim is actually supported by its cited chunk, probably via a cross-encoder
or a second LLM call.

## "Why pgvector over Pinecone or Weaviate?"

For a dataset that fits on a laptop, pgvector wins on operational simplicity
— one image, one query language, metadata filtering and vector search in the
same SQL statement. Transactional guarantees come free. At multi-million-chunk
scale, the tradeoffs shift and a managed service might win on operational
overhead. But for a prototype, pulling in a managed vector DB is
over-engineering, and it signals "I follow tutorials" rather than "I make
considered choices."

## "How do you handle PHI and HIPAA?"

This project uses MTSamples only — public, de-identified data — so HIPAA
doesn't technically apply. But I built it as if it did, because those habits
need to exist before real data shows up.

Specifically: API keys live only in a gitignored `.env` file, never in code.
Audit logs contain query hashes, not raw query text — because in production
queries might reference patient identifiers. All SQL is parameterized, so
query content can't be injected. Containers run as non-root. CORS is locked
down to the UI origin only. The health endpoint reveals nothing about
internals.

For a real deployment with PHI: BAA with Anthropic first, or swap the
generator for a self-hosted Llama via vLLM in a VPC. Add de-identification
in ingestion — NER for names, dates, MRNs. Encrypt the vector DB volume at
rest. Immutable audit logging to a WORM bucket. Role-based access controls
with short-lived credentials. Network segmentation with an egress proxy
logging every outbound API call.

## "Why didn't you fine-tune the embedder?"

Two reasons. First, without a labeled evaluation set big enough to measure
a real delta, fine-tuning is theatrical — you produce a model and a feeling,
not a measurable improvement. Second, the base model is already
transfer-learned twice: BERT → PubMedBERT for biomedical domain,
PubMedBERT → S-PubMedBERT-MS-MARCO for the sentence-embedding objective.
It's domain-appropriate out of the box.

That said, here's exactly what I'd do next if I had two more days. Generate
synthetic query-chunk pairs with Claude — maybe 1000-2000 triplets of
(query, relevant_chunk, irrelevant_chunk). Have a clinician validate a
random sample. Fine-tune with MultipleNegativesRankingLoss. Measure
precision@5 and MRR against the current baseline before shipping. If the
delta is under 3 points, keep the off-the-shelf model and spend the effort
elsewhere. That's the production mindset — measure, then decide.

## "What's the difference between a BERT embedder and a GPT-style LLM?"

Different architectures for different jobs. BERT is a bi-directional encoder
— every token attends to every other token in both directions, and the model
produces a single fixed-size vector per input. Trained with masked language
modeling and then contrastive objectives for embedders. It's good at "what
does this text mean?" in a single vector.

GPT-style models are autoregressive decoders — left-to-right, next-token
prediction. They're good at generation, not at producing single
representations. You can extract embeddings from them, but they're generally
worse than purpose-built bi-directional encoders for retrieval.

In this pipeline, the BERT-family encoder handles retrieval, the Claude
decoder handles generation. Right tool for each job.

## "How would you scale this to millions of notes?"

Two levers. Ingestion becomes embarrassingly parallel — PySpark or Ray
across a cluster, embedding in batches on GPU nodes. I'd keep pgvector for
moderate scale (tens of millions of chunks) with proper HNSW tuning and
partitioning, or move to a managed service like Databricks Vector Search
beyond that.

At query time, the bottleneck moves to two places: the ANN index (tune
`ef_search` for the precision/latency tradeoff), and the LLM call (add a
reranker so you can retrieve top-50 cheaply and reranker-narrow to top-5
before paying the generation cost). Prompt caching on the system prompt
saves real money at scale.

Monitoring becomes essential. Track retrieval precision, faithfulness,
latency percentiles, refusal rate. Drift in any of these is the first
signal that the index or the corpus has changed in ways that hurt quality.

## "How do you work with clinical stakeholders?"

Demo-driven. A Streamlit prototype in a meeting is worth ten slides.
Clinicians tell you almost instantly what's wrong with retrieval when they
can see both the answer and the sources side-by-side — which is why my UI
shows retrieved chunks alongside every answer. Transparency isn't a
nice-to-have in healthcare; it's the feature that makes clinicians trust
the tool enough to use it.

Iteration cycles: build minimum viable retrieval, show it, collect specific
failure cases from the clinicians, bake those into the eval set, improve
against the eval, show the delta. Repeat. Every "the system got this wrong"
becomes a test case that prevents regression.

## "What would you do differently next time?"

Three things, roughly in order.

Build the eval harness first. I built the pipeline and then the eval, which
meant I had to retrofit test queries around what the system already did.
Writing the eval first would have forced clearer thinking about what
"success" meant.

Invest earlier in reranking. Cross-encoder reranking on top-20 candidates
improves precision significantly for marginal latency cost. I left it as a
"next step" but it should have been in V1.

Make evaluation continuous. Right now `run_eval.py` is a one-shot script.
In production I'd want these metrics tracked over time, alerting on
regression, with a versioned eval set that grows as clinicians flag new
failure cases.

## Bridge lines for your existing experience

- **Data pipelines:** "I've built ETL pipelines in Python — ingestion here is
  the same pattern, with a neural network doing the transform step."
- **PostgreSQL:** "I know Postgres well, so pgvector let me reuse all my
  existing indexing and query optimization intuition."
- **FastAPI:** "This is my normal API stack — the only difference is the
  endpoint returns both the answer and the sources for auditability."
- **Docker:** "Standard multi-container setup. The interesting constraint
  was container hardening — non-root users, no secrets baked in."
- **Streamlit:** "I use Streamlit for demos that need to land with
  non-technical stakeholders. Here it's showing clinical users both the
  answer and the evidence."
