#!/usr/bin/env bash
# Hu et al. 2026 worked example, end to end. Run from anywhere; needs
# `pip install -r requirements.txt` and an indexed trusted lexicon
# (`python -m scripts.concept_mapping index`, once).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$(cd "$HERE/../.." && pwd)"
W="${1:-$(mktemp -d)}"
cd "$SKILL"
python examples/ttl/build_example.py                                   # fixture from the curated TTL
cp examples/ttl/paper_final.json examples/ttl/kg_plan.json examples/ttl/paper.txt "$W"/
python -m scripts.concept_mapping map "$W/paper_final.json" --sources trusted   # trusted ontologies, by priority
python examples/ttl/build_example.py fallback "$W/paper_final.json"   # offline stand-in for local hybrid / BioPortal
python examples/ttl/build_example.py reviews "$W/paper_final.json" "$W/judge/reviews"
python -m scripts.judge_prepare "$W/paper_final.json" --source "$W/paper.txt" --kg-plan "$W/kg_plan.json" --out-dir "$W/judge"
python -m scripts.judge_combine "$W/paper_final.json" --reviews "$W"/judge/reviews/*.json --kg-plan "$W/kg_plan.json" --report "$W/judge/report.json"
python -m scripts.json_to_ttl "$W/paper_final.json" --kg-plan "$W/kg_plan.json" --source "$W/paper.txt" --out "$W/hu2026.ttl"
python -m scripts.validate_ttl "$W/hu2026.ttl"
echo "result: $W/hu2026.ttl (compare with examples/ttl/hu2026.ttl)"
