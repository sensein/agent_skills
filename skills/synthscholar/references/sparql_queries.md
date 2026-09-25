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
`semanticscholar_pdf`, **`ezproxy_pdf`** — institutional subscription access —
**`user_supplied_pdf`** — a reviewer-supplied corpus — `article_store`, `cache`;
empty if not recorded). It matches `article_full_text.full_text_source` in
PostgreSQL.

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

---

# Reading basis and screening provenance

These answer *what the review actually read*: how many reports were retrieved,
by which route, how many eligibility decisions had the full text in hand, and
why studies were dropped at each stage. Written by every run — see
[data_model.md](data_model.md#retrieval-and-screening-basis-provenance) for the
full predicate list.

## `reading-basis` — retrieved vs not, and what decisions rested on

```sparql
SELECT ?retrieved ?not_retrieved ?on_full_text ?on_abstract_only
       ?included_full ?included_abstract
WHERE {
  ?rev a slr:SystematicReview .
  OPTIONAL { ?rev slr:full_text_retrieved       ?retrieved }
  OPTIONAL { ?rev slr:not_retrieved             ?not_retrieved }
  OPTIONAL { ?rev slr:assessed_on_full_text     ?on_full_text }
  OPTIONAL { ?rev slr:assessed_on_abstract_only ?on_abstract_only }
  OPTIONAL { ?rev slr:included_with_full_text   ?included_full }
  OPTIONAL { ?rev slr:included_abstract_only    ?included_abstract }
}
```

Read the two `included_*` numbers together: *"4 of 5 included studies were read
in full text, 1 was included on its abstract alone"* is the honest summary of a
review's evidence base.

## `retrieval-routes` — how the full texts were obtained

Reports per route, including `ezproxy_pdf` (institutional subscription access)
and `user_supplied_pdf` (reviewer-supplied corpus).

```sparql
SELECT ?route ?reports
WHERE {
  ?rev a slr:SystematicReview ; slr:full_text_route_record ?rec .
  ?rec slr:full_text_source ?route ; slr:report_count ?reports .
}
ORDER BY DESC(?reports)
```

## `ezproxy-articles` — which papers were read under institutional access

```sparql
SELECT ?title ?doi ?when
WHERE {
  ?pub a slr:IncludedSource ; slr:full_text_artifact ?art .
  ?art slr:full_text_source "ezproxy_pdf" .
  OPTIONAL { ?pub dcterms:title ?title }
  OPTIONAL { ?pub bibo:doi ?doi }
  OPTIONAL { ?art slr:created_at_time ?when }
}
ORDER BY ?title
```

## `exclusion-reasons` — why reports were dropped, by stage

```sparql
SELECT ?stage ?reason ?reports
WHERE {
  { ?rev slr:exclusion_reason_title_abstract ?rec .
    BIND("title_abstract" AS ?stage) }
  UNION
  { ?rev slr:exclusion_reason_full_text ?rec .
    BIND("eligibility" AS ?stage) }
  ?rec slr:exclusion_reason ?reason ; slr:report_count ?reports .
}
ORDER BY ?stage DESC(?reports)
```

## `screening-decisions` — the per-decision audit trail

One `slr:ScreeningDecisionRecord` per logged decision. `slr:assessed_on` is
`title_abstract`, `full_text`, or `abstract_only` — the last meaning the report
could not be retrieved and the call was made on its abstract.

```sparql
SELECT ?pmid ?stage ?decision ?assessed_on ?route ?reason
WHERE {
  ?rev slr:screening_decision_record ?rec .
  ?rec slr:screening_stage ?stage ; slr:decision ?decision ;
       slr:assessed_on ?assessed_on .
  OPTIONAL { ?rec bibo:pmid ?pmid }
  OPTIONAL { ?rec slr:full_text_source ?route }
  OPTIONAL { ?rec slr:decision_rationale ?reason }
}
ORDER BY ?stage ?decision ?pmid
```

## `abstract-only-inclusions` — studies included without their full text

The set to scrutinise first in any appraisal of the review, and the set to
re-run once the papers can be retrieved (`fetch_ezproxy.py` for paywalled ones).

```sparql
SELECT ?pmid ?reason
WHERE {
  ?rev slr:screening_decision_record ?rec .
  ?rec slr:screening_stage "full_text" ;
       slr:assessed_on "abstract_only" ;
       slr:decision ?decision .
  FILTER(LCASE(STR(?decision)) = "include")
  OPTIONAL { ?rec bibo:pmid ?pmid }
  OPTIONAL { ?rec slr:decision_rationale ?reason }
}
ORDER BY ?pmid
```

## `research-questions` — the protocol's questions and how many studies answered each

```sparql
SELECT ?qid ?title (COUNT(?a) AS ?answers)
WHERE {
  ?q a slr:ResearchQuestion ; slr:has_answer ?a .
  OPTIONAL { ?q slr:question_id ?qid }
  OPTIONAL { ?q dcterms:title ?title }
}
GROUP BY ?qid ?title
ORDER BY ?qid
```

An answer count below the included-study count means some articles didn't
report enough to chart that question — worth knowing before quoting a
question's synthesis as if it covered the corpus.

## `question-answers` — every study's answer to one research question

The recipe asks `RQ1.1`; edit the id, or use `--sparql` with your own file.

```sparql
SELECT ?pmid ?source_id ?answer
WHERE {
  ?q slr:question_id "RQ1.1" ; slr:has_answer ?a .
  ?a slr:answer_text ?answer .
  OPTIONAL { ?a slr:source_id ?source_id }
  OPTIONAL { ?a slr:about_source/bibo:pmid ?pmid }
}
ORDER BY ?source_id
```

Both work on any review exported since the research-question merge landed (see
`references/byo_corpus_review.md` § 5). Older exports carry the answers only
inside the charting rubrics — re-export, or run
`scripts/research_questions.py review.json --outdir out/`, to get these nodes.
