# Write Turtle from a template or with rdflib, and parse it before validating

**Type:** failure mode → workaround · **Evidence:** experience/iter0/T6_egress_gate.md — the first
hand-written `egress.ttl` failed with `rdflib … BadSyntax`; the agent then copied the shape of
`graph/cohort.ttl` and succeeded. `references/modeling_patterns.md` had no egress/promotion snippet.

Start from a snippet in `references/modeling_patterns.md`; for anything generated in a loop, build it
with rdflib and serialise. Check `uv run --with rdflib python -c "import rdflib,sys;rdflib.Graph().parse(sys.argv[1])" file.ttl`
before running `validate_graph.py`.
