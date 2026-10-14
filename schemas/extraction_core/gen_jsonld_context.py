#!/usr/bin/env python3
"""Generate the Extraction Core JSON-LD context, with ``"@version": 1.1``.

LinkML's ``gen-jsonld-context`` writes OBO prefixes, whose IRIs end in ``_``
(``CL_``, ``NCBITaxon_``), as ``{"@id": ..., "@prefix": true}``. ``@prefix`` is
a JSON-LD 1.1 keyword, so the context must declare ``"@version": 1.1``; without
it, rdflib before 7.1 does not expand ``CL:4023018`` to
``http://purl.obolibrary.org/obo/CL_4023018``. The generator does not add the
version, so this script does.

Usage:
  python schemas/extraction_core/gen_jsonld_context.py           # rewrite the context file
  python schemas/extraction_core/gen_jsonld_context.py --check   # fail if it is out of date

Requires ``linkml``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "extraction_core.yaml"
CONTEXT_PATH = HERE / "extraction_core.context.jsonld"


def build_context() -> str:
    from linkml.generators.jsonldcontextgen import ContextGenerator

    generated = json.loads(ContextGenerator(str(SCHEMA_PATH), metadata=False).serialize())
    context = {"@version": 1.1, **generated["@context"]}
    return json.dumps({"@context": context}, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the committed context differs from the schema")
    args = parser.parse_args(argv)

    text = build_context()
    if args.check:
        current = CONTEXT_PATH.read_text(encoding="utf-8") if CONTEXT_PATH.exists() else ""
        if current != text:
            print(f"{CONTEXT_PATH.name} is out of date; run {Path(__file__).name} to regenerate it",
                  file=sys.stderr)
            return 1
        print(f"{CONTEXT_PATH.name} is up to date")
        return 0
    CONTEXT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {CONTEXT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
