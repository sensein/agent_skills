#!/usr/bin/env python3
"""Check every dependency the SynthScholar skill needs, before a run.

Validation must not degrade silently: a missing library would otherwise turn a
check into "not checked" (or, in older code, into "passed"). So the agent runs
this first, asks the user to install whatever is missing (the commands are
printed), installs on their confirmation, and re-runs it until it passes.

Usage:
    python check_deps.py                 # everything a review needs, end to end
    python check_deps.py --for verify    # one stage: corpus|review|verify|pdf|query|tests

Exit 0 = all required dependencies present, 1 = something required is missing.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

# (name, kind, check, install, stages, why)
DEPS = [
    ("httpx", "py", "httpx", "pip install httpx",
     {"corpus", "review", "verify", "pdf", "fetch"},
     "registry lookups: verify_citations, audit_grounding, corpus and export gates, PDF gate"),
    ("synthscholar (dev checkout)", "py", "synthscholar.text_windows",
     "pip install -e /path/to/prisma-review-agent", {"review", "pdf"},
     "the app's pipeline and exporters: run_local_review, export_review, extract_evidence, and the "
     "appendix check (sidecars are re-rendered from review.json before delivery)"),
    ("rapidfuzz", "py", "rapidfuzz", "pip install 'rapidfuzz>=3.0'", {"review"},
     "the pipeline's evidence-span grounding filter (refuses to run without it)"),
    ("pymupdf", "py", "fitz", "pip install pymupdf", {"corpus"},
     "PDF text extraction for supplied PDFs"),
    ("pandoc", "bin", "pandoc", "conda install -c conda-forge pandoc   # or: brew install pandoc",
     {"pdf"}, "Markdown → PDF"),
    ("TeX engine or typst", "engine", "", "conda install -c conda-forge tectonic   # or MacTeX / TeX Live",
     {"pdf"}, "PDF engine (TeX for the ACM layout)"),
    ("rdflib", "py", "rdflib", "pip install rdflib", {"query"}, "SPARQL over the Turtle export"),
    ("pytest", "py", "pytest", "pip install pytest", {"tests"}, "the skill's regression tests"),
]
OPTIONAL = [
    ("docling", "py", "docling", "pip install docling", {"corpus"},
     "best tables, and the only backend that reads a scanned PDF"),
    ("playwright", "py", "playwright", "pip install playwright", {"fetch"},
     "institutional (EZproxy) sign-in for paywalled papers"),
    ("psycopg", "py", "psycopg", "pip install 'psycopg[binary]>=3.1'", {"query"},
     "SQL over the article store"),
]
STAGES = ("corpus", "review", "verify", "pdf", "query", "fetch", "tests")


def present(kind: str, check: str) -> bool:
    if kind == "py":
        try:
            return importlib.util.find_spec(check) is not None
        except (ImportError, ValueError):
            return False
    if kind == "bin":
        return shutil.which(check) is not None
    # any PDF engine generate_review_pdf.py can use
    pdf = Path(__file__).resolve().parent / "generate_review_pdf.py"
    r = subprocess.run([sys.executable, str(pdf), "--check-deps"], capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--for", dest="stage", choices=STAGES, help="check one stage only")
    args = ap.parse_args()
    wanted: set[str] = {args.stage} if args.stage else {s for s in STAGES if s not in ("tests", "query", "fetch")}
    missing = []
    print(f"python: {sys.executable}")
    for name, kind, check, install, stages, why in DEPS:
        if not stages & wanted:
            continue
        ok = present(kind, check)
        print(f"[{'ok' if ok else 'MISSING'}] {name} — {why}")
        if not ok:
            missing.append((name, install))
    for name, kind, check, install, stages, why in OPTIONAL:
        if stages & wanted:
            print(f"[{'ok' if present(kind, check) else '--'}] {name} (optional) — {why}")
    if missing:
        print("\nRequired and missing — ask the user, then install:")
        for name, install in missing:
            print(f"  {install}    # {name}")
        print("Re-run this check after installing; do not proceed until it passes.")
        return 1
    print("\nAll required dependencies are installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
