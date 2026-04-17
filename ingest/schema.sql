-- Run automatically when the postgres container starts for the first time.
-- Safe to re-run; guarded with IF NOT EXISTS.

CREATE EXTENSION IF NOT EXISTS vector;

-- Document-level table: one row per source document.
-- Lets us track provenance, license, and fetch metadata without repeating
-- it on every chunk.
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL,      -- 'mtsamples' | 'pubmed' | 'icd11'
    source_uri      TEXT,               -- canonical URI/URL where applicable
    source_id       TEXT,               -- native id within source (PMID, ICD code, MTSamples row index)
    title           TEXT,
    license         TEXT,               -- short license tag, e.g. 'public-domain', 'WHO-ICD-API-TOS'
    metadata        JSONB,              -- source-specific extras (authors, MeSH terms, ICD parent code, etc.)
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_type, source_id)
);

CREATE INDEX IF NOT EXISTS documents_source_type_idx ON documents (source_type);

-- Chunk-level table: one row per embeddable passage, linked back to its document.
CREATE TABLE IF NOT EXISTS chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section         TEXT,               -- clinical section header, ICD field name, or 'ABSTRACT'
    chunk_index     INTEGER NOT NULL,   -- ordering within the document
    chunk_text      TEXT NOT NULL,
    embedding       VECTOR(768),
    tsv             TSVECTOR,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for approximate nearest neighbor (cosine distance).
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- GIN index for full-text search (BM25-style ranking via ts_rank).
CREATE INDEX IF NOT EXISTS chunks_tsv_gin_idx
    ON chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_section_idx    ON chunks (section);

-- Auto-populate tsvector on insert/update so the ingest script doesn't
-- have to compute it explicitly.
CREATE OR REPLACE FUNCTION chunks_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('english', COALESCE(NEW.chunk_text, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS chunks_tsv_update ON chunks;
CREATE TRIGGER chunks_tsv_update
    BEFORE INSERT OR UPDATE ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_tsv_trigger();

-- Convenience view: chunks with their source provenance attached.
-- Use this in retrieval queries so you can filter by source_type or
-- include source metadata in responses without joining manually every time.
CREATE OR REPLACE VIEW chunks_with_source AS
SELECT
    c.id                AS chunk_id,
    c.chunk_text,
    c.section,
    c.chunk_index,
    c.embedding,
    c.tsv,
    d.id                AS document_id,
    d.source_type,
    d.source_uri,
    d.source_id,
    d.title,
    d.license,
    d.metadata
FROM chunks c
JOIN documents d ON c.document_id = d.id;
