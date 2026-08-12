"""Strict verification for ABCD/HBCD extraction — nothing survives on trust.

Four independent gates. An item must pass gates 1 and 2 to appear at all; gates 3
and 4 decide what it may be *called*.

  1. EVIDENCE ANCHORING (hard). Every item carries `evidence.quote` plus
     `start`/`end` offsets into the paper text. We require
     `text[start:end] == quote` after whitespace/ligature normalisation, and that
     the item's own surface form occurs inside that quote. A quote that cannot be
     found verbatim is not "close enough" — the item is rejected, with a reason.
     Offsets may be re-anchored (the quote is searched for in the text) because
     an LLM miscounting characters is expected; inventing the quote is not.

  2. OWN-STUDY SCOPE (hard). A paper's introduction and discussion are full of
     other studies' variables and other studies' results. Those are not what this
     paper did, and folding them into a synthesis would double-count the
     literature: paper A's summary of paper B would arrive as independent
     evidence. An item survives only if its evidence is the paper speaking about
     its own analysis — a Method/Results/Table section, or first-person framing —
     and findings are held to the stricter bar of the two.

  3. DICTIONARY GATE (variables). A string is only called an ABCD/HBCD *variable*
     if it resolves against a real release snapshot: by name
     (`abcd_dictionary.Dictionary.resolve`), or by wording-in-context
     (`abcd_context.ContextIndex.match`, which maps "youth-reported family
     conflict" to `fes_y_ss_fc` using the respondent, metric and instrument the
     paper stated). Everything else is kept and marked — visible, never presented
     as canonical.

  4. CONSTRUCT GATE (constructs). A construct only carries a Cognitive Atlas id
     that came back from a live/cached lookup. A fabricated `trm_` id is demoted
     the way `iri_validation.py` demotes bad IRIs.

Rejections are returned, not silently dropped: the caller writes them into
`rejected[]` so a reader can see what the model claimed and why it did not stand.
"""
from __future__ import annotations

# Run either way: `python -m scripts.<mod>` from the skill directory, or
# `python /abs/path/to/scripts/<mod>.py` from anywhere. Without this, running the
# file directly fails with ModuleNotFoundError: scripts — which forces callers to
# cd into the skill first, for no reason.
if __package__ in (None, ""):  # executed as a file, not as part of the package
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from scripts import cognitive_atlas as ca_mod
from scripts.abcd_dictionary import Dictionary, looks_like_variable_name

# Minimum quote length. Shorter "quotes" cannot establish that a claim is
# supported by the paper — a 6-character fragment matches by accident.
MIN_QUOTE_CHARS = 25

_WS = re.compile(r"\s+")
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "–": "-", "—": "-", "‘": "'",
    "’": "'", "“": '"', "”": '"', " ": " ",
}

ROLES = ("predictor", "outcome", "mediator", "moderator", "covariate",
         "confounder", "control", "instrument", "unspecified")

DIRECTIONS = ("positive", "negative", "null", "mixed", "unspecified")


def normalize(s: str) -> str:
    """Fold PDF artefacts so a real quote matches the extracted text.

    PDF text layers introduce ligatures, non-breaking spaces, and line-wrap
    whitespace that no model reproduces exactly. Folding those is not leniency
    about *content* — the characters still have to be there, in order.
    """
    s = unicodedata.normalize("NFKC", s or "")
    for bad, good in _LIGATURES.items():
        s = s.replace(bad, good)
    return _WS.sub(" ", s).strip()


# --------------------------------------------------------------------------- #
# gate 1: evidence anchoring
# --------------------------------------------------------------------------- #

class TextIndex:
    """A paper's text plus the offset map needed to re-anchor quotes.

    Verification runs against the normalised text, but reported offsets point at
    the ORIGINAL text, so a reader can slice the source file and see the quote.
    """

    def __init__(self, text: str):
        self.raw = text or ""
        self.norm, self._map = self._build(self.raw)
        self._lower = self.norm.lower()

    @staticmethod
    def _build(raw: str) -> Tuple[str, List[int]]:
        out: List[str] = []
        idx: List[int] = []
        prev_space = False
        for i, ch in enumerate(unicodedata.normalize("NFKC", raw)):
            ch = _LIGATURES.get(ch, ch)
            if ch.isspace():
                if prev_space or not out:
                    continue
                out.append(" ")
                idx.append(i)
                prev_space = True
            else:
                out.append(ch)
                idx.append(i)
                prev_space = False
        while out and out[-1] == " ":
            out.pop()
            idx.pop()
        return "".join(out), idx

    def find(self, quote: str) -> Optional[Tuple[int, int, int, int]]:
        """Locate `quote`. Returns (norm_start, norm_end, raw_start, raw_end)."""
        q = normalize(quote)
        if not q:
            return None
        pos = self._lower.find(q.lower())
        if pos < 0:
            return None
        end = pos + len(q)
        raw_start = self._map[pos] if pos < len(self._map) else 0
        raw_end = (self._map[end - 1] + 1) if end - 1 < len(self._map) else len(self.raw)
        return pos, end, raw_start, raw_end

    def occurrences(self, quote: str, limit: int = 8) -> List[int]:
        q = normalize(quote).lower()
        out, start = [], 0
        while len(out) < limit:
            pos = self._lower.find(q, start)
            if pos < 0:
                break
            out.append(pos)
            start = pos + 1
        return out

    def context(self, norm_start: int, norm_end: int, window: int = 240) -> str:
        """The sentence-ish window around a span — the `used_context` field."""
        lo = max(0, norm_start - window)
        hi = min(len(self.norm), norm_end + window)
        chunk = self.norm[lo:hi]
        # Trim to sentence boundaries where we can find them.
        first = chunk.find(". ", 0, max(1, norm_start - lo))
        if first != -1:
            chunk = chunk[first + 2:]
        tail = chunk.rfind(". ")
        if tail > len(chunk) // 2:
            chunk = chunk[: tail + 1]
        return chunk.strip()


def verify_evidence(item: dict, index: TextIndex, *,
                    surface_keys: Sequence[str] = ("name", "variable", "term"),
                    require_surface: bool = True,
                    require_any: Sequence[str] = (),
                    ) -> Tuple[Optional[dict], Optional[str]]:
    """Anchor one item's evidence. Returns (verified_item, rejection_reason).

    On success the item gains a normalised `evidence` block: exact quote, offsets
    into the original text, the surrounding `used_context`, and how the offsets
    were obtained (`as_reported` or `re_anchored`).

    The surface requirement is per-section, because "present in the paper" means
    different things for different claims:

      * A **variable** must appear literally — `require_surface=True`. If the
        paper never writes `nihtbx_flanker_uncorrected`, we have no business
        saying it used that variable.
      * A **construct** is an interpretation of the prose ("executive function
        was indexed by...") and legitimately never appears verbatim, so
        `require_surface=False`. We still record `label_in_quote` so a reader can
        tell a verbatim mention from a mapped reading.
      * A **finding** statement is a paraphrase, so instead of the statement we
        require at least one of the variables it references (`require_any`) to be
        present in the quote. That keeps the claim tied to the text without
        demanding the model quote its own sentence back.
    """
    ev = item.get("evidence") or {}
    quote = ev.get("quote") or item.get("quote") or ""
    if not quote or not str(quote).strip():
        return None, "missing_quote"

    quote_n = normalize(str(quote))
    if len(quote_n) < MIN_QUOTE_CHARS:
        return None, f"quote_too_short(<{MIN_QUOTE_CHARS}chars)"

    found = index.find(quote_n)
    if not found:
        return None, "quote_not_found_in_paper"
    n_start, n_end, r_start, r_end = found

    reported = ev.get("start")
    method = "as_reported"
    if isinstance(reported, int):
        # Trust reported offsets only if they actually land on the quote.
        raw_slice = normalize(index.raw[reported: reported + len(str(quote))])
        if raw_slice.lower() != quote_n.lower():
            method = "re_anchored"
    else:
        method = "re_anchored"

    def _in_quote(needle: str) -> bool:
        """Is `needle` present in the quote, tolerating PDF mangling?

        Compared both as-is and flattened to alphanumerics, so
        `ab_g_dyn__visit_type` still matches when the PDF renders it as
        `ab g dyn visit type`.
        """
        n = normalize(needle).lower()
        if not n:
            return False
        if n in quote_n.lower():
            return True
        flat = re.sub(r"[^a-z0-9]", "", n)
        hay = re.sub(r"[^a-z0-9]", "", quote_n.lower())
        return bool(flat) and flat in hay

    surface = next((str(item[k]) for k in surface_keys if item.get(k)), "")
    label_in_quote = _in_quote(surface) if surface else None
    if require_surface and surface and not label_in_quote:
        return None, "surface_form_absent_from_quote"

    matched_refs = [r for r in require_any if _in_quote(str(r))]
    if require_any and not matched_refs:
        return None, "no_referenced_variable_in_quote"

    out = dict(item)
    occ = index.occurrences(quote_n)
    out["evidence"] = {
        **{k: v for k, v in ev.items() if k not in ("quote", "start", "end")},
        "quote": index.norm[n_start:n_end],
        "start": r_start,
        "end": r_end,
        "used_context": index.context(n_start, n_end),
        "anchor_method": method,
        "occurrences_in_paper": len(occ),
        "label_in_quote": label_in_quote,
        "referenced_variables_in_quote": matched_refs or None,
        "verified": True,
    }
    return out, None


# --------------------------------------------------------------------------- #
# gate 2: own-study scope
# --------------------------------------------------------------------------- #

# A citation attached to a claim means the claim belongs to somebody else.
# The opening "\(" belongs to the FIRST alternative only. Hoisting it in front of the
# group — as this pattern did — silently killed the other two: a narrative citation
# ("Telzer and Fuligni (2013) found ...") and a numeric one ("[12]") can only start
# with "(" if the sentence literally opens one, so they never matched real text and
# gate 2 saw no citation at all. That is the failure mode this gate exists to prevent:
# an Introduction sentence reporting somebody else's result passes as the paper's own
# unless some other cue happens to fire.
_CITATION_RE = re.compile(
    r"\([A-Z][A-Za-z\-']+(?:\s+(?:et\s+al\.?|and|&)\s+[A-Z][A-Za-z\-']+)?"
    r"(?:\s+et\s+al\.?)?,?\s*(?:19|20)\d{2}[a-z]?(?:;[^)]*)?\)"      # (Smith, 2019)
    # Narrative form. "&" is as common as "and" in APA-style prose
    # ("Tezler & Fugilini (2013) found"), and a single-author narrative citation
    # ("Steinberg (2001) warned") carries exactly the same meaning, so both count.
    r"|\b[A-Z][A-Za-z\-']+(?:\s+(?:et\s+al\.?|and|&)\s+[A-Z][A-Za-z\-']+)?"
    r"(?:\s+et\s+al\.?)?\s*\((?:19|20)\d{2}[a-z]?\)"                 # Smith et al. (2019)
    r"|\[\d{1,3}(?:[,\-–]\s*\d{1,3})*\]"                             # [12], [3-5]
)
# The paper talking about its own analysis.
_OWN_STUDY_RE = re.compile(
    r"\b(?:we|our|the (?:present|current|this) (?:study|analysis|sample|paper)|"
    r"in the (?:present|current) (?:study|analysis)|this study|the present "
    r"investigation|here we)\b|\bresults? (?:showed|indicated|revealed)\b|"
    r"\bas (?:shown|reported) in (?:table|figure|fig)\b", re.I)
# The paper stating what has NOT been done — its own contribution, not a borrowed
# claim. Checked before the prior-work cues so "no prior work has considered the
# impact of family conflict on problem behaviors" is not read as somebody else's
# finding.
_GAP_STATEMENT_RE = re.compile(
    r"\b(?:no|little|few|limited|scarce|scant)\s+(?:prior\s+|previous\s+|"
    r"existing\s+)?(?:work|research|studies|study|evidence|data|attention)\b"
    r"|\bhas\s+not\s+(?:yet\s+)?been\s+(?:examined|tested|studied|explored|"
    r"established|investigated|addressed)"
    r"|\bhave\s+not\s+(?:yet\s+)?been\s+(?:examined|tested|studied|explored|"
    r"established|investigated|addressed)"
    r"|\bremains?\s+(?:un(?:clear|known|examined|tested)|to be (?:examined|tested))"
    r"|\bis\s+not\s+(?:yet\s+)?(?:clear|known|established)"
    r"|\bwe\s+(?:are\s+aware\s+of\s+no|know\s+of\s+no)\b", re.I)

# Prior-literature framing, even without a bracketed citation.
_PRIOR_WORK_RE = re.compile(
    r"\b(?:previous(?:ly)?|prior|earlier|existing|extant|other) "
    r"(?:studies|study|work|research|literature|findings|reports?|investigations?)"
    r"|\bhas(?:\s+been)? (?:shown|found|reported|demonstrated|associated)"
    r"|\bhave(?:\s+been)? (?:shown|found|reported|demonstrated|associated)"
    r"|\bresearch (?:suggests?|indicates?|shows?|has)"
    r"|\bit (?:is|has been) (?:well[\s-]?)?(?:established|documented|known)"
    r"|\b(?:meta[\s-]?analys[ei]s|systematic review)\b"
    r"|\bfor (?:example|instance)\b|\be\.g\.,", re.I)

# Where the paper reports what it did. Matched against the leading part of the
# extractor's section path ("Method - Measures - Financial Strain" -> "method").
_OWN_SECTIONS = ("method", "methods", "material", "measure", "result", "results",
                 "table", "figure", "analysis", "analytic", "sample",
                 "participants", "procedure", "abstract", "data")
# Where other people's work is discussed.
_LITERATURE_SECTIONS = ("introduction", "background", "discussion", "conclusion",
                        "limitation", "future", "implication", "related")


def _section_head(section: Optional[str]) -> str:
    return re.split(r"[-–—:>|/]", str(section or ""), maxsplit=1)[0].strip().lower()


def gate_scope(item: dict, *, strict: bool) -> Tuple[dict, Optional[str]]:
    """Decide whether this item is about the paper's own study.

    `strict` is for findings: a result only counts as this paper's if it is
    reported in a results-bearing section or framed in the first person. Variables
    are held to a looser bar — a measure named in the introduction and used in the
    analysis is still this paper's measure — but a measure that appears only inside
    a citation of prior work is not.

    Returns (item with `scope` recorded, rejection reason or None).
    """
    out = dict(item)
    ev = out.get("evidence") or {}
    quote = str(ev.get("quote") or "")
    context = str(ev.get("used_context") or "")
    head = _section_head(ev.get("section"))
    blob = f"{quote} {context}"

    # "no prior work has considered X" is the paper motivating its own study, not a
    # claim borrowed from anybody. Without this the gate rejected the construct in
    # this paper's own title, matching on the words "prior work".
    negated_gap = bool(_GAP_STATEMENT_RE.search(quote))
    cited = bool(_CITATION_RE.search(quote))
    cited_ctx = bool(_CITATION_RE.search(context))
    own_words = bool(_OWN_STUDY_RE.search(blob))
    prior_words = bool(_PRIOR_WORK_RE.search(quote)) and not negated_gap
    in_own_section = any(head.startswith(s) for s in _OWN_SECTIONS)
    in_lit_section = any(head.startswith(s) for s in _LITERATURE_SECTIONS)

    signals = {
        "section": head or None,
        "section_class": ("own_study" if in_own_section else
                          "literature" if in_lit_section else "unknown"),
        "citation_in_quote": cited,
        "citation_in_context": cited_ctx,
        "own_study_phrasing": own_words,
        "prior_work_phrasing": prior_words,
        "gap_statement": negated_gap,
    }

    if strict:
        # A finding must be this paper's own result.
        if not (in_own_section or own_words):
            out["scope"] = "cited_work"
            out["scope_signals"] = signals
            return out, "finding_not_from_this_study"
        if (cited or prior_words) and not own_words:
            out["scope"] = "cited_work"
            out["scope_signals"] = signals
            return out, "finding_attributed_to_cited_work"
    else:
        # A variable/construct only fails if the evidence is purely somebody
        # else's work: a literature section, a citation, and no first-person cue.
        if in_lit_section and (cited or prior_words) and not own_words:
            out["scope"] = "cited_work"
            out["scope_signals"] = signals
            return out, "measure_only_mentioned_in_cited_work"

    out["scope"] = "own_study"
    out["scope_signals"] = signals
    return out, None


# --------------------------------------------------------------------------- #
# gate 3: dictionary gate for variables
# --------------------------------------------------------------------------- #

# Statuses that carry a real table/domain, in descending strength.
MAPPED_STATUSES = ("verified", "verified_via_nda_api", "context_variable",
                   "context_family", "context_domain", "instrument_table")


def gate_variable(item: dict, dictionary: Optional[Dictionary], *,
                  context_index: Optional["Any"] = None,
                  releases: Optional[Sequence[str]] = None,
                  nda_release: Optional[str] = None,
                  study: Optional[str] = None,
                  nda: Optional["Any"] = None) -> dict:
    """Attach dictionary verification to a variable item (never drops it).

    Four sources, tried in order of how much they prove:

      1. the literal name the paper printed (`nihtbx_cryst_fc`)
      2. the same name confirmed live by the NDA element API, when it is absent
         from every loaded snapshot — usually a release we do not have
      3. the paper's *wording*, matched against dictionary labels in context
      4. NDA's own full-text element search, restricted to this study's tables

    Every outcome records how it was reached, what the alternatives were and which
    releases it holds in, so a reader can disagree with a specific step.
    """
    out = dict(item)
    candidate = str(item.get("name") or item.get("variable") or "").strip()
    # How the PAPER wrote it, kept separate from what it resolves to: the mention
    # is the evidence, the dictionary variable is the interpretation, and a reader
    # must be able to see both ("NIH Toolbox Flanker score" ->
    # nihtbx_flanker_uncorrected).
    out["mention_as_written"] = candidate
    out.setdefault("nda_or_nbdc_table", None)
    out.setdefault("nbdc_domain", None)
    out.setdefault("nbdc_sub_domain", None)
    if not dictionary:
        out["dictionary_status"] = "no_dictionary_loaded"
        out["dictionary_match"] = None
        return out

    label = str(item.get("label") or item.get("measure") or "").strip()
    hits = dictionary.resolve(candidate)
    if not hits and label:
        hits = dictionary.resolve(label)
    if hits:
        best = hits[0]
        match = best.to_dict()
        out["dictionary_status"] = "verified"
        out["dictionary_match"] = match
        out["nda_or_nbdc_table"] = match.get("nda_or_nbdc_table")
        out["nbdc_domain"] = match.get("nbdc_domain")
        out["nbdc_sub_domain"] = match.get("nbdc_sub_domain")
        out["dd_releases_containing"] = dictionary.releases_for(best.name)
        loaded = sorted({s["release"] for s in dictionary.snapshots})
        missing = [r for r in loaded
                   if r not in out["dd_releases_containing"]]
        if missing:
            # Present in some loaded releases but not others: usually a rename.
            out["dd_release_gap"] = missing
        gap = _nda_release_conflict(getattr(best, "row", None) or {}, nda_release)
        if gap:
            out["nda_release_conflict"] = gap
        return out

    # -- 3. the paper's wording, in context -------------------------------- #
    if context_index is not None:
        ev = item.get("evidence") or {}
        result = context_index.match(
            candidate,
            label=label or None,
            context=" ".join(p for p in (str(ev.get("used_context") or ""),
                                         str(ev.get("quote") or "")) if p) or None,
            instrument=str(item.get("instrument") or "").strip() or None,
            respondent=str(item.get("respondent") or "").strip().lower() or None,
            role=str(item.get("role") or "").strip().lower() or None,
            study=study,
            releases=releases,
            nda_release=nda_release,
        )
        out["context_mapping"] = result.to_dict()
        cue = (result.cues or {}).get("respondent_cue") or {}
        section = str(((item.get("evidence") or {}).get("section") or "")).strip()
        if cue.get("source") == "context" and re.match(r"table\b", section, re.I):
            # The cue was read from prose around a table row. Tables carry notes
            # that contradict the Methods often enough to be worth flagging: a
            # parent/youth mix-up here is a different measure, not a near miss.
            out["context_mapping"]["respondent_cue_from_table_note"] = True
        if result.matched:
            out["dictionary_status"] = result.status
            out["dictionary_match"] = {
                "variable": result.variable,
                "study": result.study,
                "dd_release": ",".join(result.dd_releases) or None,
                "match_method": out["context_mapping"]["match_method"],
                "match_score": out["context_mapping"]["match_score"],
                "label": result.label,
                "nda_or_nbdc_table": result.nda_or_nbdc_table,
                "nbdc_domain": result.nbdc_domain,
                "nbdc_sub_domain": result.nbdc_sub_domain,
                "nbdc_table": result.nbdc_table,
                "instrument": result.instrument,
                "family_size": result.family_size or None,
                "family_prefix": result.family_prefix,
            }
            out["nda_or_nbdc_table"] = result.nda_or_nbdc_table
            out["nbdc_domain"] = result.nbdc_domain
            out["nbdc_sub_domain"] = result.nbdc_sub_domain
            out["dd_releases_containing"] = result.dd_releases
            if result.variable:
                out["dd_releases_containing"] = (
                    dictionary.releases_for(result.variable) or result.dd_releases)
            return out
        # Not matched, but the matcher may still have a more precise verdict than
        # "not a variable name" — `ambiguous` means it found candidates and refused
        # to choose, which is a different thing for a reader to act on.
        if result.status == "ambiguous":
            out["dictionary_status"] = "ambiguous"
            out["dictionary_match"] = None
            return out

    # -- 2/4. ask NDA, if the caller enabled it ---------------------------- #
    if nda is not None:
        api = _ask_nda(candidate, label, dictionary, nda)
        if api:
            out.update(api)
            if api.get("dictionary_status"):
                return out

    out["dictionary_status"] = (
        "unverified_variable" if looks_like_variable_name(candidate)
        else "not_a_variable_name"
    )
    out["dictionary_match"] = None
    return out


def _ask_nda(candidate: str, label: str, dictionary: Optional[Dictionary],
             nda: "Any") -> Optional[dict]:
    """NDA API fallback: confirm a printed name, or search on the wording.

    A search hit is only usable if every one of its structures belongs to this
    study's dictionary — `search_in_study` enforces that — and if the surviving
    hits agree on one table. NDA ranks admin elements ("Number Answered") highly,
    so those are dropped before the agreement test.
    """
    try:
        if looks_like_variable_name(candidate):
            hit = nda.element(candidate)
            if hit:
                tables = [t for t in hit.get("data_structures") or []]
                return {
                    "dictionary_status": "verified_via_nda_api",
                    "dictionary_match": {
                        "variable": hit["name"],
                        "label": hit.get("description"),
                        "match_method": "nda_element_api",
                        "match_score": 1.0,
                        "nda_or_nbdc_table": tables[0] if tables else None,
                        "nda_data_structures": tables,
                        "aliases": hit.get("aliases") or [],
                        "source": hit.get("source"),
                        "retrieved_at": hit.get("retrieved_at"),
                    },
                    "nda_or_nbdc_table": tables[0] if tables else None,
                    "nbdc_domain": None,
                    "nbdc_sub_domain": None,
                    "dd_releases_containing": [],
                    "nda_api_note": ("name confirmed by NDA but absent from every "
                                     "loaded snapshot — likely a release we do "
                                     "not have"),
                }
            return None

        # NO mapping is claimed from a full-text search. Every table NDA can
        # return is already in the loaded snapshots, so anything the search finds
        # was seen and rejected by the context matcher a moment ago — and NDA's
        # ranking is lexical over the whole archive, which produced exactly the
        # errors you would expect: "internalizing behaviors" -> an ADULT Behavior
        # Checklist score, "financial strain" -> a life-events item about a
        # parent's finances, "age at time of scan" -> an SST series timestamp.
        # The hits are recorded as suggestions a human can follow up; they are not
        # evidence that the paper used that variable.
        query = " ".join(p for p in (candidate, label) if p)
        found = nda.search_in_study(query, dictionary, limit=8)
        hits = [h for h in found["hits"]
                if not _NDA_ADMIN_RE.search(str(h.get("description") or ""))]
        if not hits:
            return None
        return {
            "nda_api_suggestions": {
                "query": query,
                "note": ("candidates from NDA full-text search, NOT a mapping — "
                         "the offline matcher already rejected these tables for "
                         "this wording"),
                "dropped_outside_study": found["dropped_outside_study"],
                "hits": [{"variable": h["name"], "label": h.get("description"),
                          "score": h.get("score"),
                          "tables": h["matched_tables"]} for h in hits[:5]],
                "source": hits[0].get("source"),
                "retrieved_at": hits[0].get("retrieved_at"),
            },
        }
    except Exception:
        # The API is a bonus, never a dependency: a network failure must leave the
        # run exactly as it would have been offline.
        return None


def _nda_release_conflict(row: dict, nda_release: Optional[str]) -> Optional[dict]:
    """Did the paper cite a variable whose structure did not ship in its release?

    A warning, never a rejection. `nda_releases` is structure-level, papers do
    misstate their release, and deleting a variable the paper plainly analysed would
    be worse than flagging it. But it is checkable now, and passing it over in
    silence is what let a 3.0 paper cite a 4.0-only variable unremarked.
    """
    if not nda_release:
        return None
    labels = [r for r in str(row.get("nda_releases") or "").split(";") if r]
    if not labels or nda_release in labels:
        return None
    return {
        "paper_states_release": nda_release,
        "structure_shipped_in": labels,
        "note": ("structure-level check: the variable's NDA structure is not listed "
                 "for the release the paper states"),
    }


_NDA_ADMIN_RE = re.compile(
    r"\b(?:number (?:of )?(?:answer\w*|miss\w*|valid\w*|total\w*|question\w*)|"
    r"date ?finished|version|language|itmcnt|theta)\b", re.I)


# --------------------------------------------------------------------------- #
# gate 3: construct gate
# --------------------------------------------------------------------------- #

def gate_construct(item: dict, atlas: Optional["ca_mod.CognitiveAtlas"]) -> dict:
    """Attach a Cognitive Atlas mapping, or record why there isn't one."""
    out = dict(item)
    claimed = str(item.get("construct_id") or "").strip()
    term = str(item.get("construct") or item.get("name") or item.get("label") or "").strip()

    hit = atlas.map_term(term) if (atlas and term) else None
    if hit:
        out.update(hit)
        if claimed and claimed != hit["construct_id"]:
            out["claimed_construct_id_discarded"] = claimed
        return out

    if claimed:
        # The model supplied an id we could not confirm. Demote it — shape alone
        # is not evidence (same stance as iri_validation.py).
        out["construct_id"] = None
        out["mapping_provenance"] = "validation_failed"
        out["demoted_claim"] = {
            "construct_id": claimed,
            "reason": "not_returned_by_cognitive_atlas_lookup",
            "shape_valid": ca_mod.valid_id(claimed),
        }
    else:
        out["construct_id"] = None
        out["mapping_provenance"] = "unmapped" if atlas else "no_atlas_loaded"
    return out


# --------------------------------------------------------------------------- #
# whole-payload verification
# --------------------------------------------------------------------------- #

def _norm_enum(value: Any, allowed: Sequence[str], default: str) -> str:
    v = str(value or "").strip().lower().replace(" ", "_")
    return v if v in allowed else default


def verify_payload(payload: dict, text: str, *,
                   dictionary: Optional[Dictionary] = None,
                   atlas: Optional["ca_mod.CognitiveAtlas"] = None,
                   context_index: Optional["Any"] = None,
                   nda: Optional["Any"] = None,
                   releases: Optional[Sequence[str]] = None,
                   nda_release: Optional[str] = None,
                   study: Optional[str] = None) -> dict:
    """Verify a whole extractor payload for one paper.

    Returns a new payload with `variables`/`models`/`findings`/`constructs` kept
    only where evidence anchored and the claim belongs to this study, plus
    `rejected[]`, a `coverage` audit and a `verification` summary.
    """
    index = TextIndex(text)
    out: Dict[str, Any] = {k: v for k, v in payload.items()
                           if k not in ("variables", "models", "findings", "constructs")}
    rejected: List[dict] = []
    counts: Dict[str, Dict[str, int]] = {}

    def run(section: str, items: Any, post=None, *, require_surface: bool = True,
            surface_keys: Sequence[str] = ("name", "variable", "term"),
            refs_from: Sequence[str] = (),
            strict_scope: bool = False) -> List[dict]:
        kept: List[dict] = []
        reasons: Dict[str, int] = {}
        for raw in items or []:
            if not isinstance(raw, dict):
                reasons["not_an_object"] = reasons.get("not_an_object", 0) + 1
                continue
            refs: List[str] = []
            for key in refs_from:
                val = raw.get(key)
                if isinstance(val, str):
                    refs.extend(v.strip() for v in re.split(r"[;,]", val) if v.strip())
                elif isinstance(val, (list, tuple)):
                    refs.extend(str(v) for v in val if v)
            item, why = verify_evidence(
                raw, index, surface_keys=surface_keys,
                require_surface=require_surface, require_any=refs,
            )
            if item is not None and not why:
                item, why = gate_scope(item, strict=strict_scope)
            if why or item is None:
                why = why or "no_evidence"
                reasons[why] = reasons.get(why, 0) + 1
                rejected.append({
                    "section": section,
                    "reason": why,
                    "claim": {k: raw.get(k) for k in
                              ("name", "variable", "construct", "statement", "role",
                               "direction") if raw.get(k) is not None},
                    "claimed_quote": (str((raw.get("evidence") or {}).get("quote")
                                          or raw.get("quote") or ""))[:300] or None,
                })
                continue
            if post:
                item = post(item)
            kept.append(item)
        counts[section] = {"kept": len(kept), "rejected": sum(reasons.values()),
                           **{f"reason_{k}": v for k, v in sorted(reasons.items())}}
        return kept

    # A variable name must appear literally in its quote.
    out["variables"] = run(
        "variables", payload.get("variables"),
        lambda it: gate_variable(it, dictionary, context_index=context_index,
                                 releases=releases, nda_release=nda_release,
                                 study=study, nda=nda),
        require_surface=True, surface_keys=("name", "variable", "term"))
    # A construct is a reading of the prose — the label need not be verbatim, but
    # the quote must exist and `label_in_quote` records which case it was.
    out["constructs"] = run("constructs", payload.get("constructs"),
                            lambda it: gate_construct(it, atlas),
                            require_surface=False,
                            surface_keys=("construct", "label", "name"))
    # A model spec is a paraphrase; tie it to the text via any variable it names.
    out["models"] = run("models", payload.get("models"), _normalize_model,
                        require_surface=False, surface_keys=(),
                        refs_from=("predictors", "outcomes", "mediators",
                                   "moderators", "covariates"))
    # A finding statement is a paraphrase; require one referenced variable — and,
    # unlike a measure, it must be THIS paper's result (gate 2, strict).
    out["findings"] = run("findings", payload.get("findings"),
                          lambda it: _normalize_finding(it, atlas),
                          require_surface=False, surface_keys=(),
                          refs_from=("variables", "variable", "predictor",
                                     "outcome", "mediator", "moderator"),
                          strict_scope=True)
    out["variables"], merged_n = _merge_duplicate_variables(out["variables"])
    out["rejected"] = rejected
    out["coverage"] = _coverage_audit(out)

    by_status: Dict[str, int] = {}
    for v in out["variables"]:
        key = str(v.get("dictionary_status") or "unknown")
        by_status[key] = by_status.get(key, 0) + 1
    mapped = sum(n for s, n in by_status.items() if s in MAPPED_STATUSES)
    named = sum(1 for v in out["variables"]
                if (v.get("dictionary_match") or {}).get("variable"))
    with_table = sum(1 for v in out["variables"] if v.get("nda_or_nbdc_table"))
    out["verification"] = {
        "min_quote_chars": MIN_QUOTE_CHARS,
        "by_section": counts,
        # Kept under the old names so existing readers do not break; "verified"
        # still means a literal dictionary name.
        "variables_dictionary_verified": by_status.get("verified", 0),
        "variables_unverified": len(out["variables"]) - mapped,
        "variables_mapped_any_method": mapped,
        "variables_resolved_to_a_variable": named,
        "variables_with_table": with_table,
        "variables_by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "constructs_mapped": sum(1 for c in out["constructs"] if c.get("construct_id")),
        "constructs_unmapped": sum(1 for c in out["constructs"]
                                   if not c.get("construct_id")),
        "rejected_total": len(rejected),
        "variables_merged_as_duplicates": merged_n,
        "variables_with_nda_release_conflict": sum(
            1 for v in out["variables"] if v.get("nda_release_conflict")),
        "rejected_as_cited_work": sum(
            1 for r in rejected
            if str(r.get("reason") or "").startswith(("finding_not_from",
                                                      "finding_attributed",
                                                      "measure_only"))),
    }
    return out


# Ranked best-first: a merged entry keeps the strongest mapping any of its
# duplicates achieved, so one wording resolving and another not stops producing two
# rows that disagree about the same measure.
_STATUS_RANK = {s: i for i, s in enumerate(
    ("verified", "verified_via_nda_api", "context_variable", "context_family",
     "context_domain", "instrument_table", "ambiguous", "unverified_variable",
     "not_a_variable_name", "no_dictionary_loaded"))}


def _merge_duplicate_variables(variables: List[dict]) -> Tuple[List[dict], int]:
    """One entry per (variable, timepoint) — the extractor's own definition.

    A paper writes "internalizing behaviors" in its Methods and "internalizing
    behavior" in its Results, and the extractor emits both. They are one variable:
    left separate, one of them resolves to a table and the other does not, and the
    synthesis shows a measure that half the paper apparently did not use.

    Timepoint is part of the key on purpose — family conflict at year 1 and at year
    2 ARE distinct quantities, and the prompt asks for them separately.
    """
    groups: Dict[Tuple[str, str], List[dict]] = {}
    order: List[Tuple[str, str]] = []
    for v in variables:
        key = (_norm_key(v.get("name") or v.get("variable")),
               _norm_key(v.get("timepoint")))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(v)

    out: List[dict] = []
    merged = 0
    for key in order:
        items = groups[key]
        if len(items) == 1:
            out.append(items[0])
            continue
        merged += len(items) - 1
        best = min(items, key=lambda v: _STATUS_RANK.get(
            str(v.get("dictionary_status")), 99))
        winner = dict(best)
        others = [v for v in items if v is not best]
        winner["also_written_as"] = sorted({
            str(v.get("mention_as_written") or v.get("name"))
            for v in others} - {str(winner.get("mention_as_written"))})
        winner["merged_from"] = [
            {"mention_as_written": v.get("mention_as_written"),
             "role": v.get("role"),
             "dictionary_status": v.get("dictionary_status"),
             "evidence": {k: (v.get("evidence") or {}).get(k)
                          for k in ("section", "page", "quote")}}
            for v in others
        ]
        roles = [str(v.get("role") or "unspecified") for v in items]
        claimed = [r for r in roles if r != "unspecified"]
        if claimed and str(winner.get("role") or "unspecified") == "unspecified":
            # Keep a stated role over an unstated one; a merge must not lose the
            # only role claim the paper made.
            winner["role"] = claimed[0]
        winner["merged_roles"] = sorted(set(roles))
        out.append(winner)
    return out, merged


def _coverage_audit(doc: dict) -> dict:
    """What the models and findings mention but the variable list never declared.

    The extraction is meant to enumerate every variable the study used. When a
    model lists `family income` as a covariate and no variable entry exists for it,
    that is a hole in the extraction, not a modelling detail — and it silently
    becomes a synthesis row with no table, no domain and no provenance. Naming the
    gap is the only way it gets fixed.
    """
    declared = {_norm_key(v.get("name") or v.get("variable"))
                for v in doc.get("variables") or []}
    declared.discard("")
    referenced: Dict[str, set] = {}
    for m in doc.get("models") or []:
        for key in ("predictors", "outcomes", "mediators", "moderators",
                    "covariates"):
            for name in m.get(key) or []:
                k = _norm_key(name)
                if k:
                    referenced.setdefault(k, set()).add(str(name).strip())
    for f in doc.get("findings") or []:
        for name in f.get("variables") or []:
            k = _norm_key(name)
            if k:
                referenced.setdefault(k, set()).add(str(name).strip())

    missing = sorted(k for k in referenced if k not in declared)
    return {
        "variables_declared": len(declared),
        "variables_referenced": len(referenced),
        "referenced_but_not_declared": [
            {"key": k, "as_written": sorted(referenced[k])} for k in missing
        ],
        "declared_coverage": (
            round(1 - len(missing) / len(referenced), 3) if referenced else None),
        "note": ("Every variable the study analysed should appear in `variables` "
                 "with its own quote. Entries listed here were named in a model or "
                 "finding but never declared, so they carry no evidence, no table "
                 "and no domain."),
    }


def _norm_key(value: Any) -> str:
    """Grouping key for a variable mention: case, spacing and plural folded."""
    text = _WS.sub(" ", str(value or "").strip().lower()).strip(" .,;:()[]")
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith(("ss", "us", "is")):
        return text[:-1]
    return text


def _normalize_model(item: dict) -> dict:
    out = dict(item)
    for key in ("predictors", "outcomes", "mediators", "moderators", "covariates"):
        val = out.get(key)
        if isinstance(val, str):
            out[key] = [v.strip() for v in re.split(r"[;,]", val) if v.strip()]
        elif val is None:
            out[key] = []
    return out


def _normalize_finding(item: dict, atlas) -> dict:
    out = dict(item)
    out["direction"] = _norm_enum(out.get("direction"), DIRECTIONS, "unspecified")
    out["role"] = _norm_enum(out.get("role"), ROLES, "unspecified")
    if out.get("construct") and atlas:
        hit = atlas.map_term(str(out["construct"]))
        if hit:
            out["construct_id"] = hit["construct_id"]
            out["construct_label"] = hit["construct_label"]
            out["mapping_provenance"] = "tool"
        else:
            out["construct_id"] = None
            out["mapping_provenance"] = "unmapped"
    return out
