"""The skill's bundled SLR ontology: check it, check graphs against it, refresh it.

The skill is self-contained: the ontology the exports and queries use ships in
`ontology/` (slr_ontology.owl.ttl, slr_ontology.yaml (LinkML), slr_ontology.schema.json,
slr_diagram.md). Nothing needs to be fetched from GitHub.

    python scripts/check_ontology.py                 # bundled files parse; every slr: term
                                                     # the skill's docs/queries use is declared
    python scripts/check_ontology.py review.ttl ...  # every slr: term an exported graph
                                                     # uses is declared
    python scripts/check_ontology.py --refresh /path/to/synthscholar-checkout
                                                     # copy the app's ontology in, record its
                                                     # commit and checksums in ontology/README.md

Exit 0 when everything used is declared, 1 otherwise (the undeclared terms are listed).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
ONT = SKILL / "ontology"
FILES = ("slr_ontology.owl.ttl", "slr_ontology.yaml", "slr_ontology.schema.json", "slr_diagram.md")
NS = "https://w3id.org/slr-ontology/"
_TERM = re.compile(r"\bslr:([A-Za-z_][A-Za-z0-9_]*)")


def declared_terms() -> set[str]:
    import rdflib
    g = rdflib.Graph().parse(ONT / "slr_ontology.owl.ttl")
    return {str(s)[len(NS):] for s in g.subjects() if str(s).startswith(NS)}


def terms_used_by_skill() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for p in list(SKILL.glob("*.md")) + list(SKILL.glob("references/*.md")) + list(SKILL.glob("scripts/*.py")):
        if p.name == "check_ontology.py":
            continue
        for t in _TERM.findall(p.read_text(encoding="utf-8", errors="replace")):
            used.setdefault(t, []).append(str(p.relative_to(SKILL)))
    return used


def terms_used_by_graph(path: Path) -> dict[str, list[str]]:
    import rdflib
    g = rdflib.Graph().parse(path)
    used: dict[str, list[str]] = {}
    for s, p, o in g:
        for x in (s, p, o):
            if isinstance(x, rdflib.URIRef) and str(x).startswith(NS) and len(str(x)) > len(NS):
                used.setdefault(str(x)[len(NS):], [path.name])
    return used


def report(used: dict[str, list[str]], declared: set[str], what: str) -> int:
    missing = sorted(t for t in used if t not in declared)
    print(f"{what}: {len(used)} slr terms used, {len(used) - len(missing)} declared in the bundled ontology")
    for t in missing:
        print(f"  NOT DECLARED  slr:{t}   (used in {', '.join(sorted(set(used[t])))[:120]})")
    return 1 if missing else 0


def refresh(checkout: Path) -> None:
    src = checkout / "synthscholar" / "ontology"
    if not (src / "slr_ontology.owl.ttl").is_file():
        raise SystemExit(f"{src}: no slr_ontology.owl.ttl — pass the root of a synthscholar checkout")
    ONT.mkdir(exist_ok=True)
    for f in FILES:
        if (src / f).is_file():
            shutil.copy2(src / f, ONT / f)
    def git(*a):
        try:
            return subprocess.run(["git", "-C", str(checkout), *a], capture_output=True, text=True).stdout.strip()
        except OSError:
            return ""
    commit = git("log", "-1", "--format=%H %cI", "--", "synthscholar/ontology")
    branch, remote = git("branch", "--show-current"), git("remote", "get-url", "origin")
    dirty = git("status", "--porcelain", "synthscholar/ontology")
    rows = "\n".join(f"| `{f}` | `{hashlib.sha256((ONT / f).read_bytes()).hexdigest()}` |"
                     for f in FILES if (ONT / f).is_file())
    (ONT / "README.md").write_text(f"""# SLR ontology (bundled)

The skill's exports (`scripts/export_review.py` -> `ttl`, `jsonld`) and every SPARQL recipe
(`references/sparql_queries.md`, `scripts/query_sparql.py`) use the SLR ontology,
namespace `{NS}`. It ships here so the skill is self-contained — no
network fetch, no GitHub lookup:

| file | what |
|---|---|
| `slr_ontology.owl.ttl` | OWL in Turtle: classes, properties, enumerations — load next to a review graph |
| `slr_ontology.yaml` | the LinkML source the OWL and JSON Schema are generated from |
| `slr_ontology.schema.json` | JSON Schema for the review JSON |
| `slr_diagram.md` | class diagram (Mermaid) |

Source: `{remote or 'synthscholar checkout'}`, branch `{branch or '?'}`, last ontology commit
`{commit or '?'}`{' (working tree had uncommitted ontology changes)' if dirty else ''}; bundled
{dt.date.today().isoformat()}. Checksums (sha256):

| file | sha256 |
|---|---|
{rows}

Check: `python scripts/check_ontology.py` (terms the skill uses) and
`python scripts/check_ontology.py <review.ttl>` (terms an export uses). Refresh after the
app's ontology changes: `python scripts/check_ontology.py --refresh <synthscholar checkout>`.
""")
    print(f"refreshed ontology/ from {src} ({commit or 'unknown commit'})")


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("graphs", nargs="*", type=Path, help="exported review .ttl files to check")
    ap.add_argument("--refresh", type=Path, help="root of a synthscholar checkout to copy the ontology from")
    args = ap.parse_args()
    if args.refresh:
        refresh(args.refresh)
    declared = declared_terms()
    print(f"bundled ontology: {len(declared)} slr terms declared ({ONT / 'slr_ontology.owl.ttl'})")
    rc = report(terms_used_by_skill(), declared, "skill docs + scripts")
    for g in args.graphs:
        rc |= report(terms_used_by_graph(g), declared, str(g))
    return rc


if __name__ == "__main__":
    sys.exit(main())
