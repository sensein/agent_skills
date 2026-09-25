# Worked example — Hu et al. 2026, end to end to validated Turtle

**Paper.** Hu B, Temiz NZ, Chou C-N, Rupprecht P, Meissner-Bernard C, Titze B,
Chung S, Friedrich RW. *Representational learning by optimization of neural
manifolds in an olfactory memory network.* Nature Neuroscience (2026).
doi:[10.1038/s41593-026-02429-3](https://doi.org/10.1038/s41593-026-02429-3).
Open access under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/);
`paper.txt` is its text, extracted from the publisher PDF and whitespace-normalised
(no other change), redistributed under that licence with this attribution.

```bash
examples/ttl/run.sh [work-dir]          # every step below, then the gate
```

## Files

| file | what it is |
|---|---|
| `hu2026_curated.ttl` | a human-curated NER representation of the paper: 63 entities, their keys and classes, 28 OLS4-verified mappings, RO/SKOS/PROV edges, 2 causal claims. The reference everything is checked against. |
| `paper.txt` | the paper's text; every offset in the example points into it. |
| `build_example.py` | turns the curated file into pipeline inputs (below), and — as `fallback` / `reviews` — the two stand-ins described under *What is simulated*. |
| `paper_final.json` | aligned working JSON as an extractor leaves it: **every occurrence** of each entity in `paper.txt` (1,039 mentions, long forms and abbreviations as separate mentions), real offsets, context sentence and section, labels from the neuroscience extractor's taxonomy. No mappings yet. |
| `kg_plan.json` | the judgment layer (prompts/kg-plan.md): keys, specific classes, 41 RO edges, SKOS/PROV links, 2 causal claims and their chain. |
| `curated_mappings.json` | the curated mappings, used only by the reference mapping review and the fallback. |
| `hu2026.ttl` | **the result**: 39,014 triples, VALID against the v2.4 ontology + SHACL shapes, one connected component, with the full judge and mapping provenance. |
| `judge_report.json` | what the ensemble did: drops, demotions, fixes. |

## What happens

1. **Trusted-ontology mapping** (`concept_mapping map --sources trusted`) maps
   279 mentions from the 42 enabled trusted ontologies, in `priority.md` order.
2. **Fallback** — the 329 mentions the trusted ontologies cannot resolve (short
   chemical symbols CHEBI-lite has no synonyms for, phrases like "juvenile fish")
   would go to the local hybrid service and then BioPortal. The example replaces
   that network step with the curated OLS4-verified mappings, labelled
   `mapping_source: "ols4 (curated …)"`.
3. **Judge ensemble.** `grounding_script` passes all 76 surface groups (every
   mention is in the text). The mapping review **demotes 4 exact-label trusted
   matches that mean the wrong thing**: "alanine", "serine" and "histidine" matched the
   stereo-unspecified CHEBI terms where the paper means the L-amino acids, and "Dp"
   matched an UBERON term where the curated identity is the zebrafish structure
   ZFA:0000181. The correct L-forms still arrive through "Ala" / "Ser" / "His". The
   kg-keys review renames the guardrail key `neuron` to `neuron_pdp` — and every edge
   pointing at it follows.
4. **TTL** — the 76 surface groups collapse into the curated 63 entities through
   their shared keys. **All 28 concept IRIs and 62 of 63 entity IRIs are identical to
   the curated graph's UUIDv5 IRIs** (`entity|<key>`, `concept|<IRI>`,
   `ontology|<ACRONYM>`, default_ontology/ttl_config.json `iri`). The one difference is
   deliberate: the curated key `neuron` is on the `generic_keys` guardrail, and the
   kg-keys review renames it `neuron_pdp` — a bare `neuron` key would merge every
   paper's neurons into one node.
5. **Gate** — `validate_ttl`: 0 violations.

## What is simulated, and says so

- **Remote mappers** → `build_example.py fallback` (offline, deterministic).
- **LLM judges** → `build_example.py reviews`: reviews derived from the curated file,
  written as `"model": "reference:hu2026_curated.ttl", "mode": "deterministic"`. In a
  real run each judge is a model following `prompts/judge-*.md`; the combine step,
  the TTL and the gate are exactly the ones shown here.
