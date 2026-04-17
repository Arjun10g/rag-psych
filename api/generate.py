"""
Generation: call Claude with retrieved chunks, force citations, validate.

The retrieval layer (api/hybrid.py) returns either a ranked list of
RerankedHit objects or `[]` when the best chunk falls below the refusal
threshold. We mirror that contract:

  - Empty hits → return the canonical refusal string without an API call.
    Saves money and guarantees identical refusal behavior across paths.
  - Non-empty → ask Claude to answer from those chunks only, with
    `[chunk_id]` citations after every factual claim. Post-generation we
    parse the cited IDs and flag any that don't appear in the retrieved
    set — that's our hallucination tripwire.

Polarity handling lives in the system prompt as defense-in-depth on top of
the retrieval-time NegEx filter (`api/negation.py`): if a denied/negated
chunk somehow survives RRF + rerank + NegEx, the model is still instructed
not to cite it as positive evidence.

Single-turn for now; Phase 4 wraps this in a FastAPI endpoint with audit
logging. Phase 6 will call it many times from the eval harness — the
system prompt is well below Haiku 4.5's 4096-token cache minimum so
prompt caching isn't worth wiring up here.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import anthropic

from .hybrid import RerankedHit

REFUSAL_STRING = "The provided notes do not contain information to answer this."

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 1024

_CITATION_RE = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = """You are a clinical reference assistant for a portfolio RAG demo.
You answer questions strictly from the numbered context chunks the user provides — nothing else.

RULES (follow exactly, in order):

1. Use ONLY the information in the numbered context chunks. Do not draw on outside
   knowledge, even if you know it from training.

2. Every factual claim in your answer must be followed by a citation in square
   brackets giving the chunk id, e.g. "Generalised anxiety disorder is characterised
   by marked symptoms of anxiety [42]." If multiple chunks support one claim, cite
   all of them: "[42][57]".

3. Polarity check before citing. If a chunk states the patient does NOT have, denies,
   or has no history of a condition, do NOT cite it as evidence FOR that condition.
   The patient denying X is evidence about the patient's denial, not about the
   presence of X. The same applies to "ruled out", "negative for", and "without".

4. If the chunks do not contain enough information to answer the question, respond
   with EXACTLY this string and nothing else:
   "The provided notes do not contain information to answer this."

   Use this when:
     - No chunk addresses the question
     - The chunks are about adjacent topics but don't answer the specific question
     - The chunks contradict each other and you cannot resolve the contradiction

Output format: prose, complete sentences, citations inline. Aim for 2-4 sentences
for most queries — concise is better than padded."""


@dataclass(frozen=True)
class Generation:
    answer: str
    cited_ids: list[int]
    invalid_cited_ids: list[int]
    refused: bool
    model: str
    latency_ms: float


def generate(
    query: str,
    reranked_hits: list[RerankedHit],
    *,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Generation:
    """Call Claude with retrieved chunks; return answer + citation audit.

    `reranked_hits=[]` short-circuits to the refusal path without an API
    call. The `refused` field is True when the model returns the exact
    refusal string (or when we short-circuited).
    """
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    if not reranked_hits:
        return Generation(
            answer=REFUSAL_STRING,
            cited_ids=[],
            invalid_cited_ids=[],
            refused=True,
            model=model,
            latency_ms=0.0,
        )

    user_msg = _build_user_message(query, reranked_hits)
    client = anthropic.Anthropic()
    t0 = time.perf_counter()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    latency_ms = (time.perf_counter() - t0) * 1000

    answer = "".join(b.text for b in response.content if b.type == "text").strip()
    retrieved_ids = {h.hit.chunk_id for h in reranked_hits}
    cited = [int(m) for m in _CITATION_RE.findall(answer)]
    cited_unique = list(dict.fromkeys(cited))
    invalid = [c for c in cited_unique if c not in retrieved_ids]
    refused = (answer == REFUSAL_STRING)

    return Generation(
        answer=answer,
        cited_ids=cited_unique,
        invalid_cited_ids=invalid,
        refused=refused,
        model=model,
        latency_ms=latency_ms,
    )


def _build_user_message(query: str, hits: list[RerankedHit]) -> str:
    blocks = []
    for r in hits:
        h = r.hit
        provenance = h.source_type
        if h.section:
            provenance += f" / {h.section}"
        if h.title:
            provenance += f" / {h.title}"
        blocks.append(f"[{h.chunk_id}] ({provenance})\n{h.chunk_text}")
    chunks_text = "\n\n".join(blocks)
    return f"CONTEXT CHUNKS:\n\n{chunks_text}\n\nQUESTION: {query}"
