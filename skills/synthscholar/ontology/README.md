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

Source: `git@github.com:sensein/synthscholar.git`, branch `research-questions-in-protocol`, last ontology commit
`083d305a9b37fd61257431b864bb4a865c345f12 2026-08-16T04:31:49+05:45`; bundled
2026-10-02. Checksums (sha256):

| file | sha256 |
|---|---|
| `slr_ontology.owl.ttl` | `2f9dd7b327fb81187d8dce04beaff786edc4bd819952f0d93d217d5063535215` |
| `slr_ontology.yaml` | `cd57fa822ac6908e3827a6647f25abde01fdaf55dd406c95e314c4423fae527a` |
| `slr_ontology.schema.json` | `a44a91e504324c1ec5c76d8ff0ed7b94534e96ccd839378a34a09df6c8168036` |
| `slr_diagram.md` | `8628ab11c9375070b35d54f2693e1c3b14f2990a21b07477b871d22ed452f329` |

Check: `python scripts/check_ontology.py` (terms the skill uses) and
`python scripts/check_ontology.py <review.ttl>` (terms an export uses). Refresh after the
app's ontology changes: `python scripts/check_ontology.py --refresh <synthscholar checkout>`.
