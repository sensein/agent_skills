# SPARQL recipe catalog

Queries run against a SynthScholar RDF export (`.ttl` / `.jsonld`) that follows
the SLR ontology (`https://w3id.org/slr-ontology/`). All recipes share these
prefixes:

```sparql
PREFIX slr:     <https://w3id.org/slr-ontology/>
PREFIX rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX bibo:    <http://purl.org/ontology/bibo/>
PREFIX fabio:   <http://purl.org/spar/fabio/>
PREFIX prov:    <http://www.w3.org/ns/prov#>
```

The named IDs below (`included-full-text`, etc.) are the `--query` values
accepted by `scripts/query_sparql.py`.

---

## `included-full-text` — included publications that have full text (+ content)

```sparql
SELECT DISTINCT ?pub ?title ?doi ?pmid ?bytes ?hash ?text
WHERE {
  ?review a slr:SystematicReview ; slr:included_sources ?pub .
  ?pub a slr:IncludedSource ; dcterms:title ?title .
  OPTIONAL { ?pub bibo:doi  ?doi }
  OPTIONAL { ?pub bibo:pmid ?pmid }
  ?pub slr:full_text_artifact ?art .
  ?art slr:content_text ?text .
  OPTIONAL { ?art slr:content_size_bytes ?bytes }
  OPTIONAL { ?art slr:content_hash ?hash }
}
ORDER BY ?title
```

## `included-full-text-brief` — same, without the (large) body text

```sparql
SELECT DISTINCT ?pub ?title ?doi ?pmid ?bytes ?hash
WHERE {
  ?review a slr:SystematicReview ; slr:included_sources ?pub .
  ?pub a slr:IncludedSource ; dcterms:title ?title .
  OPTIONAL { ?pub bibo:doi  ?doi }
  OPTIONAL { ?pub bibo:pmid ?pmid }
  ?pub slr:full_text_artifact ?art .
  OPTIONAL { ?art slr:content_size_bytes ?bytes }
  OPTIONAL { ?art slr:content_hash ?hash }
}
ORDER BY ?title
```

## `abstract-only` — included but no full text

```sparql
SELECT DISTINCT ?pub ?title ?doi ?abstract
WHERE {
  ?review a slr:SystematicReview ; slr:included_sources ?pub .
  ?pub a slr:IncludedSource ; dcterms:title ?title .
  OPTIONAL { ?pub bibo:doi ?doi }
  OPTIONAL { ?pub dcterms:abstract ?abstract }
  FILTER NOT EXISTS { ?pub slr:full_text_artifact ?art }
}
ORDER BY ?title
```

## `status-all` — every included publication labelled full-text vs abstract-only

```sparql
SELECT ?title
       (IF(BOUND(?art), "full_text", "abstract_only") AS ?status)
       ?doi ?bytes
WHERE {
  ?review a slr:SystematicReview ; slr:included_sources ?pub .
  ?pub a slr:IncludedSource ; dcterms:title ?title .
  OPTIONAL { ?pub bibo:doi ?doi }
  OPTIONAL { ?pub slr:full_text_artifact ?art .
             OPTIONAL { ?art slr:content_size_bytes ?bytes } }
}
ORDER BY ?status ?title
```

## `counts` — how many included, full-text, abstract-only

```sparql
SELECT
  (COUNT(DISTINCT ?pub) AS ?included)
  (COUNT(DISTINCT ?ft)  AS ?full_text)
  (COUNT(DISTINCT ?pub) - COUNT(DISTINCT ?ft) AS ?abstract_only)
WHERE {
  ?review a slr:SystematicReview ; slr:included_sources ?pub .
  ?pub a slr:IncludedSource .
  OPTIONAL { ?pub slr:full_text_artifact ?art . BIND(?pub AS ?ft) }
}
```

## `provenance` — where each full text came from (source + hash + size)

`slr:full_text_source` records which resolver / provider produced the text
(`pmc_oa`, `europe_pmc_oa`, `unpaywall_pdf`, `biorxiv_pdf`, `openalex_pdf`,
`semanticscholar_pdf`, `article_store`, `cache`; empty if not recorded). It
matches `article_full_text.full_text_source` in PostgreSQL.

```sparql
SELECT ?title ?source ?hash ?bytes ?media
WHERE {
  ?pub a slr:IncludedSource ; dcterms:title ?title ;
       slr:full_text_artifact ?art .
  OPTIONAL { ?art slr:full_text_source ?source }
  OPTIONAL { ?art slr:content_hash ?hash }
  OPTIONAL { ?art slr:content_size_bytes ?bytes }
  OPTIONAL { ?art slr:media_type ?media }
}
ORDER BY ?source ?title
```

## `source-counts` — full-text count grouped by provenance source

```sparql
SELECT ?source (COUNT(?art) AS ?n)
WHERE {
  ?pub a slr:IncludedSource ; slr:full_text_artifact ?art .
  OPTIONAL { ?art slr:full_text_source ?src }
  BIND(COALESCE(?src, "(unknown)") AS ?source)
}
GROUP BY ?source
ORDER BY DESC(?n)
```

## `retrieval-chain` — the PROV retrieval step per full text (what/from-what/when)

Each full-text artifact is `prov:wasGeneratedBy` a retrieval activity (a
`slr:ToolInvocation`, `slr:tool_category = retrieval`) that `prov:used` the
source publication; the artifact is also `prov:wasDerivedFrom` that source. The
activity carries the provider (`slr:tool_name`), the content hash
(`slr:result_hash`), and the completion time (`prov:endedAtTime`). The artifact
itself also has `slr:created_at_time`.

```sparql
SELECT ?title ?source ?when ?hash
WHERE {
  ?pub a slr:IncludedSource ; dcterms:title ?title ;
       slr:full_text_artifact ?art .
  ?art prov:wasGeneratedBy ?act .
  ?art prov:wasDerivedFrom ?used .
  OPTIONAL { ?act slr:tool_name    ?source }
  OPTIONAL { ?act prov:endedAtTime ?when }
  OPTIONAL { ?act slr:result_hash  ?hash }
}
ORDER BY ?when ?title
```
