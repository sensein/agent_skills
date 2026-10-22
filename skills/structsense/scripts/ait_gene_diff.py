"""Pass 4 of the AIT mapping mode: entity cards with the marker-gene diff.

For every current mapping edge (a non-superseded `mappings.csv` row with an
`ait_node_id`, whose entity is not quarantined) this writes one `entity_cards.csv`
row: the edge, the three gene sets, their counts and Jaccard, the assay and panel
depth, and the evidence. Every value is a join or a set operation — nothing is
judged here.

Inputs beyond the tables:

* `--markers markers.json` — `{ait_node_id: [gene, ...]}`, the AIT node marker sets
  retrieved with the AIT taxonomy reader.
* `--panels panels.json` (optional) — `{gene_panel_name or assay_id: [gene, ...]}`
  for targeted assays (MERFISH, Xenium, …).
* `--namespace` — the namespace both sides were resolved into (e.g. `HGNC`, `MGI`).

Both gene sets must already be in that one namespace, with cross-species orthologs
resolved (GeneOrthology tool). Symbols are compared exactly: folding `Pvalb` onto
`PVALB` by case is an ortholog assumption, and making it here would hide it.

Jaccard is `NA` when the paper names no marker genes for the entity: with nothing on
the paper side there is nothing to agree or disagree with, and a 0 would read as
disagreement (and sort the edge to the bottom of a review queue). It is a number only
when the paper states at least one marker.

A targeted assay cannot mention a gene that is not on its panel, so a large
`genes_taxonomy_only` is not evidence against the mapping. For a targeted assay,
`panel_limited = true` and `jaccard_panel_restricted` is computed with both sets
restricted to the panel (`NA` when the panel gene list was not found); for an
untargeted assay it is left empty (not applicable).

    python -m scripts.ait_gene_diff out/ --markers markers.json --panels panels.json --namespace HGNC

It also writes `out/marker_report.json`: per edge whether the paper and the taxonomy
node have markers, which nodes carry taxonomy markers and which do not, and the counts.
`run_report.md` quotes these numbers; it never summarises marker availability itself.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from scripts import ait_tables as tables

NA = tables.NA


def _clean(genes: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for g in genes:
        g = g.strip()
        if g and g != NA and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def gene_diff(paper: Iterable[str], taxonomy: Iterable[str],
              panel: Optional[Iterable[str]] = None) -> Dict[str, object]:
    """Shared / paper-only / taxonomy-only sets, counts and Jaccard.

    `panel=None` means untargeted (no restriction); an empty iterable means the panel
    is targeted but its gene list is unknown, so the restricted Jaccard is `NA`."""
    p, t = _clean(paper), _clean(taxonomy)
    ps, ts = set(p), set(t)
    shared = [g for g in p if g in ts]
    paper_only = [g for g in p if g not in ts]
    taxonomy_only = [g for g in t if g not in ps]
    union = ps | ts
    out: Dict[str, object] = {
        "genes_shared": shared, "genes_paper_only": paper_only, "genes_taxonomy_only": taxonomy_only,
        "n_shared": len(shared), "n_paper_only": len(paper_only), "n_taxonomy_only": len(taxonomy_only),
        # NA (None) unless the paper states a marker: an empty paper side is no evidence
        "jaccard": round(len(shared) / len(union), 4) if ps else None,
        "panel_limited": panel is not None,
        "jaccard_panel_restricted": None,
    }
    if panel is not None:
        pn = set(_clean(panel))
        if pn:
            rp, rt = ps & pn, ts & pn
            ru = rp | rt
            out["jaccard_panel_restricted"] = round(len(rp & rt) / len(ru), 4) if rp else NA
        else:
            out["jaccard_panel_restricted"] = NA
    return out


def _merge_provenance(*values: str) -> str:
    merged: Dict[str, List[str]] = {}
    for v in values:
        for field, labels in tables.parse_provenance(v or "").items():
            bucket = merged.setdefault(field, [])
            bucket.extend(l for l in labels if l not in bucket)
    return tables.format_provenance(merged)


def build_cards(data: Dict[str, List[Dict[str, str]]], markers: Dict[str, List[str]],
                panels: Optional[Dict[str, List[str]]] = None, *, namespace: str) -> tuple:
    """(cards, warnings). One card per current, non-quarantined mapping edge."""
    panels = panels or {}
    entities = {e["mention_id"]: e for e in data.get("entities.csv", [])}
    assays = {m["assay_id"]: m for m in data.get("methods.csv", [])}
    cards, warnings = [], []
    for m in tables.current_mappings(data.get("mappings.csv", [])):
        node = m.get("ait_node_id", "")
        ent = entities.get(m.get("mention_id", ""))
        if not node or ent is None or tables.is_quarantined(ent):
            continue
        assay_ids = tables.split_multi(ent.get("assay_id", ""))
        assay_rows = [assays[a] for a in assay_ids if a in assays]
        targeted = [a for a in assay_rows if a.get("targeted") == "true"]
        panel: Optional[List[str]] = None
        if targeted:
            panel = []
            for a in targeted:
                panel += panels.get(a.get("gene_panel_name", ""), []) or panels.get(a["assay_id"], [])

        paper_genes = tables.split_multi(ent.get("marker_genes_normalized", ""))
        # The card carries only evidence that was found; a not_found sentence is not evidence.
        sentences = tables.split_multi(ent.get("evidence_sentence", ""))
        statuses = tables.split_multi(ent.get("evidence_verified", ""))
        found = [i for i, s in enumerate(statuses) if s != "not_found" and i < len(sentences)]
        row: Dict[str, str] = {
            "paper_id": m.get("paper_id", ""), "mention_id": m["mention_id"],
            "cell_type_name_as_in_paper": ent.get("label_verbatim", ""),
            "ait_id": m.get("ait_id", ""), "ait_node_id": node,
            "ait_cell_type_label": m.get("ait_cell_type_label", ""),
            "skos_relation": m.get("skos_relation", ""),
            "match_confidence": tables.SKOS_TO_CONFIDENCE.get(m.get("skos_relation", ""), ""),
            "gene_namespace": namespace,
            "assay_name": tables.join_multi(a.get("assay_name", "") for a in assay_rows),
            "gene_panel_size": tables.join_multi(a.get("gene_panel_size", "") for a in targeted),
            "evidence_sentence": tables.join_multi(sentences[i] for i in found),
            "evidence_verified": tables.join_multi(statuses[i] for i in found),
            "field_provenance": _merge_provenance(ent.get("field_provenance", ""), m.get("field_provenance", "")),
            "run_id": m.get("run_id", ""),
        }
        if node not in markers:
            warnings.append(f"{m['mention_id']} -> {node}: no marker set in --markers; gene columns NA")
            for key in ("genes_shared", "genes_paper_only", "genes_taxonomy_only",
                        "n_shared", "n_paper_only", "n_taxonomy_only", "jaccard"):
                row[key] = NA
            row["panel_limited"] = "true" if targeted else "false"
            row["jaccard_panel_restricted"] = NA if targeted else ""
        else:
            if targeted and not panel:
                warnings.append(f"{m['mention_id']}: targeted assay but no panel gene list in --panels")
            diff = gene_diff(paper_genes, markers[node], panel)
            for key in ("genes_shared", "genes_paper_only", "genes_taxonomy_only"):
                row[key] = tables.join_multi(diff[key])
            for key in ("n_shared", "n_paper_only", "n_taxonomy_only"):
                row[key] = str(diff[key])
            row["jaccard"] = NA if diff["jaccard"] is None else str(diff["jaccard"])
            row["panel_limited"] = "true" if diff["panel_limited"] else "false"
            jpr = diff["jaccard_panel_restricted"]
            row["jaccard_panel_restricted"] = "" if jpr is None else str(jpr)
        cards.append(row)
    return cards, warnings


def marker_report(cards: List[Dict[str, str]], markers: Dict[str, List[str]],
                  labels: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    """What the marker comparison could and could not say, from the cards themselves."""
    labels = labels or {c["ait_node_id"]: c.get("ait_cell_type_label", "") for c in cards}
    nodes = sorted({c["ait_node_id"] for c in cards})
    with_tax = [n for n in nodes if markers.get(n)]
    def n_of(c, side):
        v = c.get(f"n_{side}", "")
        return int(v) if v not in ("", NA) else 0
    paper_side = [c for c in cards if n_of(c, "shared") + n_of(c, "paper_only")]
    return {
        "edges": len(cards),
        "nodes": len(nodes),
        "nodes_with_taxonomy_markers": [{"ait_node_id": n, "label": labels.get(n, ""), "markers": markers[n]}
                                        for n in with_tax],
        "nodes_without_taxonomy_markers": len(nodes) - len(with_tax),
        "edges_where_paper_names_markers": len(paper_side),
        "edges_with_shared_markers": sum(1 for c in cards if n_of(c, "shared")),
        "edges_jaccard_na": sum(1 for c in cards if c.get("jaccard") == NA),
        "edges_jaccard_na_reason": "Jaccard is NA when the paper names no marker for the entity",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--markers", type=Path, required=True, help="{ait_node_id: [genes]}")
    ap.add_argument("--panels", type=Path, help="{gene_panel_name or assay_id: [genes]}")
    ap.add_argument("--namespace", required=True, help="e.g. HGNC or MGI")
    args = ap.parse_args(argv)

    markers = json.loads(args.markers.read_text(encoding="utf-8"))
    panels = json.loads(args.panels.read_text(encoding="utf-8")) if args.panels else {}
    cards, warnings = build_cards(tables.load_dir(args.out_dir), markers, panels, namespace=args.namespace)
    tables.write_table(args.out_dir / "entity_cards.csv", "entity_cards.csv", cards)
    rep = marker_report(cards, markers)
    (args.out_dir / "marker_report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
    for w in warnings:
        print("WARNING", w)
    print(f"entity_cards.csv: {len(cards)} rows")
    print(f"markers: {rep['nodes']} mapped node(s), {len(rep['nodes_with_taxonomy_markers'])} with taxonomy "
          f"markers; paper names markers on {rep['edges_where_paper_names_markers']}/{rep['edges']} edge(s), "
          f"shared on {rep['edges_with_shared_markers']}; Jaccard NA on {rep['edges_jaccard_na']} "
          f"(paper names no marker). Details: marker_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
