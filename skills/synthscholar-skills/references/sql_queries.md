# SQL recipe catalog

Queries run against the PostgreSQL article store used by SynthScholar.
Two tables matter:

- **`article_store`** — one row per fetched article (keyed by `pmid`, unique).
  Columns include `title`, `abstract`, `doi`, `pmc_id`, `source`, and
  `full_text` (the FTS-indexed copy).
- **`article_full_text`** (migration 006) — the authoritative full-text +
  provenance record. Columns: `pmid` (PK), `content`, `content_sha256`,
  `media_type`, `content_size_bytes`, `full_text_source`, `retrieved_at`.

A publication is **full-text-included** iff it has a row in `article_full_text`
with non-empty `content`; **abstract-only** iff it has an `article_store` row
with a non-empty `abstract` and no `article_full_text` row.

The named IDs below are the positional query argument accepted by
`scripts/query_postgres.py`.

---

## `included-full-text` — included publications with full text + provenance

```sql
SELECT s.pmid, s.doi, s.title,
       aft.full_text_source,
       aft.content_size_bytes,
       aft.content_sha256,
       aft.retrieved_at
FROM article_store s
JOIN article_full_text aft USING (pmid)
WHERE aft.content <> ''
ORDER BY s.title;
```

## `abstract-only` — included but no full text

```sql
SELECT s.pmid, s.doi, s.title
FROM article_store s
LEFT JOIN article_full_text aft USING (pmid)
WHERE aft.pmid IS NULL AND s.abstract <> ''
ORDER BY s.title;
```

## `status-all` — every article labelled full-text vs abstract-only

```sql
SELECT s.pmid, s.title,
       CASE WHEN aft.pmid IS NOT NULL AND aft.content <> ''
            THEN 'full_text' ELSE 'abstract_only' END AS status,
       aft.content_size_bytes
FROM article_store s
LEFT JOIN article_full_text aft USING (pmid)
ORDER BY status, s.title;
```

## `counts` — totals

```sql
SELECT
  (SELECT count(*) FROM article_store)                              AS articles,
  (SELECT count(*) FROM article_full_text WHERE content <> '')      AS full_text,
  (SELECT count(*) FROM article_store s
     LEFT JOIN article_full_text aft USING (pmid)
     WHERE aft.pmid IS NULL AND s.abstract <> '')                   AS abstract_only;
```

## `source-breakdown` — where full text came from

Values mirror the RDF side: `pmc_oa`, `europe_pmc_oa`, `unpaywall_pdf`,
`openalex_pdf`, `semanticscholar_pdf`, `biorxiv_pdf` / `medrxiv_pdf`,
`ezproxy_pdf` (institutional subscription access), `user_supplied_pdf` (a
reviewer-supplied corpus), `article_store`, `cache`, or empty for rows
backfilled before provenance was recorded.

```sql
SELECT COALESCE(NULLIF(full_text_source, ''), '(unknown/backfilled)') AS source,
       count(*) AS n,
       pg_size_pretty(sum(content_size_bytes)::bigint) AS total_bytes
FROM article_full_text
WHERE content <> ''
GROUP BY 1
ORDER BY n DESC;
```

Splitting subscription from open access is often the point of this query — how
much of the corpus depended on institutional entitlement:

```sql
SELECT full_text_source IN ('ezproxy_pdf') AS via_subscription,
       count(*) AS n
FROM article_full_text
WHERE content <> ''
GROUP BY 1;
```

## `hash-dupes` — identical full text across different PMIDs

```sql
SELECT content_sha256, count(*) AS copies, array_agg(pmid) AS pmids
FROM article_full_text
WHERE content_sha256 <> ''
GROUP BY content_sha256
HAVING count(*) > 1
ORDER BY copies DESC;
```

## `get-content` — the full-text body for one PMID (parameter: `--pmid`)

```sql
SELECT pmid, full_text_source, content_size_bytes, content_sha256, content
FROM article_full_text
WHERE pmid = %(pmid)s;
```

## `full-text-search` — FTS over stored bodies (parameter: `--q`)

```sql
SELECT pmid, ts_rank(search_vector, plainto_tsquery('english', %(q)s)) AS rank
FROM article_full_text
WHERE search_vector @@ plainto_tsquery('english', %(q)s)
ORDER BY rank DESC
LIMIT %(limit)s;
```
