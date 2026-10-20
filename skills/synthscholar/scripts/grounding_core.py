"""Text matching shared by ``audit_grounding.py`` and ``extract_evidence.py``.

One definition of "this quote is text of that paper" and "this number is that
number", so the extraction script and the audit cannot disagree.

Normalisation folds what does not change meaning — case, curly quotes,
whitespace, PDF line-break hyphenation, thousands separators, Unicode minus
signs — and keeps what does: the sign of a number, its decimal point, an
inequality or ``±`` before it, and a ``%`` after it. "−5 points" and "+5 points"
do not match; "p < 0.05" and "p = 0.05" do not match; "34,771" and "34771" do.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Optional

# Sections a paper read only as an abstract can still supply.
ABSTRACT_SECTIONS = {"", "abstract", "title", "summary", "keywords"}
# Share of a span's 6-token runs found in the source for it to count as a paraphrase.
PARAPHRASE = 0.5
# Values that state an absence; there is nothing in a paper to quote for them.
ABSENT_VALUES = {"", "nr", "n/r", "na", "n/a", "not reported", "not stated", "none reported",
                 "unknown", "unclear", "not applicable", "cannot tell", "not specified", "no",
                 "-", "—", "none"}
# What an ungrounded value is replaced with in a delivered review.
UNGROUNDED = "NR (not source-grounded)"

_MINUS = "−‒–—‐‑﹣－"
_DATE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")
_RANGE = re.compile(r"(?<=\d)\s*-\s*(?=\d)")
_TOKEN = re.compile(r"[<>≤≥±]|(?<![\w.])[+-]?\d+(?:\.\d+)?%?|[a-z]+")
# Measurement units kept with a number: "5 mg" is not "5 g".
_UNITS = (r"%|‰|mg/kg|mg/dl|mmol/l|µg|μg|ug|ng|mg|kg|g|ml|µl|μl|dl|l|mmol|µmol|μmol|nmol|mol|"
          r"nm|µm|μm|mm|cm|km|m|ms|msec|sec|s|min|mins|minutes?|h|hr|hrs|hours?|days?|weeks?|"
          r"months?|years?|yrs?|hz|khz|mhz|db|°c|bpm|mmhg|kcal|cal|fold|x|points?|pts?|kb|mb|gb|tb")
# A quantity: optional comparator, optional sign, number, optional unit.
_QTY = re.compile(r"(?P<op>[<>≤≥±])?\s*(?<![\w.,])(?P<sign>[+-])?(?P<num>\d+(?:\.\d+)?)(?![\d])"
                  rf"(?:\s?(?P<unit>{_UNITS})(?![\w]))?", re.I)
_URL_OR_DOI = re.compile(r"https?://\S+|\b10\.\d{4,9}/\S+", re.I)
# "12 of 40", "12/40", "12 out of 40": a count over its denominator.
_PAIR = re.compile(r"(?<![\w.])(\d[\d,]*)\s*(?:of|out of|/)\s*(\d[\d,]*)(?![\d.])", re.I)
# Words before a number that make it a label, not a quantity.
_LABEL_BEFORE = re.compile(r"(?:section|sections|table|tables|figure|figures|fig|appendix|item|items|"
                           r"rq|ec|ic|step|phase|version|v|pmid|pmids|level|question|q|domain|"
                           r"criterion|criteria|wave|arm|chapter|page|pages|ref|refs)\s*[.:#]?\s*$", re.I)


def canon(s: str) -> str:
    """Unicode-folded text with signs, ranges, dates and separators made uniform."""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)                     # PDF hyphenation
    s = re.sub(f"[{_MINUS}]", "-", s)                                  # every minus is "-"
    s = s.replace("<=", "≤").replace(">=", "≥").replace("+/-", "±").replace("+-", "±")
    s = _DATE.sub(" DATE ", s)
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)                          # 34,771 → 34771
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    s = _RANGE.sub(" to ", s)                                           # 10-20 is a range
    s = re.sub(r"(?<=\d)\s+%", "%", s)
    return s


def norm(s: str) -> str:
    """Tokens joined by spaces: words lower-cased, numbers with sign/decimal/%, comparators."""
    s = canon(s).lower()
    s = re.sub(r"[‘’“”'\"`´]", "", s)
    return " ".join(_TOKEN.findall(s))


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="replace")).hexdigest()


def source_text(a: dict) -> str:
    """Everything stored for a paper: title, abstract, full text."""
    return "\n".join(x for x in (a.get("title"), a.get("abstract"), a.get("full_text")) if x)


def text_sha256(a: dict) -> str:
    """Hash of what was *read* of a paper (abstract + full text; not the title,
    which a metadata correction may change without a new text being read)."""
    return sha256_text("\n".join(x for x in (a.get("abstract"), a.get("full_text")) if x))


def locate(quote: str, source: str, nsource: Optional[str] = None) -> dict:
    """Where ``quote`` occurs in ``source``: exact / normalized / paraphrase / absent."""
    q = (quote or "").strip().strip("\"'“”‘’")
    if not q:
        return {"match": "absent", "coverage": 0.0}
    i = source.find(q)
    if i >= 0:
        return {"match": "exact", "offset": i, "length": len(q), "coverage": 1.0}
    nq = norm(q)
    ns = nsource if nsource is not None else norm(source)
    if nq and f" {nq} " in f" {ns} ":
        return {"match": "normalized", "normalized_offset": ns.find(nq), "coverage": 1.0}
    w = nq.split()
    grams = {" ".join(w[i:i + 6]) for i in range(max(1, len(w) - 5))}
    cov = sum(f" {g} " in f" {ns} " for g in grams) / max(1, len(grams))
    return {"match": "paraphrase" if cov >= PARAPHRASE else "absent", "coverage": round(cov, 3)}


def verbatim(quote: str, source: str, nsource: Optional[str] = None) -> bool:
    return locate(quote, source, nsource)["match"] in ("exact", "normalized")


def quantities(text: str, keep_years: bool = False) -> list[dict]:
    """Quantities in ``text`` with their comparator, sign and percent preserved.

    Not quantities: years (unless signed, compared or a percentage), labels
    ("Table 2", "RQ1", "item 4"), parts of dates and identifiers ("NKG-004").
    """
    s = _URL_OR_DOI.sub(" ", canon(text))
    out = []
    for m in _QTY.finditer(s):
        num, sign, op = m.group("num"), m.group("sign") or "", m.group("op") or ""
        unit = (m.group("unit") or "").lower().replace("μ", "µ")
        pct = unit == "%"
        unit = "" if pct else _UNIT_ALIAS.get(unit, unit)
        start = m.start("num") - len(sign)
        before = s[max(0, start - 14):start]
        if re.search(r"[A-Za-z_]-?$", before):          # NKG-004, RQ1, COVID-19, v2
            continue
        if _LABEL_BEFORE.search(before):
            continue
        if not (keep_years or op or sign or pct) and re.fullmatch(r"(19|20)\d\d", num):
            continue
        out.append({"op": op, "sign": "-" if sign == "-" else "", "value": float(num),
                    "decimals": len(num.split(".")[1]) if "." in num else 0, "pct": pct, "unit": unit,
                    "text": f"{op}{'-' if sign == '-' else ''}{num}{'%' if pct else (' ' + unit if unit else '')}",
                    "pos": m.start()})
    return out


_UNIT_ALIAS = {"msec": "ms", "sec": "s", "mins": "min", "minute": "min", "minutes": "min", "hr": "h",
               "hrs": "h", "hour": "h", "hours": "h", "day": "days", "week": "weeks", "month": "months",
               "year": "years", "yr": "years", "yrs": "years", "point": "points", "pt": "points",
               "pts": "points", "ug": "µg", "x": "fold"}


def same_quantity(a: dict, b: dict) -> bool:
    """Equal comparator, sign, value, percent — and unit, when ``a`` states one.

    ``a`` is the claimed quantity, ``b`` the one in the evidence: a claim that
    says "5 mg" needs "5 mg" in the evidence; a claim that says only "5" accepts
    "5 mg" there (it asserts less).
    """
    return (a["op"] == b["op"] and a["sign"] == b["sign"] and a["pct"] == b["pct"]
            and abs(a["value"] - b["value"]) < 1e-9
            and (not a.get("unit") or a.get("unit") == b.get("unit")))


def pairs(text: str) -> list[tuple[int, int]]:
    """Count-over-denominator pairs in ``text`` ("12 of 40", "12/40"), years excluded."""
    s = _URL_OR_DOI.sub(" ", canon(text))
    out = []
    for m in _PAIR.finditer(s):
        n, d = (int(x.replace(",", "")) for x in m.groups())
        if any(re.fullmatch(r"(19|20)\d\d", g.replace(",", "")) for g in m.groups()):
            continue                                    # "2019 of 2020", "12 of 2021" are not shares
        out.append((n, d))
    return out


def unpaired(text: str, quotes: list[str]) -> list[str]:
    """Denominator pairs of ``text`` that no single quote contains together."""
    miss = []
    for n, d in pairs(text):
        if not any(n in {int(q["value"]) for q in quantities(x, True) if not q["pct"]}
                   and d in {int(q["value"]) for q in quantities(x, True) if not q["pct"]}
                   for x in quotes):
            miss.append(f"{n} of {d}")
    return miss


# ── sections and pages ──

_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:[0-9IVX]+(?:\.\d+)*\.?\s+)?(abstract|summary|introduction|background|"
    r"(?:materials and |patients and |subjects and )?methods?|methodology|experimental(?: section| procedures)?|"
    r"results(?: and discussion)?|findings|discussion|conclusions?|concluding remarks|limitations|"
    r"references|acknowledg\w*|supplementary\b.*)\s*[:.]?\s*$", re.I | re.M)
_CANON_SECTION = [
    (r"abstract|summary", {"abstract"}), (r"introduction|background", {"introduction"}),
    (r"results and discussion", {"results", "discussion"}),
    (r"method|experimental|materials", {"methods"}), (r"result|finding", {"results"}),
    (r"discussion|limitation", {"discussion"}), (r"conclu", {"conclusion"}),
    (r"reference|acknowledg|supplementary", {"back"}),
]


def canonical_sections(name: str) -> set[str]:
    n = (name or "").strip().lower()
    for pat, out in _CANON_SECTION:
        if re.search(pat, n):
            return out
    return {"title"} if n == "title" else set()


def sections_of(a: dict) -> list[tuple[set[str], str]]:
    """``(canonical names, text)`` for each part of a stored paper. The text before
    the first heading counts as the abstract (most full texts open with it)."""
    parts: list[tuple[set[str], str]] = [({"title"}, a.get("title") or ""), ({"abstract"}, a.get("abstract") or "")]
    ft = a.get("full_text") or ""
    heads = list(_HEADING.finditer(ft))
    if not heads:
        return parts + ([(set(), ft)] if ft else [])
    parts.append(({"abstract", "front"}, ft[:heads[0].start()]))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(ft)
        parts.append((canonical_sections(h.group(1)), ft[h.end():end]))
    return parts


def check_section(claimed: str, quote: str, a: dict) -> str:
    """``ok`` / ``mismatch`` (the quote is elsewhere) / ``unverifiable`` (the paper
    has no detectable sections) / ``skip`` (no section claimed)."""
    want = canonical_sections(claimed)
    if not want:
        return "skip"
    parts = sections_of(a)
    found = [names for names, text in parts if text and verbatim(quote, text)]
    if any(names & want for names in found):
        return "ok"
    # The stated section can only be contradicted if the paper has a detectable
    # heading for it; otherwise its text may sit under a heading we do not know.
    if not any(names & want for names, text in parts if text):
        return "unverifiable"
    return "mismatch" if found else "unverifiable"


def check_page(page: Any, quote: str, page_texts: Optional[list[str]]) -> str:
    """``ok`` / ``mismatch`` / ``unverifiable`` (no page map) / ``skip`` (no page claimed)."""
    if page in (None, "", 0):
        return "skip"
    if not page_texts:
        return "unverifiable"
    try:
        n = int(str(page).split("-")[0].strip())
    except ValueError:
        return "mismatch"
    if not 1 <= n <= len(page_texts):
        return "mismatch"
    here = page_texts[n - 1]
    nxt = page_texts[n] if n < len(page_texts) else ""
    if verbatim(quote, here):
        return "ok"
    # a quote may run onto the next page, but must start on the stated one
    if nxt and verbatim(quote, here + "\n" + nxt) and not verbatim(quote, nxt):
        return "ok"
    return "mismatch"


def quantity_in(q: dict, text: str) -> bool:
    """``q`` (sign, comparator, % included) occurs as a quantity of ``text``."""
    return any(same_quantity(q, x) for x in quantities(text, keep_years=True))


def missing_quantities(value: str, quotes: list[str]) -> list[str]:
    """Quantities of ``value`` that none of ``quotes`` contains."""
    joined = "\n".join(quotes)
    return [q["text"] for q in quantities(value) if not quantity_in(q, joined)]


def is_absent(value) -> bool:
    """The value states that nothing was reported ("NR", "None (non-disease resource)")."""
    v = str(value if value is not None else "").strip()
    if v == UNGROUNDED:
        return True
    head = re.sub(r"\s*[(\[].*$", "", v).strip().lower().rstrip(".;:")
    return head in ABSENT_VALUES


def rubric_for(review: dict, arts: dict) -> dict[str, dict]:
    """Included study → its charting rubric, matched by DOI, then title, then position.

    The rubric carries its own (LLM-extracted) metadata and a ``source_id`` that
    is not the study id, so the link is re-derived rather than trusted.
    """
    rubrics = review.get("data_charting_rubrics") or []
    by_doi = {_doi(r.get("doi")): r for r in rubrics if r.get("doi")}
    by_title = {norm(r.get("title") or ""): r for r in rubrics if r.get("title")}
    out = {}
    for i, (pid, a) in enumerate(arts.items()):
        r = by_doi.get(_doi(a.get("doi"))) or by_title.get(norm(a.get("title") or ""))
        if r is None and len(rubrics) == len(arts):
            r = rubrics[i]
        if r is not None:
            out[pid] = r
    return out


def _doi(d) -> str:
    d = str(d or "").strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d)
