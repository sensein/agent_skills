# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Download the pinned public inputs for one ABIDE I site and verify their SHA-256.

Usage: uv run fetch_inputs.py --site Caltech --dest data/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Some hosts refuse Python's default user agent (HTTP 403); send an explicit one.
UA = {"User-Agent": "neuroimaging-kg-demo/0.1 (+https://github.com/sensein/agent_skills)"}


def fetch(url: str, dest: Path, sha256: str) -> None:
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
        print(f"ok (cached)  {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        sys.exit(f"HASH MISMATCH for {url}\n  expected {sha256}\n  got      {got}")
    dest.write_bytes(data)
    print(f"ok           {dest.name}  ({len(data) / 1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", required=True)
    ap.add_argument("--dest", default="data")
    a = ap.parse_args()
    pins = json.loads((HERE / "pins.json").read_text())
    if a.site not in pins["site_nidm"]:
        sys.exit(f"site {a.site!r} is not pinned; choose from {[k for k in pins['site_nidm'] if not k.startswith('_')]}")
    dest = Path(a.dest)
    fetch(pins["phenotype_csv"]["url"], dest / "Phenotypic_V1_0b_preprocessed1.csv", pins["phenotype_csv"]["sha256"])
    fetch(pins["term_map"]["url"], dest / "abide_terms.json", pins["term_map"]["sha256"])
    for tool, spec in pins["cde"].items():
        if not tool.startswith("_"):
            fetch(spec["url"], dest / f"{tool}_cde.ttl", spec["sha256"])
    fetch(pins["nidm_linkml_schema"]["url"], dest / "nidm_schema.yaml", pins["nidm_linkml_schema"]["sha256"])
    site = pins["site_nidm"][a.site]
    fetch(pins["site_nidm"]["_url_template"].format(site=a.site), dest / f"{a.site}_nidm.ttl", site["sha256"])


if __name__ == "__main__":
    main()
