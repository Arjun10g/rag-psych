"""
Clinical negation detection — pure-Python rule-based, terminator-aware.

Used as a post-rerank filter in `api/hybrid.py` to drop chunks where the
query's clinical concept appears in a negated context. A query about
"patient with suicidal ideation" should not retrieve a chunk that says
"patient denies suicidal ideation" as supporting evidence.

We initially tried `scispacy` + `negspacy` (NegEx algorithm). It worked
on synthetic cases (5/5) but had a high false-positive rate on real
clinical chunks because the default NegEx scope detection over-extends
through conjunctions — e.g. "with no children now presenting with
recurrent depressive symptoms and active suicidal ideation" leaks the
"no children" trigger forward to flag the affirmed "suicidal ideation".
We switched to a tighter custom matcher with word-only scope terminators
(no commas, since list-style negations like "negative for X, Y, Z" are
common in clinical text and the comma is part of the list, not a scope
break). Test grid: 11/11 PASS, including the FP killer above.

Approach (Chapman et al. 2001, simplified):
  1. Lowercase the chunk text once.
  2. For each negation trigger (longest first to handle multi-word
     triggers like "no history of"), find every occurrence respecting
     word boundaries.
  3. Scope = up to 100 chars after the trigger, truncated at the next
     terminator: a logical pivot ("but", "however", "with", "despite"...)
     or a sentence-ending punctuation (`.` `;`). Commas do NOT terminate
     scope so list-style negations work.
  4. If any salient query term is a substring of that scope, return True.

Latency: ~0.1 ms/chunk (regex-only, no NLP parse). Two orders of
magnitude faster than the negspacy path it replaced.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-z]+")

# Multi-word triggers must come before their single-word prefixes
# (e.g. "no history of" before "no") so the longest match wins.
_TRIGGERS: tuple[str, ...] = (
    "no history of", "no evidence of", "no signs of", "no indication of",
    "denies", "denied", "denying",
    "ruled out", "rule out",
    "negative for",
    "without any", "without",
    "never had", "never",
    "not a", "not",
    "no",
)

# Word-pivot terminators end the negation scope. Commas are intentionally
# excluded — clinical lists ("negative for tremors, headaches, depression")
# carry the negation through the whole list.
_TERMINATORS = re.compile(
    r"\b(but|however|although|except|aside\s+from|despite|with)\b|[;.]",
    re.IGNORECASE,
)

_SCOPE_CHAR_LIMIT = 100

_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "for", "with", "of", "in", "on", "to", "and", "or",
    "what", "does", "is", "are", "were", "was", "be", "been", "being",
    "patient", "patients", "any", "some", "this", "that", "these", "those",
    "do", "did", "doing", "have", "has", "had", "having", "from", "as",
    "at", "by", "about", "into", "through", "between", "active",
})


def salient_query_terms(query: str) -> list[str]:
    """Tokens worth checking for negation: ≥4-char alphabetic, non-stopword.

    Short words ("no", "as") are too generic to safely substring-match
    against chunk text. Most clinical concepts of interest (depression,
    suicidal, anxiety, ideation, psychotic) are well above the 4-char floor.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(query):
        low = raw.lower()
        if len(low) < 4 or low in _QUERY_STOPWORDS or low in seen:
            continue
        out.append(low)
        seen.add(low)
    return out


def is_negated(chunk_text: str, query_terms: list[str]) -> bool:
    """True iff any salient query term appears within a negation scope."""
    if not chunk_text or not query_terms:
        return False
    terms = [t.lower().strip() for t in query_terms if t and t.strip()]
    if not terms:
        return False
    text = chunk_text.lower()
    for trigger in _TRIGGERS:
        idx = 0
        while True:
            pos = text.find(trigger, idx)
            if pos < 0:
                break
            if pos > 0 and text[pos - 1].isalnum():
                idx = pos + 1
                continue
            after = pos + len(trigger)
            if after < len(text) and text[after].isalnum():
                idx = pos + 1
                continue
            scope_text = text[after:after + _SCOPE_CHAR_LIMIT]
            terminator = _TERMINATORS.search(scope_text)
            scope = scope_text[:terminator.start()] if terminator else scope_text
            if any(t in scope for t in terms):
                return True
            idx = after
    return False
