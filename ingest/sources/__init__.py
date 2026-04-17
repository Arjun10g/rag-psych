"""
Shared types and the Source protocol for pluggable ingestion.

Every data source (MTSamples, PubMed, ICD-11) implements a `Source` by
providing two iterators: one that yields RawDocuments, one that chunks
a RawDocument into Chunks. The top-level runner (`ingest/run.py`) wires
them together with batched embedding and bulk insertion.

This keeps sources independent — each lives in its own file, tests on its
own, and can be debugged without touching the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


@dataclass(frozen=True)
class RawDocument:
    """One source document before chunking.

    `source_id` must be stable across runs — it's the upsert key. For
    MTSamples, use the row index. For PubMed, the PMID. For ICD-11,
    the entity URI's final segment.
    """
    source_type: str           # 'mtsamples' | 'pubmed' | 'icd11'
    source_id: str
    title: str | None
    text: str
    license: str
    source_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    """One embeddable passage derived from a RawDocument."""
    section: str | None        # e.g. 'HPI', 'ABSTRACT', 'Clinical Descriptions'
    chunk_index: int           # ordering within the parent document
    text: str


class Source(Protocol):
    """Protocol every ingestion source implements.

    Not an ABC — we use structural typing so sources don't have to import
    or inherit from anything shared beyond these types.
    """

    source_type: str

    def load(self) -> Iterator[RawDocument]:
        """Yield raw documents from the source.

        Sources that fetch from the network (PubMed, ICD-11) should cache
        responses to `data/cache/{source_type}/` so re-runs don't hammer
        external APIs. Sources that read local files (MTSamples) just
        parse the file.
        """
        ...

    def chunk(self, doc: RawDocument) -> Iterator[Chunk]:
        """Split a document into chunks appropriate to its structure."""
        ...
