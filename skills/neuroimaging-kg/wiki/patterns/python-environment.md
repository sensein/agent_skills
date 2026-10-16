# Run Python through uv, not the system interpreter

**Type:** failure mode → workaround · **Evidence:** experience/iter0/T3 (steps 12, 18–20: four calls on
`ModuleNotFoundError: No module named 'rdflib'`).

The skill's scripts carry inline dependency metadata: `uv run $SKILL_DIR/scripts/<name>.py …`.
For ad-hoc code use `uv run --with rdflib --with pyoxigraph python script.py` (or add a
`# /// script` header). The system `python3` has none of these packages.
