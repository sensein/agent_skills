"""Regenerate the inline subclass lists (VALUES) in named_entity_ontology_CQs.md from the
ontology, so the standalone queries follow the class hierarchy. Run after every ontology
change:  python cqs/regen_values.py [--ontology default_ontology/named_entity_ontology.owl]"""
import argparse, re, textwrap
from pathlib import Path
import rdflib
from rdflib import OWL, RDFS, URIRef

HERE = Path(__file__).resolve().parent
N = "https://brainkb.org/ner/"
MARK = re.compile(r"  # every subclass of (?P<roots>[^\n]*?) in named_entity_ontology\.owl (?P<ver>\S+) "
                  r"\(generated\)\n  VALUES \?(?P<var>\w+) \{\n(?P<body>.*?)\n  \}\n", re.S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ontology", type=Path, default=HERE.parent / "default_ontology" / "named_entity_ontology.owl")
    ap.add_argument("--cqs", type=Path, default=HERE / "named_entity_ontology_CQs.md")
    a = ap.parse_args()
    o = rdflib.Graph().parse(a.ontology)
    ver = next(str(v) for v in o.objects(URIRef(N), OWL.versionInfo))
    kids = {}
    for c, p in o.subject_objects(RDFS.subClassOf):
        if isinstance(p, URIRef):
            kids.setdefault(p, set()).add(c)
    text = a.cqs.read_text()

    def repl(m):
        roots = [r.strip() for r in m.group("roots").split(",")]
        old = set(m.group("body").split())
        include_self = all(r in old for r in roots)   # CQ27 lists subclasses only
        out, stack = set(), [URIRef(N + r.removeprefix("ner:")) for r in roots]
        if include_self:
            out |= set(stack)
        while stack:
            x = stack.pop()
            for k in kids.get(x, ()):
                if k not in out:
                    out.add(k); stack.append(k)
        names = sorted(str(c).replace(N, "ner:") for c in out if str(c).startswith(N))
        added, removed = sorted(set(names) - old), sorted(old - set(names))
        if added or removed:
            print(f"?{m.group('var')} ({', '.join(roots)}): +{added} -{removed}")
        body = "\n".join("    " + l for l in textwrap.wrap(" ".join(names), 84))
        return (f"  # every subclass of {', '.join(roots)} in named_entity_ontology.owl {ver} (generated)\n"
                f"  VALUES ?{m.group('var')} {{\n{body}\n  }}\n")

    new, n = MARK.subn(repl, text)
    a.cqs.write_text(new)
    print(f"regenerated {n} class list(s) against ontology {ver}")


if __name__ == "__main__":
    main()
