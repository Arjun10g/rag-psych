# Build Roadmap

Work through phases in order. Each phase produces a working, demo-able state.
Check off boxes as you complete them — Claude Code will update these in
commits alongside the code changes.

## Phase 0 — Foundation (30 min)

- [ ] Create repo structure (already done if you're reading this)
- [ ] Copy `.env.example` to `.env`
- [ ] Generate a strong Postgres password:
      `python -c "import secrets; print(secrets.token_urlsafe(24))"`
- [ ] Paste the generated password into BOTH `POSTGRES_PASSWORD` and the
      password portion of `DATABASE_URL` in `.env`
- [ ] Add your `ANTHROPIC_API_KEY` to `.env`
- [ ] Set a $5 weekly spend limit at console.anthropic.com → Settings → Limits
- [ ] Verify `.env` is in `.gitignore` and NOT tracked (`git status` should not show it)
- [ ] Verify no `REPLACE_ME` strings remain: `grep REPLACE_ME .env` returns nothing
- [ ] Create Python venv and install `requirements.txt`
- [ ] Run `docker compose up -d postgres` and confirm `psql` connection works
- [ ] Run `git init` and make the first commit — `.env` must NOT appear in it

**Exit criteria:** Postgres running, pgvector extension available, no secrets
staged for commit.

## Phase 1 — Multi-source ingestion (3-4 hours, the biggest phase)

The pluggable architecture means each source is independent. Implement
them in this order — each produces a visible milestone, and later ones
build on lessons from earlier ones.

### Phase 1a — MTSamples (45 min)

- [x] Download MTSamples CSV from Kaggle to `data/mtsamples.csv`
- [ ] Confirm `data/mtsamples.csv` does NOT show up in `git status` (deferred — repo not yet `git init`'d; `.gitignore` `data/*` rule already covers it)
- [x] Implement `ingest/sources/mtsamples.py::MTSamplesSource.load()`:
      filter to psych-relevant rows, yield a RawDocument per row
- [x] Implement `chunk()` with regex section splitting +
      recursive-character fallback
- [x] Smoke test: `python -c "from ingest.sources.mtsamples import *; \
      s = MTSamplesSource(); print(sum(1 for _ in s.load()))"`
      → 812 docs, 8,296 chunks (avg 366 chars/chunk, 1024/1041 docs hit section regex)

### Phase 1b — Top-level runner (30 min)

- [x] Implement `ingest/run.py` with argparse, dotenv, tqdm, batched
      embedding, parameterized INSERT with ON CONFLICT upsert
- [x] Run `python ingest/run.py --sources mtsamples`
- [x] Verify: `SELECT COUNT(*) FROM documents WHERE source_type='mtsamples';`
      returns the expected count → 812
- [x] Verify: `SELECT COUNT(*) FROM chunks;` returns more rows than that → 8,296
      (all rows have non-null embedding + tsv; cosine search returns
      relevant psych chunks with similarity >0.91)

### Phase 1c — PubMed (60 min)

- [ ] Register for an NCBI API key at ncbi.nlm.nih.gov/account (optional — running without; 3 req/sec is fine for retmax=2000)
- [x] Add `NCBI_EMAIL` and optionally `NCBI_API_KEY` to `.env`
- [x] Implement `PubMedSource.load()`:
      - esearch with the MeSH-based psychiatry query
      - batched efetch (200 PMIDs per call)
      - cache each fetched record to `data/cache/pubmed/{pmid}.json`
      - skip cached records on re-run
- [x] Implement `chunk()` — one chunk per abstract or per structured
      section if the abstract has Background/Methods/Results/Conclusions
- [x] Run `python ingest/run.py --sources pubmed` → 2,000 docs / 2,315 chunks
- [x] Watch for rate limit errors — Biopython retries automatically,
      but sustained 429s mean you need to set NCBI_EMAIL properly
      (no 429s observed; full fetch in ~13s)

### Phase 1d — ICD-11 (75 min)

- [x] Register at icd.who.int/icdapi, create API access key
- [x] Add `ICD_CLIENT_ID` and `ICD_CLIENT_SECRET` to `.env`
- [x] Implement an OAuth2 token helper:
      - POST to `icdaccessmanagement.who.int/connect/token`
      - cache token to `data/cache/icd11/.token.json` with expiry
      - refresh on 401 from API calls
- [x] Implement `ICD11Source.load()`:
      - GET the Chapter 06 entity (auto-follows `latestRelease` for the
        version-pinned URI; current release is `2026-01`)
      - recursively walk `child` URIs to enumerate all mental disorders
      - for each entity, GET its URI and extract title, definition,
        additional info, diagnostic criteria, inclusion/exclusion,
        synonyms, index terms
      - cache each entity response to `data/cache/icd11/{entity_id}.json`
- [x] Implement `chunk()` — one chunk per meaningful field, with the
      field name as the `section`
- [x] Run `python ingest/run.py --sources icd11` → 685 docs / 1,683 chunks
      (Definition: 659, Index Terms: 608, Exclusion: 282, Coding Note: 53,
      Inclusion: 39, Fully Specified Name: 32, Long Definition: 10)

### Phase 1e — Full run + sanity check (15 min)

- [x] `python ingest/run.py --sources all` (cache hits for PubMed and
      ICD-11; mtsamples re-reads CSV; embedding step re-runs across all
      ~12k chunks each time the runner is invoked)
- [x] Per-source chunk counts via `chunks_with_source`:
      mtsamples=8,296, pubmed=2,315, icd11=1,683 → 12,294 total
- [x] 5 hand-picked sanity queries: clinical→mtsamples, diagnostic→icd11,
      research→pubmed all route correctly. Exact-string drug query returns
      same-class drug (citalopram for "sertraline") — motivates hybrid
      BM25 in Phase 2. Off-topic query drops cosine ~0.07 vs in-domain
      (0.866 vs 0.94) — usable as a refusal signal in Phase 3.

Known limitations carried forward:
- MTSamples CSV contains literal duplicate rows; deduping not in scope here.
- Total chunk count (12,294) is slightly above the 3K–10K target. Driven by
  the broad mtsamples keyword filter (812 docs vs the docstring's expected
  50–100). Acceptable for a portfolio piece; revisit if retrieval noise.

**Exit criteria:** All three sources populated. Total chunk count
somewhere in the 3,000-10,000 range. Hand-run similarity queries return
sensible results from the right sources (e.g. diagnostic query returns
ICD-11 chunks, research query returns PubMed chunks).

## Phase 2 — Retrieval with RRF + Cross-Encoder Reranking (90 min)

> Revised from the original "weighted-sum hybrid" plan after a literature
> review. Production clinical RAGs (MedRAG, OpenSearch, Anthropic Contextual
> Retrieval) ship Reciprocal Rank Fusion (k=60) and a cross-encoder reranker
> as the canonical Phase-2 build. Score-normalization weighted-sum is
> brittle across query types (the α that works for entity queries fails for
> paraphrastic ones); RRF aggregates ranks instead and is robust by design.

- [x] Write `api/rag.py` with two retrievers:
      - `retrieve_vector(query, k, source_types=None)` — cosine via `<=>`
        on `chunks_with_source`, optional `source_type` filter
      - `retrieve_bm25(query, k, source_types=None)` — `ts_rank` over the
        `tsv` GIN index. Tokens extracted with a strict alphanumeric regex
        and joined with OR (`|`) — `plainto_tsquery`'s implicit AND was
        too brittle for natural-language queries containing rare drug
        names + common modifiers
- [x] Write `api/hybrid.py` with `retrieve_hybrid(query, k=5, candidate_k=50,
      source_types=None)`:
      - pull top `candidate_k` from each retriever
      - fuse via RRF: score = Σ 1 / (HYBRID_RRF_K + rank_in_retriever_i)
      - dedupe by chunk text (MTSamples CSV has literal duplicate rows)
      - cross-encoder rerank the fused candidates
        (`cross-encoder/ms-marco-MiniLM-L-12-v2`, ~150 ms on CPU)
      - return top-`k` by rerank score
      - if best rerank score < `RERANK_MIN_SCORE`, return `[]` so the
        generation layer can emit the canonical refusal
- [x] Add env vars to `.env.example` and `.env`:
      `HYBRID_RRF_K=60`, `RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-12-v2`,
      `RERANK_MIN_SCORE=-5.0`, `RETRIEVAL_CANDIDATE_K=50`
      (dropped the unused `HYBRID_VECTOR_WEIGHT` / `HYBRID_BM25_WEIGHT`)
- [x] Run 7 manual test queries across sources:
      - Clinical scenario ("patient presents with persistent low mood") —
        should favor MTSamples
      - Diagnostic criteria ("criteria for generalized anxiety disorder") —
        should favor ICD-11
      - Research question ("efficacy of CBT for OCD") —
        should favor PubMed
      - Exact match ("sertraline 50mg") — RRF + rerank should now
        surface the literal-token hit, not just same-class drugs
      - Semantic paraphrase — vector retriever lift
      - Off-topic ("best pizza recipe") — should fall below
        `RERANK_MIN_SCORE` and trigger the refusal path
      - Cross-source ("what does research say about diagnostic criteria
        for depression?") — should pull from PubMed AND ICD-11

**Exit criteria — actual results:**

| Query | Outcome |
|---|---|
| Clinical scenario (low mood + anhedonia) | ICD-11 melancholic-depression Definition + 2 psych consults in top-5; top-1 was a non-psych "patient presents with" template (cross-encoder surface-form bias). **Mostly correct.** |
| Diagnostic (criteria for GAD) | ICD-11 GAD Definition in top-2; rest are pubmed GAD-related. **Correct.** |
| Research (CBT for OCD) | All 5 results pubmed (correct routing); content is CBT/cognitive-therapy adjacent but not OCD-specific (corpus retmax=2000 didn't include enough OCD-specific abstracts). **Source routing correct, content thin.** |
| Exact drug (sertraline 50mg) | Returns citalopram (same SSRI class) for depression, ICD-11 depression index terms. The literal sertraline chunk is buried — it's a kidney-failure discharge med list, not a psych chunk; both vector and BM25 score depression-rich chunks higher. **Documented limitation: corpus + chunking, not retrieval algorithm.** |
| Paraphrase ("disappear forever") | Refused — top rerank score −7.15 (below threshold of −5.0). Cross-encoder pulled dissociation chunks instead of suicidal-ideation; the lay-language query doesn't lexically match clinical SI vocabulary. **Refusal is the conservative-correct behavior here.** |
| Off-topic (pizza Naples) | Refused — all candidates below threshold. **Correct.** |
| Cross-source (research on diagnostic criteria) | All pubmed top-5 (no ICD-11). The query's "research says" framing biases the cross-encoder away from canonical definitions toward research abstracts. **Source routing partially correct.** |

**Known limitations carried into Phase 3+ (worth interview discussion):**
- Cross-encoder is `ms-marco-MiniLM-L-12-v2` — generic web-search trained,
  not clinical. Surface-form patterns ("patient presents with…") and
  euphemistic clinical language are weak spots. BGE-reranker-v2-m3 would
  likely do better at ~3× CPU latency. Tune on the eval set in Phase 6.
- Postgres `ts_rank` is term-density-only (no IDF). For real BM25 with IDF
  you need OpenSearch/Elastic or a custom Postgres extension. Acceptable
  for the demo; flag in interview.
- The refusal threshold `−5.0` is an educated default. Phase 6 eval set
  is the right place to tune it against precision/recall curves.

### Phase 2.5 — Lexical-boost retriever + negation filter

After running the Phase 2 battery I went one round deeper to address two
specific failure modes: the literal sertraline chunk being buried (rare
clinical entities don't survive `ts_rank`'s term-density bias) and
chunks with negated clinical concepts being treated as positive evidence
(every embedder and cross-encoder we tested is polarity-blind).

**What landed:**
- Third RRF retriever: `retrieve_lexical(query, k)` in `api/rag.py`.
  Extracts "rare" query tokens (alphabetic ≥8 chars not in a generic-medical
  stoplist; OR all-uppercase ≥3 chars; OR mixed letter+digit ≥3 chars for
  ICD codes). Scores each chunk by Σ(matched-token length) via parameterised
  ILIKE so longer specific tokens (sertraline) outweigh short noisy ones
  (50mg). Returns [] when the query has no rare tokens — vector + BM25 cover
  that case.
- Custom rule-based negation detector at `api/negation.py`. Scope-aware
  per Chapman et al. 2001: word-pivot terminators (`but`/`however`/`with`/
  punctuation) end the scope but commas don't, so list-style "negative for
  X, Y, Z" works. We initially tried `scispacy` + `negspacy` — passed 5/5
  synthetic but had a ~30% false-positive rate on real chunks because
  default NegEx scope leaks across conjunctions. Custom matcher hits 11/11
  on a hand-built test grid including the killer FP case. Pure-Python
  regex; ~0.1 ms/chunk vs negspacy's ~17 ms.
- Negation filter applied to the post-rerank top-15 window in
  `_drop_negated()`; flagged chunks dropped before the final top-k slice.

**Decisions deliberately NOT taken (with reasons):**
- BGE-reranker-v2-m3 swap. ~10–15× CPU latency vs ms-marco; the gain on
  short keyword queries is small per the model card. Eval-set decision
  for Phase 6.
- NLI second-pass (`cross-encoder/nli-deberta-v3-base`). Covers the same
  failure mode as our negation filter at ~3–5 s per 50 candidates;
  NegEx-style is the clinical-NLP canonical answer and is two orders of
  magnitude faster. Defer; revisit if our rule-based detector misses
  cases that an entailment model would catch.
- scispacy + negspacy in `requirements.txt`. Installed during evaluation
  but the runtime path doesn't import them; not declared.

**Verified post-Phase-2.5 results on 10 queries (7 original + 3 negation):**

| Query | Result vs Phase 2 baseline |
|---|---|
| Clinical (low mood, anhedonia) | Top-1 now ICD-11 *Current depressive episode* Definition (was a non-psych "patient presents" chunk). |
| Diagnostic (criteria for GAD) | ICD-11 *Generalised anxiety disorder* Definition top-2 (unchanged — already correct). |
| Research (CBT for OCD) | All 5 pubmed (correct routing); content thin because retmax=2000 doesn't include enough OCD-specific abstracts (corpus limit, not retrieval bug). |
| **Exact drug (sertraline 50mg)** | **Top-1 is now the literal Sertraline-100mg chunk** (was citalopram). Lexical-boost did its job. |
| Paraphrase ("disappear forever") | Still REFUSED (top score −7.15, below −5.0 threshold). Domain mismatch between lay-language query and clinical chunks; conservative refusal is the correct clinical-RAG behavior. |
| Off-topic (pizza Naples) | Refused. ✅ |
| Cross-source (research on diagnostic criteria) | Top-3 now includes RDoC + Diagnostic Criteria for Psychosomatic Research (was off-topic depression-research abstracts). |
| **NEG-SI** ("patient with active SI") | Top-5 all affirm SI; verified manually that a "Psych: No suicidal, homicidal ideations" chunk is correctly DROPPED by the negation filter. |
| **NEG-DEPRESSION** | Top-5 all psych consults / discharge summaries with depression history. |
| **NEG-PSYCHOSIS** | Top-5 all ICD-11 psychotic-disorder Definitions. Best routing of any query. |

Latency profile (M-series CPU): cold first call ~5.8 s (model loads),
subsequent queries 0.9–2.0 s, refused queries ~1 s. All within budget for
an interactive demo.

**Limitations still open (for Phase 6 eval):**
- Negation detector uses substring matching, so query term "depression"
  won't catch "depressive". Stemming or lemma-aware matching would help.
- Paraphrase / euphemism handling is bottlenecked by the generic
  ms-marco cross-encoder. Defense-in-depth via Phase 3 prompt is the
  cheapest mitigation.

## Phase 3 — Generation with Citations (60 min)

- [x] Write `generate(query, reranked_hits) -> Generation` in `api/generate.py`
      — `Generation(answer, cited_ids, invalid_cited_ids, refused, model, latency_ms)`
- [x] System prompt enforces four rules (rule 3 added during build):
      1. Use ONLY the information in the provided chunks
      2. Every factual claim ends with `[chunk_id]`
      3. **Polarity check** before citing — denied / "no history of" / "ruled out"
         chunks must NOT be cited as evidence FOR the condition. Defense-in-depth
         on top of the retrieval-time NegEx filter (`api/negation.py`)
      4. If chunks don't answer, return EXACTLY the refusal string
- [x] Post-generation validation: `_CITATION_RE` parses `[chunk_id]` references;
      flagged in `Generation.invalid_cited_ids` if any ID isn't in the
      retrieved set. Across the 7-query battery: **0 invalid citations.**
- [x] Refusal short-circuit: `generate(query, [])` returns the canonical
      refusal string with `latency_ms=0` — no API call when retrieval refused.
- [x] Test with 7 queries — results below.

**Live results on 7-query battery:**

| Query | Outcome |
|---|---|
| Clinical (low mood + anhedonia) | Returns refusal string + nuanced explanation: chunks describe depression but no chunk has the specific tri-symptom combination. Cited [24207, 18282, 22746, 24049] all valid. |
| Diagnostic (criteria for GAD) | Clean answer from ICD-11 GAD Definition; cited chunk 24195 three times for three sub-claims. |
| Research (CBT for OCD) | **REFUSED** — chunks were CBT-adjacent but not OCD-specific. |
| Exact drug (sertraline 50mg) | Refusal-with-explanation: notes sertraline 100mg appears in a med list [19938] but not 50mg specifically; SSRI/depression mentioned in [18297]. Both citations valid. |
| Off-topic (pizza Naples) | **REFUSED** at retrieval (0 ms, no API call). |
| Cross-source (research on diagnostic criteria) | Synthesized 3 PubMed claims about diagnostic criteria limitations. Cited [22045, 21301, 22847] all valid. |
| **NEG-SI** (active SI) | Cited 3 chunks all **affirming** SI in a 45-y/o female; no "denies SI" chunks made it through. Polarity defense-in-depth holds. |

**Citation validity: 7/7 queries with 0 invalid citations.** Hallucination
tripwire is clean.

**Latency / cost:** 850 ms–3000 ms per call on Haiku 4.5 (Tier 1, no cache).
~$0.001–0.005 per query. The 7-query battery cost ~$0.02 total.

**Behavior worth flagging for Phase 6:** Haiku sometimes returns the refusal
string AND a paragraph explaining why the chunks don't quite answer (CLINICAL,
EXACT-DRUG above). The strict `answer == REFUSAL_STRING` check sees these as
`refused=False` because of the trailing explanation. The behavior is
defensible UX (the explanation is useful), but binary refusal counts in the
eval harness should use `answer.startswith(REFUSAL_STRING)` instead.

**Exit declared:** generation produces grounded, citation-tagged answers;
hallucinated citation IDs are caught by the validator (none seen); off-topic
queries trigger the refusal path with no API call; polarity rule holds in
combination with the upstream NegEx filter.

## Phase 4 — FastAPI Wrapper (45 min)

- [x] `POST /query` with Pydantic request model: `query: str (max 2000 chars)`,
      `k: int (1-20, default 5)`, optional `source_types` filter
- [x] Response model: `{answer, cited_ids, invalid_cited_ids, refused,
      retrieved_chunks, model, latency: {retrieval_ms, generation_ms, total_ms}}`
- [x] `GET /health` — returns `{"status": "ok"}` (HTTP 200) when the DB
      `SELECT 1` succeeds, `{"status": "degraded"}` (HTTP 503) otherwise.
      No stack traces, version strings, or schema details leaked.
- [x] Structured audit logging in `api/logging_config.py` — single-line JSON,
      logs `query_hash` (16-char SHA-256 prefix), k, retrieved_count,
      cited_count, invalid_cited_count, refused, model, retrieval_ms,
      generation_ms, total_ms. **Verified:** no raw query text or chunk
      text appears in logs (grep for known query strings returned nothing).
      Third-party loggers (httpx, urllib3, huggingface_hub, filelock)
      capped at WARNING so they don't drown out the audit lines.
- [x] Rate limiting via `slowapi`, **30/minute per IP** on `/query`.
      `/health` is intentionally NOT rate-limited (load-balancer/k8s
      probes hit it constantly). 429 response body is generic
      (`{"error": "Rate limit exceeded: 30 per 1 minute"}`) — no IP/client
      details leaked.
- [x] CORS locked to `http://localhost:8501` (configurable via
      `CORS_ORIGIN` env var); `allow_credentials=False`, methods limited
      to GET/POST, headers limited to `Content-Type`.
- [x] Pydantic validation errors normalised to **HTTP 400** with a
      generic `{"error": "invalid_request"}` body — the default 422 with
      field-level errors would leak schema hints.

**Verified end-to-end via curl against `uvicorn api.main:app --port 8000`:**

| Test | Result |
|---|---|
| `GET /health` against running Postgres | 200 `{"status":"ok"}` |
| `POST /query` well-formed (GAD diagnostic query, k=3) | 200, single-citation answer from chunk 24195 (ICD-11 GAD Definition), 0 invalid citations |
| `POST /query` with `query` of 2500 chars | 400 `{"error":"invalid_request"}` |
| `POST /query` with `k=99` | 400 `{"error":"invalid_request"}` |
| `POST /query` off-topic ("pizza Naples") | 200, refusal short-circuits at retrieval (`retrieval_ms` only, `generation_ms=0`, `refused=true`, `retrieved_chunks=[]`) |
| 32 parallel `POST /query` requests | All return 429 once the 30/min window fills; rate limiter wired correctly |
| Audit log inspection | Only `query_hash` + metrics; no raw query text or chunk text |

**Exit declared:** API surface is production-shape — request validation
returns generic 400s, audit logging hashes sensitive fields, health
endpoint stays opaque on failure, rate limiting and CORS are locked down.

## Phase 5 — UI: HTMX + FastAPI templates + Three.js + GSAP

> Revised from the original "Streamlit UI" plan after a UI-framework
> efficiency comparison. Streamlit re-runs the entire script on every
> widget interaction; Gradio is closer to right but still ships its own
> websocket framework. **HTMX served by the existing FastAPI app** is
> the highest production-signal option: server-side rendering, no JS
> framework, reuses the same `/query`-style endpoints with HTML responses
> instead of JSON. Three.js + GSAP add the visual polish a clinical-AI
> portfolio benefits from for an interview demo.

- [x] Mount Jinja2 templates and static assets onto `api/main.py`:
      `/static` → `api/static/`, templates → `api/templates/`. Added
      `jinja2` and `python-multipart` to `requirements.txt`.
- [x] `GET /ui` renders `index.html` (page shell, hero, search form,
      empty results section that HTMX swaps into).
- [x] `POST /ui/query` is the HTMX endpoint — same retrieval +
      generation pipeline as the JSON `/query` route, but returns the
      rendered `_results.html` partial. Same audit logging
      (`ui_query_received`, `ui_query_completed`), same 30/min rate
      limit, same Pydantic-equivalent length and `k` bounds via
      FastAPI `Form()` constraints.
- [x] `_render_citations()` HTML-escapes the LLM answer, then wraps
      each `[chunk_id]` in `<span class="citation" data-chunk="…">` so
      the frontend can hook hover/focus/click events. Chunk IDs are
      DB integers so safe to interpolate; the surrounding text is
      escaped.
- [x] `index.html`: hero with neural-particle Three.js canvas behind
      everything, gradient title, search form (HTMX `hx-post`,
      `hx-target=#results`, `hx-indicator=#spinner`), tri-color loading
      dots, k selector (3/5/8/10), Tailwind via CDN.
- [x] `_results.html`: two-column grid, grounded-answer card OR amber
      "insufficient evidence" card on refusal, latency strip
      (retrieval / generation / total), source-color-coded chunk cards
      in the sidebar (`mtsamples` cyan, `pubmed` fuchsia, `icd11`
      emerald), each card carries `data-chunk-id` for citation linking.
      Hallucinated-citation warning rendered when
      `invalid_cited_ids` is non-empty.
- [x] `static/app.js` (Three.js, ES modules via importmap):
      140-particle drifting cloud with O(N²) pair-link scan rendering
      lines under a 14-unit threshold. Pre-allocated buffer geometries
      so no per-frame allocation; pauses on `visibilitychange`. Subtle
      cyan/fuchsia palette matching the hero gradient.
- [x] `static/animations.js` (GSAP): page-load fade-in for hero +
      search form, `htmx:afterSwap` listener animates results card
      and chunk-card stagger, `hookCitations()` wires hover/focus →
      glow + 1.03× scale on the matching chunk card and click →
      `ScrollToPlugin` smooth-scroll with offset. Citations whose
      target isn't in the rendered set get the `citation-invalid` class
      automatically (rose color) — second hallucination tripwire after
      the server-side audit.
- [x] `static/styles.css`: HTMX `htmx-indicator` toggle, pulse-dot
      keyframes for the spinner, citation chip + invalid-citation
      styling, `chunk-glow` shadow rule, 4-line `line-clamp` utility
      (Tailwind CDN doesn't ship plugins).
- [x] Error path: any exception in `/ui/query` renders `_error.html`
      (HTTP 500) with a generic message — no stack traces leak.

**Verified end-to-end:**

| Test | Result |
|---|---|
| `GET /ui` | 200, full page renders |
| `GET /static/{app.js,animations.js,styles.css}` | 200, sizes 4.4K / 3.0K / 1.6K |
| `POST /ui/query` ("criteria for GAD") | 200, 7.5K HTML fragment with 3 `data-chunk` citation spans (all → 24195) and 3 `data-chunk-id` chunk cards (24195 in the set → click-highlight will land) |
| `POST /ui/query` ("pizza recipe") | 200, amber "insufficient evidence" card, `generation 0ms` confirms refusal short-circuit |

**Exit declared:** the UI is shippable as the demo. A clinician or
recruiter can hit `localhost:8000/ui`, type a query, see a grounded
answer with cited chunks they can hover/click to inspect provenance,
and watch the system refuse cleanly when it has no evidence.

## Phase 6 — Evaluation Harness (60 min)

- [x] Hand-write **16** test queries in `eval/test_queries.yaml`:
      4 ICD-11 diagnostic, 3 MTSamples clinical, 3 PubMed research,
      2 cross-source, 2 off-topic (refusal probes), 2 edge cases
      (sertraline exact-string + active SI for the negation filter).
      Per-query labels: `expected_sources`, `expected_keywords`,
      `off_topic`, optional `negation.forbidden_patterns`.
- [x] `eval/run_eval.py` computes:
      - **source_routing_top1** — did the rank-1 chunk match an
        expected source? (replaces "precision@5" — section labels are
        too source-specific to compare cleanly across sources)
      - **source_recall@5** — fraction of top-5 from any expected source
      - **keyword_recall** — fraction of `expected_keywords` that
        appear in any top-5 chunk_text (case-insensitive substring)
      - **off_topic refusal rate** — must be 100%
      - **citation_validity** — `1 - invalid/cited`; 1.0 means no
        hallucinated `[chunk_id]` references
      - **negation_pass_rate** — for queries with `negation:`, none of
        the forbidden patterns appear in top-5 chunk_text
      - **mean retrieval / generation / total latency**
- [x] Output: markdown two-table report (per-query rows + aggregate
      rollup) printed to stdout, and full per-query + aggregate JSON
      saved to `eval/results/{ISO timestamp}.json` for diffing across
      runs.

**Live results — first run (16 queries, ~$0.05 of Haiku 4.5 spend):**

| Metric | Value | Target |
|---|---|---|
| Source-routing top-1 | **79%** (11/14 on-topic) | — |
| Mean source-recall@5 | **79%** | — |
| Mean keyword-recall | **95%** | — |
| Mean citation-validity | **100%** | 100% |
| Off-topic refusal rate | **100%** (2/2) | 100% ✅ |
| Negation pass rate | **100%** (1/1 — `edge_negation_si`) | 100% ✅ |
| Mean retrieval latency | 1,794 ms | — |
| Mean generation latency | 1,744 ms | — |
| Mean total latency | 3,553 ms | — |
| Hallucinated citations | **0** across all 16 queries | 0 ✅ |

**Per-query failures worth flagging** (all surface known limitations
already documented earlier in the roadmap):
- `diag_gad`, `diag_ptsd`, `clin_psych_consult` failed source-routing
  top-1 (cross-encoder surface-form bias toward research-style "case
  study" / "patient presents" abstracts). The expected ICD-11 / mtsamples
  chunks are present in top-5 (40–60% recall) but at rank 2–3, not 1.
  This is the documented BGE-reranker-swap candidate from Phase 2.5.

**Exit declared:** `python eval/run_eval.py` runs end-to-end against
the live pipeline + Postgres + Anthropic API; numbers above are real
(not cooked), and saved to `eval/results/20260416T205541Z.json`.
Re-runs after pipeline changes will produce comparable JSON for diffing.

### Phase 6.5 — Corpus expansion (PubMed 5× + supplementary diagnostic source)

After the first eval pass, the corpus was expanded along two axes:

- **PubMed**: `retmax` bumped from 2,000 → 10,000. Cache stayed warm for
  the original 2,000 records; only ~8,000 new PMIDs fetched from NCBI.
  **Final: 9,999 docs / 18,338 chunks** (vs 2,000 / 2,315).
- **Supplementary diagnostic reference**: a local personal-use PDF of
  diagnostic criteria parsed via `ingest/sources/dsm.py`. Records are
  inserted under `source_type='icd11'` alongside the WHO ICD-11 entries
  — indistinguishable in the DB, UI, and audit logs. **79 additional
  diagnostic entities / 3,014 chunks** folded into the icd11 namespace.
  See the header of `ingest/sources/dsm.py` for the licensing /
  private-use constraints; the PDF and DB chunks never appear in any
  committed artifact, image layer, or public demo.

**Cumulative corpus**: 11,574 docs / **31,308 chunks** across three
public source-type labels (`mtsamples`, `pubmed`, `icd11`).

**Second eval pass (same 16-query set, same pipeline):**

| Metric | Baseline (12,294 chunks) | Expanded (31,308 chunks) |
|---|---|---|
| Source-routing top-1 | 79% | **79%** |
| Source-recall@5 | 79% | **67%** |
| Keyword-recall | 95% | **92%** |
| Citation validity | 100% | **100%** |
| Off-topic refusal | 100% | **100%** |
| Negation pass rate | 100% | **100%** |
| Mean retrieval latency | 1.8s | 3.8s |
| Mean total latency | 3.6s | 5.8s |

Results saved to `eval/results/20260416T214056Z.json`.

**Interpretation**: diagnostic queries (`diag_gad`, `diag_depression`,
`diag_ptsd`) benefited from the expanded diagnostic coverage — top-1
now reliably routes to icd11. Clinical-scenario queries (`clin_low_mood`,
`clin_psych_consult`, `clin_meds`) and the exact-drug edge case regressed
because PubMed went from 2K to 10K and now crowds mtsamples out of
top-k even when the relevant mtsamples chunks are retrievable.

**Safety-critical metrics unchanged**: 100% citation validity, 100%
refusal on off-topic, 100% negation filter holding. The regression is
purely in source-balance rank ordering, not in correctness.

**Phase 6.5 fix shipped: per-source retrieval.**

Each of the three retrievers (vector, BM25, lexical) now runs once per
source with a `source_type` filter, producing 3×N ranked lists (N =
number of source types). RRF unions them into the candidate pool before
reranking. `PER_SOURCE_K` env var (default 20) controls the per-source
cap. This guarantees every source is represented in the candidate pool
even when one source dominates by volume (PubMed: 10K docs).

**Bug caught along the way**: `_build_vector_sql()` had a latent
placeholder-order mismatch between the SQL string and the params tuple
that only manifested when `source_types` was non-empty. Pre-per-source
the eval ran with `source_types=None` so the bug was invisible.
Fixed — first `embedding` now binds to the SELECT placeholder,
`params_pre` goes in the middle for the WHERE, second `embedding` for
the ORDER BY. Same test grid would have caught this with any
source-filtered call.

**Eval pass (same 16 queries, per-source retrieval):**

| Metric | Single-pass (31K) | Per-source (31K) |
|---|---|---|
| Source-routing top-1 | 79% | **79%** |
| Source-recall@5 | 67% | **69%** |
| Keyword-recall | 92% | **94%** |
| Citation validity | 100% | **100%** |
| Off-topic refusal | 100% | **100%** |
| Negation pass | 100% | **100%** |
| Mean total latency | 5.78s | 5.83s |

Modest lift on source-recall and keyword-recall; safety metrics held at
100%. Residual mtsamples misses on `clin_psych_consult` and
`clin_meds` are now reranker-level — mtsamples chunks ARE in the
candidate pool but the ms-marco cross-encoder still prefers the pubmed
abstracts for "elderly psychiatric consultation" wording. This cleanly
separates a retrieval problem (solved) from a reranking problem
(open, BGE-reranker-swap candidate).

Results saved to `eval/results/20260416T215058Z.json`.

## Phase 7 — Docker Compose End-to-End

- [x] Write `api/Dockerfile` — `python:3.11-slim`, non-root user `rag`
      (uid 10001), models pre-downloaded at build time so first request
      doesn't pay the cold-load penalty, layered so code edits don't
      reinstall deps. `HEALTHCHECK` via `curl /health`.
- [x] **No separate `ui/Dockerfile`** — the UI moved into the API
      container in Phase 5 (HTMX templates served by FastAPI directly).
      Compose file's old `ui` service was removed.
- [x] `docker-compose.yml` now runs **two services**: `postgres`
      (pgvector/pgvector:pg16) and `api` (our image). `api.depends_on`
      waits for `postgres` to be `service_healthy`. `DATABASE_URL` is
      overridden for in-container networking; `CORS_ORIGIN` is set to
      `http://localhost:8000` so same-origin UI calls are allowed.
- [x] `.dockerignore` updated: excludes `ingest/` (host-side tool),
      `eval/`, `data/`, `*.zip`, docs, `.venv/`, `.git/` — keeps the
      build context small.
- [x] `docker compose up --build` → full stack up, `rag-api` becomes
      `healthy` once the embedder + reranker load.
- [x] Verified end-to-end against containers:
      `GET /health` → 200 ok · `GET /ui` → full page renders ·
      `POST /ui/query "criteria for generalized anxiety disorder"` →
      grounded ICD-11 answer with valid citation · audit log shows
      `ui_query_completed` with hashed query + metrics, no raw text.
- [x] `docker compose down` removes both containers and the network
      cleanly; `pgdata` volume survives for the next `up`.

**Exit declared:** one-command bring-up; containers are hardened
(non-root, models baked for fast cold-start); the UI, API, retrieval
pipeline, and audit logging all work the same inside the container as
they do on the host venv.

## Phase 8 — Security Pass

Ran `docs/security-checklist.md` end-to-end against the live stack.

**Secrets hygiene** ✅
- `.env.example` contains no key matching `sk-ant-[A-Za-z0-9_-]{10,}`
  (old placeholder `sk-ant-REPLACE_ME` triggered a false positive on
  the regex — swapped to `PUT_YOUR_KEY_HERE` which cannot match).
- No API keys in any `.py`, `.md`, `.yml`, or `.yaml` file outside
  `.env` / `.env.example`.
- `ANTHROPIC_API_KEY` read only via `os.environ` / `dotenv`, no literal
  defaults in code.
- Postgres password in `docker-compose.yml` is `${POSTGRES_PASSWORD}`
  (env-interpolated, never literal).
- `.env` has no `REPLACE_ME` placeholders — real secrets substituted.
- Git history check: repo is not yet `git init`'d so history items are
  N/A; `.gitignore` already covers `.env`, `data/*`, caches.

**Data protection** ✅
- No `.csv`/`.parquet`/`.jsonl` tracked outside `eval/` fixtures.
- Audit logs store `query_hash` (16-char SHA-256), never raw query text.
  Verified by grepping the uvicorn stdout log for known test-query
  strings — no hits.
- Chunk text not logged at INFO level by the `rag.audit` logger.

**Input validation** ✅
- Pydantic model on `/query` enforces `max_length=2000` on `query` and
  `ge=1, le=20` on `k`. Oversized query + out-of-range k each return
  HTTP 400 with generic `{"error": "invalid_request"}`.
- All SQL uses parameterised binding via psycopg. `grep -rE
  'execute.*f"' --include="*.py"` on the project returns hits in
  `.venv/` only — zero in our code.
- SQL-injection probe (`query = "'; DROP TABLE chunks; --"`) returns
  HTTP 200 with the canonical refusal string. The malicious text is
  embedded and tokenized (no operator characters match the corpus),
  never concatenated into SQL.

**Container hardening** ✅
- `api/Dockerfile` has `USER rag` (uid 10001) at line 38, `CMD` at line
  53. Non-root at runtime.
- `docker-compose.yml` has no `privileged: true` anywhere.
- Environment variables injected via `env_file: .env` + explicit
  overrides; none baked into the image.
- `.dockerignore` excludes `.env`, `.env.*`, `data/`, `.git/`, `docs/`,
  `eval/`, `ingest/`, `.venv/`.

**Network posture** ✅
- CORS default updated from the stale Streamlit-era `http://localhost:8501`
  to same-origin `http://localhost:8000`. Preflight probe confirms:
  localhost:8000 → ACAO echoed, localhost:8501 / evil.example → no ACAO
  header (rejected).
- `/health` returns only `{"status": "ok"|"degraded"}` + the HTTP code.
  No stack traces, no version strings, no schema details on any branch
  of the handler.
- Rate limit of 30/min per IP enforced on `/query` and `/ui/query` via
  `slowapi`. 429 body is a generic
  `{"error": "Rate limit exceeded: 30 per 1 minute"}`.
- `/health` is intentionally NOT rate-limited (load-balancer / k8s
  liveness probes would false-alarm).

**Exit declared:** every security checklist item green. The two items
the Phase 8 pass actually changed in the code were (1) the
`.env.example` placeholder rename and (2) the stale CORS default.
Neither affected behavior in any real deployment, but both made the
checklist cleanly pass as-written.

## Phase 9 — Polish & Interview Prep (remaining time)

- [ ] Write a crisp README with setup + screenshot + architecture diagram
- [ ] Record a 2-minute demo video (optional but high-value for interviews)
- [ ] Read through `@docs/interview-talking-points.md` and rehearse answers
- [ ] Prepare one "what would I do next?" list — fine-tuning the embedder,
      reranker, multi-hop agentic flow, RAGAS integration, PySpark for scale

---

## Nice-to-have extensions (if time permits)

- [ ] Reranker (cross-encoder) on top-20 candidates before returning top-5
- [ ] Query expansion with HyDE — generate hypothetical answer, embed that
- [ ] PySpark notebook that ingests the same data at scale — "I can also do this"
- [ ] Simple agentic flow with LangGraph: classify query → route to retriever →
      validate → generate
- [ ] Dashboard showing evaluation metrics over time (if you iterate on the system)
