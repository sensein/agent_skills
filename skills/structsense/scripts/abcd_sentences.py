"""Sentence-level view of an ABCD paper, and the checks that need one.

A human annotator reads a paper sentence by sentence: every sentence gets an id,
its section, and is_claim / claim_type / causal_language / direction / hedged /
notes, with cross-references ("restates 105", "paper says DMN here and DAN
there"). The pipeline's findings are result-level (one row per table row or
result clause), which is finer than that, but they used to have no sentence ids,
no abstract or discussion claims, and nothing tying a restatement to the result it
restates. This module adds that layer:

* ``segment``: numbers every prose sentence from the abstract to the references,
  with its section. Table blocks are not sentences and are skipped; their rows are
  findings.
* ``one_sentence_per_finding``: a prose finding's quote must be one sentence.
  An extractor that joins two ("food insecurity was linked with poorer outcomes.
  Being Black/African American was associated with lower scores ...") produces a
  finding whose variables come from both. The quote is cut to the sentence that
  carries the finding, and variables that only the other sentence names are
  dropped, unless the kept sentence continues the other one ("Additionally, SCT
  was ..." keeps the "In those with PTE exclusively" subgroup).
* ``check_cited_tables``: a prose result that cites "Table N" is compared with the
  rows of Table N that share its predictor. A network the prose names but the
  table does not have ("within the default mode network" when Table 5's only
  within-network row is Dorsal attention-Dorsal attention), a sign the table
  contradicts, and a quantifier the table does not bear out ("all 9 scales" when
  two rows are not significant) are recorded on the finding.
* ``mark_mirrored_rows``: a table that lists a network pair in both orders
  (Default mode-Dorsal attention and Dorsal attention-Default mode) holds one
  relationship twice; the second row is marked so it is not counted twice.
* ``verify_sentence_claims``: the extractor's sentence-level annotation of claims
  outside the findings (abstract, discussion restatements, conclusions), anchored
  to sentence ids, with restates / conflicts_with resolved to ids.

Every quote stays verbatim; nothing here rewrites the paper's words.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# segmentation
# --------------------------------------------------------------------------- #

_BLOCK = re.compile(r"\n[ \t]*\n")
_ABBREV = re.compile(r"(?:\bet al|\be\.g|\bi\.e|\bvs|\bcf|\bFigs?|\bEq|\bNo|\bca|"
                     r"\bapprox|\bresp|\bSuppl|\bVol|\bpp?|\b[A-Z])$")
_SENT_END = re.compile(r"[.?!][\"'”)]?\s+(?=[A-Z0-9\"'“(\[])")
_HEADING = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})*)\.?[ \t]*\n?\s*"
                      r"([A-Z][^\n]{1,120}(?:\n[^\n]{1,120}){0,2}?)\s*$")
_NAMED_HEADS = {
    "abstract": "Abstract", "introduction": "Introduction", "background": "Background",
    "methods": "Methods", "method": "Methods", "materials and methods": "Methods",
    "results": "Results", "discussion": "Discussion", "conclusion": "Conclusion",
    "conclusions": "Conclusion", "limitations": "Limitations",
}
# Back matter: no claims about the study's results after these.
_STOP_HEADS = re.compile(r"^\s*(?:references|bibliography|acknowledg(?:e)?ments?|"
                         r"authors?['\u2019]? contributions?|author disclosure|credit authorship|conflicts? of interest|"
                         r"(?:declaration of )?competing interests?|declarations?\b|funding|"
                         r"data (?:and code )?availability|code availability|"
                         r"ethics (?:statement|approval)|peer review|article info|"
                         r"supporting information|supplementary (?:material|data|information)|"
                         r"appendix|compliance with ethical standards|ethical approval|"
                         r"informed consent|irb\b|contributors? statement|"
                         r"support or grant information|grant information|"
                         r"disclosures?|financial disclosures?)\b", re.I)
_REF_ENTRY = re.compile(r"^[A-Z][A-Za-z'\-]+(?:\s[A-Z][A-Za-z'\-]+)?,\s+(?:[A-Z]\.\s*)+.{0,200}?"
                        r"\b(?:19|20)\d{2}[a-z]?\.")
# Pieces of reference entries: a DOI link, "no. 8: 265-271", "Journal 24, no.",
# initials lists ("M. C., R. Guinsburg").
_REF_BITS = re.compile(r"doi\.org|\bno\.\s*\d|\d+:\s*\d+\s*[\u2013-]\s*\d+|"
                       r"\b[A-Z]\.\s?[A-Z]\.,|\bet al\.,? (?:19|20)\d{2}\.|pubmed", re.I)
# Statements that belong in back matter, not body prose.
_BACK_STATEMENT = re.compile(
    r"^(?:this (?:work|research|study|project|publication) (?:was|is) (?:supported|funded|made)|"
    r"(?:the )?authors? (?:declare|report|have|did|thank|would|are)|we (?:thank|acknowledge|are "
    r"grateful|gratefully)|statement:|supported by|data used in|data (?:are|is) available|"
    r"no (?:funding|potential|conflict)|all authors|funding)", re.I)
_BACK_WORDS = re.compile(r"\b(?:grant|funded by|foundation|writing \u2013 review|"
                         r"conceptuali[sz]ation|conflicts? of interest|competing interest|"
                         r"institutional review board|informed consent|preregister)\w*", re.I)
_TABLE_START = re.compile(r"^\s*(?:TABLE|Table)\s*([A-Z]?\d+)\b")
_FIGURE_START = re.compile(r"^\s*(?:FIGURE|Figure|Fig\.)\s*([A-Z]?\d+)\b")
# Running headers, footers, licence lines and table notes: never a claim.
_RUNNING = re.compile(r"^\s*(?:\d+\s+of\s+\d+|https?://\S+|doi:|©|This is an open "
                      r"access article|Brain and Behavior\b|Received\b|Revised\b|"
                      r"Accepted:|Correspondence:|Keywords:|Funding:|Abbreviations\s*:|"
                      r"Note[s]?\s*[:.]|\*|\d{6,},\s*\d{4}|.{0,40}Downloaded from|"
                      r"See the Terms and Conditions|Copyright\b)", re.I)


def _is_prose(block: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", block)
    if len(words) < 6:
        return False
    toks = block.split()
    numeric = sum(1 for t in toks if re.fullmatch(r"[-−<>=*]*\d[\d.,E\-*%]*\**", t))
    return numeric / max(1, len(toks)) < 0.3


def _split_sentences(text: str, base: int) -> List[Tuple[int, int]]:
    spans, left = [], 0
    for m in _SENT_END.finditer(text):
        if _ABBREV.search(text[left:m.start()]):
            continue
        end = m.end()
        spans.append((left, end))
        left = end
    if left < len(text):
        spans.append((left, len(text)))
    out = []
    for a, b in spans:
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if seg.strip():
            out.append((base + a + lead, base + b - trail))
    return out


# --------------------------------------------------------------------------- #
# headings: read from the paper, not from a fixed list
# --------------------------------------------------------------------------- #

# Words that usually mark a section's role. A hint only: a heading the list does
# not know ("Findings", "Study 2", "Analytic Strategy", "Sample") still gets a
# role from its parent heading and from where it sits in the paper.
_OWN_HINT = re.compile(
    r"\b(?:abstract|methods?|methodology|materials?|measures?|measurements?|"
    r"participants?|subjects?|samples?|procedures?|design|data|datasets?|analys[ie]s|"
    r"analytic|statistic\w*|results?|findings?|outcomes?|experiments?|study\s*\d+|"
    r"models?|modeling|modelling|covariates?|imaging|acquisition|pre-?processing|"
    r"assessments?|instruments?|variables?|cohort|exposures?|sensitivity|"
    r"robustness|supplementary analys\w*|descriptive)\b", re.I)
_LIT_HINT = re.compile(
    r"\b(?:introduction|background|literature|related work|theor\w*|overview|"
    r"rationale|motivation|the (?:present|current) stud(?:y|ies)|aims?|hypothes[ie]s|"
    r"discussion|conclusions?|concluding|limitations?|implications?|future|"
    r"strengths?|summary|perspectives?|clinical relevance)\b", re.I)
_DISCUSSION_HINT = re.compile(r"\b(?:discussion|conclusions?|concluding|limitations?|"
                              r"implications?|future|strengths?|summary)\b", re.I)
# Unnumbered headings that open a top-level section in any paper. Everything else
# unnumbered ("The Current Study", "Participants") is a sub-section of the
# section it appears in.
_TOP_NAME = re.compile(r"^(?:general |overall )?(?:introduction|background|discussion|"
                       r"conclusions?|limitations?|strengths and limitations|"
                       r"limitations and (?:strengths|future directions)|"
                       r"implications|summary|concluding remarks)$", re.I)
# Words that make a sub-heading a Methods/Results one even under another parent
# ("Statistical analyses" that the layout put inside the Introduction).
_STRONG_OWN = re.compile(r"\b(?:methods?|participants?|measures?|procedures?|"
                         r"statistical|analys[ie]s|analytic|samples?|data analysis|"
                         r"covariates?|imaging data|acquisition)\b", re.I)
_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of",
                "on", "or", "the", "to", "vs", "with", "without", "between", "among"}


_AFFILIATION = re.compile(r"\b(?:universit\w*|department|institute|centre|center|"
                          r"school of|college|hospital|faculty|laborator\w*|"
                          r"e-?mail|correspond\w*|orcid)\b", re.I)


def _looks_like_heading(block: str) -> bool:
    """A short block or line that names a section ("Participants", "Statistical
    Analysis", "STUDY 2", "Analytic strategy"), whatever words the paper chose."""
    flat = " ".join(block.split())
    words = flat.split()
    if not words or len(words) > 12 or len(flat) > 110 or len(re.findall(r"[A-Za-z]", flat)) < 3:
        return False
    if not flat[0].isupper() or flat[-1] in ".?!;," or "," in flat:
        return False
    if _RUNNING.match(flat) or _TABLE_START.match(flat) or _FIGURE_START.match(flat):
        return False
    if len(block.strip().split("\n")) > 3:
        return False                      # a list (keywords, table cells), not a title
    if re.search(r"\b(?:was|were|is|are|has|have|had|been|we|our)\b", flat, re.I):
        return False                      # a line of running text
    if re.search(r"\d{3,}|[=<>%\u00b1@;/\u2191\u2193*\u2020\u2021]|\bet al\b|"
                 r"\((?:19|20)\d{2}\)", flat) \
            or _AFFILIATION.search(flat):
        return False
    if sum(1 for w in words if re.fullmatch(r"[\d.()\-\u2212]+", w)) >= 2:
        return False                      # "Variable 11 12 13", "Mode 1 2"
    content = [w for w in re.findall(r"[A-Za-z][A-Za-z\-']*", flat)
               if w.lower() not in _SMALL_WORDS]
    if not content:
        return False
    capital = sum(1 for w in content if w[0].isupper()) / len(content)
    return flat.isupper() or capital >= 0.6 or len(content) <= 4


def _heading_key(text: str) -> str:
    """Running headers repeat with a page number ("Electronic Media Use And
    Family Conflict 3", "... 5"): compare without it."""
    return re.sub(r"\s*\d+\s*$", "", " ".join(text.split()).lower())


def _blocks(raw: str) -> List[Tuple[int, int]]:
    """Blank-line blocks, with heading lines split out of them.

    Some text layers have no blank lines at all ("Abstract" is just a line before
    the abstract's text), so a line that is a heading on its own — a named
    section, a numbered heading, or a short title-like line after a finished
    sentence and before a capitalised one — becomes its own block.
    """
    out: List[Tuple[int, int]] = []
    pos = 0
    spans = []
    for m in _BLOCK.finditer(raw):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(raw)))
    for a, b in spans:
        lines = []
        off = a
        for ln in raw[a:b].split("\n"):
            lines.append((off, off + len(ln), ln))
            off += len(ln) + 1
        if len(lines) < 2:
            out.append((a, b))
            continue
        cuts = []
        for i, (la, lb, ln) in enumerate(lines):
            t = ln.strip()
            if not t:
                continue
            prev = lines[i - 1][2].strip() if i else ""
            nxt = lines[i + 1][2].strip() if i + 1 < len(lines) else ""
            low = t.lower().rstrip(" .:")
            numbered = _HEADING.match(t)
            is_head = (
                low in _NAMED_HEADS or low == "abstract" or bool(_STOP_HEADS.match(t) and len(t) < 50)
                or (numbered and _looks_like_heading(numbered.group(2))
                    and not _SENT_END.search(numbered.group(2) + " X"))
                or (_looks_like_heading(t) and len(t.split()) <= 8
                    and (not prev or prev[-1:] in ".?!:") and nxt[:1].isupper()))
            if is_head and i > 0:
                cuts.append(la)
            if is_head and i + 1 < len(lines):
                cuts.append(lines[i + 1][0])
        edges = [a] + sorted(set(c for c in cuts if a < c < b)) + [b]
        out.extend((x, y) for x, y in zip(edges, edges[1:]) if raw[x:y].strip())
    return out


def _role(title: str) -> Optional[str]:
    if _DISCUSSION_HINT.search(title):
        return "discussion"
    if _LIT_HINT.search(title):
        return "introduction"
    if _OWN_HINT.search(title):
        return "own_study"
    return None


def classify_headings(headings: List[dict]) -> None:
    """Give every heading a role: own_study, introduction, discussion, abstract or
    back_matter.

    1. A heading whose words say it ("Method", "Findings", "Study 2", "General
       Discussion", "The Present Study") takes that role.
    2. A sub-heading takes its parent's role ("3.2 Prenatal Exposure ..." under
       "Results" is own_study, whatever its words).
    3. Anything else takes the role of the nearest classified heading before it:
       an unknown section after "Method" is still the paper's own work, one after
       "Discussion" is discussion, and one before any own-study heading (an APA
       introduction has no heading of its own) is introduction.
    """
    for h in headings:
        if h.get("role") == "back_matter":
            continue
        if h["level"] == 1:
            r = "abstract" if h["title"].lower() == "abstract" else _role(h["title"])
            if r:
                h["role"], h["role_from"] = r, "heading words"
    parent = None
    last_role = None
    for h in headings:
        if h["level"] == 1:
            parent = h
        if h.get("role"):
            if h["level"] == 1:
                last_role = h["role"]
            continue
        words = _role(h["title"])
        if h["level"] > 1 and parent is not None and parent.get("role"):
            if parent["role"] == "abstract":
                # An abstract has no sub-sections of its own; a sub-heading after
                # it is the introduction of a paper with no Introduction heading.
                h["role"] = "own_study" if _STRONG_OWN.search(h["title"]) else "introduction"
                h["role_from"] = "sub-heading after the abstract"
            elif _STRONG_OWN.search(h["title"]) and parent["role"] in ("introduction",
                                                                       "discussion"):
                # "Statistical analyses" under Introduction is a Methods heading the
                # layout moved; the heading's own words win.
                h["role"], h["role_from"] = "own_study", "heading words (over parent)"
            else:
                h["role"], h["role_from"] = parent["role"], f"parent heading '{parent['title']}'"
            continue
        r = _role(h["title"])
        if r:
            h["role"], h["role_from"] = r, "heading words"
        else:
            prev = "introduction" if last_role in (None, "abstract") else last_role
            h["role"] = "discussion" if prev == "back_matter" else prev
            h["role_from"] = "position in paper"
        if h["level"] == 1:
            last_role = h["role"]


class Outline:
    """The paper's headings and table/figure blocks, by offset into the text."""

    def __init__(self, headings: List[dict], regions: List[dict]):
        self.headings = sorted(headings, key=lambda h: h["start"])
        self.regions = regions
        classify_headings(self.headings)

    def at(self, offset: int) -> dict:
        """Where ``offset`` sits: section path, heading, role, and the table or
        figure block when it is in one."""
        region = next((r for r in self.regions if r["start"] <= offset < r["end"]), None)
        top = sub = None
        for h in self.headings:
            if h["start"] > offset:
                break
            if h["level"] == 1:
                top, sub = h, None
            else:
                sub = h
        head = sub or top
        path = " - ".join(x["title"] if x is top else x["label"] for x in (top, sub) if x)
        role = (head or {}).get("role")
        if region:
            role = "own_study"
        return {
            "section_in_paper": (f"{path} \u203a {region['label']}" if region and path
                                 else region["label"] if region else path or None),
            "section_role": role,
            "section_role_from": ("inside " + region["label"]) if region
            else (head or {}).get("role_from"),
        }


def outline(raw: str) -> Outline:
    return walk(raw)["outline"]


def segment(raw: str) -> List[dict]:
    return walk(raw)["sentences"]


def walk(raw: str) -> dict:
    """Every prose sentence from the abstract (or the start) to the back matter,
    and the paper's outline (its headings and table/figure blocks).

    Offsets are into ``raw`` (the NFKC text the verifier anchors against).
    Headings are read from the paper: numbered ones ("3.2 Prenatal Exposure ...",
    "2.5.1. Behavioral similarity analysis"), named ones ("ABSTRACT", "Method"),
    and short title-like blocks or lines in the paper's own words ("Participants",
    "Analytic Plan", "Study 2"). Text before the abstract (masthead, affiliations)
    is not part of the outline; lines that repeat through the paper are running
    headers, not headings. A sub-section is reported under its parent ("Results -
    3.2 Prenatal Exposure ..."). Table and figure blocks are skipped until prose
    resumes.
    """
    blocks = _blocks(raw)
    seen: Dict[str, int] = {}
    for a, b in blocks:
        t = raw[a:b]
        if len(t) < 120:
            k = _heading_key(t)
            seen[k] = seen.get(k, 0) + 1

    # Journal mastheads put "A B S T R A C T" letter-spaced; read it as a word.
    has_abstract = bool(re.search(r"(?mi)^\s*(?:abstract|a b s t r a c t)\b", raw))
    started = not has_abstract
    top, sub = "Front matter", None
    in_table: Optional[str] = None
    in_back = ""
    pending: List[Tuple[int, int, str]] = []      # prose blocks waiting to merge
    sentences: List[dict] = []
    headings: List[dict] = []
    regions: List[dict] = []
    table_at: Optional[int] = None

    def head(start: int, title: str, level: int, number: Optional[str] = None):
        headings.append({"start": start, "title": title, "level": level,
                         "number": number,
                         "label": f"{number} {title}" if number else title})

    def close_table(at: int):
        nonlocal table_at
        if in_table and table_at is not None:
            regions.append({"start": table_at, "end": at, "label": in_table})
        table_at = None

    def flush():
        """Split the pending prose blocks into sentences. Blocks are joined
        without whatever lay between them (a figure, a table, a running header),
        so a sentence interrupted by one reads whole; ``pieces`` keeps its
        offsets in the text."""
        if not pending:
            return
        sec = pending[0][2]
        joined, where = [], []
        for x, y, _ in pending:
            if joined:
                joined.append(" ")
                where.append(where[-1])
            for k in range(x, y):
                joined.append(raw[k])
                where.append(k)
        text = "".join(joined)
        for s0, e0 in _split_sentences(text, 0):
            # Soft hyphens ("signif\u00ad icant") break words for every reader and rule.
            txt = " ".join(re.sub(r"\u00ad\s*", "", text[s0:e0]).split())
            if len(re.findall(r"[A-Za-z]{2,}", txt)) < 3:
                continue
            pieces, cur = [], None
            for k in range(s0, e0):
                o = where[k]
                if cur and o == cur[1]:
                    cur[1] = o + 1
                elif cur is None or o != cur[1] - 1:
                    cur = [o, o + 1]
                    pieces.append(cur)
            pieces = [tuple(p) for p in pieces if raw[p[0]:p[1]].strip()]
            row = {"id": len(sentences) + 1, "section": sec,
                   "start": pieces[0][0], "end": pieces[-1][1], "text": txt}
            if len(pieces) > 1 and any(b2[0] - a2[1] > 3 for a2, b2 in zip(pieces, pieces[1:])):
                row["pieces"] = pieces
                row["interrupted_by_layout"] = True
            sentences.append(row)
        pending.clear()

    top_numbers: List[int] = []           # numbers of the numbered top headings so far
    unnumbered_tops = 0                   # named top headings without a number
    sents_in_top = 0

    def next_is_prose(i: int) -> bool:
        """Is the next non-empty block running text? A real heading is followed
        by prose; a figure label is followed by more labels."""
        for x, y in blocks[i + 1:i + 4]:
            t = raw[x:y]
            if not t.strip():
                continue
            sh = _HEADING.match(t)
            if sh and "." in sh.group(1):
                return True               # "3 Simulations" followed by "3.1 Set-Up"
            return _is_prose(t) and bool(_SENT_END.search(" ".join(t.split()) + " X"))
        return False

    for i, (a, b) in enumerate(blocks):
        block = raw[a:b]
        flat = " ".join(block.split())
        if not flat:
            continue
        # The abstract ends at its Keywords line. With no Introduction heading
        # (APA style), what follows is the introduction, not more abstract.
        if re.match(r"(?i)key\s?words\b", flat):
            if top == "Abstract" and sents_in_top:
                flush()
                top, sub = "Introduction (no heading in the paper)", None
                head(a, top, 1)
                headings[-1]["role"], headings[-1]["role_from"] = ("introduction",
                                                                    "follows the abstract's keywords")
                sents_in_top = 0
            continue
        if re.fullmatch(r"(?:[A-Z] ){3,}[A-Z](?:\s{2,}(?:[A-Z] ?)+)*", block.strip()):
            # "A R T I C L E  I N F O" -> "ARTICLE INFO"
            flat = " ".join("".join(w.split()) for w in re.split(r"\s{2,}", block.strip()))
            block = flat
        low = flat.lower().rstrip(" .:")
        if _STOP_HEADS.match(flat) and len(flat) < 60 and started:
            # Two-column layouts can put "References" before the Conclusion, so
            # back matter is skipped until the next real heading, not the end.
            flush()
            close_table(a)
            in_back = flat.lower()
            head(a, flat.rstrip(" .:"), 1)
            headings[-1]["role"] = "back_matter"
            headings[-1]["role_from"] = "back-matter heading"
            continue
        first = block.strip().split("\n", 1)
        if first[0].strip().lower().rstrip(" .:") == "abstract" and len(first) > 1:
            flush()
            close_table(a)
            head(a, "Abstract", 1)
            started, top, sub, in_table, in_back = True, "Abstract", None, None, ""
            sents_in_top = 0
            a = a + block.find(first[1]) if first[1] in block else a
            block = raw[a:b]
            flat = " ".join(block.split())
            low = flat.lower()
        if (low == "abstract" or low in _NAMED_HEADS or _HEADING.match(block)) and headings \
                and headings[-1]["title"].startswith("Introduction (no heading") \
                and not sents_in_top:
            headings.pop()                # the paper does have an Introduction heading
        if low == "abstract" or low in _NAMED_HEADS:
            flush()
            close_table(a)
            in_back = ""
            started = True
            top, sub, in_table = _NAMED_HEADS.get(low, flat.title()), None, None
            head(a, top, 1)
            unnumbered_tops += low != "abstract"
            sents_in_top = 0
            continue
        h = _HEADING.match(block)
        # Only the title can end a sentence: "1. Introduction" has a period after
        # the number, which is the numbering style, not a full stop. Affiliations
        # ("1 Department of ...") and numbered references ("3 Genon S, Eickhoff
        # SB (2022): ...") are not headings, and nothing before the abstract is.
        title = " ".join(h.group(2).split()) if h else ""
        num = h.group(1) if h else ""
        named = bool(_NAMED_HEADS.get(title.lower().rstrip(" .")))
        # A numbered TOP heading must continue the paper's numbering (1, 2, 3 ...)
        # and the paper must number its sections at all: "1 Financial strain (1)",
        # "3 Total EMU T1" are the rows of a correlation matrix in a paper whose
        # Method and Results headings carry no numbers.
        style_ok = True
        if h and "." not in num:
            n = int(num)
            if unnumbered_tops and not named:
                style_ok = False
            elif top_numbers and n != top_numbers[-1] + 1 and not named:
                style_ok = False
            elif not top_numbers and n > 2 and not named:
                style_ok = False
        # Inside a table or figure, or for a heading the paper's words do not name,
        # a real heading is followed by prose; a numbered matrix row is not.
        if h and not named and (in_table or "." not in num) and not next_is_prose(i):
            style_ok = False
        if h and style_ok and len(flat) < 130 \
                and not _SENT_END.search(title + " X") and _looks_like_heading(title) \
                and (started or named):
            flush()
            close_table(a)
            name = _NAMED_HEADS.get(title.lower().rstrip(" ."))
            if "." not in num:
                top_numbers.append(int(num))
                sents_in_top = 0
            if "." not in num and name:
                top, sub = name, None
            elif "." not in num:
                top, sub = title, None
            else:
                sub = f"{num} {title}"
            head(a, top if "." not in num else title, 1 if "." not in num else 2, num)
            in_table = None
            in_back = ""
            started = True
            continue
        if not started:
            continue
        if in_back:
            # Body text the layout scattered into the back matter: a block that
            # continues a sentence (starts in lower case), or prose among the
            # reference entries (the Conclusion after "References"). Funding,
            # data-availability and similar statements are skipped.
            if _REF_ENTRY.match(flat) or _REF_BITS.search(flat) or not _is_prose(block):
                continue
            # A continuation (lower case) is body text; so is a paragraph of
            # running prose (several sentences) that is not a funding,
            # contribution or data statement. Two-column layouts put the
            # Discussion's last paragraphs after "Funding" or "Code availability".
            long_prose = len(flat) > 600 and len(_SENT_END.findall(flat)) >= 3
            body = flat[:1].islower() or (
                (long_prose or len(flat) > 250)
                # ends a sentence, possibly with a superscript reference ("Glu.26")
                and (long_prose or re.search(r"[.?!][\"'\u201d)]?\d{0,3}(?:[,\u2013-]\d{1,3})*$",
                                             flat.rstrip()))
                and not re.search(r"https?://|www\.", flat)
                and not _BACK_STATEMENT.match(flat) and len(_BACK_WORDS.findall(flat)) < 2)
            if not body:
                continue
        t = _TABLE_START.match(flat) or _FIGURE_START.match(flat)
        if t:
            flush()
            close_table(a)
            in_table = ("Table " if _TABLE_START.match(flat) else "Figure ") + t.group(1)
            table_at = a
            continue
        if _RUNNING.match(flat):
            continue
        if len(flat) < 120 and seen.get(_heading_key(block), 0) >= 2 \
                and not _NAMED_HEADS.get(low):
            continue                      # running header or footer
        if in_table:
            # Prose resumes after a table when a block reads as sentences, or
            # continues the sentence the table interrupted (starts in lower case).
            cont = flat[:1].islower() and _is_prose(block) and flat.rstrip()[-1:] in ".?!"
            if not (cont or (_is_prose(block) and len(_SENT_END.findall(flat + " X")) >= 1
                             and len(flat) > 120)):
                continue
            close_table(a)
            in_table = None
        # An unnumbered heading in the paper's own words ("Participants",
        # "Analytic Strategy", "STUDY 2"). ALL-CAPS headings and short section
        # names open a new top section; others are sub-sections of it.
        if _looks_like_heading(block) and next_is_prose(i):
            flush()
            title = flat.rstrip(" .:")
            if title.isupper() or (_TOP_NAME.match(title) and not sub):
                top, sub = title.title() if title.isupper() else title, None
                head(a, top, 1)
                sents_in_top = 0
            else:
                sub = title
                head(a, title, 2)
            continue
        if not _is_prose(block):
            continue
        sec = f"{top} - {sub}" if sub else top
        # A continuation whose sentence is not the one just before it (another
        # column's paragraph or a figure came in between) starts a new, partial
        # sentence here: guessing which earlier sentence it finishes joins the
        # wrong ones, so it is left as the fragment it is.
        if pending:
            prev = raw[pending[-1][0]:pending[-1][1]].rstrip()
            # A column or page break inside a sentence: the previous block does
            # not end one and this one starts in lower case.
            if prev and prev[-1] not in ".?!:" and flat[:1].islower():
                pending.append((a, b, pending[0][2]))
                continue
            flush()
        pending.append((a, b, sec))
        sents_in_top += 1
    flush()
    close_table(len(raw))
    _pair_fragments(sentences)
    return {"sentences": sentences, "outline": Outline(headings, regions)}


def _pair_fragments(sentences: List[dict]) -> None:
    """Mark sentences the PDF layout split. A sentence that stops mid-way ("To the
    best of our knowledge, the present") and a later one that starts in lower
    case ("study is the first to address ...") are flagged, and each open
    fragment points at the nearest later lower-case fragment as its likely
    continuation. They are not joined: with columns and figures interleaved, a
    guessed join puts the wrong halves together."""
    open_frag = [x for x in sentences if x["text"][-1:] not in ".?!:\"\u201d)"]
    tails = [x for x in sentences if x["text"][:1].islower()]
    used = set()
    for o in open_frag:
        o["fragment"] = "unfinished (layout split)"
        nxt = next((t for t in tails if t["id"] > o["id"] and t["id"] not in used
                    and t["id"] - o["id"] <= 15), None)
        if nxt:
            used.add(nxt["id"])
            o["may_continue_in"] = nxt["id"]
            nxt["may_continue_from"] = o["id"]
    for t in tails:
        t.setdefault("fragment", "starts mid-sentence (layout split)")


def sentences_overlapping(sentences: Sequence[dict], start: int, end: int) -> List[dict]:
    """Sentences sharing at least half of themselves (or of the span) with it."""
    out = []
    for s in sentences:
        spans = s.get("pieces") or [(s["start"], s["end"])]
        ov = sum(max(0, min(end, b) - max(start, a)) for a, b in spans)
        if ov <= 0:
            continue
        size = sum(b - a for a, b in spans)
        if ov >= 0.5 * size or ov >= 0.5 * (end - start):
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# one sentence per prose finding
# --------------------------------------------------------------------------- #

_TABLE_SECTION = re.compile(r"\b(?:table|figure|fig\.)\s*[A-Z]?\d", re.I)
# A sentence that leans on the one before it for its subject or subgroup.
_CONTINUES = re.compile(r"^\s*(?:additionally|also|furthermore|moreover|in addition|"
                        r"similarly|likewise|conversely|however|in contrast|"
                        r"this|these|that|those|such|it|they|both|neither)\b", re.I)


def _names(v: str, aliases: Dict[str, List[str]]) -> List[str]:
    """Strings that count as a mention of variable ``v`` in a sentence."""
    out = [v] + list(aliases.get(v.lower(), []))
    parts = re.split(r"\s*[–—-]\s*", v)
    if len(parts) == 2:                          # "Auditory–Visual" network pair
        out.extend(parts)
    return [o for o in out if o and len(o) >= 2]


def _mentions(text: str, v: str, aliases: Dict[str, List[str]]) -> bool:
    low = text.lower()
    for n in _names(v, aliases):
        n = n.lower()
        if len(n) <= 4:
            if re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", low):
                return True
        elif n in low or n[:6] in low.split() or re.search(rf"\b{re.escape(n[:6])}", low):
            return True
    return False


def alias_map(doc: dict) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for v in doc.get("variables") or []:
        name = str(v.get("name") or "").strip()
        if not name:
            continue
        al = [str(a) for a in (v.get("aliases") or []) if a]
        if v.get("label"):
            al.append(str(v["label"]))
        out.setdefault(name.lower(), []).extend(al)
        for a in al:
            out.setdefault(a.lower(), []).append(name)
    return out


def one_sentence_per_finding(doc: dict, sentences: Sequence[dict]) -> int:
    """Cut joined quotes to the one sentence each finding comes from. Returns the
    number of findings changed; each records what was cut and why."""
    aliases = alias_map(doc)
    changed = 0
    for f in doc.get("findings") or []:
        ev = f.get("evidence") or {}
        if in_table_or_figure(ev):
            continue
        s, e = ev.get("start"), ev.get("end")
        if not isinstance(s, int) or not isinstance(e, int):
            continue
        hit = sentences_overlapping(sentences, s, e)
        if len(hit) <= 1:
            f["sentence_ids"] = [h["id"] for h in hit]
            continue
        variables = [str(v) for v in (f.get("variables") or [])]
        words = set(re.findall(r"[a-z]{4,}", str(f.get("statement") or "").lower()))

        def score(sent: dict) -> Tuple[int, int]:
            txt = sent["text"]
            return (sum(_mentions(txt, v, aliases) for v in variables),
                    len(words & set(re.findall(r"[a-z]{4,}", txt.lower()))))

        best = max(hit, key=score)
        others = [h for h in hit if h is not best]
        before = [h for h in others if h["id"] < best["id"]]
        leans = bool(_CONTINUES.match(best["text"])) and bool(before)
        dropped = []
        if not leans:
            for v in variables:
                if not _mentions(best["text"], v, aliases) and \
                        any(_mentions(o["text"], v, aliases) for o in others):
                    dropped.append(v)
        ev.setdefault("quote_as_extracted", ev.get("quote"))
        ev.update({"quote": best["text"], "start": best["start"], "end": best["end"],
                   "quote_cut_to_one_sentence": True,
                   "other_sentences_in_quote": [o["id"] for o in others]})
        if leans:
            ev["context_sentence"] = before[-1]["id"]
            ev["context_sentence_text"] = before[-1]["text"]
        f["evidence"] = ev
        if dropped:
            f["variables"] = [v for v in variables if v not in dropped]
            f["variables_dropped_with_other_sentence"] = dropped
        f["sentence_ids"] = [best["id"]]
        changed += 1
    return changed


# --------------------------------------------------------------------------- #
# prose versus the table it cites
# --------------------------------------------------------------------------- #

_CITES_TABLE = re.compile(r"\bTables?\s*([A-Z]?\d+)", re.I)
_ALL = re.compile(r"\b(?:all|every|each)\b(?:\s+(?:\d+|nine|eight|seven|six|five|four|"
                  r"three|two))?\s*(?:of the\s+)?(?:\w+\s+)?(?:scales?|outcomes?|"
                  r"measures?|scores?|domains?|subscales?)", re.I)
_WITHIN = re.compile(r"\b(?:within|intra-?\s?network connectivity (?:of|in))\s+the\s+"
                     r"([a-z][a-z\- ]{2,40}?)\s+(?:network|networks)\b", re.I)


def table_of(ev: dict) -> Optional[str]:
    """The table a quote is a row of: from the extractor's section label, or,
    when the label does not say so, from where the quote sits in the paper's
    outline (inside a "TABLE 3" block). None for prose."""
    return _table_no(ev.get("section")) or _table_no(ev.get("section_in_paper"))


def in_table_or_figure(ev: dict) -> bool:
    return bool(_TABLE_SECTION.search(str(ev.get("section") or ""))
                or _TABLE_SECTION.search(str(ev.get("section_in_paper") or "")))


def _table_no(section: Any) -> Optional[str]:
    m = re.search(r"\btable\s*([A-Z]?\d+)", str(section or ""), re.I)
    return m.group(1).upper() if m else None


def _pair(v: str) -> Optional[Tuple[str, str]]:
    parts = re.split(r"\s*[–—]\s*", v)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def _row_label(f: dict) -> str:
    bits = [" – ".join(f.get("variables") or [])]
    if f.get("subgroup"):
        bits.append(f"[{f['subgroup']}]")
    bits.append(f"({f.get('direction')}")
    stat = "; ".join(str(x) for x in (f.get("effect_size"), f.get("statistic")) if x)
    return " ".join(bits) + (f", {stat})" if stat else ")")


def check_cited_tables(doc: dict) -> int:
    """Compare each prose finding with the rows of the table it cites. Returns the
    number of findings flagged; each gets ``cited_table_check``."""
    findings = doc.get("findings") or []
    by_table: Dict[str, List[dict]] = {}
    for f in findings:
        t = table_of(f.get("evidence") or {})
        if t:
            by_table.setdefault(t, []).append(f)
    aliases = alias_map(doc)
    flagged = 0
    intra_sentences = {sid for f in findings for sid in f.get("sentence_ids") or []
                       if any(_pair(str(v)) and _pair(str(v))[0].lower() == _pair(str(v))[1].lower()
                              for v in f.get("variables") or [])}
    for f in findings:
        ev = f.get("evidence") or {}
        if table_of(ev):
            continue
        quote = str(ev.get("quote") or "")
        cited = [m.group(1).upper() for m in _CITES_TABLE.finditer(quote)]
        rows = [r for t in cited for r in by_table.get(t, [])]
        if not rows:
            continue
        fvars = [str(v) for v in f.get("variables") or []]
        fkeys = {v.lower() for v in fvars}
        # Rows of the cited table that share a non-pair variable (the predictor
        # or the outcome) with the prose finding.
        # The finding's first non-pair variable is what it is about (PTE in "PTE
        # was associated with all scales"); a row for another predictor in the
        # same table says nothing about it.
        lead = next((v.lower() for v in fvars if not _pair(v)), None)
        shared = [r for r in rows if lead and lead in
                  {str(v).lower() for v in r.get("variables") or [] if not _pair(str(v))}]
        if not shared:
            continue
        issues: List[dict] = []
        pairs_in_table = {}
        for r in shared:
            for v in r.get("variables") or []:
                p = _pair(str(v))
                if p:
                    pairs_in_table.setdefault(frozenset(x.lower() for x in p), []).append(r)
        # 1. A network the prose names that the table does not have.
        names = {x for key in pairs_in_table for x in key}
        fpairs = [_pair(v) for v in fvars if _pair(v)]
        # "within the X network" is about a within-network finding (X–X), or a
        # finding that names no pair; a between-network finding from the same
        # sentence is not.
        about_within = not fpairs or any(a.lower() == b.lower() for a, b in fpairs)
        # ... unless no finding from this sentence covers its within-network part
        # (an extractor that kept only the between-network result): then this
        # finding carries the check, or the mismatch would go unseen.
        sids = set(f.get("sentence_ids") or [])
        if not about_within and sids and not (sids & intra_sentences):
            about_within = True
        for m in (_WITHIN.finditer(quote) if about_within else ()):
            said = m.group(1).strip().lower()
            net = next((n for n in names if n == said or said.startswith(n)
                        or n.startswith(said)), said)
            if frozenset([net]) not in pairs_in_table:
                intra = [r for k, rs in pairs_in_table.items() if len(k) == 1 for r in rs]
                issues.append({
                    "kind": "network_not_in_cited_table",
                    "detail": (f"the sentence says “{m.group(0)}”, but Table "
                               f"{'/'.join(cited)} has no {net}–{net} row for "
                               f"{lead}"
                               + (f"; its within-network row(s): "
                                  + "; ".join(_row_label(r) for r in intra) if intra else "")),
                })
        said_within = any(i["kind"] == "network_not_in_cited_table" for i in issues)
        for v in fvars:
            p = _pair(v)
            if p and said_within and p[0].lower() == p[1].lower():
                continue                      # already reported as the network above
            if p and frozenset(x.lower() for x in p) not in pairs_in_table:
                issues.append({"kind": "variable_not_in_cited_table",
                               "detail": f"{v} has no row in Table {'/'.join(cited)}"})
        # 2. Signs the table contradicts.
        d = f.get("direction")
        if d in ("positive", "negative"):
            same_pair = [r for v in fvars if _pair(v)
                         for r in pairs_in_table.get(frozenset(x.lower() for x in _pair(v)), [])]
            against = [r for r in same_pair if r.get("direction") in ("positive", "negative")
                       and r.get("direction") != d]
            if against:
                issues.append({"kind": "sign_differs_from_cited_table",
                               "detail": "the table's row(s): "
                               + "; ".join(_row_label(r) for r in against)})
        # 3. "all N scales" when some of the cited rows are not significant.
        # The quantifier must be in the clause about this finding's predictor:
        # "PTE was associated with all scales, and PME ... with 7 out of 9" says
        # "all" about PTE only.
        clause = next((c for c in re.split(r",\s*(?:and|while|whereas|but)\s+|;\s*", quote)
                       if lead and _mentions(c, lead, aliases)), quote)
        if _ALL.search(clause) and d not in ("null",):
            nulls = [r for r in shared if r.get("direction") == "null"]
            if nulls:
                issues.append({
                    "kind": "cited_table_partly_supports",
                    "detail": (f"the sentence says “{_ALL.search(clause).group(0)}”, "
                               f"but {len(nulls)} of the {len(shared)} {lead} rows in Table "
                               f"{'/'.join(cited)} are not significant: "
                               + "; ".join(_row_label(r) for r in nulls)),
                })
        if issues:
            f["cited_table_check"] = issues
            flagged += 1
    return flagged


def _nonpair(rows: Sequence[dict]) -> set:
    return {str(v).lower() for r in rows for v in r.get("variables") or [] if not _pair(str(v))}


# --------------------------------------------------------------------------- #
# a pair listed in both orders
# --------------------------------------------------------------------------- #

def mark_mirrored_rows(doc: dict) -> int:
    """Within one table, "A–B" and "B–A" with the same other variables and
    subgroup are the same relationship. The later row gets ``mirror_of`` (the
    index of the first) so counts and the cross-paper grouping count it once."""
    seen: Dict[Tuple, int] = {}
    n = 0
    for i, f in enumerate(doc.get("findings") or []):
        t = table_of(f.get("evidence") or {})
        if not t:
            continue
        vs = [str(v) for v in f.get("variables") or []]
        pairs = [(_pair(v), v) for v in vs]
        if not any(p for p, _ in pairs):
            continue
        canon = tuple(sorted(" – ".join(sorted(p)) if p else v.lower()
                             for p, v in pairs))
        ordered = tuple(sorted(vs))
        key = (t, canon, str(f.get("subgroup") or ""))
        if key in seen:
            first = doc["findings"][seen[key]]
            if tuple(sorted(str(v) for v in first.get("variables") or [])) != ordered:
                f["mirror_of"] = seen[key]
                n += 1
            continue
        seen[key] = i
    return n


# --------------------------------------------------------------------------- #
# claim language
# --------------------------------------------------------------------------- #

CAUSAL_LANGUAGE = ("correlational", "direct_causal", "conditional_causal")
_CAUSAL_VERB = re.compile(
    r"\b(?:caus\w*|lead(?:s|ing)? to|led to|result(?:s|ed)? in|impact(?:s|ed|ing)?|"
    r"affect(?:s|ed|ing)?|influenc\w*|driv(?:e|es|en|ing)|contribut\w*|"
    r"play(?:s|ed)? (?:a|an) (?:\w+ )?role|produc(?:e|es|ed)|induc\w*|"
    r"increas(?:e|es|ed) the risk|protect\w*)\b", re.I)
_NEGATED = re.compile(r"\b(?:not|no|never|did not|does not|was not|were not)\s+"
                      r"(?:\w+\s+)?$", re.I)
_HEDGE = re.compile(r"\b(?:may|might|could|possibly|potentially|likely|suggest\w*|"
                    r"tend(?:s|ed)? to|appear(?:s|ed)? to|seem\w*|indicat\w* that|"
                    r"perhaps|presumably|it is possible)\b", re.I)


def causal_language(text: str) -> str:
    for m in _CAUSAL_VERB.finditer(text):
        if _NEGATED.search(text[max(0, m.start() - 20):m.start()]):
            continue                     # "did not impact" is a null, not a cause
        before = text[max(0, m.start() - 30):m.start()]
        return "conditional_causal" if _HEDGE.search(before) else "direct_causal"
    return "correlational"


def hedged(text: str) -> bool:
    return bool(_HEDGE.search(text))


# --------------------------------------------------------------------------- #
# significance, change over time, prediction, and the human claim scheme
# --------------------------------------------------------------------------- #

_P_VALUE = re.compile(
    r"\b(?:p|q|p[-\s]?fdr|fdr|p[-\s]?adj(?:usted)?|p[-\s]?corr(?:ected)?)\s*(?:-?value)?\s*"
    r"(=|<|>|\u2264|\u2265|\u2248)\s*(?:<\s*)?"
    r"((?:\d*\.\d+|\d+)(?:\s*(?:e|E|\u00d7\s*10\^?|x\s*10\^?)\s*[-\u2212]?\s*\d+)?)",
    re.I)
_NOT_SIG = re.compile(r"\b(?:not|no|nor|non-?)\s*(?:statistically\s+)?significan\w*|"
                      r"\bn\.s\.|did not reach (?:statistical )?significance|"
                      r"failed to reach significance", re.I)
_SIG = re.compile(r"\bsignifican(?:t|tly|ce)\b", re.I)
_ALPHA = re.compile(
    r"(?:p|fdr|false discovery rate|alpha|\u03b1|q)[^.;]{0,40}?(?:<|\u2264|of|at|=)\s*(0?\.\d+)"
    r"[^.;]{0,40}?(?:considered|deemed|taken as|was used|as the threshold|threshold)"
    r"|significance (?:level|threshold|criterion)[^.;]{0,30}?(?:<|=|of|at)\s*(0?\.\d+)", re.I)


def paper_alpha(text: str) -> float:
    """The significance threshold the paper states ("an FDR of < 0.05 was
    considered significant"), or .05."""
    for m in _ALPHA.finditer(" ".join(text.split())):
        v = m.group(1) or m.group(2)
        try:
            x = float(v)
            if 0 < x < 0.2:
                return x
        except (TypeError, ValueError):
            pass
    return 0.05


def _p(value: str) -> Optional[float]:
    v = value.replace("\u2212", "-").replace(" ", "")
    v = re.sub(r"(?:\u00d710\^?|x10\^?)", "e", v, flags=re.I)
    try:
        return float(v)
    except ValueError:
        return None


def significance(text: str, alpha: float = 0.05) -> dict:
    """Is the result in ``text`` significant? From its p / FDR values when it
    prints them (all below alpha: yes; all at or above: no; some of each:
    partial), else from wording ("not significant", "significantly").
    """
    ps = []
    for m in _P_VALUE.finditer(text or ""):
        val = _p(m.group(2))
        if val is None:
            continue
        op = m.group(1)
        sig = (val < alpha) if op in ("=", "\u2248") else \
            (val <= alpha) if op in ("<", "\u2264") else False
        ps.append({"op": op, "p": val, "significant": sig})
    if ps:
        flags = {x["significant"] for x in ps}
        verdict = True if flags == {True} else False if flags == {False} else "partial"
        return {"significant": verdict, "p_values": [f"{x['op']} {x['p']:g}" for x in ps],
                "alpha": alpha, "from": "p values"}
    if _NOT_SIG.search(text or ""):
        return {"significant": False, "p_values": [], "alpha": alpha, "from": "wording"}
    if _SIG.search(text or ""):
        return {"significant": True, "p_values": [], "alpha": alpha, "from": "wording"}
    return {"significant": None, "p_values": [], "alpha": alpha, "from": None}


# Change over time: a comparison across waves or ages, not an association.
_OVER_TIME = re.compile(
    r"\bover time\b|\bacross (?:waves|time|years|visits|assessments|development)\b|"
    r"\bfrom baseline\b|\b(?:than|to|with|as) (?:at |in )?(?:the )?baseline\b|"
    r"\b(?:at|in|by|of) (?:the )?(?:\d+(?:st|nd|rd|th)?|one|two|three|four|five)[- ]?year\b|"
    r"\b(?:\d+(?:st|nd|rd|th)|one|two|three|four)[- ]years? (?:later|follow[- ]?up)\b|"
    r"\bfollow[- ]?up\b|\byears? later\b|\bwith age\b|\blongitudinal\w*\b|"
    r"\bdevelopmental (?:change|trajector\w*)|\btrajector\w*|"
    r"\b(?:baseline|first wave|wave 1) (?:and|to|vs\.?|versus) (?:the )?\w+", re.I)
# Verbs of change say it outright and win over comparative adjectives, which
# often compare tasks within one wave ("Flanker and Card correlated higher than
# with List ... by comparing baseline and the 4th year, we found a decreased
# similarity").
_VERB_DOWN = re.compile(r"\b(?:decreas\w*|declin\w*|reduc\w*|diminish\w*|weaken\w*|"
                        r"dropp?\w*|fell|attenuat\w*)\b", re.I)
_VERB_UP = re.compile(r"\b(?:increas\w*|grew|grow\w*|rose|strengthen\w*|emerg\w*)\b", re.I)
_CHANGE_DOWN = re.compile(
    r"\b(?:decreas\w*|declin\w*|smaller|weaker|lower|less(?: so)?|reduc\w*|"
    r"diminish\w*|dropp?\w*|fell|attenuat\w*|differentiat\w*)\b", re.I)
_CHANGE_UP = re.compile(
    r"\b(?:increas\w*|larger|greater than|stronger|higher than|grew|rose|"
    r"emerg\w*|appear\w*|only (?:showed|appeared) (?:in|at)|new (?:regions?|clusters?))\b", re.I)
_CHANGE_NONE = re.compile(
    r"\b(?:remain\w*|stable|stability|unchanged|similar to (?:the )?baseline|"
    r"no (?:significant )?change|persist\w*|consistent across)\b", re.I)


_LATER = r"(?:the )?(?:\d+(?:st|nd|rd|th)?|one|two|three|four|five)[- ]?years?(?: later| follow[- ]?up)?|follow[- ]?up|later (?:wave|data|assessment)|year[- ]?\d"
_EARLIER = r"(?:the )?baseline|earlier|the first (?:wave|assessment)"
_COMPARE_WAVE = re.compile(
    r"\b(higher|greater|larger|stronger|more|lower|smaller|weaker|less|high|low)\s+(?:than|then)\b"
    r"[^;]{0,50}?\b(?:(" + _LATER + r")|(" + _EARLIER + r"))\b", re.I)


def change_over_time(text: str) -> Optional[str]:
    """For a sentence about change across waves, the direction of the change by
    the convention the Findings sheet uses: positive = increases or appears,
    negative = decreases or weakens, null = stays the same, mixed = both. None
    when the sentence is not about change over time.

    A comparison names its reference wave: "higher than .14 of 4th year" says the
    later wave is lower, a decrease; "smaller than at baseline" is a decrease too.
    """
    text = re.sub(r"\u00ad\s*", "", text or "")
    vd, vu = bool(_VERB_DOWN.search(text)), bool(_VERB_UP.search(text))
    if _OVER_TIME.search(text) and vd != vu:
        return "negative" if vd else "positive"
    cmp = _COMPARE_WAVE.search(text)
    if cmp:
        up = cmp.group(1).lower() in ("higher", "greater", "larger", "stronger", "more", "high")
        later = bool(cmp.group(2))
        # "X higher than [the later wave]" -> later is lower -> decrease.
        return ("negative" if up else "positive") if later else ("positive" if up else "negative")
    if not _OVER_TIME.search(text):
        return None
    down, up, same = (bool(r.search(text)) for r in (_CHANGE_DOWN, _CHANGE_UP, _CHANGE_NONE))
    if down and up:
        return "mixed"
    if down:
        return "negative"
    if up:
        return "positive"
    if same:
        return "null"
    return None


# Prediction by a model (CPM, machine learning, out-of-sample), not regression
# wording: "food insecurity was a strong predictor of poor scores" is association.
_PREDICTION = re.compile(
    r"\bpredicted (?:and|vs\.?|versus) actual\b|\bprediction (?:accuracy|performance|models?)\b|"
    r"\bmutual(?:ly)? predict\w*|\bcross[- ]?(?:task )?predict\w*|\bcross[- ]validat\w*|"
    r"\bout[- ]of[- ]sample\b|\bconnectome[- ]based predictive|\bCPM\b|"
    r"\bmachine[- ]learning\b|\bclassif(?:ier|ication accuracy)\b|"
    r"\b(?:models?|networks?|measurements?|data|features?) (?:can|could|did|do|cannot|"
    r"could not|did not)?\s*(?:not )?(?:also )?(?:significantly )?predict(?:ed)?\b", re.I)


def is_prediction(text: str) -> bool:
    return bool(_PREDICTION.search(text or ""))


# This pipeline's claim types -> the scheme a sentence-level human annotator uses
# (association / group_difference / prediction / change_over_time / descriptive).
HUMAN_CLAIM_TYPE = {
    "associative": "association", "correlation": "association", "explicit": "association",
    "implicit": "association", "causal": "association", "observation": "association",
    "comparison": "group_difference", "prediction": "prediction",
    "change_over_time": "change_over_time", "descriptive": "descriptive",
}


def human_claim_type(claim_type: Optional[str], text: str) -> Optional[str]:
    """The human-scheme type for a claim: change over time and model prediction
    are read from the sentence (they cut across this pipeline's wording-based
    types); everything else goes through ``HUMAN_CLAIM_TYPE``."""
    if claim_type == "change_over_time" or change_over_time(text):
        return "change_over_time"
    if claim_type == "prediction" or is_prediction(text):
        return "prediction"
    return HUMAN_CLAIM_TYPE.get(str(claim_type or ""), "association" if claim_type else None)


_SIGNED_STAT = re.compile(
    r"(?:\b(?:r|\u03c1|rho|\u03b2|beta|b|B|t|z|d|g|OR)\s*=\s*"
    r"|\b(?:correlations?|coefficients?|heritabilit\w*|estimates?|ind(?:ex|ices)|"
    r"similarit\w*|accuracy|loadings?|associations?)\b[^;]{0,70}?"
    r"\b(?:=|was|were|is|are|of|rang(?:e|ed|es|ing)(?: between| from)?)\s*)"
    r"([-\u2212]?\s*\d*\.\d+)", re.I)


def sign_from_statistics(text: str) -> Optional[str]:
    """The sign the sentence's own statistics carry (r = .14, beta = -.21)."""
    signs = set()
    for m in _SIGNED_STAT.finditer(text or ""):
        v = m.group(1).replace("\u2212", "-").replace(" ", "")
        signs.add("negative" if v.startswith("-") else "positive")
    return signs.pop() if len(signs) == 1 else ("mixed" if signs else None)


def apply_claim_language(doc: dict, alpha: float) -> dict:
    """Significance, change over time and prediction on every finding.

    * A finding whose own statistics are all non-significant is a null result:
      its sign moves to ``direction_as_extracted`` and ``direction`` becomes
      null (``direction_null_not_significant``). Change over time is exempt:
      "the year-4 correlation, .33 (p = .344), again showed a decrease" is a
      decrease whatever the p of the correlation itself.
    * A change-over-time finding takes the direction of the change (positive
      increases or appears, negative decreases, null stays the same).
    * ``claim_type_human`` is the human-scheme type.
    """
    stats = {"set_null_not_significant": 0, "change_over_time": 0, "prediction": 0,
             "sign_from_statistics": 0}
    for f in doc.get("findings") or []:
        ev = f.get("evidence") or {}
        quote = str(ev.get("quote") or "")
        tabled = bool(table_of(ev))
        if not tabled and f.get("direction") == "unspecified":
            st = sign_from_statistics(re.sub(r"\u00ad\s*", "", quote))
            if st:
                # "showed significant correlations ... (r = .20, .26, .22)": the
                # sentence states the sign through its statistics.
                f["direction"], f["direction_from"] = st, "sign of the sentence's statistics"
                stats["sign_from_statistics"] += 1
        # A table row's quote holds every column; its own p is in `statistic`.
        own = " ".join(str(x) for x in (f.get("statistic"), f.get("effect_size")) if x)
        sig = significance(own if (tabled or own) else quote, alpha)
        if sig["significant"] is None and not tabled:
            sig = significance(quote, alpha)
        f["significance"] = sig
        cot = None if tabled else change_over_time(quote)
        if f.get("claim_type") == "change_over_time" or cot:
            stats["change_over_time"] += 1
            if cot and f.get("claim_type") != "change_over_time":
                f["change_over_time_detected"] = True
            if cot and cot != f.get("direction"):
                f.setdefault("direction_as_extracted", f.get("direction"))
                f["direction"] = cot
                f["direction_from"] = "direction of change over time"
        elif sig["significant"] is False and f.get("direction") in ("positive", "negative",
                                                                      "mixed"):
            f.setdefault("direction_as_extracted", f["direction"])
            f["direction"] = "null"
            f["directionality"] = None
            f["direction_null_not_significant"] = True
            stats["set_null_not_significant"] += 1
        if f.get("claim_type") == "prediction" or (not tabled and is_prediction(quote)):
            stats["prediction"] += 1
        f["claim_type_human"] = human_claim_type(f.get("claim_type"), "" if tabled else quote)
    return stats


# --------------------------------------------------------------------------- #
# the extractor's sentence-level claims
# --------------------------------------------------------------------------- #

def _resolve(sentences: Sequence[dict], index, quote: Any) -> Optional[int]:
    if not quote:
        return None
    if isinstance(quote, int):
        return quote if 1 <= quote <= len(sentences) else None
    found = index.find(str(quote))
    if not found:
        return None
    hit = sentences_overlapping(sentences, found[2], found[3])
    return hit[0]["id"] if hit else None


def verify_sentence_claims(payload: dict, index, sentences: Sequence[dict],
                           gate_scope, norm_enum) -> Tuple[List[dict], List[dict]]:
    """Anchor ``sentence_claims[]``. Returns (kept, rejected).

    A sentence claim is the extractor saying "this sentence states a result of
    this study". It is kept only if its quote is in the paper, falls inside one
    numbered sentence, and is not framed as somebody else's work (a citation or
    "has been shown" in the sentence itself). A Discussion sentence is accepted
    without first-person wording, because restatements ("Male sex was associated
    with higher scores ...") rarely have any.
    """
    kept, rejected = [], []
    for raw in payload.get("sentence_claims") or []:
        if not isinstance(raw, dict):
            continue
        q = str(raw.get("quote") or (raw.get("evidence") or {}).get("quote") or "")
        found = index.find(q) if len(q.strip()) >= 15 else None
        if not found:
            rejected.append({"section": "sentence_claims", "reason": "quote_not_found_in_paper",
                             "claimed_quote": q[:300] or None})
            continue
        hit = sentences_overlapping(sentences, found[2], found[3])
        if not hit:
            rejected.append({"section": "sentence_claims", "reason": "not_in_a_prose_sentence",
                             "claimed_quote": q[:300]})
            continue
        sent = hit[0]
        item = {"evidence": {"quote": sent["text"], "section": sent["section"],
                             "used_context": ""}}
        _, why = gate_scope(item, strict=False)
        from scripts.abcd_verify import (_CITATION_RE, _OWN_STUDY_RE,  # noqa: E402
                                         _PRIOR_WORK_RE)
        own = bool(_OWN_STUDY_RE.search(sent["text"]))
        # "our study has shown" is the paper's own; "has been shown (Smith 2020)"
        # or "previous studies have shown" is not.
        if _CITATION_RE.search(sent["text"]) or (_PRIOR_WORK_RE.search(sent["text"])
                                                 and not own):
            why = why or "claim_attributed_to_cited_work"
        if why:
            rejected.append({"section": "sentence_claims", "reason": why,
                             "claimed_quote": sent["text"][:300]})
            continue
        cl = str(raw.get("causal_language") or "").strip().lower().replace(" ", "_")
        kept.append({
            "sentence_id": sent["id"],
            "quote": sent["text"],
            "section": sent["section"],
            "claim_type": raw.get("claim_type"),
            "direction": norm_enum(raw.get("direction"),
                                   ("positive", "negative", "null", "mixed", "unspecified"),
                                   "unspecified"),
            "causal_language": cl if cl in CAUSAL_LANGUAGE else causal_language(sent["text"]),
            "hedged": bool(raw["hedged"]) if isinstance(raw.get("hedged"), bool)
            else hedged(sent["text"]),
            "restates": [i for i in (_resolve(sentences, index, x) for x in
                                     _as_list(raw.get("restates"))) if i],
            "conflicts_with": [
                {"sentence_id": _resolve(sentences, index, c.get("quote") if isinstance(c, dict) else c),
                 "detail": (c.get("detail") if isinstance(c, dict) else None)}
                for c in _as_list(raw.get("conflicts_with"))],
            "notes": raw.get("notes"),
        })
    for k in kept:
        k["conflicts_with"] = [c for c in k["conflicts_with"] if c["sentence_id"]]
    return kept, rejected


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------- #
# the sentence table
# --------------------------------------------------------------------------- #

def build_sentence_table(doc: dict, sentences: Sequence[dict],
                         claims: Sequence[dict], *, assessed: bool,
                         alpha: float = 0.05) -> List[dict]:
    """One row per sentence, in the human annotation's columns.

    is_claim is "yes" when a finding quotes the sentence or the extractor marked
    it as a claim, "no" otherwise, and "not assessed" for sentences outside the
    results when the payload carried no sentence_claims (an older payload).
    """
    by_sent: Dict[int, List[int]] = {}
    for i, f in enumerate(doc.get("findings") or []):
        for sid in f.get("sentence_ids") or []:
            by_sent.setdefault(sid, []).append(i)
    claim_of = {c["sentence_id"]: c for c in claims}
    # Sentence-level cross-references in both directions.
    restated_by: Dict[int, List[int]] = {}
    conflict: Dict[int, List[dict]] = {}
    for c in claims:
        for r in c["restates"]:
            restated_by.setdefault(r, []).append(c["sentence_id"])
        for x in c["conflicts_with"]:
            conflict.setdefault(c["sentence_id"], []).append(x)
            conflict.setdefault(x["sentence_id"], []).append(
                {"sentence_id": c["sentence_id"], "detail": x.get("detail")})
    for i, f in enumerate(doc.get("findings") or []):
        for chk in f.get("cited_table_check") or []:
            for sid in f.get("sentence_ids") or []:
                conflict.setdefault(sid, []).append({"sentence_id": None,
                                                     "detail": chk["detail"]})
    rows = []
    for s in sentences:
        fids = by_sent.get(s["id"], [])
        c = claim_of.get(s["id"])
        fs = [doc["findings"][i] for i in fids]
        if c or fs:
            is_claim = "yes"
        elif assessed or not re.match(r"(?:abstract|discussion|conclusion)",
                                      s["section"].lower()):
            # Introduction and Methods sentences are not this study's results;
            # abstract and discussion sentences can be, and only the extractor's
            # sentence_claims say which.
            is_claim = "no"
        else:
            is_claim = "not assessed"
        direction = c["direction"] if c else _merge_dirs([f.get("direction") for f in fs])
        claim_type = (c or {}).get("claim_type") or \
            "; ".join(sorted({str(f.get("claim_type")) for f in fs if f.get("claim_type")})) or None
        notes = []
        if c and c.get("notes"):
            notes.append(str(c["notes"]))
        if c and c["restates"]:
            notes.append("restates " + ", ".join(map(str, c["restates"])))
        if restated_by.get(s["id"]):
            notes.append("restated in " + ", ".join(map(str, restated_by[s["id"]])))
        for x in conflict.get(s["id"], []):
            notes.append("INCONSISTENT" + (f" with {x['sentence_id']}" if x["sentence_id"] else "")
                         + (f": {x['detail']}" if x.get("detail") else ""))
        if len(fs) > 1:
            notes.append(f"{len(fs)} findings from this sentence")
        if s.get("may_continue_in"):
            notes.append(f"sentence split by the PDF layout; likely continues in {s['may_continue_in']}")
        elif s.get("may_continue_from"):
            notes.append(f"sentence split by the PDF layout; likely continues {s['may_continue_from']}")
        elif s.get("fragment"):
            notes.append(f"sentence split by the PDF layout ({s['fragment']})")
        # One convention with the Findings sheet: a change-over-time claim's
        # direction is the direction of the change; a claim whose statistics are
        # all non-significant is null.
        human = human_claim_type(claim_type.split("; ")[0] if claim_type else None,
                                 s["text"]) if (c or fs) else None
        if human == "change_over_time":
            cot = change_over_time(s["text"])
            if cot and cot != direction:
                notes.append(f"direction of change over time: {cot} (claim said {direction})")
                direction = cot
        elif direction in ("positive", "negative", "mixed"):
            sig = significance(s["text"], alpha)
            if sig["significant"] is False:
                notes.append(f"not significant ({', '.join(sig['p_values']) or 'by wording'}): null")
                direction = "null"
            elif sig["significant"] == "partial":
                notes.append("significant for some of its results only ("
                             + ", ".join(sig["p_values"]) + ")")
        rows.append({
            "sentence_id": s["id"], "section": s["section"], "text": s["text"],
            "is_claim": is_claim, "claim_type": claim_type,
            "claim_type_human": human if is_claim == "yes" else None,
            "causal_language": (c or {}).get("causal_language")
            or (causal_language(s["text"]) if is_claim == "yes" else None),
            "direction": direction if is_claim == "yes" else None,
            "hedged": ("yes" if ((c or {}).get("hedged") if c else hedged(s["text"]))
                       else "no") if is_claim == "yes" else None,
            "notes": "; ".join(notes) or None,
            "findings": fids,
            "inconsistent": bool(conflict.get(s["id"])),
        })
    return rows


def _merge_dirs(dirs: Sequence[Any]) -> Optional[str]:
    ds = {d for d in dirs if d}
    if not ds:
        return None
    if len(ds) == 1:
        return ds.pop()
    signed = ds & {"positive", "negative"}
    # Mixed means opposite signs. One sign plus non-significant results reads as
    # that sign ("r = -.03/-.008/-.03, p = .003/.47/.02" is negative).
    if len(signed) == 2 or "mixed" in ds:
        return "mixed"
    if signed:
        return signed.pop()
    return "null" if "null" in ds else sorted(ds)[0]


def _sections_from_findings(doc: dict, rows: List[dict]) -> None:
    """Two-column PDFs put headings out of reading order, so the section found by
    walking the text can be the wrong one. Where a finding quotes the sentence, the
    extractor read it in its section: use that, and carry it to the sentences
    between two that agree."""
    sec: Dict[int, str] = {}
    for f in doc.get("findings") or []:
        s = str((f.get("evidence") or {}).get("section") or "")
        if s and not _TABLE_SECTION.search(s):
            for sid in f.get("sentence_ids") or []:
                sec.setdefault(sid, s)
    ids = sorted(sec)
    for a, b in zip(ids, ids[1:]):
        if sec[a] == sec[b]:
            for k in range(a + 1, b):
                sec.setdefault(k, sec[a])
    for r in rows:
        if r["sentence_id"] in sec and sec[r["sentence_id"]] != r["section"]:
            r["section_as_walked"] = r["section"]
            r["section"] = sec[r["sentence_id"]]


# Words that state a direction. "PTE was significantly associated with all
# scales" states none: a sign there came from the heading or the table.
_DIRECTION_WORDS = re.compile(
    r"\b(?:higher|lower|greater|less(?:er)?|more|fewer|increas\w*|decreas\w*|"
    r"reduc\w*|elevat\w*|diminish\w*|worse|poor\w*|better|improv\w*|positive\w*|"
    r"negative\w*|inverse\w*|risk|protect\w*|hyper\w*|hypo\w*|stronger|weaker|"
    r"no(?:t)? (?:significant(?:ly)? )?(?:associat|differ|link|predict|impact)\w*|"
    r"(?:did|was|were) not)\b", re.I)


def sign_not_in_sentence(doc: dict) -> List[int]:
    """A prose finding keeps a sign only if its sentence states one. Otherwise the
    sign is moved to ``direction_as_extracted`` and the finding reads
    "unspecified"; the table rows it cites still carry their own signs."""
    out = []
    for i, f in enumerate(doc.get("findings") or []):
        ev = f.get("evidence") or {}
        if table_of(ev) or f.get("direction") not in ("positive", "negative",
                                                                       "mixed"):
            continue
        text = f"{ev.get('context_sentence_text') or ''} {ev.get('quote') or ''}"
        if not _DIRECTION_WORDS.search(text) and not sign_from_statistics(ev.get("quote") or ""):
            f["direction_as_extracted"] = f["direction"]
            f["direction"] = "unspecified"
            f["direction_not_in_sentence"] = True
            out.append(i)
    return out


# A Results sentence that reads as a result. Method sentences ("a linear
# regression was performed", "data is summarized in Table 1") are not.
_RESULT_CUE = re.compile(r"\b(?:associat\w*|linked|correlat\w*|predict\w*|"
                         r"significant\w*|differ\w*|alteration\w*|altered|"
                         r"higher|lower|greater|increased|decreased|reduced|"
                         r"supported by|confirmed|similar results|remained|overlap\w*)\b", re.I)
# Past-tense reporting of what this analysis found, for a sentence the column
# layout filed under a Methods heading.
_FOUND_CUE = re.compile(r"\b(?:showed|found|revealed|indicated|yielded|had strong|"
                        r"was (?:also )?(?:highly )?significant|were (?:also )?(?:highly )?"
                        r"significant(?:ly)?|similar results|supported by|remained)\b", re.I)
_METHOD_CUE = re.compile(r"\b(?:was|were) (?:performed|used|included|assessed|applied|"
                         r"conducted|considered|obtained|categori[sz]ed|corrected)\b|"
                         r"\bsummari[sz]ed in\b|\bwas considered significant\b|"
                         r"\bwe assessed\b|\bto assess\b|\binclude[sd]?\b|"
                         r"\b(?:the most|the least) (?:frequent|common)\b", re.I)


def flag_possible_missed_claims(rows: List[dict]) -> List[int]:
    from scripts.abcd_verify import _CITATION_RE, _PRIOR_WORK_RE
    out = []
    for r in rows:
        if r["is_claim"] != "no":
            continue
        sec = r["section"].lower()
        text = r["text"]
        if _CITATION_RE.search(text) or _PRIOR_WORK_RE.search(text):
            continue
        results_like = sec.startswith("results") and (_RESULT_CUE.search(text)
                                                      or change_over_time(text))
        methods_misfiled = sec.startswith("methods") and _FOUND_CUE.search(text) \
            and _RESULT_CUE.search(text)
        if (results_like or methods_misfiled) and not _METHOD_CUE.search(text):
            r["is_claim"] = "possibly (no finding extracted)"
            r["notes"] = "; ".join(x for x in ("reads as a result but no finding or "
                                               "sentence claim quotes it", r["notes"]) if x)
            out.append(r["sentence_id"])
    return out


def table_sections(doc: dict, sentences: Sequence[dict]) -> Dict[str, str]:
    """Table number -> the section of the first sentence that cites it, so a table
    row can say where in the paper it belongs ("Results - 3.3 ... > Table 3")."""
    out: Dict[str, str] = {}
    for s in sentences:
        for m in _CITES_TABLE.finditer(s["text"]):
            out.setdefault(m.group(1).upper(), s["section"])
    return out


def annotate(doc: dict, index, payload: dict, *, gate_scope, norm_enum,
             sentences: Optional[List[dict]] = None) -> dict:
    """Run every sentence-level step on a verified doc, in place. Returns stats."""
    sentences = sentences if sentences is not None else segment(index.nfkc)
    alpha = paper_alpha(index.nfkc)
    cut = one_sentence_per_finding(doc, sentences)
    mirrored = mark_mirrored_rows(doc)
    flagged = check_cited_tables(doc)
    claims, rej = verify_sentence_claims(payload, index, sentences, gate_scope, norm_enum)
    for f in doc.get("findings") or []:
        ev = f.get("evidence") or {}
        q = str(ev.get("quote") or "")
        if not table_of(ev):
            f.setdefault("causal_language", causal_language(q))
            if not isinstance(f.get("hedged"), bool):
                f["hedged"] = hedged(q)
    unstated = sign_not_in_sentence(doc)
    language = apply_claim_language(doc, alpha)
    covered = {sid for f in doc.get("findings") or [] for sid in f.get("sentence_ids") or []}
    covered |= {c["sentence_id"] for c in claims}
    uncovered = sorted({o for f in doc.get("findings") or []
                        for o in (f.get("evidence") or {}).get("other_sentences_in_quote") or []}
                       - covered)
    doc["sentence_claims"] = claims
    rows = build_sentence_table(doc, sentences, claims, assessed=bool(payload.get("sentence_claims")),
                                alpha=alpha)
    _sections_from_findings(doc, rows)
    doc["sentences"] = rows
    # A table row's section: the (corrected) section of the first sentence that
    # cites the table, then the table ("Results - 3.3 ... › Table 3").
    tsec = table_sections(doc, rows)
    for f in doc.get("findings") or []:
        ev = f.get("evidence") or {}
        t = table_of(ev)
        if t and tsec.get(t):
            ev["paper_section"] = f"{tsec[t]} › Table {t}"
        elif ev.get("section"):
            ev["paper_section"] = ev["section"]
    missed = flag_possible_missed_claims(rows)
    doc.setdefault("rejected", []).extend(rej)
    return {"sentences": len(sentences), "quotes_cut_to_one_sentence": cut,
            "mirrored_table_rows": mirrored, "findings_differing_from_cited_table": flagged,
            "sentence_claims_kept": len(claims), "sentence_claims_rejected": len(rej),
            "sentences_joined_into_a_quote_without_their_own_finding": uncovered,
            "findings_whose_sign_the_sentence_does_not_state": unstated,
            "significance_threshold": alpha,
            "findings_set_null_as_not_significant": language["set_null_not_significant"],
            "change_over_time_findings": language["change_over_time"],
            "prediction_findings": language["prediction"],
            "sentences_split_by_layout": sum(1 for x in sentences if x.get("fragment")),
            "results_sentences_that_read_as_a_result_but_have_no_finding": missed}
