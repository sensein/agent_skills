# SLR ontology (bundled)

The skill's exports (`scripts/export_review.py` -> `ttl`, `jsonld`) and every SPARQL recipe
(`references/sparql_queries.md`, `scripts/query_sparql.py`) use the SLR ontology,
namespace `https://w3id.org/slr-ontology/`. It ships here so the skill is self-contained — no
network fetch, no GitHub lookup:

| file | what |
|---|---|
| `slr_ontology.owl.ttl` | OWL in Turtle: classes, properties, enumerations — load next to a review graph |
| `slr_ontology.yaml` | the LinkML source the OWL and JSON Schema are generated from |
| `slr_ontology.schema.json` | JSON Schema for the review JSON |
| `slr_diagram.md` | class diagram (Mermaid) |

Source: `git@github.com:sensein/synthscholar.git`, branch `ontology-declare-research-questions`, last ontology commit
`da23314b5f77f9789d5a2abac4f0ae67080120b4 2026-10-02T14:59:24-04:00`; bundled
2026-10-02. Checksums (sha256):

| file | sha256 |
|---|---|
| `slr_ontology.owl.ttl` | `a6a65c54d992e63195f6f9e93a9ca1ce45ae3a5e34b58b9febaeed271fe98bf1` |
| `slr_ontology.yaml` | `289dd9e74faaaf2cc6bad08b37a6c59b0c05d08fb7dff516a8f72bb36d3e4a18` |
| `slr_ontology.schema.json` | `ddffb17d6e15d322d407f7187937abab03e566b6f4b8b438632aab2752897fb1` |
| `slr_diagram.md` | `8628ab11c9375070b35d54f2693e1c3b14f2990a21b07477b871d22ed452f329` |

Check: `python scripts/check_ontology.py` (terms the skill uses) and
`python scripts/check_ontology.py <review.ttl>` (terms an export uses). Refresh after the
app's ontology changes: `python scripts/check_ontology.py --refresh <synthscholar checkout>`.
