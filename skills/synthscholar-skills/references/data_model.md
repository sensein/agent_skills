# Data model reference

How full-text availability and content are represented in the two surfaces this
skill queries, and how provenance is captured upstream.

## RDF (SLR ontology)

Namespace: `slr: <https://w3id.org/slr-ontology/>`.

- A review is a `slr:SystematicReview`; it links included publications via
  `slr:included_sources` to `slr:IncludedSource` nodes (also typed
  `fabio:Expression`).
- Bibliographic literals on a source: `dcterms:title`, `bibo:pmid`, `bibo:doi`,
  `dcterms:bibliographicCitation`, `fabio:hasPublicationYear`,
  `dcterms:creator`, `slr:journal`, and — when present — `dcterms:abstract`.
- **Full text** is a `slr:StoredArtifact` linked from the source by
  `slr:full_text_artifact` (an object property added for this feature). The
  artifact carries:
  - `slr:artifact_kind` → `slr:ArtifactKindEnum#source_text_extract`
  - `slr:media_type` (`"text/plain"`)
  - `slr:content_text` — the full-text body
  - `slr:content_size_bytes` — UTF-8 byte length
  - `slr:content_hash` — hex SHA-256 of the body (matches the DB `content_sha256`)
  - `slr:full_text_source` — provenance: which resolver produced the text
    (matches the DB `full_text_source`; empty when unrecorded)
  - `slr:created_at_time` — when the full text was retrieved (xsd:dateTime)

Each artifact also carries a **PROV retrieval step**, so the retrieval itself is
queryable:

- `?art prov:wasGeneratedBy ?activity` — the retrieval activity, a
  `slr:ToolInvocation` + `prov:Activity` with `slr:tool_category` =
  `ToolCategoryEnum#retrieval`, `slr:tool_name` (the provider/resolver),
  `slr:result_hash` (= content hash), `slr:success`, and `prov:endedAtTime`.
- `?activity prov:used ?pub` — the retrieval used the source publication.
- `?art prov:wasDerivedFrom ?pub` — the text was derived from that source.

Distinguishing rule: a source with a `slr:full_text_artifact` is full-text
included; one without is abstract-only. Emitted by `rdf_export.py`
(`_add_included_source`).

## PostgreSQL (article store)

- **`article_store`** — one row per article, unique on `pmid`. `full_text` is
  kept here as the copy that feeds the generated `search_vector` (title A,
  abstract B, full_text C).
- **`article_full_text`** (migration
  `synthscholar/cache/migrations/006_add_full_text_table.sql`) — authoritative
  full-text + provenance record:

  | column | meaning |
  | --- | --- |
  | `pmid` | PK; joins to `article_store.pmid` |
  | `content` | full-text body |
  | `content_sha256` | hex SHA-256 of `content` (== RDF `slr:content_hash`) |
  | `media_type` | `text/plain` |
  | `content_size_bytes` | UTF-8 byte length |
  | `full_text_source` | resolver that produced it (see below) |
  | `retrieved_at` | when it was stored |
  | `search_vector` | tsvector over `content` for body FTS |

`ArticleStore` (in `synthscholar/cache/article_store.py`) probes for the table
at connect (`_has_full_text_table`), mirrors full text into it on
`upsert_articles`, hydrates `content_sha256` / `full_text_source` back onto
`Article` in `get_by_pmids`, and provides `backfill_full_text_table()`.

## Provenance values (`full_text_source`)

Set by the pipeline / resolver as full text is obtained:

| value | origin |
| --- | --- |
| `pmc_oa` | PubMed Central OA bulk fetch |
| `europe_pmc_oa` | Europe PMC OA full-text XML |
| `biorxiv_pdf` / `medrxiv_pdf` | preprint PDF parse |
| `unpaywall_pdf` | Unpaywall-discovered PDF |
| `openalex_pdf` | OpenAlex-discovered PDF |
| `semanticscholar_pdf` | Semantic Scholar-discovered PDF |
| `ezproxy_pdf` | subscription article fetched through the reviewer's institutional EZproxy session (`synthscholar.ezproxy`) |
| `user_supplied_pdf` | PDF the reviewer provided (bring-your-own-corpus, see [byo_corpus_review.md](byo_corpus_review.md)) |
| `article_store` | pre-filled from a prior run's stored article |
| `cache` | restored from the resolver's `resolved_fulltext` cache |
| `` (empty) | historical row backfilled from `article_store.full_text` |

`ezproxy_pdf` and `user_supplied_pdf` are worth distinguishing in any audit:
the first means the paper was read under a subscription entitlement, the second
that it never passed through a resolver at all.

## Retrieval and screening-basis provenance

`PRISMAFlowCounts` records what the review actually read, and the RDF export
emits each field so it is queryable:

| Field | RDF predicate on the review |
| --- | --- |
| `full_text_retrieved` / `not_retrieved` | `slr:full_text_retrieved` / `slr:not_retrieved` |
| `sought_fulltext`, `screened_title_abstract`, `excluded_title_abstract`, `assessed_eligibility`, `excluded_eligibility` | same-named `slr:` predicates |
| `full_text_sources` (route → count) | `slr:full_text_route_record` → `slr:RetrievalRouteCount` (`slr:full_text_source`, `slr:report_count`) |
| `assessed_on_full_text` / `assessed_on_abstract_only` | `slr:assessed_on_full_text` / `slr:assessed_on_abstract_only` |
| `included_with_full_text` / `included_abstract_only` | `slr:included_with_full_text` / `slr:included_abstract_only` |
| `excluded_reasons_title_abstract` / `excluded_reasons_full_text` | `slr:exclusion_reason_title_abstract` / `slr:exclusion_reason_full_text` → `slr:ExclusionReasonCount` (`slr:exclusion_reason`, `slr:report_count`) |

Every individual decision is exported too: one
`slr:ScreeningDecisionRecord` per `ScreeningLogEntry`, carrying `bibo:pmid`,
`slr:screening_stage`, `slr:decision`, **`slr:assessed_on`**
(`title_abstract` / `full_text` / `abstract_only`), `slr:full_text_source`, and
`slr:decision_rationale` (plus `slr:exclusion_reason` when excluded). That is
the audit trail behind the aggregate counts — it answers "was this study
included after someone read the paper, or only its abstract?".

## Intake provenance (PreWorkflowSession / UserInput)

The protocol-setup conversation should itself be recorded so a review shows not
just *what* the protocol was but *how it was elicited*:

- `slr:PreWorkflowSession` — one session per setup, with `session_type`
  (protocol setup), `session_date`, `decisions_locked`, and
  `resulting_configuration_uri` (the finished protocol).
- `slr:UserInput` — one per answer, with `input_type`, `question_asked`,
  `input_value`, `options_presented` (when the user chose from a list), and
  `captured_at_time`. Sessions link to their inputs via `user_inputs`.

Both attach to the review through the standard PROV pattern
(`prov:wasGeneratedBy` / `prov:wasAttributedTo`). Capturing intake this way
means the same provenance queries that trace full text can also trace *why a
criterion or PICO term is what it is*.

## Parity

The content hash is the join key between surfaces: RDF `slr:content_hash` and
SQL `article_full_text.content_sha256` are both the hex SHA-256 of the UTF-8
body (`synthscholar.provenance.content_sha256`). The same review exported to RDF
and stored in Postgres will agree on hashes for every full-text source.
