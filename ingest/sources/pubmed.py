"""
PubMed — peer-reviewed biomedical abstracts via NCBI E-utilities.

Source: https://www.ncbi.nlm.nih.gov/books/NBK25497/  (Entrez docs)
License: abstracts are effectively public — NLM allows unrestricted reuse
         of bibliographic metadata and abstracts. Cite NCBI/PubMed.

Auth: optional. No key = 3 req/sec. With key = 10 req/sec.
      Get a key at https://www.ncbi.nlm.nih.gov/account/settings/ → API Keys.
      Set NCBI_API_KEY and NCBI_EMAIL in .env.

Search: MeSH-based psychiatry/psychology query, English, has-abstract.

Caching: every fetched record is written to
  data/cache/pubmed/{pmid}.json
as a small JSON dict. Re-runs read from cache and never re-hit NCBI for
records we've already seen. The cache is the source of truth on re-runs.

Chunking: most abstracts fit in one chunk. Structured abstracts
(Background/Methods/Results/Conclusions) split into one chunk per section
so retrieval can return just the relevant slice.

DO NOT:
  - Skip setting Entrez.email — NCBI will block anonymous bulk traffic.
  - Fetch full-text articles — PMC has per-article licensing. Stick to abstracts.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator

from Bio import Entrez

from . import Chunk, RawDocument

logger = logging.getLogger(__name__)

EFETCH_BATCH_SIZE = 200
CHUNK_MAX_CHARS = 1500
CHUNK_OVERLAP_CHARS = 150
DEFAULT_SEARCH_TERM = (
    '("Mental Disorders"[MeSH] OR "Psychology"[MeSH]) '
    'AND hasabstract[text] AND English[lang]'
)


class PubMedSource:
    source_type = "pubmed"

    def __init__(
        self,
        search_term: str = DEFAULT_SEARCH_TERM,
        retmax: int = 10000,
        cache_dir: Path | None = None,
    ) -> None:
        self.search_term = search_term
        self.retmax = retmax
        self.cache_dir = cache_dir or Path("data/cache/pubmed")

    def load(self) -> Iterator[RawDocument]:
        """Yield one RawDocument per PubMed record matching the search.

        Reads cached records from disk; fetches uncached PMIDs in batches
        of 200 and writes each to its own JSON file. NCBI requires
        Entrez.email — we read it from the NCBI_EMAIL env var.
        """
        _configure_entrez()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        pmids = _esearch(self.search_term, self.retmax)
        logger.info("PubMed esearch returned %d PMIDs", len(pmids))

        uncached = [p for p in pmids if not (self.cache_dir / f"{p}.json").exists()]
        if uncached:
            logger.info("fetching %d uncached records (batch=%d)",
                        len(uncached), EFETCH_BATCH_SIZE)
            _efetch_and_cache(uncached, self.cache_dir)

        for pmid in pmids:
            cache_file = self.cache_dir / f"{pmid}.json"
            if not cache_file.exists():
                continue
            record = json.loads(cache_file.read_text())
            yield _record_to_document(pmid, record)

    def chunk(self, doc: RawDocument) -> Iterator[Chunk]:
        """One chunk per labelled section, or one chunk for the full abstract.

        Long sections sub-split with overlap. Structured-abstract labels
        come from the source XML's `<AbstractText Label="...">` attributes
        and are stored in `metadata['abstract_sections']` by load().
        """
        sections = doc.metadata.get("abstract_sections") or []
        idx = 0
        if sections:
            for label, body in sections:
                body = (body or "").strip()
                if not body:
                    continue
                for piece in _split(body):
                    yield Chunk(section=label, chunk_index=idx, text=piece)
                    idx += 1
            return
        for piece in _split(doc.text):
            yield Chunk(section="ABSTRACT", chunk_index=idx, text=piece)
            idx += 1


def _configure_entrez() -> None:
    email = os.environ.get("NCBI_EMAIL", "").strip()
    if not email or email == "you@example.com":
        raise RuntimeError(
            "NCBI_EMAIL must be set to a real address in .env — "
            "NCBI requires it and will block anonymous bulk traffic."
        )
    Entrez.email = email
    api_key = os.environ.get("NCBI_API_KEY", "").strip()
    if api_key:
        Entrez.api_key = api_key


def _esearch(term: str, retmax: int) -> list[str]:
    handle = Entrez.esearch(
        db="pubmed", term=term, retmax=retmax, sort="relevance"
    )
    try:
        result = Entrez.read(handle)
    finally:
        handle.close()
    return list(result.get("IdList", []))


def _efetch_and_cache(pmids: list[str], cache_dir: Path) -> None:
    for start in range(0, len(pmids), EFETCH_BATCH_SIZE):
        batch = pmids[start:start + EFETCH_BATCH_SIZE]
        handle = Entrez.efetch(
            db="pubmed", id=",".join(batch), retmode="xml", rettype="abstract"
        )
        try:
            parsed = Entrez.read(handle)
        finally:
            handle.close()
        for article in parsed.get("PubmedArticle", []):
            record = _extract_record(article)
            pmid = record.get("pmid")
            if not pmid:
                continue
            (cache_dir / f"{pmid}.json").write_text(
                json.dumps(record, ensure_ascii=False)
            )


def _extract_record(article: Any) -> dict[str, Any]:
    """Pull the bits we need out of the Medline XML structure.

    Returns a plain dict (not a Biopython StringElement tree) so the cache
    files are stable JSON we can read without Biopython.
    """
    citation = article.get("MedlineCitation", {})
    art = citation.get("Article", {})
    pmid = str(citation.get("PMID", ""))

    title = str(art.get("ArticleTitle", "")).strip()
    abstract_node = art.get("Abstract", {})
    abstract_texts = abstract_node.get("AbstractText", []) or []

    sections: list[tuple[str, str]] = []
    flat_parts: list[str] = []
    for item in abstract_texts:
        text = str(item).strip()
        if not text:
            continue
        label = None
        attrs = getattr(item, "attributes", None) or {}
        if attrs.get("Label"):
            label = str(attrs["Label"]).upper()
        if label:
            sections.append((label, text))
        flat_parts.append(f"{label}: {text}" if label else text)
    abstract = "\n\n".join(flat_parts)

    mesh = []
    for m in citation.get("MeshHeadingList", []) or []:
        descriptor = m.get("DescriptorName")
        if descriptor:
            mesh.append(str(descriptor))

    authors = []
    for a in art.get("AuthorList", []) or []:
        last = a.get("LastName")
        initials = a.get("Initials")
        if last:
            authors.append(f"{last} {initials}".strip() if initials else str(last))

    journal = str(art.get("Journal", {}).get("Title", "")).strip() or None
    pub_year = _extract_year(art.get("Journal", {}))

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "abstract_sections": sections,
        "mesh": mesh,
        "authors": authors,
        "journal": journal,
        "year": pub_year,
    }


def _extract_year(journal: Any) -> str | None:
    issue = journal.get("JournalIssue", {})
    pub_date = issue.get("PubDate", {})
    year = pub_date.get("Year")
    if year:
        return str(year)
    medline_date = pub_date.get("MedlineDate")
    if medline_date:
        m = re.match(r"(\d{4})", str(medline_date))
        if m:
            return m.group(1)
    return None


def _record_to_document(pmid: str, record: dict[str, Any]) -> RawDocument:
    title = record.get("title") or None
    abstract = record.get("abstract") or ""
    text = f"{title}\n\n{abstract}".strip() if title else abstract
    return RawDocument(
        source_type="pubmed",
        source_id=pmid,
        title=title,
        text=text,
        license="public-pubmed-abstract",
        source_uri=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        metadata={
            "mesh": record.get("mesh", []),
            "authors": record.get("authors", []),
            "journal": record.get("journal"),
            "year": record.get("year"),
            "abstract_sections": record.get("abstract_sections", []),
        },
    )


def _split(text: str) -> Iterator[str]:
    text = (text or "").strip()
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
