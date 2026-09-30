# BrainKB Named Entity Ontology (v2.5) — competency questions

Run `python cqs/run_cqs.py /path/to/output --engine auto --report report.json`.
Directory input recursively discovers canonical Turtle, excluding `.entities.ttl`,
`.invalid.ttl` and hidden working directories. Do not mix projections with canonical data.
Each query declares its own prefixes and requires no reasoner or ontology loading.
Inline subclass lists are maintained by `regen_values.py`.

Parameters default to all matching data (`UNDEF`), rather than unrelated example-paper
values. Use `--entity-key`, `--doi`, `--ontology-acronym`, or `--term-iri`, or replace
UNDEF in the corresponding VALUES block with a quoted literal / angle-bracketed IRI.
An unfiltered neighborhood query can be large; use `--only CQ26r --entity-key hippocampus`.
Distinct entity IRIs establish identity; equal labels or normalized keys alone do not.
A source need not have a DOI. Queries return source IRIs where source attribution matters.

Compact output omits detailed decisions, classifications, reviews, changes and validation
reports: CQ9, CQ23, CQ24b and CQ41–CQ46 need these full-profile records. Empty results
are not query errors or proof of extraction failure. Causal chains, quantitative effects,
lineage and other optional relations can also be absent. The runner distinguishes missing
required records from a valid query with no matching answer.

Entity mappings and materialized direct edges aggregate across sources. Entity provenance
only says where the entity occurs. CQ25, CQ51 and CQ55 therefore use RelationAssertion
provenance and expose negation/modality/context; they do not fabricate source support from
entity provenance. Missing assertion evidence cannot be reconstructed from a direct edge.
CQ55 deliberately returns each expression assertion separately: joining region/species
edges on a global cell IRI creates unsupported combinations. Inspect other assertions with
the same evidence mention/context before combining them. Direct-edge queries show only
materialized edges; use assertions for qualified or negated claims.

Provisional BRAINKB concepts count as unresolved external coverage in CQ10/CQ38. Ontology
coverage includes BRAINKB explicitly in CQ49. Mapping tiers are not all identity claims.
CQ22 leaves the run blank when compact mentions lack a snapshot/annotation link;
a run that merely used the same document is not evidence that it extracted that mention.
Dates in CQ17–CQ19 are normalized to integer years; undated sources cannot enter a year bin.

Every query below **declares its own prefixes** — it runs as pasted into any
SPARQL endpoint, with nothing assumed. The namespaces used, for reference:

| prefix | namespace |
|---|---|
| `ner:` | `https://brainkb.org/ner/` |
| `obo:` | `http://purl.obolibrary.org/obo/` |
| `skos:` | `http://www.w3.org/2004/02/skos/core#` |
| `prov:` | `http://www.w3.org/ns/prov#` |
| `rdf:` | `http://www.w3.org/1999/02/22-rdf-syntax-ns#` |
| `rdfs:` | `http://www.w3.org/2000/01/rdf-schema#` |
| `dcterms:` | `http://purl.org/dc/terms/` |
| `xsd:` | `http://www.w3.org/2001/XMLSchema#` |
| `owl:` | `http://www.w3.org/2002/07/owl#` |

## A. Entities, mentions, grounding

**CQ1 — Which entities of a given type exist, and in which papers?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?e ?pub ?label ?doi WHERE {
  VALUES ?entityType { ner:Drug }
  ?e a ?entityType ; ner:normalizedEntityLabel ?label ;
     prov:hadPrimarySource ?pub .
 OPTIONAL { ?pub ner:doi ?doi }
} ORDER BY ?label
```

**CQ2 — Under which verbatim surface forms is an entity mentioned
(terminology variants across papers)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT DISTINCT ?pub ?key ?surface ?doi WHERE {
 VALUES ?requestedKey { UNDEF }
 FILTER(!BOUND(?requestedKey) || ?key = ?requestedKey)
  ?e ner:normalizedEntityKey ?key ; ner:hasMention ?m .
  ?m ner:surfaceForm ?surface ;
     ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
 OPTIONAL { ?pub ner:doi ?doi }
}
```

**CQ3 — Exactly where in the source document does each mention sit
(character-level, reproducible grounding)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT DISTINCT ?m ?pub ?surface ?start ?end ?doi WHERE {
  ?m a ner:EntityMention ; ner:surfaceForm ?surface ;
     ner:documentStartOffset ?start ; ner:documentEndOffset ?end ;
     ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
 OPTIONAL { ?pub ner:doi ?doi }
} ORDER BY ?doi ?start
```

**CQ4 — Full typed inventory contributed by one paper?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?pub ?doi ?e ?key ?cls WHERE {
 VALUES ?requestedDoi { UNDEF }
 FILTER(!BOUND(?requestedDoi) || ?doi = ?requestedDoi)
  ?e prov:hadPrimarySource ?pub ;
     ner:normalizedEntityKey ?key ; a ?cls .
  OPTIONAL { ?pub ner:doi ?doi }
  FILTER(STRSTARTS(STR(?cls), STR(ner:)) && ?cls != ner:NamedEntity)
} ORDER BY ?cls ?key
```

## B. Cross-paper structure

**CQ5 — Which entities are shared across papers?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?e ?key (COUNT(DISTINCT ?pub) AS ?papers) WHERE {
  ?e ner:normalizedEntityKey ?key ; prov:hadPrimarySource ?pub .
} GROUP BY ?e ?key HAVING (COUNT(DISTINCT ?pub) > 1) ORDER BY DESC(?papers)
```

**CQ6 — Which papers are most related to a given paper (by shared
entities)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?p1 ?p2 ?doi ?otherDoi (COUNT(DISTINCT ?e) AS ?shared) WHERE {
 VALUES ?requestedDoi { UNDEF }
 FILTER(!BOUND(?requestedDoi) || ?doi = ?requestedDoi)
  ?e prov:hadPrimarySource ?p1 , ?p2 .
  OPTIONAL { ?p1 ner:doi ?doi }
  OPTIONAL { ?p2 ner:doi ?otherDoi } FILTER(?p1 != ?p2)
} GROUP BY ?p1 ?p2 ?doi ?otherDoi ORDER BY DESC(?shared)
```

**CQ7 — Which external mappings belong to entities mentioned in multiple sources (not source-specific mapping endorsements)?**
```sparql
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?obo (COUNT(DISTINCT ?pub) AS ?papers) WHERE {
  ?e (skos:exactMatch|skos:closeMatch|skos:broadMatch) ?obo ;
     prov:hadPrimarySource ?pub .
  FILTER(!STRSTARTS(STR(?obo), "https://brainkb.org/concept/"))
} GROUP BY ?obo HAVING (COUNT(DISTINCT ?pub) > 1) ORDER BY DESC(?papers)
```

## C. Ontology alignment & its provenance

**CQ8 — What is an entity mapped to, at which mapping relationship (tier)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT DISTINCT ?e ?key ?tier ?obo WHERE {
  VALUES (?p ?tier) { (skos:exactMatch "exact") (skos:closeMatch "close")
                      (skos:narrowMatch "narrow") (skos:broadMatch "broad") (skos:relatedMatch "related") }
  ?e ner:normalizedEntityKey ?key ; ?p ?obo .
  FILTER(!STRSTARTS(STR(?obo), "https://brainkb.org/concept/"))
} ORDER BY ?key
```

empty remove
**CQ9 — How was a mapping decided: candidates, ranks, scores, the decision,
its status, confidence, and the method used?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?key ?dec ?status ?method ?conf ?cand ?rank ?scoreType ?scoreValue WHERE {
  ?dec a ner:ConceptMappingDecision ; ner:decisionForNormalizedEntity ?e ; ner:mappingStatus ?status .
  ?e ner:normalizedEntityKey ?key .
  OPTIONAL { ?dec ner:decisionConfidence ?conf }
  OPTIONAL { ?dec prov:wasGeneratedBy/ner:usedMappingMethod ?method }
  OPTIONAL { ?dec (ner:selectedCandidate|ner:rejectedCandidate) ?cand .
    OPTIONAL { ?cand ner:mappingRank ?rank }
    OPTIONAL { ?cand ner:hasMappingScore ?s . ?s ner:scoreType ?scoreType ; ner:scoreValue ?scoreValue }
  }
}
```

**CQ10 — Which entities lack an external-ontology mapping (including provisional BRAINKB concepts)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?e ?cls ?key ?gap WHERE {
  ?e a ner:NamedEntity ; ner:normalizedEntityKey ?key ; a ?cls .
  MINUS { SELECT DISTINCT ?e WHERE {
    ?e ner:resolvedToConcept ?c .
    ?c ner:conceptInOntologyVersion/ner:versionOfOntology/ner:ontologyAcronym ?acronym .
    FILTER(UCASE(STR(?acronym)) != "BRAINKB")
  } }
  OPTIONAL { ?e rdfs:comment ?gap }
  FILTER(STRSTARTS(STR(?cls), STR(ner:)) && ?cls != ner:NamedEntity)
} ORDER BY ?cls
```

**CQ11 — Tier distribution per external ontology (are the target
ontologies granular enough for this corpus?)**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?acr ?tier (COUNT(*) AS ?n) WHERE {
 { SELECT DISTINCT ?e ?term ?acr ?tier WHERE {
  { { ?e skos:exactMatch ?term . BIND("exact" AS ?tier) }
 UNION
{ ?e skos:closeMatch ?term . BIND("close" AS ?tier) }
 UNION
{ ?e skos:broadMatch ?term . BIND("broad" AS ?tier) }
 UNION
{ ?e skos:narrowMatch ?term . BIND("narrow" AS ?tier) }
 UNION
{ ?e skos:relatedMatch ?term . BIND("related" AS ?tier) } }
  { SELECT DISTINCT ?term ?acr WHERE {
    ?concept ner:conceptIRI ?iri ; ner:conceptInOntologyVersion/ner:versionOfOntology/ner:ontologyAcronym ?acr .
    FILTER(UCASE(STR(?acr)) != "BRAINKB")
    BIND(IRI(STR(?iri)) AS ?term)
  } }
  ?e a ner:NamedEntity .
 } }
} GROUP BY ?acr ?tier ORDER BY ?acr DESC(?n)
```

## D. Causal claims & evidence

**CQ12 — Every causal claim an entity participates in (as cause, effect, or
mediator), with negation/hypotheticality flags?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?key ?role ?causeKey ?effectKey ?negated ?hypothetical WHERE {
 VALUES ?requestedKey { UNDEF }
 FILTER(!BOUND(?requestedKey) || ?key = ?requestedKey)
  ?x ner:normalizedEntityKey ?key .
  ?rel ner:hasCause ?c ; ner:hasEffect ?ef ;
       ner:hasCurrentCausalRelationVersion ?v .
  ?v ner:causalNegated ?negated ; ner:causalHypothetical ?hypothetical .
  ?c ner:normalizedEntityKey ?causeKey . ?ef ner:normalizedEntityKey ?effectKey .
  { ?rel ner:hasCause ?x    BIND("cause" AS ?role) } UNION
  { ?rel ner:hasEffect ?x   BIND("effect" AS ?role) } UNION
  { ?rel ner:hasMediator ?x BIND("mediator" AS ?role) }
}
```

**CQ13 — Reconstruct an ordered multi-step mechanism (causal chain)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?chain ?chainLabel ?rel (COUNT(DISTINCT ?prior) AS ?position) ?causeKey ?effectKey WHERE {
 ?chain a ner:CausalChain ; ner:hasChainRelation ?rel .
 OPTIONAL { ?chain rdfs:label ?chainLabel }
 ?rel ner:hasCause/ner:normalizedEntityKey ?causeKey ; ner:hasEffect/ner:normalizedEntityKey ?effectKey .
 OPTIONAL { ?chain ner:hasChainRelation ?prior . ?prior ner:nextCausalRelation+ ?rel . FILTER(?prior != ?rel) }
} GROUP BY ?chain ?chainLabel ?rel ?causeKey ?effectKey ORDER BY ?chain ?position
```

**CQ14 — All quantitative evidence, meta-analysis-shaped (measure, value,
p, n per cause→effect pair)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?causeKey ?effectKey ?measure ?value ?p ?n WHERE {
  ?rel ner:hasCause/ner:normalizedEntityKey ?causeKey ;
       ner:hasEffect/ner:normalizedEntityKey ?effectKey ;
       ner:hasCurrentCausalRelationVersion ?v .
  ?v ner:hasEffectEstimate ?est .
  ?est ner:effectMeasure ?measure ; ner:effectValue ?value .
  OPTIONAL { ?est ner:pValue ?p } OPTIONAL { ?est ner:sampleSize ?n }
}
```


--not used
**CQ15 — Which cross-source claims have the same entity pair and opposite negation (candidates for review, not proof of disagreement)?**
```sparql
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX ner: <https://brainkb.org/ner/>
SELECT DISTINCT ?r1 ?r2 ?pub1 ?pub2 ?causeKey ?effectKey WHERE {
  ?r1 ner:hasCause ?c ; ner:hasEffect ?ef ;
      ner:hasCurrentCausalRelationVersion/ner:causalNegated true .
  ?r2 ner:hasCause ?c ; ner:hasEffect ?ef ;
      ner:hasCurrentCausalRelationVersion/ner:causalNegated false .
  ?r1 prov:hadPrimarySource ?pub1 . ?r2 prov:hadPrimarySource ?pub2 . FILTER(?pub1 != ?pub2)
  ?c ner:normalizedEntityKey ?causeKey . ?ef ner:normalizedEntityKey ?effectKey .
}
```

not used
**CQ16 — Which participants are recorded for drug entities (RO:0000057; not a pharmacological targeting predicate)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
SELECT ?drug ?target WHERE {
  ?d a ner:Drug ; ner:normalizedEntityKey ?drug ;
     obo:CHEBI_92386/ner:normalizedEntityKey ?target .
}
```

## E. Temporal evolution

**CQ17 — When did each entity first appear, and how widely did it spread?**
```sparql
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT ?e ?key (MIN(?year) AS ?first) (COUNT(DISTINCT ?pub) AS ?papers) WHERE {
  ?e ner:normalizedEntityKey ?key ; prov:hadPrimarySource ?pub .
  ?pub (ner:publicationDate|dcterms:issued) ?date .
  BIND(xsd:integer(SUBSTR(STR(?date),1,4)) AS ?year)
} GROUP BY ?e ?key ORDER BY ?first
```

**CQ18 — How many dated sources mention entities mapped exactly or closely to each selected term, per year?**
```sparql
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT ?term ?year (COUNT(DISTINCT ?pub) AS ?papers) WHERE {
 VALUES ?requestedTerm { UNDEF }
 FILTER(!BOUND(?requestedTerm) || ?term = ?requestedTerm)
  ?e (skos:exactMatch|skos:closeMatch) ?term ;
     prov:hadPrimarySource ?pub .
  ?pub (ner:publicationDate|dcterms:issued) ?date .
  BIND(xsd:integer(SUBSTR(STR(?date),1,4)) AS ?year)
} GROUP BY ?term ?year ORDER BY ?year
```

**CQ19 — What hypotheticality is recorded for claims about an effect, by claim-source year?**
```sparql
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT ?pub ?effectKey ?year ?doi ?causeKey ?hypothetical WHERE {
 VALUES ?requestedKey { UNDEF }
 FILTER(!BOUND(?requestedKey) || ?effectKey = ?requestedKey)
  ?rel ner:hasEffect/ner:normalizedEntityKey ?effectKey ;
       ner:hasCause ?c ;
       ner:hasCurrentCausalRelationVersion/ner:causalHypothetical ?hypothetical .
  ?c ner:normalizedEntityKey ?causeKey .
  ?rel prov:hadPrimarySource ?pub .
  OPTIONAL { ?pub ner:doi ?doi }
  ?pub (ner:publicationDate|dcterms:issued) ?date .
  BIND(xsd:integer(SUBSTR(STR(?date),1,4)) AS ?year)
} ORDER BY ?year
```

**CQ20 — For how long was each version of a claim considered current
(validity intervals)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?rel ?rev ?from ?until WHERE {
  ?v a ner:CausalRelationVersion ; ner:versionOfCausalRelation ?rel ;
     ner:relationRevisionNumber ?rev .
  OPTIONAL { ?v ner:validFrom ?from } OPTIONAL { ?v ner:validUntil ?until }
} ORDER BY ?rel ?rev
```

## F. Extraction provenance

**CQ21 — Who extracted what, when, with which model version and
configuration?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?run ?agent ?cfg ?runId ?started ?ended ?agentVersion ?hash WHERE {
  ?run a ner:NERExtractionActivity ; ner:runIdentifier ?runId .
  OPTIONAL { ?run prov:startedAtTime ?started }
  OPTIONAL { ?run prov:endedAtTime ?ended }
  OPTIONAL { ?run prov:wasAssociatedWith ?agent . OPTIONAL { ?agent ner:agentVersion ?agentVersion } }
  OPTIONAL { ?run prov:used ?cfg . ?cfg a ner:ConfigurationArtifact ;
             ner:configurationHash ?hash }
}
```

**CQ22 — Full provenance walk for one entity: verbatim mention → paper →
extraction run → agent?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?e ?m ?dv ?pub ?key ?surface ?doi ?runId ?agentLabel WHERE {
 VALUES ?requestedKey { UNDEF }
 FILTER(!BOUND(?requestedKey) || ?key = ?requestedKey)
  ?e ner:normalizedEntityKey ?key ; ner:hasMention ?m .
  ?m ner:surfaceForm ?surface ; ner:partOfDocumentVersion ?dv .
  ?dv ner:versionOfDocument ?pub .
  OPTIONAL { ?pub ner:doi ?doi }
  OPTIONAL {
    ?m ner:hasCurrentAnnotationVersion/ner:inSnapshot ?snapshot .
    ?snapshot prov:wasGeneratedBy ?run .
    ?run ner:runIdentifier ?runId .
    OPTIONAL { ?run prov:wasAssociatedWith/rdfs:label ?agentLabel }
  }
}
```

**CQ23 — What classification confidence is recorded for each mention in the full audit profile?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?surface ?confidence WHERE {
  ?m a ner:EntityMention ; ner:surfaceForm ?surface ;
     ner:hasCurrentAnnotationVersion/ner:hasClassification/ner:classificationConfidence ?confidence .
} ORDER BY DESC(?confidence)
```

**CQ24b — What did each (LLM or human) judge decide about an item, on
which dimension, with what confidence — and what did the combiner conclude?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?surface ?dimension ?status ?conf ?agentVersion WHERE {
  ?m ner:surfaceForm ?surface ;
     ner:hasCurrentAnnotationVersion/ner:hasReviewDecision ?rd .
  ?rd ner:reviewDimension ?dimension ; ner:reviewStatus ?status .
  OPTIONAL { ?rd ner:reviewConfidence ?conf }
  OPTIONAL { ?rd prov:wasGeneratedBy/prov:wasAssociatedWith/ner:agentVersion ?agentVersion }
} ORDER BY ?surface ?dimension
```
(The combiner's decision carries dimension "ensemble-combined" and
`prov:wasDerivedFrom` each per-judge decision — the full ensemble is one
provenance walk.)

**CQ24 — Per-paper extraction density (entities, mentions)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?pub ?doi (COUNT(DISTINCT ?e) AS ?entities) (COUNT(DISTINCT ?m) AS ?mentions) WHERE {
 ?e a ner:NamedEntity ; ner:hasMention ?m .
 ?m ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
 OPTIONAL { ?pub ner:doi ?doi }
} GROUP BY ?pub ?doi ORDER BY ?pub
```

**CQ26r — Which entities/nodes are linked with a given entity (by key or by
its kb/bke IRI), in either direction, and via which predicates?** (CQ26 in the
ontology repository's list; renumbered here because G uses CQ25–CQ40.)
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?key ?direction ?predicate ?other ?otherLabel WHERE {
 VALUES ?requestedKey { UNDEF }
 FILTER(!BOUND(?requestedKey) || ?key = ?requestedKey)
  ?x ner:normalizedEntityKey ?key .   # or: VALUES ?x { <kb-or-bke-IRI> }
  { ?x ?predicate ?other . BIND("out" AS ?direction) }
  UNION
  { ?other ?predicate ?x . BIND("in" AS ?direction) }
  FILTER(isIRI(?other))
  FILTER(?predicate != <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>)
  OPTIONAL { ?other rdfs:label ?otherLabel }
} ORDER BY ?direction ?predicate
```
(Anchoring by IRI also answers the inverse: point ?x at an OBO term to list
every entity, from any paper, linked to that concept.)

## G. Scientific questions

These are the questions a neuroscientist asks of the graph, not of the pipeline.
They use only paper-stated structure (RO/BFO edges, `skos:broader`, `rdfs:seeAlso`,
`prov:used` / `prov:wasDerivedFrom` from kg_plan, reviewed by the claims judge) and
the causal module.

**CQ25 — Which cell types are located in which brain regions, according to which
papers?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?cell ?region ?pub ?doi ?negated ?modality ?context ?evidence WHERE {
  # every subclass of ner:CellType in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?cellClass {
    ner:Astrocyte ner:CellSubtype ner:CellType ner:EpendymalCell ner:ExcitatoryNeuron
    ner:GlialCell ner:InhibitoryNeuron ner:Interneuron ner:Microglia ner:MotorNeuron
    ner:NeuralCellType ner:NeuralStemCell ner:Neuron ner:Oligodendrocyte
    ner:ProjectionNeuron ner:SensoryNeuron
  }
  ?c a ?cellClass ; ner:normalizedEntityKey ?cell .
  ?assertion a ner:RelationAssertion ; ner:assertionSubject ?c ; ner:assertionPredicate obo:RO_0001025 ; ner:assertionObject ?r ; prov:hadPrimarySource ?pub .
  ?r ner:normalizedEntityKey ?region .
  OPTIONAL { ?pub ner:doi ?doi }
  OPTIONAL { ?assertion ner:assertionNegated ?negated }
  OPTIONAL { ?assertion ner:assertionModality ?modality }
  OPTIONAL { ?assertion ner:assertionContext ?context }
  OPTIONAL { ?assertion ner:evidenceText ?evidence }
} ORDER BY ?region ?cell
```

**CQ26 — Anatomical containment: which structures are stated to be part of which
(BFO:0000050), with their ontology terms where mapped?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
SELECT ?part ?whole ?partTerm ?wholeTerm WHERE {
  ?p obo:BFO_0000050 ?w ; ner:normalizedEntityKey ?part .
  ?w ner:normalizedEntityKey ?whole .
  OPTIONAL { ?p ner:resolvedToConcept/ner:conceptIdentifier ?partTerm }
  OPTIONAL { ?w ner:resolvedToConcept/ner:conceptIdentifier ?wholeTerm }
} ORDER BY ?whole ?part
```

**CQ27 — Which organisms, strains and life stages were studied, per paper?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?pub ?doi ?kind ?key WHERE {
  ?e a ?kind ; ner:normalizedEntityKey ?key ; prov:hadPrimarySource ?pub .
  # every subclass of ner:OrganismEntity in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?kind {
    ner:DevelopmentalStage ner:Genotype ner:LifeStage ner:Organism ner:Species
    ner:Strain
  }
 OPTIONAL { ?pub ner:doi ?doi }
} ORDER BY ?doi ?kind ?key
```

**CQ28 — Which transgenic lines / genotypes carry which indicator, effector or
regulatory element (RO:0000057 from the line)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?line ?carries ?carriesClass WHERE {
  VALUES ?lineClass { ner:Genotype ner:Strain ner:CellLine }
  ?l a ?lineClass ; ner:normalizedEntityKey ?line ;
     obo:RO_0000057 ?x .
  ?x ner:normalizedEntityKey ?carries ; a ?carriesClass .
  FILTER(STRSTARTS(STR(?carriesClass), STR(ner:)) && ?carriesClass != ner:NamedEntity)
}
```

**CQ29 — Which participants are explicitly linked to methods (RO:0000057)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?method ?participant WHERE {
  # every subclass of ner:Method, ner:ImagingModality, ner:ElectrophysiologyModality, ner:Intervention in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?methodClass {
    ner:Assay ner:ComputationalMethod ner:ElectrophysiologyModality
    ner:ExperimentalMethod ner:GeneticIntervention ner:ImagingModality ner:Intervention
    ner:Method ner:NeuroimagingModality ner:NeuromodulationIntervention
    ner:PharmacologicalIntervention ner:StatisticalMethod ner:Stimulation
    ner:SurgicalIntervention
  }
  ?m a ?methodClass ; ner:normalizedEntityKey ?method ; obo:RO_0000057 ?x .
  ?x ner:normalizedEntityKey ?participant .
} ORDER BY ?method
```

**CQ30 — Which chemical participants are linked to assays, and what broader classes are recorded?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?assay ?stimulus ?class WHERE {
  # every subclass of ner:Assay in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?assayClass {
    ner:Assay
  }
  # every subclass of ner:ChemicalEntity in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?stimulusClass {
    ner:AminoAcid ner:Carbohydrate ner:ChemicalEntity ner:Drug ner:Hormone ner:Ion
    ner:Lipid ner:Metabolite ner:Neuromodulator ner:Neurotransmitter
    ner:NeurotransmitterEntity ner:TherapeuticAgent ner:Toxin
  }
  ?a a ?assayClass ; ner:normalizedEntityKey ?assay ; obo:RO_0000057 ?s .
  ?s a ?stimulusClass ; ner:normalizedEntityKey ?stimulus .
  OPTIONAL { ?s skos:broader/ner:normalizedEntityKey ?class }
} ORDER BY ?class ?stimulus
```

**CQ31 — Which effects are supported by this paper's own INTERVENTION, and which
only by correlation, simulation or argument (what still needs an experiment)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?causeKey ?effectKey ?hypothetical ?basis ?evidence WHERE {
  ?rel ner:hasCause/ner:normalizedEntityKey ?causeKey ;
       ner:hasEffect/ner:normalizedEntityKey ?effectKey ;
       ner:hasCurrentCausalRelationVersion ?v .
  ?v ner:causalHypothetical ?hypothetical .
  OPTIONAL { ?v rdfs:comment ?evidence }
  OPTIONAL { ?v ner:causalEvidenceBasis ?b . BIND(STRAFTER(STR(?b), "causal-basis/") AS ?basis) }
} ORDER BY ?hypothetical ?causeKey
```

**CQ32 — Which effects have p < 0.05, grouped by measure and ordered by absolute value (units and study designs still need review)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?causeKey ?effectKey ?measure ?value ?p ?n WHERE {
  ?rel ner:hasCause/ner:normalizedEntityKey ?causeKey ;
       ner:hasEffect/ner:normalizedEntityKey ?effectKey ;
       ner:hasCurrentCausalRelationVersion/ner:hasEffectEstimate ?est .
  ?est ner:effectMeasure ?measure ; ner:effectValue ?value ; ner:pValue ?p .
  OPTIONAL { ?est ner:sampleSize ?n }
  FILTER(?p < 0.05)
} ORDER BY ?measure DESC(ABS(?value))
```

**CQ33 — What mediates, moderates or confounds a stated effect?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?causeKey ?effectKey ?role ?third WHERE {
  ?rel ner:hasCause/ner:normalizedEntityKey ?causeKey ;
       ner:hasEffect/ner:normalizedEntityKey ?effectKey .
  { ?rel ner:hasMediator ?t BIND("mediator" AS ?role) } UNION
  { ?rel ner:hasModerator ?t BIND("moderator" AS ?role) } UNION
  { ?rel ner:hasConfounder ?t BIND("confounder" AS ?role) }
  ?t ner:normalizedEntityKey ?third .
}
```

**CQ34 — Which entities have cross-references (rdfs:seeAlso), and what are their mappings (not evidence of homology by itself)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?a ?aTerm ?b ?bTerm WHERE {
  ?x rdfs:seeAlso ?y ; ner:normalizedEntityKey ?a .
  ?y a ner:NamedEntity ; ner:normalizedEntityKey ?b .
  OPTIONAL { ?x ner:resolvedToConcept/ner:conceptIdentifier ?aTerm }
  OPTIONAL { ?y ner:resolvedToConcept/ner:conceptIdentifier ?bTerm }
}
```

**CQ35 — How are the paper's measures derived (prov:wasDerivedFrom lineage from a
quantity back to the data it was computed from)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?measure ?derivedFrom WHERE {
  # every subclass of ner:Measurement in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?measureClass {
    ner:ElectrophysiologicalMeasurement ner:ImagingMeasurement ner:Measurement
    ner:MolecularMeasurement
  }
  ?m a ?measureClass ; ner:normalizedEntityKey ?measure ;
     prov:wasDerivedFrom+ ?src .
  ?src ner:normalizedEntityKey ?derivedFrom .
} ORDER BY ?measure
```

**CQ36 — Which software and algorithms does the analysis depend on, and for what
(prov:used)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?user ?uses ?usesClass WHERE {
  ?u prov:used ?x ; ner:normalizedEntityKey ?user .
  ?x ner:normalizedEntityKey ?uses ; a ?usesClass .
  FILTER(STRSTARTS(STR(?usesClass), STR(ner:)) && ?usesClass != ner:NamedEntity)
} ORDER BY ?uses
```

**CQ37 — Which direct broader relationships are materialized in the corpus?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?child ?parent ?parentTerm WHERE {
  ?c skos:broader ?p ; ner:normalizedEntityKey ?child .
  ?p ner:normalizedEntityKey ?parent .
  OPTIONAL { ?p ner:resolvedToConcept/ner:conceptIdentifier ?parentTerm }
} ORDER BY ?parent ?child
```

**CQ38 — Which entities without external mappings have the most mentions (including provisional BRAINKB concepts)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?e ?cls ?key (COUNT(DISTINCT ?m) AS ?mentions) WHERE {
  ?e a ner:NamedEntity , ?cls ; ner:normalizedEntityKey ?key ; ner:hasMention ?m .
  MINUS { SELECT DISTINCT ?e WHERE {
    ?e ner:resolvedToConcept ?c .
    ?c ner:conceptInOntologyVersion/ner:versionOfOntology/ner:ontologyAcronym ?acronym .
    FILTER(UCASE(STR(?acronym)) != "BRAINKB")
  } }
  FILTER(STRSTARTS(STR(?cls), STR(ner:)) && ?cls != ner:NamedEntity)
} GROUP BY ?e ?cls ?key ORDER BY DESC(?mentions)
```

**CQ39 — Which cause/effect pairs share polarity and negation across sources, and is any claim non-hypothetical?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?causeKey ?effectKey ?polarity ?negated (COUNT(DISTINCT ?pub) AS ?papers)
       (MAX(IF(?hyp = false, 1, 0)) AS ?anyNonHypothetical) WHERE {
  ?rel ner:hasCause/ner:normalizedEntityKey ?causeKey ;
       ner:hasEffect/ner:normalizedEntityKey ?effectKey ;
       prov:hadPrimarySource ?pub ;
       ner:hasCurrentCausalRelationVersion ?v .
  ?v ner:causalHypothetical ?hyp ; ner:causalNegated ?negated .
  OPTIONAL { ?v ner:causalPolarity ?polarity }
} GROUP BY ?causeKey ?effectKey ?polarity ?negated HAVING (COUNT(DISTINCT ?pub) > 1)
```

**CQ40 — Which explicit participates-in relationships are materialized (RO:0000056)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
SELECT ?entity ?process WHERE {
  ?e obo:RO_0000056 ?p ; ner:normalizedEntityKey ?entity .
  ?p ner:normalizedEntityKey ?process .
}
```

## H. Judge and mapping provenance (quality control as data)

**CQ41 — Which judges ran, with which model version and which exact prompt
(hash), in which mode?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?judge ?model ?prompt ?hash ?mode WHERE {
  ?act a ner:AutomaticValidationActivity ; rdfs:label ?judge ;
       prov:wasAssociatedWith ?ag .
  OPTIONAL { ?ag ner:agentVersion ?model }
  OPTIONAL { ?act ner:usedPrompt ?p . ?p rdfs:label ?prompt ; ner:promptHash ?hash }
  OPTIONAL { ?act rdfs:comment ?mode }
  FILTER(STRSTARTS(?judge, "judge:"))
} ORDER BY ?judge
```

**CQ42 — Every verdict on an entity, by which judge, with its confidence and
reason?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?key ?dimension ?status ?conf ?reason WHERE {
  ?e ner:normalizedEntityKey ?key ; ner:hasReviewDecision ?r .
  ?r ner:reviewDimension ?dimension ; ner:reviewStatus ?status .
  OPTIONAL { ?r ner:reviewConfidence ?conf }
  OPTIONAL { ?r ner:reviewComment ?reason }
} ORDER BY ?key ?dimension
```

**CQ43 — What did the judges change, and on whose authority (relabels, tier
changes, key renames, demoted mappings, dropped items and claims)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?kind ?key ?field ?old ?new ?reason ?licensedBy WHERE {
  ?cr a ?kind ; ner:changedField ?field ; ner:triggeredByActivity ?act .
  # every subclass of ner:ChangeRecord in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?kind {
    ner:CanonicalFormChangedChange ner:CausalConfidenceChangedChange
    ner:CausalContextChangedChange ner:CausalDirectionChangedChange
    ner:CausalModalityChangedChange ner:CausalPolarityChangedChange
    ner:CausalPredicateMappingChangedChange ner:CausalRelationAddedChange
    ner:CausalRelationChange ner:CausalRelationRemovedChange ner:CausalTypeChangedChange
    ner:ChangeRecord ner:ClassificationChangedChange ner:CoreferenceChangedChange
    ner:MappingAddedChange ner:MappingChangedChange ner:MappingDeprecatedChange
    ner:MappingRemovedChange ner:MentionAddedChange ner:MentionRemovedChange
    ner:OntologyConceptReplacedChange ner:ReviewStatusChangedChange
    ner:SpanChangedChange ner:SpecificityChangedChange
  }
  OPTIONAL { ?cr ner:changedEntity/ner:normalizedEntityKey ?key }
  OPTIONAL { ?cr ner:oldLiteralValue ?old } OPTIONAL { ?cr ner:newLiteralValue ?new }
  OPTIONAL { ?cr ner:changeReason ?reason }
  OPTIONAL { ?cr ner:hasReviewDecision/prov:wasGeneratedBy/rdfs:label ?licensedBy }
} ORDER BY ?kind ?key
```

**CQ44 — Which tool mappings did the mapping judge reject as meaning something
else (demoted: the IRI removed, the entity kept)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?key ?removedIri ?reason WHERE {
  ?cr a ner:MappingRemovedChange ; (ner:changedEntity|^ner:hasChangeRecord)/ner:normalizedEntityKey ?key ;
      ner:oldLiteralValue ?removedIri ; ner:changeReason ?reason .
}
```

**CQ45 — Which source produced each accepted mapping (trusted ontology vs local
hybrid vs BioPortal), by method?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?source ?method (COUNT(DISTINCT ?dec) AS ?decisions) WHERE {
  ?dec a ner:ConceptMappingDecision ; ner:mappingStatus <https://brainkb.org/ner/mapping-status/accepted> ; prov:wasGeneratedBy ?act .
  ?act rdfs:label ?source .
  OPTIONAL { ?act ner:usedMappingMethod ?m . BIND(STRAFTER(STR(?m), "mapping-method/") AS ?method) }
} GROUP BY ?source ?method ORDER BY DESC(?decisions)
```

**CQ46 — The quality summary of each snapshot (what the ensemble dropped,
demoted and fixed)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?snapshot ?label ?summary ?generated WHERE {
  ?snapshot a ner:ExtractionSnapshot ; ner:hasValidationReport ?report .
  OPTIONAL { ?report rdfs:comment ?summary }
  OPTIONAL { ?report rdfs:label ?label }
  OPTIONAL { ?snapshot prov:generatedAtTime ?generated }
}
```

**CQ47 — Which entities map to each selected ontology, at which tier (entity-level mappings)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?e ?key ?acronym ?curie ?conceptLabel ?tier WHERE {
 { SELECT DISTINCT ?e ?term ?tier WHERE { { ?e skos:exactMatch ?term . BIND("exact" AS ?tier) }
 UNION
{ ?e skos:closeMatch ?term . BIND("close" AS ?tier) }
 UNION
{ ?e skos:broadMatch ?term . BIND("broad" AS ?tier) }
 UNION
{ ?e skos:narrowMatch ?term . BIND("narrow" AS ?tier) }
 UNION
{ ?e skos:relatedMatch ?term . BIND("related" AS ?tier) } } }
 # StructSense stores conceptIRI as xsd:anyURI, while SKOS objects are IRIs.
 # Convert the bound SKOS object for an indexed lookup instead of a string cross-join.
 BIND(STRDT(STR(?term), xsd:anyURI) AS ?iri)
 ?c ner:conceptIRI ?iri ; ner:conceptIdentifier ?curie .
 ?e ner:normalizedEntityKey ?key ; ner:resolvedToConcept ?c .
 { SELECT DISTINCT ?c ?acronym WHERE {
   ?c ner:conceptInOntologyVersion/ner:versionOfOntology/ner:ontologyAcronym ?acronym .
 } }
 VALUES ?requestedOntology { UNDEF }
 FILTER(!BOUND(?requestedOntology) || STR(?acronym)=STR(?requestedOntology))
 OPTIONAL { ?c ner:preferredLabel ?conceptLabel }
} ORDER BY ?acronym ?curie ?key
```

**CQ48 — For each selected ontology term, which mapped entities exist and how many sources mention those entities?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?acronym ?conceptType (GROUP_CONCAT(DISTINCT ?key; separator=", ") AS ?entities)
       (COUNT(DISTINCT ?pub) AS ?papers) WHERE {
 VALUES ?requestedOntology { UNDEF }
 FILTER(!BOUND(?requestedOntology) || STR(?acronym) = STR(?requestedOntology))
  ?e ner:normalizedEntityKey ?key ; ner:resolvedToConcept ?c ;
     prov:hadPrimarySource ?pub .
  ?c ner:conceptIdentifier ?conceptType ;
     ner:conceptInOntologyVersion/ner:versionOfOntology/ner:ontologyAcronym ?acronym .
} GROUP BY ?acronym ?conceptType ORDER BY DESC(?papers)
```

**CQ49 — Which ontologies does the graph link into at all, and how many entities
does each anchor (the per-ontology coverage table)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?acronym (COUNT(DISTINCT ?e) AS ?entities) (COUNT(DISTINCT ?c) AS ?concepts) WHERE {
  ?e ner:resolvedToConcept ?c .
  ?c ner:conceptInOntologyVersion/ner:versionOfOntology/ner:ontologyAcronym ?acronym .
} GROUP BY ?acronym ORDER BY DESC(?entities)
```

## J. Phenotypes

**CQ50 — Which phenotypes does the corpus contain, at which level (behavioral, cognitive, electrophysiological, morphological, molecular, cellular, clinical/symptom), from which papers?**
```sparql
PREFIX ner:  <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?pub ?level ?key ?doi WHERE {
  # every subclass of ner:Phenotype in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?level {
    ner:BehavioralPhenotype ner:CellularPhenotype ner:ClinicalPhenotype
    ner:CognitivePhenotype ner:ElectrophysiologicalPhenotype ner:MolecularPhenotype
    ner:MorphologicalPhenotype ner:MorphologyClass ner:NeurologicalSign ner:Phenotype
    ner:Symptom
  }
  ?p a ?level ; ner:normalizedEntityKey ?key .
  OPTIONAL { ?p prov:hadPrimarySource ?pub }
 OPTIONAL { ?pub ner:doi ?doi }
} ORDER BY ?level ?key
```

**CQ51 — Which entities (genes, cell types, drugs, regions) bear which phenotypes (`RO:0002200 has phenotype`), and in which papers was the association stated?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?bearer ?phenotype ?pub ?doi ?evidence ?negated ?modality ?context WHERE {
 ?assertion a ner:RelationAssertion ; ner:assertionSubject ?c ; ner:assertionPredicate obo:RO_0002200 ; ner:assertionObject ?m ; prov:hadPrimarySource ?pub .
 ?c ner:normalizedEntityKey ?bearer . ?m ner:normalizedEntityKey ?phenotype .
 OPTIONAL { ?pub ner:doi ?doi }
 OPTIONAL { ?assertion ner:evidenceText ?evidence }
 OPTIONAL { ?assertion ner:assertionNegated ?negated }
 OPTIONAL { ?assertion ner:assertionModality ?modality }
 OPTIONAL { ?assertion ner:assertionContext ?context }
} ORDER BY ?bearer ?phenotype
```

**CQ52 — How well are phenotypes grounded in HP / MP / PATO, at which tier (the phenotype-vocabulary coverage report)?**
```sparql
PREFIX ner:  <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?vocab ?tier (COUNT(DISTINCT ?e) AS ?n) WHERE {
  # every subclass of ner:Phenotype in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?cls {
    ner:BehavioralPhenotype ner:CellularPhenotype ner:ClinicalPhenotype
    ner:CognitivePhenotype ner:ElectrophysiologicalPhenotype ner:MolecularPhenotype
    ner:MorphologicalPhenotype ner:MorphologyClass ner:NeurologicalSign ner:Phenotype
    ner:Symptom
  }
  VALUES (?p ?tier) { (skos:exactMatch "exact") (skos:closeMatch "close")
                      (skos:narrowMatch "narrow") (skos:broadMatch "broad") (skos:relatedMatch "related") }
  ?e a ?cls ; ?p ?obo .
  FILTER(STRSTARTS(STR(?obo), "http://purl.obolibrary.org/obo/"))
  BIND(STRBEFORE(STRAFTER(STR(?obo), "obo/"), "_") AS ?vocab)
 FILTER(?vocab IN ("HP", "MP", "PATO"))
} GROUP BY ?vocab ?tier ORDER BY ?vocab DESC(?n)
```

**CQ53 — Which causal claims have a phenotype as their EFFECT (genotype/intervention → phenotype), with negation/hypotheticality flags and any effect size?**
```sparql
PREFIX ner:  <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?causeKey ?phenotype ?negated ?hypothetical ?measure ?value WHERE {
  # every subclass of ner:Phenotype in named_entity_ontology.owl 2.5.0 (generated)
  VALUES ?cls {
    ner:BehavioralPhenotype ner:CellularPhenotype ner:ClinicalPhenotype
    ner:CognitivePhenotype ner:ElectrophysiologicalPhenotype ner:MolecularPhenotype
    ner:MorphologicalPhenotype ner:MorphologyClass ner:NeurologicalSign ner:Phenotype
    ner:Symptom
  }
  ?rel ner:hasEffect ?p ; ner:hasCause/ner:normalizedEntityKey ?causeKey ;
       ner:hasCurrentCausalRelationVersion ?v .
  ?p a ?cls ; ner:normalizedEntityKey ?phenotype .
  ?v ner:causalNegated ?negated ; ner:causalHypothetical ?hypothetical .
  OPTIONAL { ?v ner:hasEffectEstimate ?est .
             ?est ner:effectMeasure ?measure ; ner:effectValue ?value }
}
```

## K. Hierarchies extracted from the text

The extractor states hierarchy and relations alongside entities (per-mention
`relations` / `broader`, the cns-cells `cell_context`), resolved to entities by
scripts/relations.py and reviewed by the claims judge before they reach the graph.

**CQ54 — Which corpus-level broader paths connect leaves to ancestors (paths may combine sources and are not restricted to cells)?**
```sparql
PREFIX ner:  <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?leaf ?ancestor ?ancestorTerm WHERE {
  ?l skos:broader+ ?a ; ner:normalizedEntityKey ?leaf .
  FILTER NOT EXISTS { ?child skos:broader ?l }
  ?a ner:normalizedEntityKey ?ancestor .
  OPTIONAL { ?a ner:resolvedToConcept/ner:conceptIdentifier ?ancestorTerm }
} ORDER BY ?leaf ?ancestor
```

**CQ55 — Which cell-marker expression assertions are source-grounded, with their qualifiers and evidence?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX obo: <http://purl.obolibrary.org/obo/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT DISTINCT ?cell ?marker ?pub ?doi ?evidence ?negated ?modality ?context WHERE {
 ?assertion a ner:RelationAssertion ; ner:assertionSubject ?c ; ner:assertionPredicate obo:RO_0002292 ; ner:assertionObject ?m ; prov:hadPrimarySource ?pub .
 ?c ner:normalizedEntityKey ?cell . ?m ner:normalizedEntityKey ?marker .
 OPTIONAL { ?pub ner:doi ?doi }
 OPTIONAL { ?assertion ner:evidenceText ?evidence }
 OPTIONAL { ?assertion ner:assertionNegated ?negated }
 OPTIONAL { ?assertion ner:assertionModality ?modality }
 OPTIONAL { ?assertion ner:assertionContext ?context }
} ORDER BY ?cell ?marker
```

## L. How papers name and characterize cells

**CQ58 — How does each paper name each cell type, how is it classified, which ontology term is it mapped to, and in which species does that paper place it?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX obo: <http://purl.obolibrary.org/obo/>
SELECT ?cell (SAMPLE(?cellLabel) AS ?label)
       (GROUP_CONCAT(DISTINCT ?nerClass; separator=" | ") AS ?nerClasses)
       ?doi ?title
       (GROUP_CONCAT(DISTINCT ?name; separator=" | ") AS ?namesInPaper)
       (COUNT(DISTINCT ?m) AS ?mentions)
       (GROUP_CONCAT(DISTINCT ?mapping; separator=" ; ") AS ?ontologyMappings)
       (GROUP_CONCAT(DISTINCT ?species; separator=" ; ") AS ?speciesInPaper)
WHERE {
  ?e a ?t ; ner:normalizedEntityKey ?cell ; ner:normalizedEntityLabel ?cellLabel ; ner:hasMention ?m .
  FILTER (?t IN (ner:CellType, ner:CellSubtype, ner:NeuralCellType, ner:Neuron, ner:Interneuron,
                 ner:ExcitatoryNeuron, ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:MotorNeuron,
                 ner:SensoryNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia, ner:Oligodendrocyte,
                 ner:EpendymalCell, ner:NeuralStemCell))
  BIND (CONCAT("ner:", STRAFTER(STR(?t), "https://brainkb.org/ner/")) AS ?nerClass)
  ?m ner:surfaceForm ?sf ; ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
  BIND (REPLACE(STR(?sf), "\\s+", " ") AS ?name)
  OPTIONAL { ?pub ner:doi ?doi }
  OPTIONAL { ?pub ner:title ?title }
  OPTIONAL {
    ?e ner:resolvedToConcept ?c .
    ?c ner:conceptIRI ?iri ; ner:conceptIdentifier ?cid .
    OPTIONAL { ?c ner:preferredLabel ?plabel }
    ?e ?tier ?x .
    FILTER (?tier IN (skos:exactMatch, skos:closeMatch, skos:broadMatch, skos:narrowMatch, skos:relatedMatch)
            && STR(?x) = STR(?iri))
    BIND (CONCAT(?cid, ' "', COALESCE(?plabel, ""), '" [', STRAFTER(STR(?tier), "#"), "]",
                 IF(STRSTARTS(STR(?iri), "https://brainkb.org/concept/"), " (BrainKB default, no external term)", ""))
          AS ?mapping)
  }
  OPTIONAL {                       # species THIS paper states for the cell (in_taxon)
    ?sa a ner:RelationAssertion ; ner:assertionSubject ?e ; ner:assertionPredicate obo:RO_0002162 ;
        ner:assertionObject ?sp ; ner:assertionNegated false ; prov:hadPrimarySource ?pub .
    ?sp ner:normalizedEntityKey ?spKey .
    OPTIONAL {
      ?sp ner:resolvedToConcept ?spc . ?spc ner:conceptIdentifier ?spId .
      FILTER (STRSTARTS(?spId, "NCBITaxon:"))
      OPTIONAL { ?spc ner:preferredLabel ?spName }
    }
    BIND (IF(BOUND(?spId), CONCAT(COALESCE(?spName, ?spKey), " (", ?spId, ")"), ?spKey) AS ?species)
  }
}
GROUP BY ?cell ?doi ?title
ORDER BY ?cell ?doi
```

**CQ59 — Which cell types are named by more than one paper, and what does each paper call them?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?cell ?doi (GROUP_CONCAT(DISTINCT ?name; separator=" | ") AS ?namesInPaper) ?paperCount
WHERE {
  {
    SELECT ?e (COUNT(DISTINCT ?p) AS ?paperCount) WHERE {
      ?e a ?t ; ner:hasMention/ner:partOfDocumentVersion/ner:versionOfDocument ?p .
      FILTER (?t IN (ner:CellType, ner:CellSubtype, ner:NeuralCellType, ner:Neuron, ner:Interneuron,
                     ner:ExcitatoryNeuron, ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:MotorNeuron,
                     ner:SensoryNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia, ner:Oligodendrocyte,
                     ner:EpendymalCell, ner:NeuralStemCell))
    } GROUP BY ?e HAVING (COUNT(DISTINCT ?p) > 1)
  }
  ?e ner:normalizedEntityKey ?cell ; ner:hasMention ?m .
  ?m ner:surfaceForm ?sf ; ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
  BIND (REPLACE(STR(?sf), "\\s+", " ") AS ?name)
  OPTIONAL { ?pub ner:doi ?doi }
}
GROUP BY ?cell ?doi ?paperCount
ORDER BY DESC(?paperCount) ?cell ?doi
```

**CQ60 — Do differently named cells in different papers mean the same thing (same ontology concept)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?conceptId ?conceptLabel
       (GROUP_CONCAT(DISTINCT CONCAT(?cell, " [", STRAFTER(STR(?tier), "#"), "]"); separator=" ; ") AS ?entitiesWithTier)
       (GROUP_CONCAT(DISTINCT ?name; separator=" | ") AS ?namesUsed)
       (COUNT(DISTINCT ?pub) AS ?papers)
WHERE {
  ?e a ?t ; ner:normalizedEntityKey ?cell ; ner:resolvedToConcept ?c ; ner:hasMention ?m .
  FILTER (?t IN (ner:CellType, ner:CellSubtype, ner:NeuralCellType, ner:Neuron, ner:Interneuron,
                 ner:ExcitatoryNeuron, ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:MotorNeuron,
                 ner:SensoryNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia, ner:Oligodendrocyte,
                 ner:EpendymalCell, ner:NeuralStemCell))
  ?c ner:conceptIRI ?iri ; ner:conceptIdentifier ?conceptId .
  OPTIONAL { ?c ner:preferredLabel ?conceptLabel }
  FILTER (!STRSTARTS(STR(?iri), "https://brainkb.org/concept/"))   # external terms only
  ?e ?tier ?x . FILTER (?tier IN (skos:exactMatch, skos:closeMatch, skos:broadMatch, skos:narrowMatch, skos:relatedMatch)
                        && STR(?x) = STR(?iri))
  ?m ner:surfaceForm ?sf ; ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
  BIND (REPLACE(STR(?sf), "\\s+", " ") AS ?name)
}
GROUP BY ?conceptId ?conceptLabel
HAVING (COUNT(DISTINCT ?pub) > 1)
ORDER BY DESC(?papers)
```

**CQ61 — How does each paper characterize a cell (markers, regions, layers, species, phenotypes), with its evidence?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?cell ?doi ?predicate ?object ?negated ?evidence
WHERE {
  ?a a ner:RelationAssertion ; ner:assertionSubject ?e ; ner:assertionPredicate ?pred ;
     ner:assertionObject ?o ; ner:assertionNegated ?negated ; prov:hadPrimarySource ?pub .
  OPTIONAL { ?a ner:evidenceText ?ev }
  ?e a ?t ; ner:normalizedEntityKey ?cell .
  FILTER (?t IN (ner:CellType, ner:CellSubtype, ner:NeuralCellType, ner:Neuron, ner:Interneuron,
                 ner:ExcitatoryNeuron, ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:MotorNeuron,
                 ner:SensoryNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia, ner:Oligodendrocyte,
                 ner:EpendymalCell, ner:NeuralStemCell))
  ?o ner:normalizedEntityKey ?object .
  BIND (IF(?pred = <http://purl.obolibrary.org/obo/RO_0002292>, "expresses",
        IF(?pred = <http://purl.obolibrary.org/obo/RO_0001025>, "located_in",
        IF(?pred = <http://purl.obolibrary.org/obo/RO_0002162>, "in_taxon",
        IF(?pred = <http://purl.obolibrary.org/obo/RO_0002200>, "has_phenotype", STR(?pred))))) AS ?predicate)
  BIND (REPLACE(STR(?ev), "\\s+", " ") AS ?evidence)
  OPTIONAL { ?pub ner:doi ?doi }
}
ORDER BY ?cell ?doi ?predicate
```

**CQ62 — Where do papers agree or disagree about the same cell (each characteristic, how many papers assert it, how many negate it)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?cell ?predicate ?object
       (COUNT(DISTINCT IF(?negated = false, ?pub, ?none)) AS ?papersAsserting)
       (COUNT(DISTINCT IF(?negated = true, ?pub, ?none)) AS ?papersNegating)
       ?papersNamingCell
WHERE {
  {
    SELECT ?e (COUNT(DISTINCT ?p) AS ?papersNamingCell) WHERE {
      ?e ner:hasMention/ner:partOfDocumentVersion/ner:versionOfDocument ?p .
    } GROUP BY ?e HAVING (COUNT(DISTINCT ?p) > 1)
  }
  ?a a ner:RelationAssertion ; ner:assertionSubject ?e ; ner:assertionPredicate ?pred ;
     ner:assertionObject ?o ; ner:assertionNegated ?negated ; prov:hadPrimarySource ?pub .
  ?e a ?t ; ner:normalizedEntityKey ?cell .
  FILTER (?t IN (ner:CellType, ner:CellSubtype, ner:NeuralCellType, ner:Neuron, ner:Interneuron,
                 ner:ExcitatoryNeuron, ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:MotorNeuron,
                 ner:SensoryNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia, ner:Oligodendrocyte,
                 ner:EpendymalCell, ner:NeuralStemCell))
  ?o ner:normalizedEntityKey ?object .
  BIND (IF(CONTAINS(STR(?pred), "/obo/"), STRAFTER(STR(?pred), "/obo/"), REPLACE(STR(?pred), "^.*[/#]", "")) AS ?predicate)
}
GROUP BY ?cell ?predicate ?object ?papersNamingCell
ORDER BY ?cell DESC(?papersAsserting)
```

**CQ63 — Candidate same-meaning cells: differently keyed cells in different papers that share characteristics (markers, region, layer) — do they mean the same?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?cellA ?cellB (GROUP_CONCAT(DISTINCT ?shared; separator=" | ") AS ?sharedCharacteristics)
       (COUNT(DISTINCT ?shared) AS ?nShared)
WHERE {
  ?a1 a ner:RelationAssertion ; ner:assertionSubject ?e1 ; ner:assertionPredicate ?pred ;
      ner:assertionObject ?o ; ner:assertionNegated false ; prov:hadPrimarySource ?p1 .
  ?a2 a ner:RelationAssertion ; ner:assertionSubject ?e2 ; ner:assertionPredicate ?pred ;
      ner:assertionObject ?o ; ner:assertionNegated false ; prov:hadPrimarySource ?p2 .
  FILTER (?e1 != ?e2 && ?p1 != ?p2 && STR(?e1) < STR(?e2))
  FILTER (?pred != <http://purl.obolibrary.org/obo/RO_0002162>)   # sharing a species says little
  ?e1 a ?t1 ; ner:normalizedEntityKey ?cellA .
  ?e2 a ?t2 ; ner:normalizedEntityKey ?cellB .
  FILTER (?t1 IN (ner:CellType, ner:CellSubtype, ner:Neuron, ner:Interneuron, ner:ExcitatoryNeuron,
                  ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia,
                  ner:Oligodendrocyte, ner:NeuralStemCell))
  FILTER (?t2 IN (ner:CellType, ner:CellSubtype, ner:Neuron, ner:Interneuron, ner:ExcitatoryNeuron,
                  ner:InhibitoryNeuron, ner:ProjectionNeuron, ner:GlialCell, ner:Astrocyte, ner:Microglia,
                  ner:Oligodendrocyte, ner:NeuralStemCell))
  ?o ner:normalizedEntityKey ?obj .
  BIND (CONCAT(IF(CONTAINS(STR(?pred), "/obo/"), STRAFTER(STR(?pred), "/obo/"), REPLACE(STR(?pred), "^.*[/#]", "")), " ", ?obj) AS ?shared)
}
GROUP BY ?cellA ?cellB
HAVING (COUNT(DISTINCT ?shared) >= 2)
ORDER BY DESC(?nShared)
```

## M. Identity basis — what an entity is AND why

**CQ64 — Why is each cell mention identified as that cell type: its canonical type, hierarchy level, and every observed characteristic with its role and source?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?cell ?mention ?doi ?canonical ?level ?kind ?value ?role ?source ?polarity ?quote ?featureEntity
WHERE {
  ?e ner:normalizedEntityKey ?cell ; ner:hasMention ?m .
  ?m ner:surfaceForm ?sf ; ner:hasIdentityBasis ?b ; ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
  BIND (REPLACE(STR(?sf), "\\s+", " ") AS ?mention)
  OPTIONAL { ?pub ner:doi ?doi }
  OPTIONAL { ?b ner:canonicalCandidateLabel ?canonical }
  OPTIONAL { ?b ner:identityHierarchyLevel ?lv . BIND (STRAFTER(STR(?lv), "hierarchy-level/") AS ?level) }
  ?b ner:hasIdentityFeature ?f .
  ?f a ?fc ; ner:featureValue ?value ; ner:featureRole ?r ; ner:featureEvidenceSource ?s .
  FILTER (STRENDS(STR(?fc), "IdentityFeature") && ?fc != ner:IdentityFeature)
  BIND (REPLACE(STRAFTER(STR(?fc), "https://brainkb.org/ner/"), "IdentityFeature$", "") AS ?kind)
  BIND (STRAFTER(STR(?r), "feature-role/") AS ?role)
  BIND (STRAFTER(STR(?s), "feature-source/") AS ?source)
  OPTIONAL { ?f ner:featurePolarity ?polarity }
  OPTIONAL { ?f ner:featureQuote ?quote }
  OPTIONAL { ?f ner:featureEntity/ner:normalizedEntityKey ?featureEntity }
}
ORDER BY ?cell ?mention (IF(?role = "defining", 0, IF(?role = "supporting", 1, IF(?role = "excluding", 2, 3))))
```

**CQ65 — Which characteristics justify (or contradict) each cell-type ontology mapping?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?cell ?conceptId ?conceptLabel ?status
       (GROUP_CONCAT(DISTINCT ?just; separator=" ; ") AS ?justifiedBy)
       (GROUP_CONCAT(DISTINCT ?contra; separator=" ; ") AS ?contradictedBy)
WHERE {
  ?dec a ner:ConceptMappingDecision ; ner:decisionForNormalizedEntity ?e ;
       ner:selectedCandidate/ner:candidateConcept ?c .
  ?e ner:normalizedEntityKey ?cell .
  ?c ner:conceptIdentifier ?conceptId .
  OPTIONAL { ?c ner:preferredLabel ?conceptLabel }
  OPTIONAL { ?dec ner:mappingStatus ?st . BIND (STRAFTER(STR(?st), "mapping-status/") AS ?status) }
  OPTIONAL { ?dec ner:justifiedByIdentityFeature ?jf . ?jf ner:featureValue ?jv ; ner:featureRole ?jr .
             BIND (CONCAT(?jv, " (", STRAFTER(STR(?jr), "feature-role/"), ")") AS ?just) }
  OPTIONAL { ?dec ner:contradictedByIdentityFeature ?cf . ?cf ner:featureValue ?cv ; ner:featureRole ?cr .
             BIND (CONCAT(?cv, " (", STRAFTER(STR(?cr), "feature-role/"), ")") AS ?contra) }
  FILTER (BOUND(?just) || BOUND(?contra))
}
GROUP BY ?cell ?conceptId ?conceptLabel ?status
ORDER BY ?cell
```

**CQ66 — Do different papers identify the same cell type by the same defining characteristics?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?cell ?doi
       (GROUP_CONCAT(DISTINCT ?def; separator=" ; ") AS ?definingInPaper)
       (GROUP_CONCAT(DISTINCT ?sup; separator=" ; ") AS ?supportingInPaper)
       (GROUP_CONCAT(DISTINCT ?ctx; separator=" ; ") AS ?contextInPaper)
WHERE {
  ?e ner:normalizedEntityKey ?cell ; ner:hasMention ?m .
  ?m ner:hasIdentityBasis ?b ; ner:partOfDocumentVersion/ner:versionOfDocument ?pub .
  OPTIONAL { ?pub ner:doi ?doi }
  OPTIONAL { ?b ner:hasIdentityFeature ?f1 . ?f1 ner:featureRole <https://brainkb.org/ner/feature-role/defining> ; ner:featureValue ?def }
  OPTIONAL { ?b ner:hasIdentityFeature ?f2 . ?f2 ner:featureRole <https://brainkb.org/ner/feature-role/supporting> ; ner:featureValue ?sup }
  OPTIONAL { ?b ner:hasIdentityFeature ?f3 . ?f3 ner:featureRole <https://brainkb.org/ner/feature-role/contextual> ; ner:featureValue ?ctx }
}
GROUP BY ?cell ?doi
ORDER BY ?cell ?doi
```

**CQ67 — Cell mentions with no stated identity basis, or with no defining characteristic (named but never characterized)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?cell (COUNT(DISTINCT ?m) AS ?mentions) (COUNT(DISTINCT ?mb) AS ?mentionsWithBasis)
       (COUNT(DISTINCT ?md) AS ?mentionsWithDefiningFeature)
WHERE {
  ?e a ?t ; ner:normalizedEntityKey ?cell ; ner:hasMention ?m .
  FILTER (?t IN (ner:CellType, ner:CellSubtype, ner:Neuron, ner:Interneuron, ner:GlialCell))
  OPTIONAL { ?m ner:hasIdentityBasis ?b . BIND (?m AS ?mb) }
  OPTIONAL { ?m ner:hasIdentityBasis/ner:hasIdentityFeature/ner:featureRole <https://brainkb.org/ner/feature-role/defining> .
             BIND (?m AS ?md) }
}
GROUP BY ?cell
ORDER BY ?mentionsWithDefiningFeature ?cell
```

**CQ68 — For each cell mention: what is it (canonical type, level, specificity, ontology term) and why does it qualify — one row per characteristic (kind, value, role, source), whether it justified the mapping, with every node IRI?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?entity ?cell ?mentionNode ?mention ?paper ?doi
       ?salience ?attributedTo ?definitionalRole
       ?identityBasis ?canonicalType ?hierarchyLevel ?specificity ?coordinatedElements
       ?concept ?ontologyTerm
       ?feature ?featureKind ?featureValue ?role ?source ?polarity ?quote
       ?linkedEntityNode ?linkedEntity ?justifiesMapping
WHERE {
  ?entity ner:normalizedEntityKey ?cell ; ner:hasMention ?mentionNode .
  ?mentionNode ner:surfaceForm ?sf ; ner:hasIdentityBasis ?identityBasis ;
               ner:partOfDocumentVersion/ner:versionOfDocument ?paper .
  BIND (REPLACE(STR(?sf), "\\s+", " ") AS ?mention)
  OPTIONAL { ?paper ner:doi ?doi }
  OPTIONAL { ?mentionNode ner:mentionSalience ?sal . BIND (STRAFTER(STR(?sal), "salience/") AS ?salience) }
  OPTIONAL { ?mentionNode ner:attributedTo ?att . BIND (STRAFTER(STR(?att), "attribution/") AS ?attributedTo) }
  OPTIONAL { ?mentionNode ner:definitionalRole ?dr . BIND (STRAFTER(STR(?dr), "definitional-role/") AS ?definitionalRole) }
  OPTIONAL { ?identityBasis ner:canonicalCandidateLabel ?canonicalType }
  OPTIONAL { ?identityBasis ner:identityHierarchyLevel ?lv . BIND (STRAFTER(STR(?lv), "hierarchy-level/") AS ?hierarchyLevel) }
  OPTIONAL { ?identityBasis ner:identitySpecificity ?sp . BIND (STRAFTER(STR(?sp), "specificity/") AS ?specificity) }  # cell_phenotype | cell_vague | cell_hetero
  OPTIONAL { ?mentionNode ner:coordinatedElementCount ?coordinatedElements }   # a coordinated span names this many cells
  OPTIONAL { ?entity ner:resolvedToConcept ?concept . ?concept ner:conceptIdentifier ?ontologyTerm }   # WHAT
  OPTIONAL {                                                                    # WHY: one row per characteristic
    ?identityBasis ner:hasIdentityFeature ?feature .
    ?feature a ?fc ; ner:featureValue ?featureValue ; ner:featureRole ?r ; ner:featureEvidenceSource ?s .
    FILTER (STRENDS(STR(?fc), "IdentityFeature") && ?fc != ner:IdentityFeature)
    BIND (REPLACE(STRAFTER(STR(?fc), "https://brainkb.org/ner/"), "IdentityFeature$", "") AS ?featureKind)
    BIND (STRAFTER(STR(?r), "feature-role/") AS ?role)
    BIND (STRAFTER(STR(?s), "feature-source/") AS ?source)
    OPTIONAL { ?feature ner:featurePolarity ?polarity }
    OPTIONAL { ?feature ner:featureQuote ?quote }
    OPTIONAL { ?feature ner:featureEntity ?linkedEntityNode . ?linkedEntityNode ner:normalizedEntityKey ?linkedEntity }
    BIND (EXISTS { ?dec ner:justifiedByIdentityFeature ?feature } AS ?justifiesMapping)
  }
}
ORDER BY ?cell ?doi ?mention ?featureKind ?featureValue
```

**CQ69 — Which papers actually characterize a given cell (rank by document focus and subject/supporting mentions, never raw counts)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?cell ?paper ?doi ?documentFocus
       (COUNT(DISTINCT ?subj) AS ?subjectMentions) (COUNT(DISTINCT ?supp) AS ?supportingMentions)
       (COUNT(DISTINCT ?inc) AS ?incidentalMentions) (COUNT(DISTINCT ?defn) AS ?mentionsWithDefiningFeature)
WHERE {
  ?e ner:normalizedEntityKey ?cell ; ner:hasMention ?m .
  ?m ner:partOfDocumentVersion/ner:versionOfDocument ?paper .
  OPTIONAL { ?paper ner:doi ?doi }
  OPTIONAL { ?paper ner:documentFocus ?f . BIND (STRAFTER(STR(?f), "document-focus/") AS ?documentFocus) }
  OPTIONAL { ?m ner:mentionSalience <https://brainkb.org/ner/salience/subject_of_passage> . BIND (?m AS ?subj) }
  OPTIONAL { ?m ner:mentionSalience <https://brainkb.org/ner/salience/supporting_evidence> . BIND (?m AS ?supp) }
  OPTIONAL { ?m ner:mentionSalience <https://brainkb.org/ner/salience/incidental_mention> . BIND (?m AS ?inc) }
  OPTIONAL { ?m ner:hasIdentityBasis/ner:hasIdentityFeature/ner:featureRole <https://brainkb.org/ner/feature-role/defining> .
             BIND (?m AS ?defn) }
}
GROUP BY ?cell ?paper ?doi ?documentFocus
ORDER BY ?cell (IF(?documentFocus = "subject", 0, IF(?documentFocus = "substantial", 1, 2)))
         DESC(?mentionsWithDefiningFeature) DESC(?subjectMentions) DESC(?supportingMentions)
```

**CQ70 — Which cell mentions are only incidental, attributed to cited work, contrastive or negative (valid mentions that are not this paper's evidence)?**
```sparql
PREFIX ner: <https://brainkb.org/ner/>
SELECT ?cell ?entity ?mentionNode ?mention ?doi ?salience ?attributedTo ?sentence
WHERE {
  ?entity ner:normalizedEntityKey ?cell ; ner:hasMention ?mentionNode .
  ?mentionNode ner:surfaceForm ?sf ; ner:partOfDocumentVersion/ner:versionOfDocument ?paper .
  BIND (REPLACE(STR(?sf), "\\s+", " ") AS ?mention)
  OPTIONAL { ?paper ner:doi ?doi }
  OPTIONAL { ?mentionNode ner:mentionSalience ?sal . BIND (STRAFTER(STR(?sal), "salience/") AS ?salience) }
  OPTIONAL { ?mentionNode ner:attributedTo ?att . BIND (STRAFTER(STR(?att), "attribution/") AS ?attributedTo) }
  OPTIONAL { ?mentionNode ner:inSentence/ner:sentenceText ?st . BIND (REPLACE(STR(?st), "\\s+", " ") AS ?sentence) }
  FILTER (?salience IN ("incidental_mention", "background_citation", "contrastive_aside", "negative_statement", "out_of_scope")
          || ?attributedTo IN ("cited_work", "hypothetical"))
}
ORDER BY ?cell ?doi
```
