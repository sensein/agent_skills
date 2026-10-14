"""Allen Institute (AIT) taxonomy catalog: lookup and ranking for the AIT mapping mode.

`prompts/extractor-cell-type-ait-mapping.md` Pass 3 picks a taxonomy from species,
brain region and assay. The list of taxonomies lives in ONE place —
`data/allen_taxonomies.json`, a dated snapshot of the brain-map.org taxonomy index —
and is never restated in a prompt. It holds eight taxonomies on purpose; a paper none
of them covers gets `skos_relation = none`, never a forced closeMatch to the nearest
one.

`ait_id` in `mappings.csv` is the taxonomy's AIT number when the catalog has one —
the per-species number for a multi-species taxonomy (`ait_ids_by_species`) — otherwise
its CCN, otherwise its `taxonomy_name`. `identifiers()` returns all of them so the
table validator can accept any.

    python -m scripts.ait_taxonomy list
    python -m scripts.ait_taxonomy rank --species human --region "middle temporal gyrus"
    python -m scripts.ait_taxonomy show AIT15.3
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "allen_taxonomies.json"

_CCN = re.compile(r"CCN\d+")


def load_catalog(path: Optional[Path] = None) -> dict:
    """The whole snapshot: {source, snapshot_date, copied_from, taxonomies: [...]}."""
    with open(path or CATALOG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def taxonomies(path: Optional[Path] = None) -> List[dict]:
    return load_catalog(path)["taxonomies"]


def identifiers(entry: dict) -> List[str]:
    """Every string that may appear in `mappings.ait_id` for this taxonomy."""
    ids = list(entry.get("ait_ids") or [])
    ids += _CCN.findall(entry.get("ccn") or "")
    if entry.get("taxonomy_name"):
        ids.append(entry["taxonomy_name"])
    return ids


def preferred_id(entry: dict, species: Optional[str] = None) -> str:
    """The value to write into `mappings.ait_id`: the AIT number for `species` when the
    taxonomy has per-species numbers, else AIT number > CCN > taxonomy_name."""
    by_species = entry.get("ait_ids_by_species") or {}
    if species and species.lower() in by_species:
        return by_species[species.lower()]
    return identifiers(entry)[0]


def find(identifier: str, path: Optional[Path] = None) -> Optional[dict]:
    """Catalog entry for an AIT id, CCN or taxonomy_name (case-insensitive), else None."""
    key = identifier.strip().lower()
    for entry in taxonomies(path):
        if key in {i.lower() for i in identifiers(entry)}:
            return entry
    return None


def rank(species: Sequence[str], regions: Sequence[str], *, path: Optional[Path] = None,
         top_n: int = 5) -> List[dict]:
    """Score catalog entries against a paper's species and regions.

    `species[0]` is the primary experimental species (+8 when the taxonomy covers it),
    +3 per matched region (max 3), +2 per further matched species, +1 each for
    whole-brain coverage and MapMyCells availability when any species matched.
    The ranking is a shortlist for the model to adjudicate, not a decision.
    """
    sp = {s.lower() for s in species}
    primary = species[0].lower() if species else ""
    rg = " ".join(regions).lower()
    ranked = []
    for t in taxonomies(path):
        cat_sp = {x.lower() for x in t["species"]}
        matched_sp = sorted(sp & cat_sp)
        matched_rg = sorted({r for r in t["regions"] if r.lower() in rg})
        score = 3 * min(len(matched_rg), 3) + 2 * len([s for s in matched_sp if s != primary])
        if primary in cat_sp:
            score += 8
        if matched_sp and "whole brain" in [r.lower() for r in t["regions"]]:
            score += 1
        if matched_sp and t.get("mapmycells"):
            score += 1
        ranked.append({"ait_id": preferred_id(t, primary), "title": t["title"],
                       "taxonomy_name": t["taxonomy_name"], "ccn": t["ccn"], "score": score,
                       "species_match": matched_sp, "region_match": matched_rg,
                       "hierarchy": t["hierarchy"], "publication": t["publication"]})
    ranked.sort(key=lambda r: -r["score"])
    return ranked[:top_n]


def species_covered(identifier: str, species: str, path: Optional[Path] = None) -> Optional[bool]:
    """True/False if the taxonomy covers `species`; None if the taxonomy is unknown."""
    entry = find(identifier, path)
    if entry is None:
        return None
    return species.strip().lower() in {s.lower() for s in entry["species"]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every taxonomy in the snapshot")
    r = sub.add_parser("rank", help="shortlist taxonomies for a paper")
    r.add_argument("--species", action="append", default=[],
                   help="repeatable; the first is the primary experimental species")
    r.add_argument("--region", action="append", default=[], help="repeatable")
    r.add_argument("--top", type=int, default=5)
    s = sub.add_parser("show", help="one entry by AIT id, CCN or taxonomy_name")
    s.add_argument("identifier")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        cat = load_catalog()
        print(f"snapshot {cat['snapshot_date']} ({len(cat['taxonomies'])} taxonomies)")
        for t in cat["taxonomies"]:
            print(f"  {preferred_id(t):<34} {', '.join(t['species']):<28} {t['title']}")
    elif args.cmd == "rank":
        print(json.dumps(rank(args.species, args.region, top_n=args.top), indent=2))
    else:
        entry = find(args.identifier)
        if entry is None:
            print(f"not in catalog: {args.identifier}")
            return 1
        print(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
