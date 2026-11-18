#!/usr/bin/env python3
"""Assemble the review manuscript from ``review.json`` + ``review.manuscript.json``.

``review.json`` holds the review's data: flow, included studies, screening,
appraisal, charting, GRADE and per-group answers. ``review.manuscript.json``
holds the prose that makes it a paper: the full Discussion, prior reviews,
gaps, the confidence statement, figure interpretations and declarations. Its
schema is in ``references/review_template.md``. This renders the two, in the
template's section order, into ``<stem>.manuscript.md``, the document the PDF
is built from:

* every declared heading, numbered as emitted (no gaps by construction);
  a section with no content says "Not assessed: <reason>";
* figures (``review_figures.py`` + your own) placed in their sections, numbered
  in order of appearance, and ``{fig:<id>}`` references resolved to "Figure N";
* per-record appendices written as sibling files and linked with row counts
  (``appendix_policy: sibling``, the default), never inlined;
* the template's export gates evaluated (``--check``: exit 1 on a failure).

Empty authored fields fall back to the pipeline's prose in ``review.json``
(introduction, themes, limitations, GRADE), so a pipeline review assembles
without a manuscript file. The gates then show what still has to be written.

Usage:
    python assemble_manuscript.py out/review.json                       # writes out/review.manuscript.md
    python assemble_manuscript.py out/review.json --check               # + gates, exit 1 on failure
    python assemble_manuscript.py out/review.json --manuscript ms.json --references vancouver

Stdlib only; ``review_figures.py`` draws the figures when ``figures/figures.json``
is absent (``--no-figures`` to skip).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_figures  # noqa: E402

FIG_REF = re.compile(r"\{fig:([\w-]+)\}")
NOT_ASSESSED = "Not assessed: {}."
WORDS_PER_PAGE, ROWS_PER_PAGE, FIG_PAGES, REF_PAGES = 550, 45, 0.4, 0.045
INLINE_ROW_CAP = 60

# Figure slots in document order: numbering follows this, so a number never
# depends on which section happened to be rendered first.
SLOT_ORDER = ["methods.retrieval", "results.flow", "results.overview", "results.characteristics",
              "results.rq", "results.meta_analysis", "results.cross_cutting", "results.groups",
              "results.appraisal", "results.certainty", "results.gaps", "discussion"]


def _s(v: Any) -> str:
    return "" if v is None else (v if isinstance(v, str) else str(v))


def _group_summary(v: Any) -> str:
    """A group's summary as prose. The package's GroupSummary is structured
    (label, n_studies, aggregate_finding, representative_pmids, caveats); passing it
    through _s() printed its Python repr into the manuscript."""
    if isinstance(v, dict):
        finding = _s(v.get("aggregate_finding") or v.get("summary") or v.get("text")).strip()
        caveats = _s(v.get("caveats")).strip()
        return " ".join(x if x.endswith((".", "!", "?")) else f"{x}." for x in (finding, caveats) if x)
    return _s(v).strip()


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][\w'’-]*", text))


def _cell(v: Any, cap: Optional[int] = None) -> str:
    """A table cell. Authored prose is never cut; ``cap`` is for per-record data columns,
    and cuts at a word boundary so a cell never ends mid-word."""
    t = re.sub(r"\s+", " ", _s(v)).strip().replace("|", "\\|")
    if cap is None or len(t) <= cap:
        return t
    return t[:cap].rsplit(" ", 1)[0].rstrip(",;:") + " …"


def _widths(header: list[str], rows: list[list[str]], budget: int = 90) -> list[int]:
    """Relative column widths from the content: pandoc reads them from the dash counts.

    Equal dashes make every column the same width, so a 4-character year column
    gets as much room as a 300-character description and the description wraps
    one word per line. Each column wants its typical (not longest) cell, and is
    never narrower than its longest word ("NKG-005", "2009", "representation"),
    so short columns never split a word. When the wants exceed ``budget``
    characters (about one line of table type), only the wide columns give way.
    """
    floors, wants = [], []
    for j, h in enumerate(header):
        col = [r[j] for r in rows if j < len(r)]
        lens = sorted(len(c) for c in col) or [0]
        typical = lens[int(0.75 * (len(lens) - 1))]
        word = max((len(w) for c in col for w in c.split()), default=3)
        head = max((len(w) for w in h.split()), default=3)
        # a long header word ("publications" over a column of 1s) may run a little
        # wide rather than take a dozen characters from the column beside it
        floor = max(3, min(14, word), min(10, head))
        floors.append(floor)
        wants.append(max(floor, min(60, typical)))
    slack = sum(wants) - sum(floors)
    room = budget - sum(floors)
    if sum(wants) > budget and slack > 0:
        scale = max(0.0, room) / slack
        wants = [f + (w - f) * scale for f, w in zip(floors, wants)]
    return [max(3, round(w)) for w in wants]


def _table(header: list[str], rows: list[list[Any]], caps: Optional[list[Optional[int]]] = None) -> list[str]:
    limits: list[Optional[int]] = list(caps) if caps else [None] * len(header)
    cells = [[_cell(c, limits[j] if j < len(limits) else None) for j, c in enumerate(r)] for r in rows]
    header = [_s(h).replace("_", " ") for h in header]   # data keys ("resource_id") read as words
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("-" * w for w in _widths(header, cells)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in cells]
    return out + [""]


def _criteria(v: Any) -> list[str]:
    """Criteria as a list: given as one, split on lines, '; ' or their own codes (IC1 … IC2 …)."""
    if isinstance(v, list):
        return [_s(x).strip() for x in v if _s(x).strip()]
    parts = re.split(r"\n|;\s+|\s+(?=\b[IE]C\d+\b)", _s(v))
    return [x.strip() for x in parts if x.strip()]


def _short_cite(v: Any) -> str:
    """'Larson SD, Martone ME (2009). Ontologies for …' → 'Larson and Martone (2009)'."""
    t = _s(v).strip()
    m = re.match(r"^(.*?)\s*\((\d{4}[a-z]?)\)", t)
    if not m:
        return t
    etal = "et al" in m.group(1)
    authors = re.sub(r"\bet al\.?", "", m.group(1))
    names = [re.sub(r"\s+[A-Z][A-Z-]{0,3}$", "", n.strip()) for n in re.split(r",|;|&| and ", authors)]
    names = [n for n in names if n]
    if not names:
        return t
    who = (f"{names[0]} et al." if etal or len(names) > 2
           else " and ".join(names))
    return f"{who} ({m.group(2)})"


def _strip_label(v: Any, name: str) -> str:
    """'Maturity verdict: emerging …' under a 'Maturity' label → 'emerging …'."""
    t = _s(v).strip()
    first = name.split()[0]
    return re.sub(rf"^{re.escape(first)}[\w ]*?:\s*", "", t, flags=re.I) if t else t


def _get(d: Any, *path: str, default: Any = "") -> Any:
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d in (None, "", [], {}) else d


class Manuscript:
    def __init__(self, review: dict, ms: dict, figures: list[dict], outdir: Path, stem: str,
                 references_file: Optional[Path], write_files: bool = True):
        self.r, self.ms, self.outdir, self.stem = review, ms, outdir, stem
        self.write_files = write_files
        self.p = review.get("protocol") or {}
        self.pr = review.get("prisma_review") or {}
        self.flow = review.get("flow") or {}
        self.kind = (ms.get("review_type") or "effect").lower()
        st = ms.get("settings") or {}
        self.page_limit = int(st.get("page_limit") or 50)
        self.policy = st.get("appendix_policy") or "sibling"
        self.word_target = int(st.get("word_count_target") or self.p.get("word_count_target") or 8000)
        self.references_file = references_file
        self.lines: list[str] = []
        self.gates: list[dict] = []
        self.section_words: dict[str, int] = {}
        self.siblings: list[tuple[str, int]] = []      # (file, rows) the text points at
        self.table_rows = 0
        self.appendix_words = 0
        self.appendix_rows = 0
        self.n_refs = 0
        self.num = [0, 0]
        # figures: theirs then yours, numbered by slot order
        self.figs = {f["id"]: dict(f) for f in figures}
        for fid, extra in (ms.get("figures") or {}).items():
            self.figs.setdefault(fid, {"id": fid}).update(extra or {})
        self.figs = {k: v for k, v in self.figs.items() if v.get("file")}
        # Numbers follow placement order, found by a first pass (build() runs
        # twice): an unplaced figure takes no number, and RQ10 never precedes RQ2.
        self.fig_no: dict[str, int] = {f: i for i, f in enumerate(self.figs, 1)}
        self.placed: list[str] = []
        self.referenced: set[str] = set()
        self.emitted: list[tuple[int, ...]] = []

    # ── primitives ──

    def gate(self, level: str, code: str, msg: str) -> None:
        self.gates.append({"level": level, "code": code, "message": msg})

    def resolve(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            fid = m.group(1)
            if fid in self.fig_no:
                self.referenced.add(fid)
                return f"Figure {self.fig_no[fid]}"
            self.gate("warning", "figure-ref-unknown", f"{{fig:{fid}}} names no figure")
            return f"Figure ({fid})"
        return FIG_REF.sub(sub, text)

    def h(self, level: int, title: str) -> str:
        """Numbered heading: level 1 = section, 2 = subsection; 0 = unnumbered."""
        if level == 1:
            self.num = [self.num[0] + 1, 0]
            label = f"{self.num[0]}. {title}"
            self.lines += ["", f"## {label}", ""]
            self.emitted.append((self.num[0],))
        elif level == 2:
            self.num[1] += 1
            label = f"{self.num[0]}.{self.num[1]} {title}"
            self.lines += ["", f"### {label}", ""]
            self.emitted.append((self.num[0], self.num[1]))
        else:
            label = title
            self.lines += ["", f"## {title}", ""]
        self._current = label
        return label

    def para(self, text: Any, key: str = "", reason: str = "") -> int:
        text = _s(text).strip()
        if not text:
            text = NOT_ASSESSED.format(reason or "no content was written for this section")
        # authored text sits inside a numbered section: its own headings go below it
        text = re.sub(r"^(#{1,3})(\s)", lambda m: "####" + m.group(2), self.resolve(text), flags=re.M)
        self.lines += [text, ""]
        n = _words(text)
        if key:
            self.section_words[key] = self.section_words.get(key, 0) + n
        return n

    def table(self, header: list[str], rows: list[list[Any]], appendix: bool = False,
              caps: Optional[list[Optional[int]]] = None) -> None:
        self.lines += _table(header, rows, caps)
        if appendix:
            self.appendix_rows += len(rows)
        else:
            self.table_rows += len(rows)

    def _figure_path(self, file: str) -> str:
        """The figure's path relative to the manuscript, whatever base it was recorded against."""
        p = Path(file)
        if not p.is_absolute():
            for base in (self.outdir, Path.cwd()):
                if (base / p).is_file():
                    p = base / p
                    break
            else:
                return p.as_posix()
        return Path(os.path.relpath(p.resolve(), self.outdir.resolve())).as_posix()

    def place(self, slot: str) -> None:
        for f in self.figs.values():
            if f.get("section") == slot and f["id"] not in self.placed:
                self.placed.append(f["id"])
                rel = self._figure_path(f["file"])
                interp = _s(f.get("interpretation")).strip()
                caption = _s(f.get("caption")).strip()
                if interp and FIG_REF.search(interp):
                    self.para(interp)          # body text that discusses the figure
                elif interp:
                    # a takeaway with no reference of its own belongs in the caption, as in a
                    # journal figure legend, not as a "Figure N: …" line above the image
                    caption = f"{caption.rstrip('.')}. {interp}" if caption else interp
                self.lines += [f"![]({rel}){{width=100%}}", "",
                               f"**Figure {self.fig_no[f['id']]}.** {caption}", ""]

    def sibling(self, name: str, rows: int, write: Optional[Callable[[Path], None]] = None) -> str:
        path = self.outdir / name
        if write is not None and self.write_files:
            write(path)
        self.siblings.append((name, rows))
        return f"Full table: `{name}` ({rows} rows)."

    # ── document ──

    def build(self) -> str:
        """Render twice: the first pass fixes figure order, the second numbers by it."""
        self._render()
        order = list(self.placed)
        for attr, val in (("lines", []), ("gates", []), ("section_words", {}), ("siblings", []),
                          ("table_rows", 0), ("appendix_words", 0), ("appendix_rows", 0), ("n_refs", 0),
                          ("num", [0, 0]), ("placed", []), ("referenced", set()), ("emitted", [])):
            setattr(self, attr, val)
        self.fig_no = {fid: i for i, fid in enumerate(order, 1)}
        return self._render()

    def _render(self) -> str:
        self.front_matter()
        self.abstract()
        self.introduction()
        self.methods()
        self.results()
        self.discussion()
        self.conclusions()
        self.declarations()
        self.references()
        self.appendices()
        self.supplementary()
        self.check_python_repr()
        self.check_gates()
        return "\n".join(self.lines).strip() + "\n"

    def front_matter(self) -> None:
        fm = self.ms.get("front_matter") or {}
        title = self.p.get("title") or self.pr.get("title") or "Systematic review"
        reg = self.p.get("registration_number") or "Not registered"
        n_inc = len(self.r.get("included_articles") or []) or int(self.flow.get("included_synthesis") or 0)
        ft = int(self.flow.get("included_with_full_text") or 0)
        ab = int(self.flow.get("included_abstract_only") or 0)
        unit = (f"{len(self.ms.get('resource_catalog') or [])} unique resources described in {n_inc} publications"
                if self.kind == "mapping" else f"{n_inc} included studies")
        self.lines += [f"# {title}", ""]
        rows = [
            f"**Review type:** {fm.get('review_type_label') or ('Scoping review' if self.kind == 'mapping' else 'Systematic review')}",
            f"**Protocol:** {self.p.get('protocol_url') or 'see Methods'} | **Registration:** {reg}",
            f"**Searches executed:** {fm.get('searches_executed') or 'NR'} | **Review id:** {self.p.get('review_id') or 'NR'}"
            f" | **Generated:** {_s(self.r.get('timestamp'))[:10] or 'NR'}",
            f"**Unit of analysis:** {unit}",
            f"**Reading basis:** {ft} of {n_inc} included publications read in full text, {ab} on the abstract only",
        ]
        if fm.get("analysis"):
            rows.append(f"**Analysis:** {fm['analysis']}")
        self.lines += ["  \n".join(rows), ""]
        if not (ft or ab):
            self.gate("warning", "reading-basis", "the flow records no reading basis (full text vs abstract only)")

    def abstract(self) -> None:
        self.h(0, "Abstract")
        a = self.ms.get("abstract") or {}
        pa = self.pr.get("abstract") or {}
        labels = [("Background", a.get("background") or pa.get("background")),
                  ("Objectives", a.get("objectives") or pa.get("objective")),
                  ("Methods", a.get("methods") or pa.get("methods")),
                  ("Results", a.get("results") or pa.get("results")),
                  ("Conclusions", a.get("conclusions") or pa.get("conclusion")),
                  ("Limitations", a.get("limitations")),
                  ("Registration", a.get("registration") or self.p.get("registration_number") or "Not registered"),
                  ("Keywords", "; ".join(a.get("keywords") or []))]
        if not any(v for _, v in labels[:5]) and self.r.get("structured_abstract"):
            self.para(self.r["structured_abstract"], "abstract")
        else:
            for label, value in labels:
                self.lines.append(f"**{label}:** {self.resolve(_s(value).strip()) or NOT_ASSESSED.format('not written')}  ")
                self.section_words["abstract"] = self.section_words.get("abstract", 0) + _words(_s(value))
            self.lines.append("")
            missing = [lab for lab, v in labels if not _s(v).strip()]
            if missing:
                self.gate("warning", "abstract-labels", f"abstract missing: {', '.join(missing)}")
        if self.section_words.get("abstract", 0) < 250:
            self.gate("warning", "abstract-short",
                      f"abstract is {self.section_words.get('abstract', 0)} words (< 250)")
        abstract_text = " ".join(_s(v) for _, v in labels) + _s(self.r.get("structured_abstract"))
        if not re.search(r"full[- ]text|abstract only|abstract-only", abstract_text, re.I):
            self.gate("warning", "reading-basis", "the abstract does not state the reading basis")

    def introduction(self) -> None:
        i = self.ms.get("introduction") or {}
        pi = self.pr.get("introduction") or {}
        self.h(1, "Introduction")
        self.h(2, "Background and context")
        n = self.para(i.get("background") or pi.get("background") or self.r.get("introduction_text"),
                      "introduction", "no background written")
        if n < 400:
            self.gate("warning", "background-short", f"background is {n} words (< 400)")
        self.h(2, "Prior reviews and what this review adds")
        prior = self.ms.get("prior_reviews") or []
        if prior:
            self.table(["Review", "Scope", "Cutoff", "Gap this review addresses"],
                       [[_short_cite(x.get("citation")), x.get("scope"), x.get("year_cutoff"), x.get("gap_addressed")]
                        for x in prior])
        text = i.get("prior_reviews_text") or ""
        if prior or text:
            self.para(text or "", "introduction") if text else None
        else:
            self.para("", reason="no prior reviews were recorded")
            self.gate("warning", "prior-reviews", "no prior review and no 'none identified' statement")
        self.h(2, "Objectives and research questions")
        self.para(self.p.get("objective") or pi.get("objectives"), "introduction", "no objective recorded")
        rqs = self.p.get("research_questions") or []
        if rqs:
            self.table(["ID", "Research question", "Answered in"],
                       [[q.get("question_id"), q.get("question"), "§3, " + _s(q.get("question_id"))] for q in rqs])
        self.h(2, "Scope boundary")
        self.para(i.get("scope") or pi.get("problem_statement"), "introduction", "no scope boundary written")
        self.h(2, "Terminology")
        terms = i.get("terminology") or {}
        if terms:
            self.table(["Term", "Meaning in this review"], [[k, v] for k, v in terms.items()])
        else:
            self.para("", reason="no terms defined")

    def methods(self) -> None:
        m = self.ms.get("methods") or {}
        pm = self.pr.get("methods") or {}
        self.h(1, "Methods")
        self.h(2, "Protocol, registration and deviations")
        self.para(f"Registration: {self.p.get('registration_number') or 'Not registered'}. "
                  f"Deviations from the protocol: {_s(self.p.get('amendments') or 'none').strip().rstrip('.')}.",
                  "methods")
        self.h(2, "Eligibility criteria")
        inc, exc = self.p.get("inclusion_criteria") or "", self.p.get("exclusion_criteria") or ""
        inc_l, exc_l = _criteria(inc), _criteria(exc)
        if inc_l or exc_l:
            # two lists, not a two-column table: inclusion item k has nothing to do with exclusion item k
            for label, items in (("Inclusion criteria", inc_l), ("Exclusion criteria", exc_l)):
                if items:
                    self.lines += [f"**{label}.**", ""] + [f"- {x}" for x in items] + [""]
                    self.section_words["methods"] = self.section_words.get("methods", 0) + sum(map(_words, items))
        else:
            self.para("", reason="no eligibility criteria recorded")
        self.h(2, "Information sources")
        self.para(m.get("sources") or ("Databases: " + ", ".join(self.p.get("databases") or []) + "."
                                       if self.p.get("databases") else ""), "methods",
                  "no information sources recorded")
        self.h(2, "Search strategy")
        qs = self.r.get("search_queries") or []
        if qs:
            self.lines += ["```", *[_s(q) for q in qs], "```", ""]
            self.para(f"Full strings per source are in Appendix A ({len(qs)} queries).", "methods")
        else:
            self.para(pm.get("search_strategy"), "methods", "no search strings recorded")
        self.h(2, "Selection process")
        self.para(m.get("selection"), "methods", "the selection process was not described")
        self.h(2, "Full-text retrieval and reading basis")
        routes = self.flow.get("full_text_sources") or {}
        if routes:
            self.table(["Retrieval route", "Reports"],
                       [[review_figures.ROUTE_LABELS.get(k, k), v] for k, v in sorted(routes.items(), key=lambda kv: -kv[1])]
                       + [["Not retrieved", self.flow.get("not_retrieved", 0)]])
        self.place("methods.retrieval")
        self.para(m.get("retrieval") or "", "methods") if m.get("retrieval") else None
        self.h(2, "Data charting")
        n_items = len(self.p.get("charting_questions") or []) + len(self.p.get("research_questions") or [])
        self.para(m.get("charting") or (f"{n_items} charting items were applied to every included study." if n_items else ""),
                  "methods", "no charting procedure described")
        self.h(2, "Unit of analysis")
        self.para(self.ms.get("dedup_rule"), "methods", "no unit-of-analysis rule recorded")
        self.h(2, "Cross-study coding")
        cm = self.ms.get("coding_methods") or []
        if cm:
            self.table(["Category", "Rule", "Script"], [[c.get("category"), c.get("rule"), c.get("script")] for c in cm])
            self.para("Per-record answers are authoritative; derived categories summarise them.", "methods")
        else:
            self.para("", reason="no derived categories were coded")
        self.h(2, "Critical appraisal")
        doms = self.p.get("appraisal_domains") or []
        self.para(m.get("appraisal") or f"Instrument: {self.p.get('rob_tool') or 'NR'}."
                  + (f" Domains: {', '.join(_s(d.get('name') if isinstance(d, dict) else d) for d in doms)}." if doms else ""),
                  "methods")
        self.h(2, "Synthesis approach")
        self.para(m.get("synthesis"), "methods", "no synthesis approach described")
        self.h(2, "Independent verification")
        v = self.ms.get("verification") or {}
        rep = self._citation_summary()
        self.para(v.get("summary") or (f"Citations were checked against Crossref, DataCite and PubMed: {rep}." if rep else ""),
                  "methods", "no independent verification was performed")

    def _citation_summary(self) -> str:
        p = self.outdir / f"{self.stem}.citations.json"
        try:
            s = json.loads(p.read_text(encoding="utf-8")).get("summary") or {}
        except (OSError, ValueError):
            return ""
        return (f"{s.get('references', 0)} references, {s.get('errors', 0)} errors, "
                f"{s.get('warnings', 0)} warnings, retracted: {', '.join(s.get('retracted') or []) or 'none'}")

    # ── results ──

    def results(self) -> None:
        res = self.ms.get("results") or {}
        self.h(1, "Results")
        self.h(2, "Selection and flow")
        self.place("results.flow")
        f = self.flow
        self.table(["Stage", "n"], [
            ["Records identified", f.get("total_identified", 0)], ["Duplicates removed", f.get("duplicates_removed", 0)],
            ["Records screened", f.get("screened_title_abstract", 0)], ["Records excluded", f.get("excluded_title_abstract", 0)],
            ["Reports sought for retrieval", f.get("sought_fulltext", 0)], ["Reports not retrieved", f.get("not_retrieved", 0)],
            ["Reports assessed for eligibility", f.get("assessed_eligibility", 0)],
            ["Reports excluded", f.get("excluded_eligibility", 0)], ["Studies included", f.get("included_synthesis", 0)],
            ["… read in full text", f.get("included_with_full_text", 0)],
            ["… on the abstract only", f.get("included_abstract_only", 0)]])
        for stage, key in (("title/abstract screening", "excluded_reasons_title_abstract"),
                           ("full-text eligibility", "excluded_reasons_full_text")):
            reasons = f.get(key) or (f.get("excluded_reasons") if key.endswith("full_text") else {}) or {}
            if reasons:
                self.lines.append(f"Exclusion reasons at {stage}:")
                self.lines.append("")
                self.table(["Reason", "n"], sorted(([k, v] for k, v in reasons.items()), key=lambda r: -int(r[1] or 0)))
        self.para(res.get("flow") or "", "results") if res.get("flow") else None

        self.h(2, "Descriptive overview")
        self.para(res.get("overview"), "results", "the corpus was not described")
        self.place("results.overview")

        if self.kind == "mapping":
            self.h(2, "Resource inventory")
            self.inventory(res)
        else:
            self.h(2, "Study characteristics")
            self.characteristics(res)

        self.research_questions(res)

        if self.kind == "mapping":
            self.h(2, "Cross-cutting comparison")
            self.para(res.get("cross_cutting"), "results", "no cross-cutting comparison was made")
            self.place("results.cross_cutting")
        else:
            self.h(2, "Meta-analysis")
            qa = _get(self.pr, "results", "quantitative_analysis")
            self.para(res.get("meta_analysis") or (json.dumps(qa) if isinstance(qa, dict) and qa else ""),
                      "results", "a meta-analysis was not performed (no pooled estimate reported)")
            self.place("results.meta_analysis")

        self.h(2, "Per-group analysis")
        pga = self.r.get("per_group_analysis") or {}
        lead = res.get("groups_lead")
        self.para(lead, "results", "no comparison across groups was written")
        self.place("results.groups")
        if not lead and pga.get("groups"):
            self.gate("warning", "groups-lead", "per-group blocks without a lead comparing the groups")
        for g in pga.get("groups") or []:
            self.lines += [f"**{_s(g.get('label'))}** (n = {g.get('n_studies', 0)}). {_group_summary(g.get('summary'))}", ""]
            for qa in g.get("answers") or []:
                q = _s(qa.get("question") if isinstance(qa, dict) else "")
                a = _s(qa.get("answer") if isinstance(qa, dict) else qa)
                if a:
                    self.lines += [f"- *{q}* {a}" if q else f"- {a}"]
            self.lines.append("")

        self.h(2, "Critical appraisal")
        self.para(res.get("appraisal") or self.r.get("bias_assessment"), "results", "no appraisal narrative written")
        self.place("results.appraisal")
        self.appraisal_table()

        self.h(2, "Certainty of evidence" if self.kind == "effect" else "Confidence in the synthesis")
        self.place("results.certainty")
        grade = self.r.get("grade_assessments") or {}
        if grade and self.kind == "effect":
            self.table(["Outcome", "Certainty", "Summary"],
                       [[g.get("outcome") or k, g.get("overall_certainty"), g.get("summary")] for k, g in grade.items()])
        cs = self.ms.get("confidence_statement")
        if cs or not grade:
            self.para(cs, "results", "certainty was not rated")
        if not grade and not cs:
            self.gate("warning", "certainty", "no GRADE assessment and no confidence statement")

        self.h(2, "Knowledge and capability gaps")
        self.place("results.gaps")
        gaps = self.ms.get("gaps") or []
        if gaps:
            for k, g in enumerate(gaps, 1):
                line = f"{k}. **{_s(g.get('gap')).strip()}** {_s(g.get('evidence')).strip()}"
                extra = "; ".join(x for x in (_s(g.get("affected_groups")), _s(g.get("severity"))) if x)
                self.lines.append(self.resolve(line + (f" ({extra})" if extra else "")))
                self.section_words["results"] = self.section_words.get("results", 0) + _words(line)
            self.lines.append("")
        else:
            self.para("", reason="no gaps were recorded")
        if len(gaps) < 5 or any(not re.search(r"\d", _s(g.get("evidence")) + _s(g.get("gap"))) for g in gaps):
            self.gate("warning", "gaps", f"{len(gaps)} gap(s) recorded; the template expects ≥ 5, each with a number")

    def characteristics(self, res: dict) -> None:
        self.place("results.characteristics")
        arts = self.r.get("included_articles") or []
        concern = self._concern_by_study()
        rows = []
        for a in arts:
            ed = a.get("extracted_data") or {}
            first = (_s(a.get("authors")).split(",")[0].split(";")[0].strip() or "NR")
            rows.append([f"{first} ({_s(a.get('year')) or 'n.d.'})", ed.get("study_design") or "NR",
                         ed.get("sample_size") or "NR", ed.get("population") or "NR",
                         "; ".join((ed.get("outcomes") or [])[:2]) or "NR",
                         concern.get(_s(a.get("pmid")), "NR")])
        header = ["Study", "Design", "Sample", "Population", "Outcomes", "Appraisal concern"]
        if rows:
            shown = rows if len(rows) <= INLINE_ROW_CAP else rows[:INLINE_ROW_CAP]
            self.table(header, shown, caps=[None, 40, 30, 80, 80, None])
            if len(rows) > INLINE_ROW_CAP:
                self.lines += [self.sibling(f"{self.stem}.characteristics.csv", len(rows),
                                            lambda p: _write_csv(p, header, rows)), ""]
        self.para(res.get("characteristics_lead"), "results", "the study characteristics were not summarised")

    def inventory(self, res: dict) -> None:
        self.place("results.characteristics")
        cat = self.ms.get("resource_catalog") or []
        if cat:
            header = list(dict.fromkeys(k for row in cat for k in row if k != "publication_ids"))
            rows = [[row.get(k, "") for k in header] for row in cat]
            self.table(header, rows if len(rows) <= INLINE_ROW_CAP else rows[:INLINE_ROW_CAP],
                       caps=[80] * len(header))
            self.lines += [self.sibling("resource_catalog.csv", len(cat),
                                        lambda p: _write_csv(p, header + ["publication_ids"],
                                                             [r + ["; ".join(map(str, c.get("publication_ids") or []))]
                                                              for r, c in zip(rows, cat)])), ""]
        self.para(res.get("characteristics_lead"), "results", "no resource inventory was recorded")

    def research_questions(self, res: dict) -> None:
        rqs = self.p.get("research_questions") or []
        authored = res.get("rq") or {}
        themes = res.get("themes") or {}
        if rqs:
            for q in rqs:
                qid = _s(q.get("question_id"))
                self.h(2, f"{qid} — {q.get('short_title') or q.get('theme') or q.get('question')}")
                if _s(authored.get(qid)).strip():
                    n = self.para(authored.get(qid), f"rq:{qid}")
                    self.place(f"results.rq:{qid}")
                    if n < 250:
                        self.gate("warning", "rq-short", f"{qid} synthesis is {n} words (< 250)")
                elif self.charted_answers(q):
                    self.place(f"results.rq:{qid}")
                    self.gate("warning", "rq-unsynthesised",
                              f"{qid} shows its charted answers only: write the synthesis (pattern, "
                              f"denominators, exemplars, NR count) in results.rq.{qid}")
                else:
                    self.para("", f"rq:{qid}", f"no study's answer to {qid} was charted")
                    self.place(f"results.rq:{qid}")
                    self.gate("warning", "rq-short", f"{qid} has no synthesis and no charted answers")
        elif themes:
            for name, text in themes.items():
                self.h(2, f"Theme: {name}")
                self.para(text, "results")
        else:
            pthemes = _get(self.pr, "results", "themes", default=[]) or []
            if pthemes:
                for t in pthemes:
                    self.h(2, f"Theme: {t.get('theme_name')}")
                    self.para(t.get("description"), "results")
                    for kf in t.get("key_findings") or []:
                        self.lines.append(f"- {kf}")
                    self.lines.append("")
            else:
                self.h(2, "Synthesis")
                body = _s(self.r.get("synthesis_text"))
                body = re.sub(r"^(#{1,3})(\s)", lambda m: "####" + m.group(2), body, flags=re.M)
                self.para(body, "results", "no synthesis was written")
        if not authored and self.r.get("synthesis_text") and rqs:
            self.h(2, "Narrative synthesis")
            body = re.sub(r"^(#{1,3})(\s)", lambda m: "####" + m.group(2), _s(self.r["synthesis_text"]), flags=re.M)
            self.para(body, "results")

    def _source_to_article(self) -> dict[str, str]:
        """Charting/appraisal ``source_id`` (S-001) → article pmid, via the rubric's DOI or title."""
        by_doi = {_s(a.get("doi")).lower(): _s(a.get("pmid")) for a in self.r.get("included_articles") or [] if a.get("doi")}
        by_title = {re.sub(r"\W+", " ", _s(a.get("title")).lower()).strip(): _s(a.get("pmid"))
                    for a in self.r.get("included_articles") or []}
        out = {}
        for rb in self.r.get("data_charting_rubrics") or []:
            sid = _s(rb.get("source_id"))
            pm = by_doi.get(_s(rb.get("doi")).lower()) or by_title.get(re.sub(r"\W+", " ", _s(rb.get("title")).lower()).strip())
            if sid and pm:
                out[sid] = pm
        return out

    NR = re.compile(r"^\s*(nr|n/?a|not reported|not stated|not specified|none reported|unclear|unknown)\b", re.I)

    def charted_answers(self, q: dict) -> bool:
        """Render one question's charted answers: a factual lead and one row per study.

        Used when no synthesis has been written for the question. The answers are
        the per-study ``custom_fields`` the charting stage filled, keyed by the
        question's id (or its text). Nothing is summarised or inferred: the lead
        only counts, and every answer is shown as charted. Returns False when no
        study answered.
        """
        qid, text = _s(q.get("question_id")), _s(q.get("question")).strip().lower()
        arts = {_s(a.get("pmid")): a for a in self.r.get("included_articles") or []}
        to_article = self._source_to_article()
        rows = []
        for rb in self.r.get("data_charting_rubrics") or []:
            custom = rb.get("custom_fields") or {}
            ans = next((_s(v).strip() for k, v in custom.items()
                        if _s(k) == qid or _s(k).strip().lower() == text), "")
            if not ans:
                continue
            art = arts.get(to_article.get(_s(rb.get("source_id")), ""), {})
            first = (_s(art.get("authors") or rb.get("authors")).split(",")[0].split(";")[0].strip()
                     or _s(rb.get("title"))[:40] or _s(rb.get("source_id")))
            rows.append([first, _s(art.get("year") or rb.get("year")) or "n.d.", ans])
        if not rows:
            return False
        n_total = len(self.r.get("included_articles") or []) or len(rows)
        nr = sum(1 for r in rows if self.NR.match(r[2]))
        self.para(f"{qid} asked: “{_s(q.get('question')).strip()}” Answers were charted for "
                  f"{len(rows)} of {n_total} included studies; {nr} of those did not report it. "
                  "The table gives each study's charted answer.", f"rq:{qid}")
        rows.sort(key=lambda r: (bool(self.NR.match(r[2])), r[1], r[0]))
        header = ["Study", "Year", f"Charted answer ({qid})"]
        self.table(header, rows[:INLINE_ROW_CAP], caps=[None, None, 300])
        safe = re.sub(r"[^\w.-]+", "-", qid) or "question"
        self.lines += [self.sibling(f"{self.stem}.rq-{safe}.csv", len(rows),
                                    lambda path: _write_csv(path, header, rows)), ""]
        return True

    def _concern_by_study(self) -> dict[str, str]:
        """Worst domain concern per article pmid (appraisals are keyed by source_id)."""
        order = {"Low": 0, "Some": 1, "High": 2}
        to_article = self._source_to_article()
        out = {}
        for ap in self._appraisals():
            levels = [d.get("domain_concern") for d in ap.get("domains") or [] if d.get("domain_concern") in order]
            if levels:
                sid = _s(ap.get("source_id"))
                out[to_article.get(sid, sid)] = max(levels, key=lambda x: order[x])
        return out

    def _appraisals(self) -> list[dict]:
        return self.r.get("structured_appraisal_results") or _get(self.pr, "methods", "critical_appraisal_results", default=[]) or []

    def appraisal_table(self) -> None:
        per: dict[str, Counter] = {}
        for ap in self._appraisals():
            for d in ap.get("domains") or []:
                per.setdefault(d.get("domain_name") or "Domain", Counter())[d.get("domain_concern") or "NR"] += 1
        if per:
            self.table(["Domain", "Low", "Some", "High", "NR"],
                       [[dom, c.get("Low", 0), c.get("Some", 0), c.get("High", 0), c.get("NR", 0)] for dom, c in per.items()])

    # ── discussion, conclusions, declarations, references ──

    def discussion(self) -> None:
        d = self.ms.get("discussion") or {}
        pd = self.pr.get("discussion") or {}
        pc = self.pr.get("conclusion") or {}
        impl = pd.get("implications")
        impl_text = ("\n\n".join(f"**{k.replace('_', ' ').capitalize()}.** {v}" for k, v in impl.items() if v)
                     if isinstance(impl, dict) else _s(impl))
        self.h(1, "Discussion")
        parts = [("Principal findings", d.get("principal_findings") or pd.get("summary_of_findings")),
                 ("Interpretation", d.get("interpretation") or pd.get("interpretation")),
                 ("Comparison with prior reviews", d.get("comparison") or pd.get("comparison_with_literature")),
                 ("Implications", d.get("implications") or impl_text),
                 ("Research agenda", d.get("research_agenda") or pc.get("future_research")),
                 ("Strengths", d.get("strengths")),
                 ("Limitations", d.get("limitations") or self.r.get("limitations") or pd.get("limitations")),
                 ("Reflexivity on automated authorship", d.get("reflexivity"))]
        self.place("discussion")
        core = {"Principal findings", "Interpretation", "Comparison with prior reviews",
                "Implications", "Research agenda"}
        written = 0
        for title, text in parts:
            self.h(2, title)
            self.para(text, "discussion", f"no {title.lower()} were written" if title.endswith("s") else f"no {title.lower()} was written")
            if _s(text).strip() and title in core:
                written += 1
        if not written:
            self.gate("fail", "discussion-only-limitations",
                      "the Discussion holds only limitations: principal findings, interpretation, comparison, "
                      "implications and research agenda are required")
        lim = _s(parts[6][1])
        if lim and not re.search(r"full[- ]text|abstract", lim, re.I):
            self.gate("warning", "reading-basis", "the limitations do not state the reading basis")

    def conclusions(self) -> None:
        self.h(1, "Conclusions")
        items = self.ms.get("conclusions") or []
        items = [x for x in items if _s(x).strip()]
        if items:
            self.lines += [self.resolve(f"{k}. {_s(x).strip()}") for k, x in enumerate(items, 1)] + [""]
            self.section_words["conclusions"] = sum(_words(_s(x)) for x in items)
        else:
            text = self.r.get("conclusions_text") or _get(self.pr, "conclusion", "key_takeaways")
            self.para(text, "conclusions")
            if not _s(text).strip():
                self.gate("fail", "conclusions-empty", "Conclusions are empty; the section is mandatory")
        verdict = self.ms.get("certainty_verdict")
        ready = self.ms.get("translation_readiness")
        if verdict or ready:
            label = "Evidence certainty" if self.kind == "effect" else "Maturity"
            for name, value in ((label, verdict), ("Translation readiness", ready)):
                self.lines += [f"**{name}.** {_strip_label(value, name) or 'NR'}", ""]

    def declarations(self) -> None:
        dc = self.ms.get("declarations") or {}
        self.h(1, "Declarations")
        rows = [["Registration", self.p.get("registration_number") or "Not registered"],
                ["Protocol availability", self.p.get("protocol_url") or dc.get("protocol") or "NR"],
                ["Funding", dc.get("funding") or ", ".join(self.p.get("funding_sources") or []) or "NR"],
                ["Competing interests", dc.get("competing_interests") or self.p.get("competing_interests") or "NR"],
                ["Data and code availability", dc.get("data_availability") or "See Supplementary datasets."],
                ["AI assistance", dc.get("ai_assistance") or "NR"],
                ["Contributions", dc.get("contributions") or "NR"],
                ["Acknowledgements", dc.get("acknowledgements") or "None"]]
        for item, statement in rows:
            self.para(f"**{item}.** {_s(statement).strip().rstrip('.')}.", "declarations")

    def references(self) -> None:
        self.h(0, "References")
        if self.references_file and self.references_file.is_file():
            body = self.references_file.read_text(encoding="utf-8").split("\n")
            if body and body[0].startswith("## "):
                body = body[1:]
            self.lines += body + [""]
            numbered = sum(1 for ln in body if re.match(r"^\s*(\d+\.|\[\d+\])\s", ln))
            blocks = len([b for b in re.split(r"\n\s*\n", "\n".join(body)) if b.strip()])
            self.n_refs = numbered or blocks      # wrapped lines are not extra references
            return
        for k, a in enumerate(self.r.get("included_articles") or [], 1):
            doi = f" https://doi.org/{a['doi']}" if a.get("doi") else ""
            pmid = f" PMID: {a['pmid']}." if re.fullmatch(r"\d{1,9}", _s(a.get("pmid"))) else ""
            self.lines.append(f"{k}. {_s(a.get('authors'))} ({_s(a.get('year')) or 'n.d.'}). "
                              f"{_s(a.get('title')).rstrip('.')}. *{_s(a.get('journal'))}*.{doi}{pmid}")
            self.n_refs += 1
        self.lines.append("")

    # ── appendices and supplementary ──

    def appendices(self) -> None:
        start_words = sum(_words(ln) for ln in self.lines)
        start_rows = self.appendix_rows
        sib = self.policy == "sibling"

        def app(letter: str, title: str) -> None:
            self.lines += ["", f"## Appendix {letter}: {title}", ""]

        app("A", "Search strings")
        qs = self.r.get("search_queries") or []
        self.lines += (["```", *[_s(q) for q in qs], "```", ""] if qs
                       else [NOT_ASSESSED.format("no search strings recorded"), ""])
        app("B", "PRISMA checklist")
        cl = self.ms.get("checklist") or []
        if cl:
            self.table(["Item", "Reported in"], [[c.get("item"), c.get("section")] for c in cl], appendix=True)
        else:
            self.lines += [NOT_ASSESSED.format("the checklist was not mapped to sections"), ""]
        app("C", "Records excluded at eligibility")
        excl = [e for e in self.r.get("screening_log") or []
                if _s(e.get("decision")).lower().startswith("exclud") and "elig" in _s(e.get("stage")).lower()]
        reasons = Counter(_s(e.get("reason")) or "NR" for e in excl)
        if excl:
            self.table(["Reason", "n"], [[k, v] for k, v in reasons.most_common()], appendix=True)
            hdr = ["pmid", "title", "reason", "assessed_on"]
            rows = [[e.get("pmid"), e.get("title"), e.get("reason"), e.get("assessed_on")] for e in excl]
            if sib:
                self.lines += [self.sibling(f"{self.stem}.screening.csv", len(rows), lambda p: _write_csv(p, hdr, rows)), ""]
            else:
                self.table(hdr, rows, appendix=True)
        else:
            self.lines += [NOT_ASSESSED.format("no eligibility exclusions were logged"), ""]
        app("D", "Critical appraisal detail")
        self.appraisal_table()
        self.lines += [self.sibling(f"{self.stem}.appraisal.md", len(self._appraisals())), ""]
        app("E", "Charting form")
        items = [[_s(q.get("question_id")), _s(q.get("question"))] for q in self.p.get("research_questions") or []]
        for q in self.p.get("charting_questions") or []:
            m = re.match(r"^\s*([A-Z]{1,3}\d{1,3})[.:)]?\s+(.*)$", _s(q), re.S)
            items.append([m.group(1), m.group(2)] if m else ["", _s(q)])
        if items:
            self.table(["ID", "Item"], items, appendix=True)
        else:
            self.lines += [NOT_ASSESSED.format("no charting items recorded"), ""]
        app("F", "Evidence spans")
        spans = self.r.get("evidence_spans") or []
        hdr = ["paper_pmid", "section", "grounding_score", "text"]
        rows = [[s.get("paper_pmid"), s.get("section"), s.get("grounding_score"), s.get("text")] for s in spans]
        self.lines += [self.sibling(f"{self.stem}.evidence.csv", len(rows), lambda p: _write_csv(p, hdr, rows)), ""]
        app("G", "Per-question charting tables")
        self.lines += [self.sibling(f"{self.stem}.charting.md", len(self.r.get("included_articles") or [])), ""]
        app("H", "URL and repository verification")
        urls = (self.ms.get("verification") or {}).get("urls") or {}
        self.lines += [f"{len(urls)} URLs checked." if urls else NOT_ASSESSED.format("no URLs were checked"), ""]
        app("I", "Citation verification")
        summ = self._citation_summary()
        self.lines += [summ + "." if summ else NOT_ASSESSED.format("citations were not verified"), ""]
        if summ:
            self.lines += [self.sibling(f"{self.stem}.citations.md", 0).replace(" (0 rows)", ""), ""]
        if self.kind == "mapping":
            app("J", "Resource to publication map")
            self.lines += [f"See `resource_catalog.csv` ({len(self.ms.get('resource_catalog') or [])} rows).", ""]
        self.appendix_words = sum(_words(ln) for ln in self.lines) - start_words
        self.appendix_rows -= start_rows

    def supplementary(self) -> None:
        self.h(0, "Supplementary datasets")
        desc = {".json": ("review data (PRISMAReviewResult)", "per review"),
                ".ttl": ("SLR-ontology RDF", "per review"),
                ".charting.md": ("per-question charting tables", "per publication"),
                ".appraisal.md": ("appraisal detail", "per publication"),
                ".citations.md": ("citation verification report", "per reference"),
                ".screening.csv": ("records excluded at eligibility", "per decision"),
                ".evidence.csv": ("evidence spans", "per span"),
                ".characteristics.csv": ("study characteristics", "per publication")}
        rows = []
        for suffix, (what, unit) in desc.items():
            p = self.outdir / f"{self.stem}{suffix}"
            if p.is_file():
                rows.append([f"`{p.name}`", what, unit])
        if (self.outdir / "resource_catalog.csv").is_file():
            rows.append(["`resource_catalog.csv`", "deduplicated resource inventory", "per resource"])
        self.table(["File", "Contents", "Unit"], rows) if rows else self.lines.extend(
            [NOT_ASSESSED.format("no datasets exported"), ""])

    # ── gates ──

    def body_words(self) -> int:
        return sum(v for k, v in self.section_words.items())

    def pages(self) -> float:
        words = sum(_words(ln) for ln in self.lines if not ln.startswith(("|", "```", "![")))
        return (words / WORDS_PER_PAGE + FIG_PAGES * len(self.placed)
                + (self.table_rows + self.appendix_rows) / ROWS_PER_PAGE + REF_PAGES * self.n_refs)

    _PY_REPR = re.compile(r"""\{'[A-Za-z_][\w ]*': |\[\{'[A-Za-z_]""")

    def check_python_repr(self) -> None:
        """A dict or list printed with str() lands in the manuscript as Python repr
        ({'label': ..., 'n_studies': ...}) — readable to no one, and it reads as a bug
        because it is one. Fails, with the line, so it is fixed at the source field."""
        for i, line in enumerate(self.lines, 1):
            if self._PY_REPR.search(line) and not line.lstrip().startswith(("```", "    ")):
                self.gate("fail", "python-repr", f"line {i}: structured data printed as Python repr: {line.strip()[:120]}")

    def check_gates(self) -> None:
        # 1. numbering continuity of the headings this assembler emitted
        prev = (0, 0)
        for n in self.emitted:
            ok = (n == (prev[0] + 1,)) or (len(n) == 2 and n[0] == prev[0] and n[1] == (prev[1] if len(prev) > 1 else 0) + 1)
            if not ok:
                self.gate("fail", "numbering", f"heading numbering jumps at {'.'.join(map(str, n))}")
            prev = n if len(n) == 2 else (n[0], 0)
        # 3. pages
        pages = self.pages()
        self.estimated_pages = pages
        if pages > self.page_limit:
            self.gate("fail", "page-limit", f"estimated {pages:.0f} pages > {self.page_limit}: move per-record "
                      "material to sibling files (never cut the Discussion)")
        app_pages = self.appendix_words / WORDS_PER_PAGE + self.appendix_rows / ROWS_PER_PAGE
        if pages and app_pages / pages > 0.20:
            self.gate("warning", "appendix-share", f"appendices are {100 * app_pages / pages:.0f}% of the pages (> 20%)")
        # 4. words
        bw = self.body_words()
        if bw < 0.7 * self.word_target:
            self.gate("warning", "body-words", f"body prose {bw} words < 70% of the {self.word_target}-word target "
                      "— sections were skipped, not shortened")
        # 5. figures
        for fid in self.placed:
            if fid not in self.referenced and not _s(self.figs[fid].get("interpretation")).strip():
                self.gate("warning", "figure-uninterpreted",
                          f"Figure {self.fig_no[fid]} ({fid}) is placed but never discussed: refer to it as "
                          f"{{fig:{fid}}} in its section, or give figures.{fid}.interpretation")
        for fid in set(self.figs) - set(self.placed):
            self.gate("warning", "figure-unplaced", f"figure {fid} has no section slot ({self.figs[fid].get('section')})")
        # 11. siblings
        for name, _ in self.siblings:
            if not (self.outdir / name).is_file():
                self.gate("warning", "sibling-missing", f"`{name}` is referenced but not in {self.outdir}"
                          + (" (export it with export_review.py --formats charting appraisal)"
                             if name.endswith((".charting.md", ".appraisal.md")) else ""))
        # 14. bare PMIDs in the body
        body = "\n".join(self.lines[:next((i for i, ln in enumerate(self.lines) if ln == "## References"), len(self.lines))])
        bare = len(re.findall(r"\bPMID:?\s*(?:local_[0-9a-f]+|\d{5,9})\b", body))
        if bare:
            self.gate("warning", "bare-pmid", f"{bare} bare-PMID citation(s) in the body: cite by author–year")


def template_gates(review_path: Path, page_limit: Optional[int] = None) -> dict:
    """The template gates for a review, without writing anything.

    For ``export_review.py --check``: assembles in memory from the review, its
    ``<stem>.manuscript.json`` and existing figures, and returns the stats and
    gates. ``authored`` says whether a manuscript file exists yet.
    """
    review = json.loads(review_path.read_text(encoding="utf-8"))
    stem, outdir = review_path.stem, review_path.parent
    ms_path = outdir / f"{stem}.manuscript.json"
    ms = json.loads(ms_path.read_text(encoding="utf-8")) if ms_path.is_file() else {}
    if page_limit:
        ms.setdefault("settings", {})["page_limit"] = page_limit
    man = outdir / "figures" / "figures.json"
    figures = json.loads(man.read_text(encoding="utf-8")).get("figures") or [] if man.is_file() else []
    style = _style_for(review.get("protocol") or {})
    m = Manuscript(review, ms, figures, outdir, stem,
                   outdir / f"{stem}.references.{style}.md" if style else None, write_files=False)
    m.build()
    return {"authored": ms_path.is_file(), "body_words": m.body_words(), "word_target": m.word_target,
            "estimated_pages": m.estimated_pages, "page_limit": m.page_limit, "gates": m.gates}


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows([[_s(c) for c in r] for r in rows])


def _style_for(protocol: dict) -> Optional[str]:
    s = _s(protocol.get("citation_style")).lower()
    return next((k for k in ("apa", "vancouver", "ieee", "nature") if k in s), None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review", help="review JSON (PRISMAReviewResult)")
    ap.add_argument("--manuscript", help="authored sections (default: <stem>.manuscript.json beside the review)")
    ap.add_argument("--figures", help="figures manifest (default: <dir>/figures/figures.json; drawn if absent)")
    ap.add_argument("--no-figures", action="store_true", help="assemble without figures")
    ap.add_argument("--references", metavar="STYLE",
                    help="use <stem>.references.<STYLE>.md (default: protocol.citation_style, if that file exists)")
    ap.add_argument("--page-limit", type=int, help="override settings.page_limit")
    ap.add_argument("--out", help="output Markdown (default: <stem>.manuscript.md)")
    ap.add_argument("--check", action="store_true", help="evaluate the template gates; exit 1 on a failure")
    args = ap.parse_args()

    src = Path(args.review)
    try:
        review = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"{src}: {e}", file=sys.stderr)
        return 2
    stem, outdir = src.stem, src.parent
    ms_path = Path(args.manuscript) if args.manuscript else outdir / f"{stem}.manuscript.json"
    ms = {}
    if ms_path.is_file():
        try:
            ms = json.loads(ms_path.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"{ms_path}: not valid JSON ({e})", file=sys.stderr)
            return 2
    else:
        print(f"note: {ms_path.name} not found — assembling from review.json alone; the gates show what to write",
              file=sys.stderr)
    if args.page_limit:
        ms.setdefault("settings", {})["page_limit"] = args.page_limit

    figures: list[dict] = []
    if not args.no_figures:
        man = Path(args.figures) if args.figures else outdir / "figures" / "figures.json"
        if man.is_file():
            figures = json.loads(man.read_text(encoding="utf-8")).get("figures") or []
        else:
            figures = review_figures.build(review, outdir / "figures")
            (outdir / "figures").mkdir(exist_ok=True)
            man.write_text(json.dumps({"review": str(src), "figures": figures}, indent=2) + "\n", encoding="utf-8")
            print(f"drew {len(figures)} figure(s) → {man.parent}", file=sys.stderr)

    # a rebuild keeps the references list the last assembly chose (``--references cited``)
    check_file = outdir / f"{stem}.manuscript.check.json"
    try:
        previous = json.loads(check_file.read_text(encoding="utf-8")).get("references") if check_file.is_file() else None
    except (OSError, ValueError):
        previous = None
    style = args.references or previous or _style_for(review.get("protocol") or {})
    ref_file = outdir / f"{stem}.references.{style}.md" if style else None

    m = Manuscript(review, ms, figures, outdir, stem, ref_file)
    text = m.build()
    out = Path(args.out) if args.out else outdir / f"{stem}.manuscript.md"
    out.write_text(text + f"\n<!-- manuscript: {m.body_words()} words of body prose (target {m.word_target}); "
                   f"estimated {m.estimated_pages:.1f} pages (limit {m.page_limit}); {len(m.placed)} figures -->\n",
                   encoding="utf-8")
    report = {"manuscript": str(out), "body_words": m.body_words(), "word_target": m.word_target,
              "estimated_pages": round(m.estimated_pages, 1), "page_limit": m.page_limit,
              "figures": len(m.placed), "gates": m.gates,
              "references": style if ref_file and ref_file.is_file() else None}
    check_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    fails = [g for g in m.gates if g["level"] == "fail"]
    warns = [g for g in m.gates if g["level"] == "warning"]
    print(f"manuscript: {out}  ({m.body_words()} words of body prose, target {m.word_target}; "
          f"~{m.estimated_pages:.0f} of {m.page_limit} pages; {len(m.placed)} figures)", file=sys.stderr)
    if args.check:
        for g in fails + warns:
            print(f"  [{'FAIL' if g['level'] == 'fail' else 'warn'}] {g['code']}: {g['message']}", file=sys.stderr)
        print(f"{len(fails)} failure(s), {len(warns)} warning(s)", file=sys.stderr)
        return 1 if fails else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
