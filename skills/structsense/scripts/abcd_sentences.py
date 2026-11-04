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
                         r"author contributions|conflicts? of interest|funding|"
                         r"data availability|ethics statement|peer review|"
                         r"supporting information)\b", re.I)
_REF_ENTRY = re.compile(r"^[A-Z][A-Za-z'\-]+(?:\s[A-Z][A-Za-z'\-]+)?,\s+(?:[A-Z]\.\s*)+.{0,200}?"
                        r"\b(?:19|20)\d{2}[a-z]?\.")
# Pieces of reference entries: a DOI link, "no. 8: 265-271", "Journal 24, no.",
# initials lists ("M. C., R. Guinsburg").
_REF_BITS = re.compile(r"doi\.org|\bno\.\s*\d|\d+:\s*\d+\s*[\u2013-]\s*\d+|"
                       r"\b[A-Z]\.\s?[A-Z]\.,|\bet al\.,? (?:19|20)\d{2}\.|pubmed", re.I)
_TABLE_START = re.compile(r"^\s*(?:TABLE|Table)\s*([A-Z]?\d+)\b")
_FIGURE_START = re.compile(r"^\s*(?:FIGURE|Figure|Fig\.)\s*([A-Z]?\d+)\b")
# Running headers, footers, licence lines and table notes: never a claim.
_RUNNING = re.compile(r"^\s*(?:\d+\s+of\s+\d+|https?://\S+|doi:|©|This is an open "
                      r"access article|Brain and Behavior\b|Received:|Revised:|"
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


def segment(raw: str) -> List[dict]:
    """Every prose sentence from the abstract (or the start) to the back matter.

    Offsets are into ``raw`` (the NFKC text the verifier anchors against). A
    heading is a short numbered block ("3.2\\nPrenatal Exposure ...") or a named
    one ("ABSTRACT", "Discussion"); a sub-section is reported under its parent
    ("Results - 3.2 Prenatal Exposure ..."). Table and figure blocks are skipped
    until prose resumes.
    """
    blocks: List[Tuple[int, int]] = []
    pos = 0
    for m in _BLOCK.finditer(raw):
        blocks.append((pos, m.start()))
        pos = m.end()
    blocks.append((pos, len(raw)))

    started = not re.search(r"(?mi)^\s*abstract\s*$", raw)
    top, sub = "Front matter", None
    in_table: Optional[str] = None
    in_back = ""
    pending: List[Tuple[int, int, str]] = []      # prose blocks waiting to merge
    sentences: List[dict] = []

    def flush():
        if not pending:
            return
        a, b, sec = pending[0][0], pending[-1][1], pending[0][2]
        for s, e in _split_sentences(raw[a:b], a):
            txt = " ".join(raw[s:e].split())
            if len(re.findall(r"[A-Za-z]{2,}", txt)) < 3:
                continue
            sentences.append({"id": len(sentences) + 1, "section": sec,
                              "start": s, "end": e, "text": txt})
        pending.clear()

    for a, b in blocks:
        block = raw[a:b]
        flat = " ".join(block.split())
        if not flat:
            continue
        low = flat.lower().rstrip(" .:")
        if _STOP_HEADS.match(flat) and len(flat) < 60:
            # Two-column layouts can put "References" before the Conclusion, so
            # back matter is skipped until the next real heading, not the end.
            flush()
            in_back = flat.lower()
            continue
        first = block.strip().split("\n", 1)
        if first[0].strip().lower().rstrip(" .:") == "abstract" and len(first) > 1:
            flush()
            started, top, sub, in_table = True, "Abstract", None, None
            a = a + block.find(first[1]) if first[1] in block else a
            block = raw[a:b]
            flat = " ".join(block.split())
            low = flat.lower()
        if low == "abstract" or low in _NAMED_HEADS:
            flush()
            in_back = ""
            started = True
            top, sub, in_table = _NAMED_HEADS.get(low, flat.title()), None, None
            continue
        h = _HEADING.match(block)
        if h and not _SENT_END.search(flat + " X") and len(flat) < 130:
            flush()
            num, title = h.group(1), " ".join(h.group(2).split())
            name = _NAMED_HEADS.get(title.lower().rstrip(" ."))
            if "." not in num and name:
                top, sub = name, None
            elif "." not in num:
                top, sub = title, None
            else:
                sub = f"{num} {title}"
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
            if not (flat[:1].islower() or (in_back.startswith(("references", "bibliography"))
                                           and len(_SENT_END.findall(flat)) >= 2)):
                continue
        t = _TABLE_START.match(flat) or _FIGURE_START.match(flat)
        if t:
            flush()
            in_table = ("Table " if _TABLE_START.match(flat) else "Figure ") + t.group(1)
            continue
        if _RUNNING.match(flat):
            continue
        if in_table:
            # Prose resumes after a table when a block reads as sentences.
            if not (_is_prose(block) and len(_SENT_END.findall(flat + " X")) >= 1
                    and len(flat) > 120):
                continue
            in_table = None
        if not _is_prose(block):
            continue
        sec = f"{top} - {sub}" if sub else top
        if pending:
            prev = raw[pending[-1][0]:pending[-1][1]].rstrip()
            # A column or page break inside a sentence: the previous block does
            # not end one and this one starts in lower case.
            if prev and prev[-1] not in ".?!:" and flat[:1].islower():
                pending.append((a, b, pending[0][2]))
                continue
            flush()
        pending.append((a, b, sec))
    flush()
    return sentences


def sentences_overlapping(sentences: Sequence[dict], start: int, end: int) -> List[dict]:
    """Sentences sharing at least half of themselves (or of the span) with it."""
    out = []
    for s in sentences:
        ov = min(end, s["end"]) - max(start, s["start"])
        if ov <= 0:
            continue
        if ov >= 0.5 * (s["end"] - s["start"]) or ov >= 0.5 * (end - start):
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
        if _TABLE_SECTION.search(str(ev.get("section") or "")):
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
        t = _table_no((f.get("evidence") or {}).get("section"))
        if t:
            by_table.setdefault(t, []).append(f)
    aliases = alias_map(doc)
    flagged = 0
    intra_sentences = {sid for f in findings for sid in f.get("sentence_ids") or []
                       if any(_pair(str(v)) and _pair(str(v))[0].lower() == _pair(str(v))[1].lower()
                              for v in f.get("variables") or [])}
    for f in findings:
        ev = f.get("evidence") or {}
        if _table_no(ev.get("section")):
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
        t = _table_no((f.get("evidence") or {}).get("section"))
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
                         claims: Sequence[dict], *, assessed: bool) -> List[dict]:
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
        rows.append({
            "sentence_id": s["id"], "section": s["section"], "text": s["text"],
            "is_claim": is_claim, "claim_type": claim_type,
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
    return "mixed" if len(signed) == 2 or ("null" in ds and signed) else sorted(ds)[0]


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
        if _table_no(ev.get("section")) or f.get("direction") not in ("positive", "negative",
                                                                       "mixed"):
            continue
        text = f"{ev.get('context_sentence_text') or ''} {ev.get('quote') or ''}"
        if not _DIRECTION_WORDS.search(text):
            f["direction_as_extracted"] = f["direction"]
            f["direction"] = "unspecified"
            f["direction_not_in_sentence"] = True
            out.append(i)
    return out


# A Results sentence that reads as a result. Method sentences ("a linear
# regression was performed", "data is summarized in Table 1") are not.
_RESULT_CUE = re.compile(r"\b(?:associat\w*|linked|correlat\w*|predict\w*|"
                         r"significant\w*|differ\w*|alteration\w*|altered|"
                         r"higher|lower|greater|increased|decreased|reduced)\b", re.I)
_METHOD_CUE = re.compile(r"\b(?:was|were) (?:performed|used|included|assessed|applied|"
                         r"conducted|considered|obtained|categori[sz]ed|corrected)\b|"
                         r"\bsummari[sz]ed in\b|\bwas considered significant\b|"
                         r"\bwe assessed\b|\bto assess\b|\binclude[sd]?\b|"
                         r"\b(?:the most|the least) (?:frequent|common)\b", re.I)


def flag_possible_missed_claims(rows: List[dict]) -> List[int]:
    out = []
    for r in rows:
        if r["is_claim"] != "no" or not r["section"].lower().startswith("results"):
            continue
        if _RESULT_CUE.search(r["text"]) and not _METHOD_CUE.search(r["text"]):
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


def annotate(doc: dict, index, payload: dict, *, gate_scope, norm_enum) -> dict:
    """Run every sentence-level step on a verified doc, in place. Returns stats."""
    sentences = segment(index.nfkc)
    cut = one_sentence_per_finding(doc, sentences)
    mirrored = mark_mirrored_rows(doc)
    flagged = check_cited_tables(doc)
    claims, rej = verify_sentence_claims(payload, index, sentences, gate_scope, norm_enum)
    for f in doc.get("findings") or []:
        ev = f.get("evidence") or {}
        q = str(ev.get("quote") or "")
        if not _table_no(ev.get("section")):
            f.setdefault("causal_language", causal_language(q))
            if not isinstance(f.get("hedged"), bool):
                f["hedged"] = hedged(q)
    unstated = sign_not_in_sentence(doc)
    covered = {sid for f in doc.get("findings") or [] for sid in f.get("sentence_ids") or []}
    covered |= {c["sentence_id"] for c in claims}
    uncovered = sorted({o for f in doc.get("findings") or []
                        for o in (f.get("evidence") or {}).get("other_sentences_in_quote") or []}
                       - covered)
    doc["sentence_claims"] = claims
    rows = build_sentence_table(doc, sentences, claims, assessed=bool(payload.get("sentence_claims")))
    _sections_from_findings(doc, rows)
    doc["sentences"] = rows
    # A table row's section: the (corrected) section of the first sentence that
    # cites the table, then the table ("Results - 3.3 ... › Table 3").
    tsec = table_sections(doc, rows)
    for f in doc.get("findings") or []:
        ev = f.get("evidence") or {}
        t = _table_no(ev.get("section"))
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
            "results_sentences_that_read_as_a_result_but_have_no_finding": missed}
