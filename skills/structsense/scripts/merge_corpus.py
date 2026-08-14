#!/usr/bin/env python3
"""Merge per-paper extraction results into one corpus JSON + a Markdown report.

Per-paper `<stem>_final.json` files stay the authoritative record (SKILL.md rule 9).
This adds the corpus view on top: one canonical row per cell across every paper, with
which documents it appears in, so you can answer "which cell types does this corpus
talk about, and where" without opening N files.

    python -m scripts.merge_corpus out/*_final.json --out-json corpus.json --out-md corpus.md

Design notes
------------
The corpus index is **grouped, not concatenated**. Concatenating raw mentions would
duplicate every span — a 40-paper corpus of cell-typing papers runs to hundreds of
thousands of mentions, and the per-paper files already hold them. Pass
``--include-mentions`` if you genuinely want the raw union embedded (it is exact, just
large); ``--no-index`` if you only want the roll-up.

Grouping reuses ``group_by_entity._canonical_key`` so a corpus row collapses exactly
the same way a per-paper ``entities_grouped`` row does. If those two disagreed, corpus
counts would not reconcile against per-paper counts and neither number could be
trusted.

Document identity comes from ``source_metadata`` (doi, then paper_title, then
source_path, then the filename). A corpus assembled from files that all lack
source_metadata degenerates to filenames, which is why the report prints the
identity it used rather than assuming you know.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS.parent) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS.parent))

from scripts.group_by_entity import _canonical_key, _pick_canonical_surface  # noqa: E402

# Item lists a result may carry, and the surface field each uses.
_ITEM_LISTS = (("entities", "entity"), ("key_terms", "term"), ("resources", "name"))

# Specificity values from the cell-annotation conventions, in report order.
_SPECIFICITY = ("cell_phenotype", "cell_vague", "cell_hetero")


def _document_id(result: dict, path: Path) -> tuple[str, str]:
    """Return (document_id, how_it_was_derived)."""
    meta = result.get("source_metadata") or {}
    for key in ("doi", "paper_title", "source_path"):
        val = (meta.get(key) or "").strip() if isinstance(meta.get(key), str) else ""
        if val:
            return val, key
    return path.stem, "filename"


def _load(paths: Iterable[Path]) -> list[tuple[Path, dict]]:
    loaded = []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  SKIP {p.name}: not valid JSON ({exc})", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"  SKIP {p.name}: top level is {type(data).__name__}, expected object",
                  file=sys.stderr)
            continue
        loaded.append((p, data))
    return loaded


def _ontology_of(item: dict) -> Optional[str]:
    """The item's ontology id, ignoring values a fabrication guard demoted."""
    if item.get("concept_mapping_provenance") in ("llm_knowledge", "validation_failed"):
        return None
    oid = item.get("ontology_id")
    return oid if isinstance(oid, str) and oid.strip() else None


def build_index(docs: list[tuple[str, dict]], *, include_mentions: bool) -> dict:
    """Cross-document grouped index, keyed the same way per-paper grouping is."""
    out: dict[str, list[dict]] = {}
    for list_name, surface_key in _ITEM_LISTS:
        buckets: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
        for doc_id, result in docs:
            for item in result.get(list_name) or []:
                surface = item.get(surface_key)
                if not isinstance(surface, str) or not surface.strip():
                    continue
                buckets[_canonical_key(surface, item.get("label") or item.get("type"))].append(
                    (doc_id, item)
                )
        if not buckets:
            continue

        rows = []
        for (_, label), pairs in buckets.items():
            items = [it for _, it in pairs]
            per_doc: dict[str, int] = defaultdict(int)
            for doc_id, _ in pairs:
                per_doc[doc_id] += 1

            # Ontology ids seen for this canonical form. More than one is a real
            # signal — the same cell name mapped differently in different papers is
            # exactly the disagreement a curator needs to see, so surface all of
            # them rather than silently picking a winner.
            ontology_ids = sorted({o for o in (_ontology_of(it) for it in items) if o})
            specificities = sorted({
                it["specificity"] for it in items
                if isinstance(it.get("specificity"), str)
            })
            source_models = sorted({
                it["source_model"] for it in items
                if isinstance(it.get("source_model"), str)
            })

            row: dict[str, Any] = {
                surface_key: _pick_canonical_surface([it[surface_key] for it in items]),
                "label": label or None,
                "mention_count": len(items),
                "document_count": len(per_doc),
                "documents": dict(sorted(per_doc.items())),
                "ontology_ids": ontology_ids,
                "ontology_conflict": len(ontology_ids) > 1,
                "source_models": source_models,
            }
            if specificities:
                row["specificities"] = specificities
                # Disagreement across papers is signal, not noise — the same surface
                # form can be a grounded phenotype in one and part of a hedged set in
                # another. Only collapse when every mention agrees.
                row["specificity"] = specificities[0] if len(specificities) == 1 else None
            if include_mentions:
                row["mentions"] = [
                    {**it, "document_id": doc_id} for doc_id, it in pairs
                ]
            rows.append(row)

        rows.sort(key=lambda r: (-r["mention_count"], -r["document_count"],
                                 str(r.get(surface_key) or "")))
        out[f"{list_name}_index"] = rows
    return out


def corpus_stats(docs: list[tuple[str, dict]], index: dict) -> dict:
    """Roll-up totals. Deliberately recomputed from items, not summed from each
    file's own `stats`, so a stale per-paper stats block cannot corrupt the total."""
    totals = {"document_count": len(docs)}
    by_label: dict[str, int] = defaultdict(int)
    by_specificity: dict[str, int] = defaultdict(int)
    mapped = unmapped = 0

    for list_name, _surface_key in _ITEM_LISTS:
        n = sum(len(r.get(list_name) or []) for _, r in docs)
        if n or list_name == "entities":
            totals[f"total_{list_name}"] = n
        rows = index.get(f"{list_name}_index")
        if rows is not None:
            totals[f"unique_{list_name}"] = len(rows)

    for _, result in docs:
        for item in result.get("entities") or []:
            by_label[str(item.get("label") or "unlabeled")] += 1
            spec = item.get("specificity")
            if isinstance(spec, str):
                by_specificity[spec] += 1
            if _ontology_of(item):
                mapped += 1
            else:
                unmapped += 1

    ent = totals.get("total_entities", 0)
    uniq = totals.get("unique_entities", 0)
    stats: dict[str, Any] = {
        "totals": totals,
        "mentions_per_unique": round(ent / uniq, 2) if uniq else None,
        "by_label": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        "alignment": {"mapped": mapped, "unmapped": unmapped},
    }
    if by_specificity:
        # Ordered by the conventions doc, then anything unexpected.
        ordered = {k: by_specificity[k] for k in _SPECIFICITY if k in by_specificity}
        ordered.update({k: v for k, v in sorted(by_specificity.items())
                        if k not in ordered})
        stats["by_specificity"] = ordered
    conflicts = [r for r in index.get("entities_index", []) if r.get("ontology_conflict")]
    stats["ontology_conflicts"] = len(conflicts)
    return stats


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(rows_) + " |" for rows_ in (map(str, r) for r in rows)]
    return out


def render_markdown(corpus: dict, *, top_n: int) -> str:
    stats = corpus["stats"]
    totals = stats["totals"]
    L: list[str] = ["# Corpus extraction summary", ""]

    L += [f"**{totals['document_count']} document(s)**, "
          f"{totals.get('total_entities', 0):,} entity mentions, "
          f"{totals.get('unique_entities', 0):,} unique.", ""]

    mpu = stats.get("mentions_per_unique")
    if mpu is not None:
        L.append(f"`mentions_per_unique` = **{mpu}**.")
        if mpu < 1.5:
            # The rule-8 / cell yield check, stated where someone will read it.
            L.append("> Close to 1 on cell-typing text means surface-form "
                     "deduplication crept in somewhere — a cell name repeated across "
                     "a paper should not collapse to one row.")
        L.append("")

    if stats.get("by_specificity"):
        L += ["## Specificity", ""]
        L += _md_table(["type", "mentions"],
                       [[k, f"{v:,}"] for k, v in stats["by_specificity"].items()])
        L += ["", "`cell_vague` / `cell_hetero` carry no ontology id by design — they "
              "are complete answers, not misses.", ""]

    al = stats["alignment"]
    L += ["## Alignment", "",
          f"mapped **{al['mapped']:,}** / unmapped **{al['unmapped']:,}**"
          f" · conflicting ids across papers: **{stats['ontology_conflicts']}**", ""]

    L += ["## Documents", ""]
    L += _md_table(["document", "entities", "unique", "id source"],
                   [[d["document_id"], f"{d['entity_count']:,}",
                     f"{d['unique_entity_count']:,}", d["document_id_source"]]
                    for d in corpus["documents"]])
    L.append("")

    if stats.get("by_label"):
        L += ["## Labels", ""]
        L += _md_table(["label", "mentions"],
                       [[k, f"{v:,}"] for k, v in stats["by_label"].items()])
        L.append("")

    rows = corpus.get("entities_index") or []
    if rows:
        L += [f"## Top {min(top_n, len(rows))} entities across the corpus", ""]
        L += _md_table(["entity", "label", "mentions", "docs", "ontology"],
                       [[r.get("entity") or "", r.get("label") or "",
                         f"{r['mention_count']:,}", r["document_count"],
                         ", ".join(r["ontology_ids"]) or "—"]
                        for r in rows[:top_n]])
        L.append("")

        conflicts = [r for r in rows if r.get("ontology_conflict")]
        if conflicts:
            L += ["## Conflicting ontology ids", "",
                  "The same surface form mapped differently in different papers. "
                  "Worth a look before ingesting — one of them is probably wrong, and "
                  "the corpus index will not choose for you.", ""]
            L += _md_table(["entity", "label", "ids", "docs"],
                           [[r.get("entity") or "", r.get("label") or "",
                             ", ".join(r["ontology_ids"]), r["document_count"]]
                            for r in conflicts[:top_n]])
            L.append("")

    return "\n".join(L)


def build_corpus(paths: list[Path], *, include_mentions: bool, with_index: bool) -> dict:
    loaded = _load(paths)
    if not loaded:
        raise SystemExit("no readable result files — nothing to merge")

    docs: list[tuple[str, dict]] = []
    doc_rows = []
    seen: dict[str, int] = defaultdict(int)
    for path, result in loaded:
        doc_id, how = _document_id(result, path)
        seen[doc_id] += 1
        if seen[doc_id] > 1:
            # Two files claiming one identity would silently merge into one column.
            doc_id = f"{doc_id} #{seen[doc_id]}"
            print(f"  NOTE {path.name}: duplicate document id, disambiguated as "
                  f"{doc_id!r}", file=sys.stderr)
        docs.append((doc_id, result))
        ents = result.get("entities") or []
        doc_rows.append({
            "document_id": doc_id,
            "document_id_source": how,
            "source_path": str(path),
            "source_metadata": result.get("source_metadata") or {},
            "entity_count": len(ents),
            "unique_entity_count": len({
                _canonical_key(e.get("entity") or "", e.get("label"))
                for e in ents if isinstance(e.get("entity"), str)
            }),
            "key_term_count": len(result.get("key_terms") or []),
            "stats": result.get("stats") or {},
        })

    index = build_index(docs, include_mentions=include_mentions) if with_index else {}
    corpus: dict[str, Any] = {
        "corpus_metadata": {
            "document_count": len(docs),
            "task_type": "ner",
            "merged_from": [str(p) for p, _ in loaded],
            "index_includes_mentions": bool(include_mentions and with_index),
        },
        "documents": doc_rows,
    }
    corpus.update(index)
    corpus["stats"] = corpus_stats(docs, index)
    return corpus


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="merge_corpus",
        description="Merge per-paper extraction results into one corpus JSON + Markdown.",
    )
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="per-paper result JSON files (e.g. out/*_final.json)")
    ap.add_argument("--out-json", type=Path, default=Path("corpus_final.json"))
    # str, not Path: Path("") is Path("."), which is truthy and a directory, so a
    # Path-typed default turned "skip the report" into IsADirectoryError.
    ap.add_argument("--out-md", default="corpus_final.md",
                    help="Markdown report path; pass '' or 'none' to skip it")
    ap.add_argument("--include-mentions", action="store_true",
                    help="embed every raw mention in the index (exact, but large — "
                         "the per-paper files already hold them)")
    ap.add_argument("--no-index", action="store_true",
                    help="roll-up stats and per-document rows only")
    ap.add_argument("--top", type=int, default=50,
                    help="rows in the Markdown top-entities table (default 50)")
    args = ap.parse_args(argv)

    missing = [p for p in args.inputs if not p.is_file()]
    if missing:
        raise SystemExit("not a file: " + ", ".join(str(p) for p in missing))

    corpus = build_corpus(args.inputs, include_mentions=args.include_mentions,
                          with_index=not args.no_index)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"wrote {args.out_json}")

    out_md = (args.out_md or "").strip()
    if out_md and out_md.lower() != "none":
        md_path = Path(out_md)
        md = render_markdown(corpus, top_n=args.top)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md + "\n", encoding="utf-8")
        print(f"wrote {md_path}")

    t = corpus["stats"]["totals"]
    print(f"  {t['document_count']} document(s), {t.get('total_entities', 0)} mentions, "
          f"{t.get('unique_entities', 0)} unique, "
          f"{corpus['stats']['ontology_conflicts']} ontology conflict(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
