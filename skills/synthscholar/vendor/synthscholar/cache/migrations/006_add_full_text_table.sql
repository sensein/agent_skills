-- Migration 006: normalized full-text store with content hash + provenance.
-- Run after 001–005.
--
-- Example:
--     psql "$SYNTHSCHOLAR_PG_DSN" -f synthscholar/cache/migrations/006_add_full_text_table.sql
--
-- Rationale
-- ---------
-- Full-text bodies were previously kept only in `article_store.full_text`.
-- That column stays (it feeds the article_store FTS `search_vector`, weight C),
-- but it carried no provenance: which resolver produced the text, when, its
-- byte size, or a content hash for reproducibility / dedup.
--
-- This migration adds `article_full_text` as the authoritative provenance
-- record, keyed by PMID (mirrors the existing UNIQUE(pmid) on article_store).
-- One row per source that has retrievable full text; absence of a row = an
-- abstract-only inclusion. The content hash matches slr:content_hash emitted
-- by the RDF exporter, giving DB ↔ RDF parity.

CREATE TABLE IF NOT EXISTS article_full_text (
    pmid               TEXT        PRIMARY KEY,
    content            TEXT        NOT NULL DEFAULT '',
    content_sha256     TEXT        NOT NULL DEFAULT '',
    media_type         TEXT        NOT NULL DEFAULT 'text/plain',
    content_size_bytes INTEGER     NOT NULL DEFAULT 0,
    full_text_source   TEXT        NOT NULL DEFAULT '',
    retrieved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Body-text FTS, so full-text search can move off article_store later.
    search_vector      TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(content, ''))
    ) STORED
);

-- Deduplicate / verify by content hash across the corpus.
CREATE INDEX IF NOT EXISTS idx_aft_sha
    ON article_full_text (content_sha256)
    WHERE content_sha256 <> '';

-- Audit which provider produced full text (pmc_oa, unpaywall_pdf, biorxiv_pdf…).
CREATE INDEX IF NOT EXISTS idx_aft_source
    ON article_full_text (full_text_source)
    WHERE full_text_source <> '';

-- Full-text search over the body.
CREATE INDEX IF NOT EXISTS idx_aft_search
    ON article_full_text USING GIN (search_vector);
