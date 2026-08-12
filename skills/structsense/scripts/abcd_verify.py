"""Strict verification for ABCD/HBCD extraction — nothing survives on trust.

Three independent gates. An item must pass gate 1 to appear at all; gates 2 and 3
decide what it may be *called*.

  1. EVIDENCE ANCHORING (hard). Every item carries `evidence.quote` plus
     `start`/`end` offsets into the paper text. We require
     `text[start:end] == quote` after whitespace/ligature normalisation, and that
     the item's own surface form occurs inside that quote. A quote that cannot be
     found verbatim is not "close enough" — the item is rejected, with a reason.
     Offsets may be re-anchored (the quote is searched for in the text) because
     an LLM miscounting characters is expected; inventing the quote is not.

  2. DICTIONARY GATE (variables). A string is only called an ABCD/HBCD *variable*
     if `abcd_dictionary.Dictionary.resolve()` finds it in a real release
     snapshot. Otherwise it is kept but marked `unverified_variable` — visible,
     never presented as canonical.

  3. CONSTRUCT GATE (constructs). A construct only carries a Cognitive Atlas id
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
# gate 2: dictionary gate for variables
# --------------------------------------------------------------------------- #

def gate_variable(item: dict, dictionary: Optional[Dictionary]) -> dict:
    """Attach dictionary verification to a variable item (never drops it)."""
    out = dict(item)
    candidate = str(item.get("name") or item.get("variable") or "").strip()
    # How the PAPER wrote it, kept separate from what it resolves to: the mention
    # is the evidence, the dictionary variable is the interpretation, and a reader
    # must be able to see both ("NIH Toolbox Flanker score" ->
    # nihtbx_flanker_uncorrected).
    out["mention_as_written"] = candidate
    if not dictionary:
        out["dictionary_status"] = "no_dictionary_loaded"
        out["dictionary_match"] = None
        return out

    hits = dictionary.resolve(candidate)
    if not hits:
        label = str(item.get("label") or item.get("measure") or "").strip()
        if label:
            hits = dictionary.resolve(label)
    if hits:
        best = hits[0]
        out["dictionary_status"] = "verified"
        out["dictionary_match"] = best.to_dict()
        out["nda_or_nbdc_table"] = out["dictionary_match"].get("nda_or_nbdc_table")
        out["nbdc_domain"] = out["dictionary_match"].get("nbdc_domain")
        out["nbdc_sub_domain"] = out["dictionary_match"].get("nbdc_sub_domain")
        out["dd_releases_containing"] = dictionary.releases_for(best.name)
        others = dictionary.releases_for(best.name)
        loaded = sorted({s["release"] for s in dictionary.snapshots})
        missing = [r for r in loaded if r not in others]
        if missing:
            # Present in some loaded releases but not others: usually a rename.
            out["dd_release_gap"] = missing
    else:
        out["dictionary_status"] = (
            "unverified_variable" if looks_like_variable_name(candidate)
            else "not_a_variable_name"
        )
        out["dictionary_match"] = None
        out["nda_or_nbdc_table"] = None
        out["nbdc_domain"] = None
        out["nbdc_sub_domain"] = None
    return out


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
                   atlas: Optional["ca_mod.CognitiveAtlas"] = None) -> dict:
    """Verify a whole extractor payload for one paper.

    Returns a new payload with `variables`/`models`/`findings`/`constructs` kept
    only where evidence anchored, plus `rejected[]` and a `verification` summary.
    """
    index = TextIndex(text)
    out: Dict[str, Any] = {k: v for k, v in payload.items()
                           if k not in ("variables", "models", "findings", "constructs")}
    rejected: List[dict] = []
    counts: Dict[str, Dict[str, int]] = {}

    def run(section: str, items: Any, post=None, *, require_surface: bool = True,
            surface_keys: Sequence[str] = ("name", "variable", "term"),
            refs_from: Sequence[str] = ()) -> List[dict]:
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
            if why:
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
    out["variables"] = run("variables", payload.get("variables"),
                           lambda it: gate_variable(it, dictionary),
                           require_surface=True,
                           surface_keys=("name", "variable", "term"))
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
    # A finding statement is a paraphrase; require one referenced variable.
    out["findings"] = run("findings", payload.get("findings"),
                          lambda it: _normalize_finding(it, atlas),
                          require_surface=False, surface_keys=(),
                          refs_from=("variables", "variable", "predictor",
                                     "outcome", "mediator", "moderator"))
    out["rejected"] = rejected

    verified_vars = [v for v in out["variables"]
                     if v.get("dictionary_status") == "verified"]
    out["verification"] = {
        "min_quote_chars": MIN_QUOTE_CHARS,
        "by_section": counts,
        "variables_dictionary_verified": len(verified_vars),
        "variables_unverified": len(out["variables"]) - len(verified_vars),
        "constructs_mapped": sum(1 for c in out["constructs"] if c.get("construct_id")),
        "constructs_unmapped": sum(1 for c in out["constructs"]
                                   if not c.get("construct_id")),
        "rejected_total": len(rejected),
    }
    return out


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
