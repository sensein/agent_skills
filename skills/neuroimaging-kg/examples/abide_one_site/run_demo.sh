#!/usr/bin/env bash
# End-to-end demo of the neuroimaging-kg skill on one ABIDE I site (default: Caltech).
# Run from an empty working directory:  bash /path/to/run_demo.sh [Caltech|SBL|OHSU|Stanford]
set -euo pipefail
SITE="${1:-Caltech}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$HERE/../../scripts"
PINS="$HERE/pins.json"
START=$(date +%s)
say() { printf '\n### %s\n' "$*"; }

say "Step 1 — fetch pinned public inputs for $SITE and check their SHA-256"
uv run "$HERE/fetch_inputs.py" --site "$SITE" --dest data

say "Steps 2–6 — build the graph (subset table, csv2nidm, identity join, cohort, claim, provenance)"
uv run "$HERE/build_site.py" --site "$SITE" --data data --out graph --agent "${DEMO_AGENT:-Demo user}"

say "Step 7a — validate the layers we authored with the skill's core rules, and prove a removed derivation edge is caught"
uv run "$SCRIPTS/validate_graph.py" --data graph/provenance.ttl --data graph/identity_links.ttl \
  --data graph/cohort.ttl --data graph/dataset_claims.ttl --mutation-test --report graph/validation_authored.json

say "Step 7b — validate the NIDM layer against SHACL generated from PyNIDM's LinkML schema (findings, not a gate)"
LINKML=$(python3 -c "import json;print(json.load(open('$PINS'))['linkml'])")
uvx --from "$LINKML" gen-shacl --non-closed data/nidm_schema.yaml > graph/nidm_linkml_shapes.ttl 2>/dev/null
uv run "$SCRIPTS/validate_graph.py" --data graph/phenotype.ttl --shapes graph/nidm_linkml_shapes.ttl \
  --report graph/validation_nidm.json --warn-only > /dev/null
python3 - <<'PY'
import json; r = json.load(open("graph/validation_nidm.json"))
print(f"phenotype.ttl (written by PyNIDM csv2nidm) vs PyNIDM's own LinkML schema: conforms={r['conforms']}, "
      f"{r['results']} results; first messages:")
for m in r["messages"][:3]: print("   ", m)
PY

say "Step 8 — compute gold answers from the source files (not from the graph store)"
uv run "$HERE/gold.py" --site "$SITE" --data data --graph graph --out graph/gold.json

say "Step 9 — load every layer into Oxigraph as named graphs and answer the competency questions"
uv run "$SCRIPTS/query_graph.py" \
  --graph urn:g:phenotype=graph/phenotype.ttl --graph urn:g:repronim=data/"$SITE"_nidm.ttl \
  --graph urn:g:links=graph/identity_links.ttl --graph urn:g:cohort=graph/cohort.ttl \
  --graph urn:g:claims=graph/dataset_claims.ttl --graph urn:g:prov=graph/provenance.ttl \
  --graph urn:g:cde/ants=data/ants_cde.ttl --graph urn:g:cde/fsl=data/fsl_cde.ttl \
  --graph urn:g:cde/freesurfer=data/freesurfer_cde.ttl \
  --queries "$HERE/queries" --gold graph/gold.json --union --out graph/answers.json

say "Step 10 — compare our graph with ReproNim's graph for $SITE as two contributors (identifiers, vocabulary, conflicts)"
uv run "$SCRIPTS/compare_graphs.py" \
  --graph ours=graph/phenotype.ttl,graph/cohort.ttl,graph/dataset_claims.ttl \
  --graph repronim=data/"$SITE"_nidm.ttl,data/freesurfer_cde.ttl,data/fsl_cde.ttl,data/ants_cde.ttl \
  --attributed ours="${DEMO_AGENT:-Demo user}" --attributed repronim="ReproNim simple2_NIDM_examples@e0dff17" \
  --out graph/compare.json --markdown graph/compare.md --review graph/mapping_review.ttl \
  --attribution graph/contributors.ttl | sed -n '1,14p;/Conflicts/p'

say "Done in $(( $(date +%s) - START )) s — graph files are in ./graph, answers in graph/answers.json"
