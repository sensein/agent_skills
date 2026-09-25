# KG plan — the judgment layer of the TTL representation

The Turtle file is written by `scripts/json_to_ttl.py`, deterministically, from
the aligned result: every entity, mention, offset, sentence, tool mapping and
judge review. What a script cannot decide is written here, once per paper, as
`kg_plan.json` (schema: `schemas/kg-plan.schema.json`):

- the **normalized key** of each entity (the ingestion merge handle),
- a **more specific ontology class** than the label map gives,
- the **honest skos tier** of a mapping, when you know better than the default,
- the **relations and causal claims the paper states**.

The extractor already states relations, hierarchy (`broader`) and causal claims
alongside the entities (prompts/extractor-ner-*.md → scripts/relations.py); the
plan adds what extraction could not — coreference keys, finer classes, cross-
sentence relations, chains — and never needs to repeat an extracted relation
(the converter writes each edge once). Write it AFTER alignment and BEFORE judging: the kg-keys and claims judges
review it, and `judge_combine.py` prunes whatever they fail. Writing it is a default step for NER; every field in it is optional, so an empty
`{}` is a valid plan when the paper gives nothing to add (the TTL is then flatter).
Do not write Turtle yourself.

## System

```
You plan how ONE paper's extraction is represented in the Named Entity
Ontology (https://brainkb.org/ner/, default_ontology/named_entity_ontology.owl).
Input: the entities_grouped / key_terms_grouped of the aligned result (ids
"<entity>|<label>"), the source text, and the class list of the ontology.

Output strict JSON only — no prose, no fences:
{
  "entities": {
    "<entity>|<label>": {
      "normalized_key":   "sst_interneuron",       // references/key-normalization.md
      "normalized_label": "somatostatin-expressing interneuron (SST-IN)",
      "class":            "Interneuron",           // optional: a DECLARED class, more specific than the label's
      "skos_tier":        "exactMatch",            // optional: only for a tool mapping; see tiers below
      "tier_note":        "UBERON term is the gyrus; paper means the cortical area", // optional caveat
      "relations":  [{"predicate": "located_in", "target_key": "neocortex"}],
      "broader_key":      "gabaergic_interneuron", // in-paper hierarchy (subtype -> class, drug -> family)
      "related_keys":     [],                       // thematic sibling links (skos:related)
      "see_also_keys":    [],                       // homology / analogy, NEVER identity (rdfs:seeAlso)
      "uses_keys":        [],                       // method/model -> software, reagent, model it employs (prov:used)
      "derived_from_keys": []                       // concept lineage (prov:wasDerivedFrom)
    }
  },
  "causal_relations": [{
      "id": "rel-1",
      "cause_key": "sst_interneuron", "effect_key": "pyramidal_neuron",
      "mediator_keys": [], "moderator_keys": [], "confounder_keys": [],
      "type": "negatively_regulates",   // ner causal-type/*: activates, inhibits, promotes, suppresses, causes, ...
      "polarity": "negative",           // positive | negative | neutral | unspecified
      "modality": "asserted",           // asserted | probable | possible | uncertain | conditional | hypothetical
      "directness": "direct",           // direct | indirect | unspecified
      "evidence_basis": ["pharmacological_perturbation"],
      "hypothetical": false,            // false ONLY for this paper's own intervention
      "negated": false,
      "evidence": "Silencing SST-INs increased pyramidal neuron firing",
      "effect_estimate": {"measure": "Pearson r", "value": 0.56, "p_value": 0.004, "sample_size": 25}
  }],
  "chains": [{"id": "chain-1", "label": "SST-IN disinhibition of L2/3 PNs", "relation_ids": ["rel-1"]}]
}

RULES
1. Keys: follow references/key-normalization.md exactly — canonical referent
   (spell out "they", expand an abbreviation the paper defines), ASCII,
   lowercase, single underscores, singular. Coreferent groups ("SST-INs|CellType"
   and "somatostatin interneurons|CellType") SHARE one key: that is how they
   become one entity. Different referents never share a key. Never a generic
   key (model, neuron, imaging, analysis, mice → mus_musculus).
2. class: only a class the ontology declares (validate_ttl rejects anything
   else), and only when the paper's wording licenses it ("SST-INs" →
   Interneuron; "neurons" stays CellType).
3. skos_tier (only on items that HAVE a tool mapping; never add a mapping):
   exactMatch = is that concept; closeMatch = near-identity, say the caveat in
   tier_note; broadMatch = the mapped term is a superclass; narrowMatch = a
   subclass; relatedMatch = thematic. Leave it out if unsure — the mapping
   judge sets it, and the default is closeMatch.
4. relations: predicate from the closed list in ttl_config.json
   relation_predicates — part_of, has_part, located_in, has_participant,
   participates_in, expresses, expressed_in, has_phenotype, capable_of,
   member_of, develops_from, derives_from, in_taxon, overlaps, interacts_with. Target by normalized_key. Only edges the
   paper STATES; "known biology" is never a license.
5. Causal: only mechanisms this paper argues. hypothetical=false requires an
   interventional evidence_basis (experimental_intervention,
   genetic_perturbation, pharmacological_perturbation, randomized_intervention,
   dose_response) — the SHACL shapes reject anything else. Correlational
   (observational_adjusted / observational_unadjusted / longitudinal /
   mediation_analysis), computational_model and author_assertion stay
   hypothetical. Put a <=25-word verbatim evidence fragment in `evidence`.
   A quantified result goes in effect_estimate (numbers exactly as printed).
6. Chains: ordered relation ids, in the order of the paper's argument.
7. Never an ontology IRI anywhere in this file. Mappings come only from the
   alignment tool; this plan can only grade them.
```

## After writing it

```bash
python -m scripts.judge_prepare work/<stem>_final.json --source work/<stem>.txt --kg-plan work/kg_plan.json
# ... judges (incl. kg-keys + claims) ... judge_combine --kg-plan work/kg_plan.json ...
python -m scripts.json_to_ttl work/<stem>_final.json --kg-plan work/kg_plan.json --out <stem>.ttl
python -m scripts.validate_ttl <stem>.ttl      # must exit 0
```
