"""
MTSamples — de-identified medical transcription samples.

Source: https://www.kaggle.com/datasets/tboyle10/medicaltranscriptions
License: publicly posted de-identified transcriptions. We're using them
         for personal/educational RAG research. Cite the original source,
         don't claim ownership.

Filtering: the strict `medical_specialty == "Psychiatry / Psychology"` set
is only ~50 notes. We broaden by also pulling rows from any specialty whose
transcription/keywords/description mentions a psych-relevant term, using a
deliberately wide vocabulary (diagnoses, drug classes, named medications,
modalities). This catches psych content embedded in consults, SOAP notes,
discharge summaries, ER reports, etc.

Chunking strategy: regex split on ALL-CAPS section headers followed by a
colon ("HISTORY OF PRESENT ILLNESS:", "ASSESSMENT:", "PLAN:", etc.).
Fallback to recursive character splitting for notes without clear headers.
Each chunk is capped at 1500 chars; long sections sub-split with ~150-char
overlap so context isn't lost across the boundary.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import pandas as pd

from . import Chunk, RawDocument

PSYCH_KEYWORDS = {
    "depress", "depressive", "depressed", "anxiet", "anxious", "anxiolytic",
    "panic", "phobia", "ocd", "obsessive", "compulsive",
    "bipolar", "mania", "manic", "hypomania",
    "schizophren", "schizoaffective", "psychotic", "psychos",
    "hallucination", "delusion", "paranoia", "paranoid",
    "adhd", "attention deficit", "autism", "autistic", "asperger",
    "ptsd", "suicid", "self-harm", "self harm",
    "mental health", "mental illness", "mood disorder", "mood swing",
    "dysthym", "cyclothym", "borderline", "narcissistic", "antisocial",
    "personality disorder", "eating disorder", "anorexia", "bulimia",
    "insomnia", "dementia", "alzheimer", "cognitive", "cognition",
    "addiction", "substance abuse", "alcoholism",
    "opioid", "heroin", "methamphetamine", "cocaine", "marijuana", "cannabis",
    "antidepressant", "ssri", "snri", "tricyclic", "maoi",
    "antipsychot", "neuroleptic", "mood stabilizer",
    "lithium", "valproate", "lamotrigine", "benzodiazepine",
    "methylphenidate", "adderall", "ritalin",
    "fluoxetine", "sertraline", "paroxetine", "escitalopram", "citalopram",
    "venlafaxine", "duloxetine", "bupropion", "mirtazapine", "trazodone",
    "quetiapine", "risperidone", "olanzapine", "aripiprazole", "haloperidol",
    "clonazepam", "lorazepam", "alprazolam", "diazepam",
    "psychiat", "psycholog", "psychotherapy", "counseling",
    "cbt", "dbt", "ect", "electroconvulsive",
}

PSYCH_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in sorted(PSYCH_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

SECTION_HEADER_PATTERN = re.compile(r"([A-Z][A-Z0-9\s/&\-]{2,40}):")

CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP_CHARS = 150
SECTION_MIN_BODY_CHARS = 20


class MTSamplesSource:
    source_type = "mtsamples"

    def __init__(self, csv_path: Path | None = None) -> None:
        self.csv_path = csv_path or Path("data/mtsamples.csv")

    def load(self) -> Iterator[RawDocument]:
        """Yield psych-relevant rows from the MTSamples CSV.

        A row qualifies if its specialty matches Psychiatry/Psychology, or if
        any psych keyword appears in its transcription, keywords, or description
        fields. The Kaggle CSV's first column is an unnamed row index — we use
        it as the stable `source_id` so re-runs upsert rather than duplicate.
        """
        df = pd.read_csv(self.csv_path).dropna(subset=["transcription"])

        spec = df["medical_specialty"].fillna("").str.strip()
        is_psych_specialty = spec.str.contains("Psychiatry", case=False, na=False)

        text_blob = (
            df["transcription"].fillna("") + " "
            + df["keywords"].fillna("") + " "
            + df["description"].fillna("")
        )
        has_psych_keyword = text_blob.str.contains(PSYCH_PATTERN, na=False)

        keep = df[is_psych_specialty | has_psych_keyword]

        for _, row in keep.iterrows():
            source_id = str(row["Unnamed: 0"])
            title = _clean(row.get("sample_name")) or None
            description = _clean(row.get("description"))
            specialty = _clean(row.get("medical_specialty"))
            keywords = _clean(row.get("keywords"))

            yield RawDocument(
                source_type=self.source_type,
                source_id=source_id,
                title=title,
                text=row["transcription"],
                license="mtsamples-public-deidentified",
                source_uri=None,
                metadata={
                    "specialty": specialty,
                    "description": description,
                    "keywords": keywords,
                },
            )

    def chunk(self, doc: RawDocument) -> Iterator[Chunk]:
        """Section-aware chunking with a recursive-character fallback.

        MTSamples notes use inline ALL-CAPS section headers ending in a colon
        ("SUBJECTIVE:", "HISTORY OF PRESENT ILLNESS:"). When those exist we
        split on them and keep the header as the chunk's `section`. When a
        note has no recognizable headers, we fall back to fixed-size character
        splitting with overlap.
        """
        text = doc.text.strip()
        if not text:
            return

        sections = list(_split_by_sections(text))
        if not sections:
            for i, body in enumerate(_recursive_split(text)):
                yield Chunk(section=None, chunk_index=i, text=body)
            return

        idx = 0
        for header, body in sections:
            body = body.strip()
            if len(body) < SECTION_MIN_BODY_CHARS:
                continue
            for piece in _recursive_split(body):
                yield Chunk(section=header, chunk_index=idx, text=piece)
                idx += 1


def _clean(val: object) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and pd.isna(val):
        return ""
    return str(val).strip()


def _split_by_sections(text: str) -> Iterator[tuple[str, str]]:
    """Yield (header, body) tuples by splitting on ALL-CAPS headers.

    Returns empty if no headers are found (caller falls back to recursive
    character splitting). The first match's preceding text — if any — is
    discarded as preamble; clinical notes don't typically have meaningful
    pre-header content.
    """
    matches = list(SECTION_HEADER_PATTERN.finditer(text))
    if not matches:
        return

    for i, m in enumerate(matches):
        header = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].lstrip(" ,:;\n")
        yield header, body


def _recursive_split(text: str) -> Iterator[str]:
    """Split on sentence-ish boundaries first, falling back to char windows.

    Greedy pack into ≤CHUNK_MAX_CHARS windows, joining sentences until the
    next would exceed the cap. For pieces longer than the cap with no usable
    boundary, hard-window with CHUNK_OVERLAP_CHARS overlap so semantic
    context isn't sliced cleanly in half.
    """
    text = text.strip()
    if not text:
        return
    if len(text) <= CHUNK_MAX_CHARS:
        yield text
        return

    parts = re.split(r"(?<=[.!?])\s+", text)
    buf = ""
    for part in parts:
        if not part:
            continue
        if len(part) > CHUNK_MAX_CHARS:
            if buf:
                yield buf
                buf = ""
            yield from _hard_window(part)
            continue
        if not buf:
            buf = part
        elif len(buf) + 1 + len(part) <= CHUNK_MAX_CHARS:
            buf += " " + part
        else:
            yield buf
            buf = part
    if buf:
        yield buf


def _hard_window(text: str) -> Iterator[str]:
    step = CHUNK_MAX_CHARS - CHUNK_OVERLAP_CHARS
    for start in range(0, len(text), step):
        piece = text[start:start + CHUNK_MAX_CHARS]
        if piece:
            yield piece
        if start + CHUNK_MAX_CHARS >= len(text):
            break
