"""
Top-level ingest runner.

Usage:
  python ingest/run.py --sources mtsamples pubmed icd11
  python ingest/run.py --sources mtsamples                # just one source
  python ingest/run.py --sources all                      # everything

The runner is deliberately thin. It:
  1. Instantiates each requested Source plugin
  2. Iterates load() → chunk() → accumulates chunks
  3. Batch-embeds chunks (CPU-friendly batch size of ~32)
  4. Upserts documents and bulk-inserts chunks

Each source handles its own fetching, caching, and chunking. The runner
doesn't know anything source-specific.

Embedding model is loaded ONCE — all sources share it, and the same model
is used at query time (see api/rag.py). If you change EMBEDDING_MODEL
in .env, you MUST re-run ingest for all existing chunks.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterator

import numpy as np
import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from psycopg.types.json import Json
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.sources import Chunk, RawDocument, Source  # noqa: E402

# `dsm5` is deliberately excluded from ALL_SOURCES: it ingests copyrighted
# text from a local PDF for private use only and must be opted into
# explicitly with `--sources dsm5`. See ingest/sources/dsm.py.
ALL_SOURCES = ("mtsamples", "pubmed", "icd11")
EMBED_BATCH_SIZE = 32
DEFAULT_MODEL = "pritamdeka/S-PubMedBert-MS-MARCO"


def main() -> None:
    args = _parse_args()
    load_dotenv()

    requested = _expand_sources(args.sources)
    db_url = os.environ["DATABASE_URL"]
    model_name = os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)

    print(f"loading embedding model: {model_name}")
    model = SentenceTransformer(model_name)

    summary: dict[str, tuple[int, int]] = {}
    with psycopg.connect(db_url) as conn:
        register_vector(conn)
        for name in requested:
            src = _get_source(name)
            if src is None:
                summary[name] = (0, 0)
                continue
            summary[name] = _ingest(conn, src, model)

    _print_summary(summary)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest sources into the RAG pipeline.")
    p.add_argument(
        "--sources",
        nargs="+",
        required=True,
        choices=list(ALL_SOURCES) + ["all", "dsm5"],
        help="Source plugins to run, or 'all' (dsm5 is opt-in, not in 'all').",
    )
    return p.parse_args()


def _expand_sources(requested: list[str]) -> list[str]:
    if "all" in requested:
        return list(ALL_SOURCES)
    return list(dict.fromkeys(requested))


def _get_source(name: str) -> Source | None:
    """Lazy-import so an unimplemented source doesn't block the others."""
    try:
        if name == "mtsamples":
            from ingest.sources.mtsamples import MTSamplesSource
            return MTSamplesSource()
        if name == "pubmed":
            from ingest.sources.pubmed import PubMedSource
            return PubMedSource()
        if name == "icd11":
            from ingest.sources.icd11 import ICD11Source
            return ICD11Source()
        if name == "dsm5":
            from ingest.sources.dsm import DSMSource
            return DSMSource()
    except NotImplementedError as e:
        print(f"  ! {name}: not yet implemented ({e}), skipping")
    return None


def _ingest(
    conn: psycopg.Connection, src: Source, model: SentenceTransformer
) -> tuple[int, int]:
    print(f"\n=== {src.source_type} ===")
    pairs = list(_load_and_chunk(src))
    if not pairs:
        print("  no documents")
        return 0, 0

    n_docs = len(pairs)
    n_chunks = sum(len(c) for _, c in pairs)
    print(f"  {n_docs} documents, {n_chunks} chunks")

    embeddings = _embed_all(model, pairs)
    return _write(conn, src.source_type, pairs, embeddings)


def _load_and_chunk(src: Source) -> Iterator[tuple[RawDocument, list[Chunk]]]:
    try:
        for doc in tqdm(src.load(), desc=f"load {src.source_type}", unit="doc"):
            chunks = list(src.chunk(doc))
            if chunks:
                yield doc, chunks
    except NotImplementedError as e:
        print(f"  ! {src.source_type}: load() not implemented ({e})")


def _embed_all(
    model: SentenceTransformer, pairs: list[tuple[RawDocument, list[Chunk]]]
) -> np.ndarray:
    texts = [c.text for _, chunks in pairs for c in chunks]
    print(f"  embedding {len(texts)} chunks (batch size {EMBED_BATCH_SIZE})...")
    return model.encode(
        texts,
        batch_size=EMBED_BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    )


def _write(
    conn: psycopg.Connection,
    source_type: str,
    pairs: list[tuple[RawDocument, list[Chunk]]],
    embeddings: np.ndarray,
) -> tuple[int, int]:
    """Upsert documents and replace their chunks in a single transaction."""
    inserted_docs = 0
    inserted_chunks = 0
    idx = 0
    with conn.transaction(), conn.cursor() as cur:
        for doc, chunks in tqdm(pairs, desc=f"upsert {source_type}", unit="doc"):
            doc_id = _upsert_document(cur, doc)
            cur.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            rows = [
                (doc_id, c.section, c.chunk_index, c.text, embeddings[idx + i])
                for i, c in enumerate(chunks)
            ]
            cur.executemany(
                "INSERT INTO chunks (document_id, section, chunk_index, chunk_text, embedding) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
            )
            idx += len(chunks)
            inserted_docs += 1
            inserted_chunks += len(rows)
    print(f"  upserted {inserted_docs} documents, {inserted_chunks} chunks")
    return inserted_docs, inserted_chunks


def _upsert_document(cur: psycopg.Cursor, doc: RawDocument) -> int:
    cur.execute(
        """
        INSERT INTO documents (source_type, source_uri, source_id, title, license, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_type, source_id) DO UPDATE SET
            source_uri = EXCLUDED.source_uri,
            title      = EXCLUDED.title,
            license    = EXCLUDED.license,
            metadata   = EXCLUDED.metadata,
            fetched_at = NOW()
        RETURNING id
        """,
        (
            doc.source_type,
            doc.source_uri,
            doc.source_id,
            doc.title,
            doc.license,
            Json(doc.metadata),
        ),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"upsert returned no id for {doc.source_type}:{doc.source_id}"
        )
    return row[0]


def _print_summary(summary: dict[str, tuple[int, int]]) -> None:
    print("\n=== summary ===")
    for name, (n_docs, n_chunks) in summary.items():
        print(f"  {name:12s}  {n_docs:6d} docs   {n_chunks:7d} chunks")


if __name__ == "__main__":
    main()
