# normalized_key — the entity-merging contract

The KG merges entities across papers **iff their normalized keys are
byte-identical** (the key seeds the UUIDv5 IRI). So the key rules are not
style; they are the identity function of the knowledge base. Follow them
exactly; when in doubt, prefer the *more specific* key — a missed merge is
recoverable (add a synonym later), a wrong merge silently corrupts every
downstream query.

## Algorithm

A key given in kg_plan.json (prompts/kg-plan.md) always wins. Otherwise
`scripts/json_to_ttl.py::derive_key` takes, in order:

1. **The trusted ontologies' preferred label** for the ONE class the entity's text
   denotes (`concept_mapping.TrustedMapper.canonical_label`): identity-strength
   matches only (label, exact synonym, symbol), within the label's route, never
   when ambiguous. So "SST-INs", "somatostatin interneurons" and "sst GABAergic
   interneuron" key alike wherever an ontology says they are one class — the
   synonym table is the ontologies themselves, as exhaustive as priority.md makes it.
2. **The preferred label of an identity-strength tool mapping** on the item.
3. **The algorithm below** (steps 2–4), with `default_ontology/key_synonyms.json`
   holding only user overrides for what no trusted ontology covers.

A fallback key that lands on `generic_keys` (ttl_config.json) is qualified with the
paper slug (`<paper>_neuron`) — kept paper-local rather than merged.

The generated table, for consumers that need it offline:
`python -m scripts.concept_mapping export-synonyms` → `lexicon/key_synonyms.tsv.gz`,
one row per (variant, class) with its prefix, IRI, match type and source. It is
context-dependent by nature — `pfc` is prefrontal cortex in UBERON and prefollicle
cell in FBbt — so filter it by prefix; never merge on the variant alone.

The steps a human (or the kg-plan author) applies:

1. Start from the **canonical entity name**, not the surface form:
   spell out the referent ("somatostatin-expressing interneuron", not "they",
   not "SST cells" if the paper defines the abbreviation).
2. Unicode-fold to ASCII (α→alpha, ω→omega, é→e), lowercase.
3. Replace every non-alphanumeric run with a single underscore; strip
   leading/trailing underscores. `GCaMP-6s` → `gcamp_6s` → synonym table → `gcamp6s`.
4. **Drop packaging words** that vary by paper without changing the referent:
   leading articles, trailing plurals where the singular is natural
   (`dendritic_spines` → `dendritic_spine`), parenthetical vendor/config
   details (`(Multiclamp 700B)` — goes in the label, never the key).
5. **Keep disambiguating words** that change the referent: species is NOT in
   the key for methods/reagents (patch clamp is patch clamp in mouse or fish)
   but IS part of anatomy keys only when the structure is taxon-specific
   (`pdp` is a zebrafish structure; `prefrontal_cortex` is generic).
   Brain-region laterality, cortical layer, developmental stage: keep if the
   paper's claims depend on them.
6. Apply the **synonyms**: the trusted ontologies' (step 1 above), then the user overrides in `default_ontology/key_synonyms.json`.
   This is the shared mini-authority; additions are code-reviewed, versioned,
   and never removed (only redirected).

## Specificity guardrails

- Too generic — forbidden as keys: `model`, `neuron` (unless the paper truly
  means neurons in general), `imaging`, `analysis`, `the_algorithm`, `mice`
  (→ `mus_musculus`). If the referent is paper-specific, qualify it:
  `biophysical_l23_pn_model`, not `model`.
- Named things keep their names: `gcamp6s`, `simclr`, `scanimage`,
  `resnet_50`. Version subtypes are distinct keys (`gcamp6s` ≠ `gcamp6f` ≠
  `gcamp8`); the family, if mentioned as such, is its own entity (`gcamp`)
  linked by `skos:broader`.
- Abbreviations: use the expansion as key when the abbreviation is ambiguous
  across the field (`ac` → `anterior_commissure`), the abbreviation when it is
  the universal name (`gaba`, `dsi`, `ap` → no: `action_potential` — two-letter
  keys are always expanded).

## Examples of canonical keys

What derive_key produces from the trusted ontologies, for orientation (the
ontologies, not this table, are the authority):

| paper wording | canonical key | via |
|---|---|---|
| zebrafish, Danio rerio | danio_rerio | NCBITaxon exact synonym / label |
| mitral cells | mitral_cell | CL label (singularised) |
| SST interneuron | sst_gabaergic_interneuron | CL symbol → preferred label |
| olfactory learning | olfactory_learning | GO label |
| GCaMP6s | gcamp6s | no trusted class — algorithmic |

## What the key is NOT

- Not the display name — that is `normalizedEntityLabel` (free text, can
  differ across papers, both kept on a merged node).
- Not the ontology ID — an entity with a tool mapping still gets a key; the
  key merges papers even where ontologies have no term.
- Not unique per paper — sharing keys across papers is the entire point.
