"""Expand extracted surface forms into one raw mention per occurrence (rule 2).

Exhaustive NER without asking a model to count: the extractor names each distinct
(surface form, label) it saw at least once; this script finds EVERY occurrence of
it in the source text and writes items whose `start`/`end` select `entity`
exactly. It makes no labeling decision of its own. Used by scripts/batch.py after
each extraction pass, and usable on its own.

Input is either the extractor prompt's own output (a list of entity items, offsets
optional) or a lexicon:

    {"entries": [
        {"entity": "PVALB", "label": "Gene"},
        {"entity": "fast-spiking interneurons", "label": "CellType", "ignore_case": true},
        {"entity": "IT", "label": "CellType", "only_in": ["IT neurons", "IT cells"]}],
     "items": [ {"entity": ..., "label": ..., "start": ..., "end": ...} ]}   # explicit

Expansion creates candidate occurrences, not evidence for new claims. Contextual
fields (relations, cell context, mappings, referent identity) stay on the grounded
occurrence that supplied them; they are never copied throughout a document.

Robustness, all learned on real PDFs: matching tolerates line wraps, NBSP and
soft hyphens inside a surface and hyphenation across a line break
("endocan-\\nnabinoids"); a citation number glued to a word ("microglia90") still
ends it (surfaces >= 4 chars ending in a letter); occurrences inside URLs and file
names are skipped; out-of-scope sections (References, Acknowledgements, Funding,
Author contributions, ...) are skipped unless `include_sections` says otherwise;
each mention gets the sentence that contains its WHOLE span and the section it
is in. Longer surfaces win: a hit inside a longer same-label hit is dropped,
unless `keep_nested` (the cns-cells conventions want nested spans).

CLI: python -m scripts.expand_mentions lexicon.json paper.txt out.json [--keep-nested]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

HEADINGS = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?(abstract|introduction|background|results?(?: and discussion)?|discussion|"
    r"conclusions?|methods?|materials and methods|star\s*methods|experimental procedures|"
    r"online methods|references|bibliography|literature cited|acknowledg\w*|supplementary\w*(?: \w+)?|"
    r"data availability|code availability|author contributions|contributions|funding|"
    r"competing interests|declaration of interests|conflicts? of interest|figure legends|extended data)\b",
    re.I,
)
# sections whose names/numbers are not the paper's scientific content
OUT_OF_SCOPE = {"references", "bibliography", "literature cited", "acknowledgements", "acknowledgments",
                "acknowledgement", "acknowledgment", "funding", "author contributions", "contributions",
                "competing interests", "declaration of interests", "conflict of interest",
                "conflicts of interest"}

URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:org|com|edu|gov|io|net|de|uk)(?:/\S*)?"
                 r"|\S+\.(?:pdf|nii|gz|csv|zip|tsv|h5|h5ad)\b", re.I)
WS = "[\\s ­]+"
HY = "(?:-[\\s­]*|[‐‑–]|­\\s*)"
# a bibliography entry: "12. Smith, J. ...", "Smith JA, ... (2019)", "... 2019; 12:345-9", a DOI
_CITATION = re.compile(r"^\s*(\[?\d{1,3}[.\])]\s+\S|[A-Z][A-Za-z'\-]+,?\s+(?:[A-Z]\.?\s?){1,3}[,.])"
                       r"|\((?:19|20)\d{2}[a-z]?\)|\b(?:19|20)\d{2}[a-z]?[;,.]\s*\d+|doi\.org/|\bdoi:|et al\.",
                       re.I)
_SENT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])|\n[ \t]*\n")


def flex(surf: str) -> str:
    """Regex for a surface that tolerates PDF line wraps, NBSP, soft hyphens and a
    hyphenated line break inside a word."""
    out: list[str] = []
    for i, ch in enumerate(surf):
        if ch.isspace():
            if not out or out[-1] != WS:
                out.append(WS)
        elif ch == "-":
            out.append(HY)
        else:
            out.append(re.escape(ch) + "­?")
            if ch.isalpha() and i + 1 < len(surf) and surf[i + 1].isalpha():
                out.append("(?:-\\n[ \\t]*)?")  # "endocan-\nnabinoids" matches "endocannabinoids"
    return "".join(out)


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENT.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if b > a]


def section_starts(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        s = line.strip().lstrip("#").strip().strip("[]")
        m = HEADINGS.match(s) if s and len(s) < 70 else None
        if m:
            out.append((pos, m.group(1).strip().title()))
        pos += len(line)
    return out


class Text:
    def __init__(self, text: str):
        self.text = text
        self.sents = sentence_spans(text)
        self.secs = section_starts(text)
        self.urls = [(m.start(), m.end()) for m in URL.finditer(text)]

    def sentence(self, a: int, b: int) -> str:
        lo = hi = None
        for s, e in self.sents:
            if lo is None and s <= a < e:
                lo = s
            if lo is not None and e >= b:
                hi = e
                break
        if lo is None:
            return self.text[max(0, a - 200):b + 200].strip()
        return self.text[lo:hi if hi is not None else b].strip()

    def section(self, a: int) -> str:
        name = "Body"
        for p, n in self.secs:
            if p <= a:
                name = n
            else:
                break
        return name

    def location(self, a: int) -> str:
        """Section for provenance: text laid out inside a reference list that is not
        itself a citation is body text, not "References"."""
        sec = self.section(a)
        if sec.lower() in ("references", "bibliography", "literature cited") and not self.is_citation(a):
            return "Body"
        return sec

    def line_at(self, a: int) -> str:
        lo = self.text.rfind("\n", 0, a) + 1
        hi = self.text.find("\n", a)
        return self.text[lo:hi if hi >= 0 else len(self.text)]

    def is_citation(self, a: int) -> bool:
        """Is the line at `a` a bibliography entry? PDFs interleave Methods text with
        the reference list, so a References heading does not make the rest of the
        document a bibliography."""
        return bool(_CITATION.search(self.line_at(a)))

    def in_url(self, a: int, b: int) -> bool:
        return any(u0 <= a and b <= u1 for u0, u1 in self.urls)


def _as_lexicon(extraction: dict) -> dict:
    """Extractor output (entities with chunk offsets) -> lexicon: every item becomes an
    explicit item AND an entry, so its other occurrences are found too."""
    if "entries" in extraction:
        return extraction
    entries, items = [], []
    for it in extraction.get("entities") or []:
        if not isinstance(it, dict) or not it.get("entity") or not it.get("label"):
            continue
        extra = {k: v for k, v in it.items() if k not in ("start", "end", "sentence", "paper_location")}
        entries.append(extra)
        if isinstance(it.get("start"), int) and isinstance(it.get("end"), int):
            items.append(it)
    return {"entries": entries, "items": items, "key_terms": extraction.get("key_terms") or [],
            "causal_relations": extraction.get("causal_relations") or []}


_CELL_LABELS = {"celltype", "cellclass", "cellsubtype", "cellpopulation", "cellontologyterm", "neuron",
                "interneuron", "glialcell", "morphologyclass"}


def _is_cell_label(label: str) -> bool:
    lab = (label or "").replace("_", "").replace(" ", "").lower()
    return lab in _CELL_LABELS or lab.endswith("cell") or lab.endswith("celltype")


def expand(lexicon: dict, text: str, *, model: str = "unknown", keep_nested: bool = False,
           include_sections: Iterable[str] = ()) -> tuple[list[dict], dict]:
    """(items, report). Items sorted by start; report names entries with 0 hits and
    explicit items that could not be grounded."""
    lex = _as_lexicon(lexicon)
    T = Text(text)
    allow = {s.lower() for s in include_sections}
    skip_secs = OUT_OF_SCOPE - allow
    # Only lexical detection hints can be propagated. Everything else belongs to
    # an individual observation (notably relations and ontology decisions).
    lexical_fields = {"entity", "label", "source_model"}
    explicit = list(lex.get("items") or [])
    for entry in lex.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("start"), int) and isinstance(entry.get("end"), int):
            explicit.append(entry)
        elif entry.get("sentence") and entry.get("entity"):
            # A unique quoted sentence can anchor a seed without numeric offsets.
            sentence, surface = entry["sentence"], entry["entity"]
            if text.count(sentence) == 1 and sentence.count(surface) == 1:
                a = text.index(sentence) + sentence.index(surface)
                explicit.append({**entry, "start": a, "end": a + len(surface)})

    def excluded(a: int, b: int) -> bool:
        if T.in_url(a, b):
            return True
        sec = T.section(a).lower()
        if sec not in skip_secs:
            return False
        if sec in ("references", "bibliography", "literature cited"):
            return T.is_citation(a)  # only the entries themselves, not text laid out among them
        return True

    # one entry per (surface, label): the first one's extra fields win
    seen_entries: dict[tuple[str, str], dict] = {}
    for e in lex.get("entries") or []:
        if isinstance(e, dict) and e.get("entity") and e.get("label"):
            seen_entries.setdefault((e["entity"], e["label"]), e)
    hits = []
    for e in seen_entries.values():
        surf = e["entity"].strip()
        if not surf:
            continue
        if e.get("referent_id") or e.get("identity_key"):
            # Homonyms require an explicit mention-level assignment.
            continue
        flags = re.I if e.get("ignore_case") else 0
        # a citation number glued to a word still ends it ("microglia90", "DS8,9"), but a
        # single digit continues a name ("SNARE-seq2", "sciATAC-seq3"); a hyphen into a
        # capital or digit continues a compound ("TF-IDF", "sci-ATAC")
        glued = r"(?!\d(?!\d|[,\u2013-]\d))" if surf[-1].isalpha() else r"(?!\d)"
        tail = r"(?![A-Za-z_])" + glued + r"(?!-[A-Z0-9])"
        head = r"(?<![\w-])" if surf[0].isalnum() else ""
        pat = re.compile(head + flex(surf) + tail, flags)
        ctx = [re.compile(flex(c), flags) for c in e.get("only_in") or []]
        for m in pat.finditer(text):
            if ctx and not any(cm.start() <= m.start() and m.end() <= cm.end()
                               for c in ctx for cm in c.finditer(text, max(0, m.start() - 200),
                                                                 min(len(text), m.end() + 200))):
                continue
            if not excluded(m.start(), m.end()):
                hits.append((m.start(), m.end(), e))

    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept = []
    for h in hits:
        inside = [k for k in kept if k[0] <= h[0] and h[1] <= k[1] and (k[1] - k[0]) > (h[1] - h[0])]
        # flat NER (the default): the longest span wins, whatever its label — "DNA" inside
        # "DNA methylation" is not a separate mention. keep_nested (cns-cells) keeps
        # nested spans of OTHER labels (a marker inside a cell-type span) but still drops
        # a same-label repeat.
        # nested spans are kept only INSIDE a cell mention (a marker, region or layer
        # within "PV+ layer 5 interneurons"); "neural" inside "neural development" or
        # "mouse" inside "mouse models" is a modifier, not a mention
        if inside and (not keep_nested
                       or any(k[2]["label"] == h[2]["label"] for k in inside)
                       or not any(_is_cell_label(k[2]["label"]) for k in inside)):
            continue
        kept.append(h)

    items, seen = [], set()
    src = f"llm_ner:{model}"
    for a, b, e in kept:
        if (a, b, e["label"]) in seen:
            continue
        it = {k: v for k, v in e.items() if k in lexical_fields}
        it.update({"entity": text[a:b], "label": e["label"], "start": a, "end": b,
                   "sentence": T.sentence(a, b), "paper_location": T.location(a)})
        it.setdefault("source_model", src)
        items.append(it)
        seen.add((a, b, e["label"]))
    ungrounded = []
    by_span = {(it["start"], it["end"], it["label"]): it for it in items}
    for it in explicit:
        a, b = it.get("start"), it.get("end")
        if not (isinstance(a, int) and isinstance(b, int) and text[a:b] == it.get("entity")):
            ungrounded.append(it.get("entity"))
            continue
        if excluded(a, b):
            continue
        it = dict(it)
        it.setdefault("sentence", T.sentence(a, b))
        it["paper_location"] = T.location(a)
        it.setdefault("source_model", src)
        key = (a, b, it["label"])
        if key in by_span:
            current = by_span[key]
            relations = current.get("relations", []) + it.get("relations", [])
            current.update(it)
            if relations:
                current["relations"] = list({json.dumps(r, sort_keys=True): r for r in relations}.values())
            continue
        items.append(it)
        by_span[key] = it
        seen.add((a, b, it["label"]))
    items.sort(key=lambda x: (x["start"], x["end"]))
    hit_entries = {id(k[2]) for k in kept}
    zero = [e["entity"] for e in seen_entries.values() if id(e) not in hit_entries]
    return items, {"mentions": len(items), "entries": len(seen_entries), "zero_hit_entries": zero,
                   "ungrounded_items": ungrounded}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("lexicon", type=Path)
    ap.add_argument("text", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--keep-nested", action="store_true")
    ap.add_argument("--include-section", action="append", default=[])
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)
    lex = json.loads(args.lexicon.read_text())
    text = args.text.read_text(encoding="utf-8")
    items, rep = expand(lex, text, model=args.model or lex.get("model", "unknown"),
                        keep_nested=args.keep_nested or bool(lex.get("keep_nested")),
                        include_sections=args.include_section)
    out = {"source_metadata": lex.get("source_metadata", {}), "task_type": "ner", "entities": items,
           "key_terms": lex.get("key_terms", []), "causal_relations": lex.get("causal_relations", [])}
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"{rep['mentions']} mentions from {rep['entries']} entries; {len(rep['zero_hit_entries'])} with 0 hits: "
          f"{rep['zero_hit_entries'][:20]}; {len(rep['ungrounded_items'])} ungrounded explicit items",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
