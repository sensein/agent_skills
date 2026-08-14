#!/usr/bin/env python3
"""Merge per-paper extraction results into one corpus JSON + a Markdown report.

Per-paper `<stem>_final.json` files stay the authoritative record (SKILL.md rule 9).
This adds the corpus view on top: one canonical row per cell across every paper, with
which documents it appears in, so you can answer "which cell types does this corpus
talk about, and where" without opening N files.

    python -m scripts.merge_corpus out/*_final.json --out out/corpus_synthesis

writing ``corpus_synthesis.json`` + ``corpus_synthesis.md``, mirroring
``scripts/abcd_synthesize.py``'s ``--out abcd_synthesis`` so the skill's two
cross-paper passes share one interface.

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

# Importable both ways: `python -m scripts.merge_corpus` from the skill root, and as a
# bare sibling (how pipeline.py imports it, with scripts/ on sys.path). Adding both
# directories and trying both spellings keeps either entry point working — the two
# scripts disagreed about this and the mismatch is invisible until one of them runs.
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_SCRIPTS), str(_SCRIPTS.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from group_by_entity import _canonical_key, _pick_canonical_surface  # noqa: E402
except ImportError:  # pragma: no cover - depends on entry point
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


# Corpus outputs must never be read back in as if they were per-paper results — a
# second run over the same directory would otherwise fold the previous synthesis into
# the new one and double every count. Same class of bug as abcd's results-directory
# exclusion, and equally invisible once it happens.
_CORPUS_SUFFIXES = ("_synthesis.json", "_corpus.json", "corpus_final.json")


def expand_inputs(inputs: Iterable[Path]) -> list[Path]:
    """Resolve files and directories to a sorted list of per-paper result files.

    A directory expands to its ``*_final.json`` (rule 9's per-paper convention),
    non-recursively — nested directories in an output tree are usually extracted text
    or payloads, not results.
    """
    out: list[Path] = []
    for item in inputs:
        if item.is_dir():
            found = sorted(p for p in item.glob("*_final.json") if p.is_file())
            if not found:
                print(f"  NOTE {item}: no *_final.json inside (per-paper results are "
                      f"named <stem>_final.json — see SKILL.md rule 9)", file=sys.stderr)
            out.extend(found)
        else:
            out.append(item)

    kept, skipped = [], []
    for p in out:
        (skipped if any(p.name.endswith(s) for s in _CORPUS_SUFFIXES) else kept).append(p)
    for p in skipped:
        print(f"  SKIP {p.name}: looks like a corpus roll-up, not a per-paper result",
              file=sys.stderr)

    # Deduplicate by resolved path: `merge_corpus out/ out/a_final.json` must not
    # count that paper twice, which would inflate its weight in every count.
    seen: set[Path] = set()
    unique = []
    for p in kept:
        rp = p.resolve()
        if rp in seen:
            print(f"  SKIP {p.name}: already included", file=sys.stderr)
            continue
        seen.add(rp)
        unique.append(p)
    return unique


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
                    help="per-paper result JSON files, and/or a directory containing "
                         "them (expands to its *_final.json)")
    # Output stem, matching scripts/abcd_synthesize.py's `--out abcd_synthesis` →
    # abcd_synthesis.{json,md,ttl}. Same convention so the two cross-paper passes in
    # this skill do not each invent their own interface.
    ap.add_argument("--out", type=Path, default=Path("corpus_synthesis"),
                    help="output stem; writes <stem>.json and <stem>.md "
                         "(default: corpus_synthesis)")
    ap.add_argument("--formats", default="json,md",
                    help="comma-separated subset of json,md (default: both)")
    ap.add_argument("--include-mentions", action="store_true",
                    help="embed every raw mention in the index (exact, but large — "
                         "the per-paper files already hold them)")
    ap.add_argument("--no-index", action="store_true",
                    help="roll-up stats and per-document rows only")
    ap.add_argument("--top", type=int, default=50,
                    help="rows in the Markdown top-entities table (default 50)")
    args = ap.parse_args(argv)

    bad = [p for p in args.inputs if not p.exists()]
    if bad:
        raise SystemExit("does not exist: " + ", ".join(str(p) for p in bad))
    inputs = expand_inputs(args.inputs)
    if not inputs:
        raise SystemExit("no per-paper result files to merge")

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    unknown = formats - {"json", "md"}
    if unknown:
        raise SystemExit(f"unknown format(s): {', '.join(sorted(unknown))} "
                         "(supported: json, md)")
    if not formats:
        raise SystemExit("--formats selected nothing; pass json, md, or json,md")

    corpus = build_corpus(inputs, include_mentions=args.include_mentions,
                          with_index=not args.no_index)

    stem = args.out
    stem.parent.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        path = stem.with_suffix(".json")
        path.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        print(f"wrote {path}")
    if "md" in formats:
        path = stem.with_suffix(".md")
        path.write_text(render_markdown(corpus, top_n=args.top) + "\n", encoding="utf-8")
        print(f"wrote {path}")

    t = corpus["stats"]["totals"]
    print(f"  {t['document_count']} document(s), {t.get('total_entities', 0)} mentions, "
          f"{t.get('unique_entities', 0)} unique, "
          f"{corpus['stats']['ontology_conflicts']} ontology conflict(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
