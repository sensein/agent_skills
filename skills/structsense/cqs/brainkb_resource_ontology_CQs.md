# BrainKB Resource Ontology — competency questions

The questions a resource KG (`scripts/resource_kg.py`, BrainKB Resource Ontology
`default_ontology/brainkb_resource_ontology.owl`) is accountable for. Queries are the
BKR 0.5.6 bundle's `sparql/cq01–cq21`, unchanged.

Instance IRIs are UUIDs: queries address resources by name, version or
`dcterms:identifier`, never by a readable IRI.

**Two retrieval paths need entailment.** `bkr:hasScope` (super-property of the four
scope properties) and `bkr:appliedToConcept` (the chain `hasScope ∘
appliesToConcept`, deliberately excluding excluded scope) are entailed, never
asserted, and most questions use `bkr:hasScope`. Run with the ontology and OWL-RL:

```bash
python cqs/run_cqs.py out/ --cqs cqs/brainkb_resource_ontology_CQs.md \
    --with-ontology default_ontology/brainkb_resource_ontology.owl --entail
```

`--entail` needs `pip install owlrl`. Questions about things an extraction from a
paper does not produce (identifier resolution attempts, dated observations,
project outputs, violated assumptions recorded by a run) are answerable once a
harvester or curator adds those records; until then they return no rows.

**CQ01 — Which catalogued resources apply to human (NCBITaxon:9606), under which kind of scope claim, and on what evidence?**

Which catalogued resources apply to human (NCBITaxon:9606), under which kind of scope claim, and on what evidence? Scope kinds include the weakest one: a facet recovered from curated text, reported as such.

Source: BKR sparql/cq01_resources_for_species.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?name ?claim ?evidence WHERE {
  ?r bkr:hasScope ?scope .
  ?scope bkrls:appliesToTaxon obo:NCBITaxon_9606 .
  # A scope typed only bkr:ApplicabilityScope is neither declared nor validated nor observed: it is
  # what an enrichment step recovered from a curator's prose. It belongs in this answer with its
  # claim kind visible, rather than being invisible to the question.
  {
    VALUES (?scopeClass ?claim) {
      (bkr:DeclaredScope "declared") (bkr:ValidatedScope "validated")
      (bkr:ObservedScope "observed in use") (bkr:OutOfScope "out of scope") }
    ?scope a ?scopeClass .
  } UNION {
    ?scope a bkr:ApplicabilityScope .
    FILTER NOT EXISTS { ?scope a bkr:DeclaredScope }
    FILTER NOT EXISTS { ?scope a bkr:ValidatedScope }
    FILTER NOT EXISTS { ?scope a bkr:ObservedScope }
    FILTER NOT EXISTS { ?scope a bkr:OutOfScope }
    BIND("recovered from text" AS ?claim)
  }
  OPTIONAL { ?scope bkr:scopeEvidenceLevel ?ev . ?ev skos:prefLabel ?evidence }
  OPTIONAL { ?r bkr:resourceName ?n1 }
  OPTIONAL { ?r bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?r bkr:versionIdentifier ?vid }
  BIND(COALESCE(?n1, ?n2, REPLACE(STR(?r), "^.*/", "ex:")) AS ?base)
  BIND(IF(BOUND(?vid), CONCAT(?base, " v", ?vid), ?base) AS ?name)
} ORDER BY ?name ?claim
```

**CQ02 — For each resource: which taxa are declared, validated, out of scope, observed in use?**

Source: BKR sparql/cq02_declared_vs_validated.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?name ?claim (GROUP_CONCAT(DISTINCT ?taxon; separator=", ") AS ?taxa) WHERE {
  VALUES (?scopeClass ?claim) {
    (bkr:DeclaredScope "1 declared") (bkr:ValidatedScope "2 validated")
    (bkr:ObservedScope "3 observed in use") (bkr:OutOfScope "4 out of scope") }
  ?r bkr:hasScope ?scope .
  ?scope a ?scopeClass ; bkrls:appliesToTaxon ?t .
  BIND(REPLACE(STR(?t), "^.*/obo/", "") AS ?taxon)
  OPTIONAL { ?r bkr:resourceName ?n1 }
  OPTIONAL { ?r bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?r bkr:versionIdentifier ?vid }
  BIND(COALESCE(?n1, ?n2, REPLACE(STR(?r), "^.*/", "ex:")) AS ?base)
  BIND(IF(BOUND(?vid), CONCAT(?base, " v", ?vid), ?base) AS ?name)
} GROUP BY ?name ?claim ORDER BY ?name ?claim
```

**CQ03 — Which resources have been applied to a taxon outside their declared scope?**

Source: BKR sparql/cq03_applied_outside_declared_scope.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?name ?offTaxon ?statement WHERE {
  ?r bkr:hasObservedScope ?obs .
  ?obs bkrls:appliesToTaxon ?t .
  OPTIONAL { ?obs bkr:scopeStatement ?statement }
  FILTER NOT EXISTS { ?r bkr:hasDeclaredScope ?dec . ?dec bkrls:appliesToTaxon ?t }
  BIND(REPLACE(STR(?t), "^.*/obo/", "") AS ?offTaxon)
  OPTIONAL { ?r bkr:resourceName ?n1 }
  OPTIONAL { ?r bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?r bkr:versionIdentifier ?vid }
  BIND(COALESCE(?n1, ?n2, REPLACE(STR(?r), "^.*/", "ex:")) AS ?base)
  BIND(IF(BOUND(?vid), CONCAT(?base, " v", ?vid), ?base) AS ?name)
}
```

**CQ04 — Which critical assumptions condition each resource, what breaks if they fail, and who says so?**

Source: BKR sparql/cq04_critical_assumptions.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?resource ?assumptionType ?statement ?consequence ?status WHERE {
  ?r bkr:resourceName ?resource ; bkr:hasAssumption ?a .
  ?a bkr:assumptionCriticality <https://brainkb.org/resource/criticality/critical> ; a ?assumptionType ; bkr:assumptionStatement ?statement .
  OPTIONAL { ?a bkr:violationConsequence ?consequence }
  OPTIONAL { ?a bkr:assumptionStatus ?st . ?st skos:prefLabel ?status }
  FILTER(STRSTARTS(STR(?assumptionType), "https://brainkb.org/resource"))
} ORDER BY ?resource
```

**CQ05 — Which runs violated an assumption of the resource they used, and with what consequence?**

Source: BKR sparql/cq05_violated_assumptions.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?run ?statement ?consequence ?criticality WHERE {
  ?a bkr:assumptionViolatedIn ?run ; bkr:assumptionStatement ?statement .
  OPTIONAL { ?a bkr:violationConsequence ?consequence }
  OPTIONAL { ?a bkr:assumptionCriticality ?c . ?c skos:prefLabel ?criticality }
}
```

**CQ06 — Which resources did each project produce, and under which DUO permissions and modifiers?**

Source: BKR sparql/cq06_project_outputs_and_data_use.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?project ?name (GROUP_CONCAT(DISTINCT ?permission; separator=", ") AS ?permissions)
       (GROUP_CONCAT(DISTINCT ?modifier; separator=", ") AS ?modifiers) WHERE {
  ?r bkr:producedByProject ?proj .
  ?proj rdfs:label ?project .
  OPTIONAL {
    { ?r bkrls:hasDataUseCondition ?duc } UNION { ?r bkr:versionOf/bkrls:hasDataUseCondition ?duc }
    OPTIONAL { ?duc bkrls:hasDataUsePermission ?p }
    OPTIONAL { ?duc bkrls:hasDataUseModifier ?m }
  }
  BIND(COALESCE(REPLACE(STR(?p), "^.*/obo/", ""), "") AS ?permission)
  BIND(COALESCE(REPLACE(STR(?m), "^.*/obo/", ""), "") AS ?modifier)
  OPTIONAL { ?r bkr:resourceName ?n1 }
  OPTIONAL { ?r bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?r bkr:versionIdentifier ?vid }
  BIND(COALESCE(?n1, ?n2, REPLACE(STR(?r), "^.*/", "ex:")) AS ?base)
  BIND(IF(BOUND(?vid), CONCAT(?base, " v", ?vid), ?base) AS ?name)
} GROUP BY ?project ?name ORDER BY ?project ?name
```

**CQ07 — Which resources are deprecated, superseded or withdrawn, and what should be used instead?**

Source: BKR sparql/cq07_deprecated_what_replaces.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?name ?status ?replacement ?reason ?guidance WHERE {
  ?r bkr:lifecycleStatus ?s .
  ?s skos:prefLabel ?status .
  FILTER(?s IN (<https://brainkb.org/resource/lifecycle-status/deprecated>, <https://brainkb.org/resource/lifecycle-status/superseded>, <https://brainkb.org/resource/lifecycle-status/withdrawn>))
  OPTIONAL { ?r bkr:isSupersededBy ?rep .
    OPTIONAL { ?rep bkr:resourceName ?repn } OPTIONAL { ?rep bkr:versionIdentifier ?repv }
    BIND(COALESCE(?repn, CONCAT("version ", ?repv), REPLACE(STR(?rep), "^.*/", "ex:")) AS ?replacement) }
  OPTIONAL { ?r bkr:deprecationReason ?reason }
  OPTIONAL { ?r bkr:migrationGuidance ?guidance }
  OPTIONAL { ?r bkr:resourceName ?n1 }
  OPTIONAL { ?r bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?r bkr:versionIdentifier ?vid }
  BIND(COALESCE(?n1, ?n2, REPLACE(STR(?r), "^.*/", "ex:")) AS ?base)
  BIND(IF(BOUND(?vid), CONCAT(?base, " v", ?vid), ?base) AS ?name)
} ORDER BY ?name
```

**CQ08 — Which changes are breaking, what kind of change were they, what triggered them, and who made them?**

Source: BKR sparql/cq08_breaking_changes.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?affected ?changeType ?trigger ?reason ?when ?agent WHERE {
  ?c bkr:isBreakingChange true ; bkr:changeAffectsResource ?res .
  OPTIONAL { ?c bkr:changeType ?ct . ?ct skos:prefLabel ?changeType }
  OPTIONAL { ?c ner:changeTrigger ?tg . BIND(REPLACE(STR(?tg), "^.*/change-trigger/", "") AS ?trigger) }
  OPTIONAL { ?c ner:changeReason ?reason }
  OPTIONAL { ?c prov:generatedAtTime ?when }
  OPTIONAL { ?c prov:wasAttributedTo ?a . OPTIONAL { ?a rdfs:label ?al } BIND(COALESCE(?al, STR(?a)) AS ?agent) }
  OPTIONAL { ?res bkr:resourceName ?n } OPTIONAL { ?res bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?res bkr:versionIdentifier ?v }
  BIND(CONCAT(COALESCE(?n, ?n2, STR(?res)), IF(BOUND(?v), CONCAT(" v", ?v), "")) AS ?affected)
} ORDER BY ?when
```

**CQ09 — What is the curation state of each catalogue record, who or what produced it, and when is it due for review?**

Source: BKR sparql/cq09_record_review_state.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?resource ?recordVersion ?reviewStatus ?producedBy ?lastVerified ?due WHERE {
  ?rec a dcat:CatalogRecord ; bkr:describesResource ?res .
  OPTIONAL { ?res bkr:resourceName ?resource }
  OPTIONAL { ?rec bkr:recordVersion ?recordVersion }
  OPTIONAL { ?rec bkr:hasReviewDecision ?rd . ?rd ner:reviewStatus ?rs .
             BIND(REPLACE(STR(?rs), "^.*/review-status/", "") AS ?reviewStatus) }
  OPTIONAL { ?rec prov:wasAttributedTo ?ag . OPTIONAL { ?ag rdfs:label ?agl } BIND(COALESCE(?agl, STR(?ag)) AS ?producedBy) }
  OPTIONAL { ?rec bkr:lastVerified ?lastVerified }
  OPTIONAL { ?rec bkr:nextReviewDue ?due }
} ORDER BY ?resource ?recordVersion
```

**CQ10 — Audit of concept mappings: which were published, by what method, with what status — and which were withheld.**

Source: BKR sparql/cq10_mapping_audit.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?field ?concept ?relation ?status ?method ?rawProvenance ?confidence WHERE {
  ?m a ner:ConceptMappingDecision .
  OPTIONAL { ?m bkr:mappedField ?field }
  OPTIONAL { ?m ner:conceptIdentifier ?concept }
  OPTIONAL { ?m ner:mappingRelationType ?rel . BIND(REPLACE(STR(?rel), "^.*/mapping-relation/", "") AS ?relation) }
  OPTIONAL { ?m ner:mappingStatus ?st . BIND(REPLACE(STR(?st), "^.*/mapping-status/", "") AS ?status) }
  OPTIONAL { ?m prov:wasGeneratedBy/ner:usedMappingMethod ?mm . BIND(REPLACE(STR(?mm), "^.*/mapping-method/", "") AS ?method) }
  OPTIONAL { ?m ner:conceptMappingProvenanceRaw ?rawProvenance }
  OPTIONAL { ?m ner:decisionConfidence ?confidence }
} ORDER BY ?status ?concept
```

**CQ11 — Full provenance of a dataset version: generating activity, inputs, responsible agents.**

Source: BKR sparql/cq11_dataset_provenance_chain.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?version ?activity ?input ?agent WHERE {
  ?v bkr:versionOf ?ds ; bkr:versionIdentifier ?version ; prov:wasGeneratedBy ?act .
  ?ds bkr:resourceName ?dsname .
  BIND(STR(?act) AS ?activity)
  OPTIONAL { ?act bkr:usedResource ?in . OPTIONAL { ?in bkr:resourceName ?inname } BIND(COALESCE(?inname, STR(?in)) AS ?input) }
  OPTIONAL { ?act prov:wasAssociatedWith ?ag . OPTIONAL { ?ag rdfs:label ?agl } BIND(COALESCE(?agl, STR(?ag)) AS ?agent) }
  FILTER(CONTAINS(?dsname, "primary motor cortex"))
} ORDER BY ?version
```

**CQ12 — If a reference resource version is superseded, which resources and activities depend on it? (addressed by name and version, since instance IRIs are UUIDs)**

Source: BKR sparql/cq12_impact_of_superseding_a_reference.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?reference ?dependent ?relation WHERE {
  ?ref bkr:versionIdentifier "2.0" .
  OPTIONAL { ?ref bkr:resourceName ?n1 }
  OPTIONAL { ?ref bkr:versionOf/bkr:resourceName ?n2 }
  BIND(COALESCE(?n1, ?n2) AS ?reference)
  FILTER(BOUND(?reference) && CONTAINS(LCASE(?reference), "taxonomy"))
  { ?r bkr:hasRequirement/bkr:requiredResource ?ref . ?r bkr:resourceName ?dependent .
    BIND("requires as reference" AS ?relation) }
  UNION { ?act bkr:usedResource ?ref . OPTIONAL { ?act rdfs:label ?al }
          BIND(COALESCE(?al, STR(?act)) AS ?dependent) BIND("used in activity" AS ?relation) }
  UNION { ?r bkr:mentions ?ref ; bkr:resourceName ?dependent . BIND("mentions" AS ?relation) }
} ORDER BY ?relation ?dependent
```

**CQ13 — Gap report: runnable or applicable resources with no applicability scope and no parent version to inherit one from.**

Source: BKR sparql/cq13_scope_gaps.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?name ?class WHERE {
  VALUES ?class { schema:SoftwareApplication bkr:SoftwareLibrary bkr:Pipeline bkr:Workflow
                  bkr:ComputationalModel bkr:DeviceResource bkrls:Atlas bkrls:CellTypeTaxonomy }
  ?r a ?class .
  OPTIONAL { ?r bkr:resourceName ?n1 }
  OPTIONAL { ?r bkr:versionOf/bkr:resourceName ?n2 }
  OPTIONAL { ?r bkr:versionIdentifier ?vid }
  BIND(COALESCE(?n1, ?n2, REPLACE(STR(?r), "^.*/", "ex:")) AS ?base)
  BIND(IF(BOUND(?vid), CONCAT(?base, " v", ?vid), ?base) AS ?name)
  FILTER NOT EXISTS { ?r bkr:hasScope ?s }
  FILTER NOT EXISTS { ?r bkr:versionOf/bkr:hasScope ?s2 }
} ORDER BY ?name
```

**CQ14 — Coverage: which concepts does the catalogue claim to cover, on which dimension, across how many resources?**

Source: BKR sparql/cq14_coverage_matrix.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?dimension ?concept (COUNT(DISTINCT ?r) AS ?resources) WHERE {
  ?r bkr:hasScope ?s .
  ?s ?dim ?c .
  ?dim rdfs:subPropertyOf* bkr:appliesToConcept .
  OPTIONAL { ?dim bkr:scopeDimensionLabel ?dl }
  BIND(COALESCE(?dl, REPLACE(STR(?dim), "^.*/appliesTo", "")) AS ?dimension)
  BIND(REPLACE(REPLACE(STR(?c), "^.*/obo/", ""), "^.*/", "") AS ?concept)
} GROUP BY ?dimension ?concept ORDER BY ?dimension ?concept
```

**CQ15 — Which catalogue statements came from a machine agent, what text anchors them, and have they been reviewed?**

Source: BKR sparql/cq15_machine_vs_human_metadata.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?subject ?property ?value ?quote ?sentence ?agent ?review WHERE {
  ?a a bkr:ResourceAssertion ; bkr:assertionAbout ?subj ; bkr:assertedProperty ?prop ;
     prov:wasAttributedTo ?ag .
  ?ag a ner:LanguageModelAgent .
  OPTIONAL { ?subj bkr:resourceName ?sn } BIND(COALESCE(?sn, REPLACE(STR(?subj), "^.*/", "ex:")) AS ?subject)
  BIND(REPLACE(STR(?prop), "^.*/", "") AS ?property)
  OPTIONAL { ?a bkr:assertedValueResource ?v . BIND(REPLACE(STR(?v), "^.*/obo/", "") AS ?value) }
  OPTIONAL { ?a bkr:evidencedByMention ?mention .
             OPTIONAL { ?mention ner:evidenceText ?quote }
             OPTIONAL { ?mention ner:inSentence/ner:sentenceText ?sentence } }
  OPTIONAL { ?ag rdfs:label ?agent }
  OPTIONAL { ?a bkr:hasReviewDecision/ner:reviewStatus ?rs . BIND(REPLACE(STR(?rs), "^.*/review-status/", "") AS ?review) }
}
```

**CQ16 — What does each resource expect as input and return as output, in what format, and under what constraints?**

Source: BKR sparql/cq16_inputs_outputs.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?resource ?direction ?io ?format ?modality ?required ?constraint WHERE {
  { ?r bkr:expectsInput ?spec . BIND("input" AS ?direction) }
  UNION { ?r bkr:producesOutput ?spec . BIND("output" AS ?direction) }
  ?r bkr:resourceName ?resource .
  ?spec bkr:ioName ?io .
  OPTIONAL { ?spec bkr:ioFormat ?format }
  OPTIONAL { ?spec bkr:ioModality ?m . BIND(REPLACE(STR(?m), "^.*/modality/", "") AS ?modality) }
  OPTIONAL { ?spec bkr:ioRequired ?required }
  OPTIONAL { ?spec bkr:ioConstraint/bkr:assumptionStatement ?constraint }
} ORDER BY ?resource ?direction ?io
```

**CQ17 — Which failure modes are silent, how severe are they, and what detects or mitigates them?**

Source: BKR sparql/cq17_failure_modes.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?resource ?statement ?condition ?symptom ?silent ?severity ?mitigation WHERE {
  ?r bkr:hasLimitation ?f ; bkr:resourceName ?resource .
  ?f a bkr:FailureMode ; bkr:limitationStatement ?statement .
  OPTIONAL { ?f bkr:failureCondition ?condition }
  OPTIONAL { ?f bkr:failureSymptom ?symptom }
  OPTIONAL { ?f bkr:isSilentFailure ?silent }
  OPTIONAL { ?f bkr:failureSeverity ?sev . BIND(REPLACE(STR(?sev), "^.*/criticality/", "") AS ?severity) }
  OPTIONAL { ?f bkr:failureMitigation ?mitigation }
} ORDER BY DESC(?silent) ?resource
```

**CQ18 — For each machine-generated record: how complete is it, what did the extractor fail to find, and has a human reviewed it?**

Source: BKR sparql/cq18_extraction_completeness.rq.

```sparql
PREFIX bkr:   <https://brainkb.org/resource/>
PREFIX bkrls: <https://brainkb.org/resource/lifesci/>
PREFIX bkrb:  <https://brainkb.org/resource/bridge/brainkb/>
PREFIX ner:   <https://brainkb.org/ner/>
PREFIX obo:   <http://purl.obolibrary.org/obo/>
PREFIX prov:  <http://www.w3.org/ns/prov#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX dcat:  <http://www.w3.org/ns/dcat#>
PREFIX schema: <https://schema.org/>
PREFIX iao:   <http://purl.obolibrary.org/obo/IAO_>
PREFIX dqv:   <http://www.w3.org/ns/dqv#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?resource ?completeness ?reviewStatus (GROUP_CONCAT(DISTINCT ?miss; separator=", ") AS ?notFound) WHERE {
  ?rec a dcat:CatalogRecord ; bkr:describesResource ?res .
  ?res bkr:resourceName ?resource .
  OPTIONAL { ?rec bkr:fieldCompleteness ?completeness }
  OPTIONAL { ?rec bkr:notFoundField ?m0 }
  OPTIONAL { ?rec bkr:hasReviewDecision/ner:reviewStatus ?rs .
             BIND(REPLACE(STR(?rs), "^.*/review-status/", "") AS ?reviewStatus) }
  BIND(COALESCE(?m0, "") AS ?miss)
} GROUP BY ?resource ?completeness ?reviewStatus ORDER BY ?resource
```

**CQ19 — Which published works attest each resource, and where in them?**

Source: BKR sparql/cq19_attesting_works.rq.

```sparql
PREFIX bkr:     <https://brainkb.org/resource/>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX adms:    <http://www.w3.org/ns/adms#>
PREFIX skos:    <http://www.w3.org/2004/02/skos/core#>
PREFIX prov:    <http://www.w3.org/ns/prov#>
SELECT ?resource ?work ?doi (GROUP_CONCAT(DISTINCT ?placeStr; separator=", ") AS ?locations) WHERE {
  ?r  bkr:resourceName   ?resource ;
      dcterms:isReferencedBy ?w .
  OPTIONAL { ?w dcterms:title ?title }
  OPTIONAL { ?w adms:identifier/skos:notation ?doi }
  BIND(COALESCE(?title, STR(?w)) AS ?work)
  # where in the work: any locator that is part of it and evidences something about this resource
  OPTIONAL {
    ?r (bkr:hasScope|bkr:hasAssumption|bkr:hasRecord|bkr:hasAssessment)?/
       (bkr:scopeAssertedIn|bkr:assumptionStatedIn|prov:hadPrimarySource|bkr:evidencedByMention/prov:hadPrimarySource) ?loc .
    ?loc dcterms:isPartOf ?w ; bkr:resourceName ?place .
  }
  BIND(COALESCE(?place, "") AS ?placeStr)
} GROUP BY ?resource ?work ?doi ORDER BY ?resource
```

**CQ20 — How has a resource evolved? Versions, change records and dated observations in one timeline.**

Source: BKR sparql/cq20_resource_evolution.rq.

```sparql
PREFIX bkr:  <https://brainkb.org/resource/>
PREFIX ner:  <https://brainkb.org/ner/>
PREFIX sosa: <http://www.w3.org/ns/sosa/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?resource ?when ?kind ?what ?source WHERE {
  {
    ?r bkr:resourceName ?resource ; bkr:hasVersion ?v .
    ?v bkr:versionIdentifier ?what .
    OPTIONAL { ?v bkr:releaseDate ?d }
    BIND(STR(COALESCE(?d, "")) AS ?when)
    BIND("version" AS ?kind)
    BIND("" AS ?source)
  } UNION {
    ?r bkr:resourceName ?resource ; bkr:hasChangeRecord ?c .
    OPTIONAL { ?c ner:changeTimestamp ?ct }
    OPTIONAL { ?c bkr:changeType/skos:prefLabel ?cl }
    BIND(STR(COALESCE(?ct, "")) AS ?when)
    BIND("change" AS ?kind)
    BIND(STR(COALESCE(?cl, "change")) AS ?what)
    BIND("" AS ?source)
  } UNION {
    ?r bkr:resourceName ?resource .
    ?o sosa:hasFeatureOfInterest ?r ;
       sosa:resultTime ?t ;
       bkr:observesResourceProperty/skos:prefLabel ?prop ;
       bkr:observationValue ?val .
    OPTIONAL { ?o bkr:observationSource/bkr:resourceName ?src }
    BIND(STR(?t) AS ?when)
    BIND("observation" AS ?kind)
    BIND(CONCAT(?prop, " = ", ?val) AS ?what)
    BIND(STR(COALESCE(?src, "")) AS ?source)
  }
} ORDER BY ?resource ?when
```

**CQ21 — Which identifiers failed to resolve, and which verified values disagree with their source?**

Source: BKR sparql/cq21_resolution_and_mismatch.rq.

```sparql
PREFIX bkr:  <https://brainkb.org/resource/>
PREFIX adms: <http://www.w3.org/ns/adms#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX prov: <http://www.w3.org/ns/prov#>
SELECT ?problem ?subject ?detail ?source ?when WHERE {
  {
    ?a bkr:resolutionStatus ?st ;
       bkr:resolutionRoute ?route ;
       bkr:resolvedIdentifier ?id .
    ?id skos:notation ?subject .
    FILTER(?st != <https://brainkb.org/resource/resolution-status/resolved>)
    ?st skos:prefLabel ?statusLabel .
    ?route skos:prefLabel ?routeLabel .
    OPTIONAL { ?a bkr:resolutionSource ?s1 . ?s1 bkr:resourceName ?src }
    OPTIONAL { ?a prov:endedAtTime ?t }
    BIND("unresolved identifier" AS ?problem)
    BIND(CONCAT(?statusLabel, " via ", ?routeLabel) AS ?detail)
  }
  UNION
  {
    ?v bkr:verificationOutcome <https://brainkb.org/resource/verification-outcome/mismatch> ;
       bkr:assertedValue ?claimed ;
       bkr:sourceValue ?actual ;
       bkr:assertionAbout ?about .
    ?about bkr:resourceName ?subject .
    OPTIONAL { ?v bkr:verifiedAgainst ?s2 . ?s2 bkr:resourceName ?src }
    OPTIONAL { ?v prov:endedAtTime ?t }
    BIND("metadata mismatch" AS ?problem)
    BIND(CONCAT("extracted ", ?claimed, " but source gives ", ?actual) AS ?detail)
  }
  BIND(STR(COALESCE(?src, "")) AS ?source)
  BIND(STR(COALESCE(?t, "")) AS ?when)
} ORDER BY ?problem ?subject
```
