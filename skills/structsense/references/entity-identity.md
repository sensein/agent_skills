# Entity identity for unstructured sources

An entity is a referent; a mention is a character span in a source version; a
classification is a judgment about a mention/entity; an ontology concept is an
external vocabulary entry. These are different records, even when their labels
look alike. Show normalized entities by default, not all audit nodes.

## Identity and provenance

The global entity IRI is deterministic UUIDv5 of `entity|<normalized_key>`.
Reuse a canonical key for the same resolved referent across sources. Keep one
entity per resolved key within a source and link every occurrence via hasMention
and refersToEntity. Source IDs, source versions and mentions retain provenance.
Cross-source reconciliation can use verified identifiers or reviewed identity
keys; generic labels or broad ontology matches cannot establish identity.

`identity_key` is an optional stable key for a resolved referent. `referent_id`
is a source-local disambiguator for homonyms: two people named Jordan must not
merge. The latter is source-qualified until a reviewed global key establishes
identity. Bare general-domain names are also kept source-local until reviewed.
A plan can consolidate acronyms, aliases and variants by assigning
one normalized_key; it must preserve different species, anatomical sides,
gene/protein distinctions and other meaningful qualifiers.

An exact repeated span for the same resolved entity is one mention even when
several extractors or overlapping chunks found it. Different spans remain
separate mentions. Expansion propagates lexical candidates only, never claims.

## Relations

Each occurrence may supply its own relation evidence. Preserve all evidence
occurrences, not just the first one. RelationAssertion records carry subject,
predicate, object, quote, source and optional evidence mention, negation,
modality, time, condition or context. Only unqualified affirmative assertions
also produce a direct entity-to-entity edge. A negative statement must not turn
into a positive edge. A shared type is not an anatomical or causal relation.

## Views

The canonical `<stem>.ttl` retains provenance and mapping records and passes
OWL/SHACL validation. Compact is the default; full adds annotation and review
history. On request (`--entity-views`) the exporter also writes
`<stem>.entities.json` with one record per entity and nested occurrences/assertions,
and `<stem>.entities.ttl` for graph viewing (one node per surface form, linked to
every sentence it occurs in). The projection is deliberately not the complete ingestion graph.
Existing TTL can be projected with `python -m scripts.entity_view <file.ttl>`.

## Validation

Use `python -m pytest scripts/tests/test_entity_identity.py` from the skill root.
The fixtures cover unstructured sources, repeated spans, source provenance,
shared global entity IRIs, local homonyms, abbreviation aliases, relation
retention, negation/conditions, compact/full output and SHACL rejection of
invalid duplicate or unsupported records. No network or LLM is required.
