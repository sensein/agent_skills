"""Context-aware mapping from a paper's wording to ABCD/HBCD dictionary variables.

`abcd_dictionary.Dictionary.resolve()` answers "is this string a variable name?".
That is the right question for a methods section that writes
`nihtbx_flanker_uncorrected` — and the wrong question for the majority of papers,
which write *prose*: "the Flanker Inhibitory Control and Attention Test", "youth-
reported family conflict", "fractional anisotropy". Those resolve to nothing, so
`nda_or_nbdc_table` and `nbdc_domain` come back empty, which is the whole point of
the mapping.

This module closes that gap by matching the paper's phrasing against dictionary
*labels*, using the surrounding context to disambiguate. The labels are rich enough
to make this a lexical problem rather than a guessing one:

    fes_y_ss_fc              Conflict Subscale from the Family Environment Scale
                             Sum of Youth Report (RAW Score)
    fes_p_ss_fc              Conflict subscale from the Family Environment Scale
                             Sum of Parent Report (RAW Score)
    nihtbx_list_uncorrected  NIH Toolbox List Sorting Working Memory Test Age 7+
                             v2.0 Uncorrected Standard Score
    nihtbx_list_v            NIH Toolbox List Sorting Working Memory Test Age 7+
                             Version

Three signals decide between those, and all three come from the paper:

  * **respondent** — "children completed" / "parent-reported" picks `_y_` over
    `_p_`. Getting this wrong is not a near miss; it is a different measure.
  * **metric** — "Fully Corrected T-score" picks `_fc` over `_uncorrected`, and
    any metric cue at all outranks the administrative siblings (Version,
    Language, ItmCnt, DateFinished) that share every content word with the
    measure but are not the measure.
  * **instrument** — "Child Behavior Checklist (CBCL)" is an instrument, not a
    variable. It maps to a table and a domain, and saying so is more useful than
    calling it unmatched.

What this module will NOT do is name a single variable when the paper did not.
A paper reporting "fractional anisotropy of white matter tracts" is talking about
a *family* of 148 ROI variables; picking one would be fabrication. Those resolve
to `context_family`: the table and domain are reported (which is what a reader
needs to find the data), the specific variable stays null, and the candidate list
travels with the result so the choice is auditable. Same discipline as the rest of
the skill — an honest gap beats a confident guess (SKILL.md hard rule 15).

    python -m scripts.abcd_context match "youth-reported family conflict" \
        --context "Children completed the Family Conflict subscale of the FES"
    python -m scripts.abcd_context instrument "Child Behavior Checklist"
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

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from scripts.abcd_dictionary import (
    Dictionary,
    canonical_field,
    norm_text,
)

# --------------------------------------------------------------------------- #
# tuning — every threshold here is reported in the output's `decision_rule`
# --------------------------------------------------------------------------- #

# Below this, nothing is claimed. Calibrated on the ABCD corpus: real measure
# mentions land at 0.55-0.97; a phrase with no dictionary counterpart ("financial
# strain", whose PRFQ items are labelled "Needed food but couldn't afford it")
# tops out around 0.53 and is caught by the coverage gate below.
MIN_SCORE = 0.55
# The paper's own words must be mostly accounted for by the label. This is the gate
# that does the real work: a long phrase matching a short label on one shared token
# has high precision and low coverage, and must not pass.
MIN_COVERAGE = 0.60
# How far ahead of the next *differently named* candidate the winner must be to be
# named alone rather than reported as a family.
NAME_MARGIN = 0.02
# Share of the top group's mass that must sit in one table before the table itself
# is reported. Mixed tables mean we do not know which instrument was used.
TABLE_MASS = 0.60
# Candidate generation only follows tokens rarer than this. "score" appears in
# 100k labels and tells us nothing about which variable is meant.
RARE_DF_CAP = 8000
# Postings are capped so one generic token cannot drag in the whole dictionary.
MAX_CANDIDATES = 20000

_TOKEN = re.compile(r"[a-z0-9]+")
# `___1` is a REDCap checkbox option, `_q7` a numbered item: both are single
# questionnaire items rather than the scored measure a paper reports.
_ITEM_LEVEL_NAME_RE = re.compile(r"___\d+$|_q\d+$|_\d{1,3}$")

# Deliberately small: these are the words that carry no discriminating signal in a
# dictionary label. Domain words ("conflict", "flanker", "anisotropy") must never
# be here.
STOPWORDS = frozenset("""
a an the of for in to and or at on by with as is was were be been from this that
these those it its his her their there which who whom what when where how not no
yes all any both each more most other some such than too very can will just
""".split())

# Administrative siblings. Every ABCD instrument carries a handful of variables
# that share all content words with the measure but record how the instrument was
# administered rather than what it measured. A paper reporting "List Sorting
# Working Memory" never means `nihtbx_list_language`.
ADMIN_LABEL_RE = re.compile(
    r"(?:\bversion\b|\blanguage\b|\bitmcnt\b|\bitem count\b|\btheta\b|"
    r"date ?finished|date and time|\btimestamp\b|administ\w*|"
    r"number (?:of )?(?:miss\w*|answer\w*|valid\w*|item\w*|total\w*|question\w*)|"
    r"number with|\bmissing\b|complete\?|completed\?|\bvalidity\b|\bqc\b|"
    r"quality control|\bflags?\b|\bissues?\b|\badmin\b)",
    re.I,
)
# Every alternative above tolerates a suffix (`answer\w*`, not `answer\b`): the
# labels say "Number Answered", and a trailing word boundary silently missed all
# of them — which is how "Number Answered" once tied with the measure itself.

# Metric cues: what the paper says it analysed -> what the label must contain.
# Ordered, because "fully corrected T-score" also contains "corrected".
METRIC_CUES: Tuple[Tuple[str, str], ...] = (
    ("fully_corrected", r"fully[\s-]*corrected|\bfc\b"),
    ("age_corrected", r"age[\s-]*corrected"),
    ("uncorrected", r"uncorrected"),
    ("t_score", r"t[\s-]*scores?\b|tscore"),
    ("percentile", r"percentile"),
    ("prorated", r"prorated"),
    ("raw", r"\braw\b"),
    ("sum", r"\bsum\b|\btotal\b"),
    ("mean", r"\bmean\b|\baverage\b"),
    ("z_score", r"\bz[\s-]*scores?\b|standardi[sz]ed"),
    ("computed", r"computed"),
)
# How the paper signals each metric. Kept separate from the label patterns above
# because the phrasings differ ("T-scores were used" vs a label's "T-score").
METRIC_QUERY_CUES: Tuple[Tuple[str, str], ...] = (
    ("fully_corrected", r"fully[\s-]*corrected"),
    ("age_corrected", r"age[\s-]*corrected|age[\s-]*adjusted"),
    ("uncorrected", r"uncorrected"),
    ("t_score", r"t[\s-]*scores?\b"),
    ("percentile", r"percentile"),
    ("prorated", r"prorated"),
    ("raw", r"\braw\s+scores?\b"),
    ("sum", r"\bsum\s+scores?\b|\btotal\s+scores?\b|\bsum\s+of\b"),
    ("mean", r"\bmean\b|\baverage\b"),
    ("z_score", r"\bz[\s-]*scores?\b|standardi[sz]ed"),
)

# Respondent cues. `_p_`/`_y_` in the name and "[Parent]"/"Parent Report" in the
# label are the dictionary's two conventions across the NDA and NBDC eras.
RESPONDENT_LABEL_RE = {
    "parent": re.compile(r"\[parent\]|\bparent(?:al)?[\s-]*report|\bcaregiver\b|"
                         r"\bparent\b", re.I),
    "youth": re.compile(r"\[youth\]|\byouth[\s-]*report|\bchild[\s-]*report|"
                        r"\bself[\s-]*report|\byouth\b", re.I),
    "teacher": re.compile(r"\bteacher\b", re.I),
}
RESPONDENT_NAME_RE = {
    # `_p`/`_y` mark the respondent, mid-name or at the end (`ple_financial_p`,
    # `fes_y_ss_fc`). A TRAILING `_t` is a t-score, not a teacher — reading it as
    # a respondent made `cbcl_scr_syn_external_t` look like a teacher measure and
    # cost it the match on a paper that plainly said parent-reported T-scores.
    "parent": re.compile(r"_p(?:_|$)"),
    "youth": re.compile(r"_y(?:_|$)"),
}
RESPONDENT_QUERY_RE = {
    "parent": re.compile(r"\bparent(?:s|al|ing)?[\s-]*(?:report|rated|reported|"
                         r"version)?\b|\bcaregiver\b|\bprimary caregiver\b", re.I),
    "youth": re.compile(r"\byouth[\s-]*(?:report|rated|reported)?\b|"
                        r"\bchild(?:ren)?[\s-]*(?:report|rated|reported|completed)\b|"
                        r"\bself[\s-]*report\b|\badolescent[\s-]*report\b", re.I),
    "teacher": re.compile(r"\bteacher[\s-]*(?:report|rated|reported)?\b", re.I),
}

# Bonuses and penalties, applied to a 0-1 lexical score. Small on purpose: they
# break ties between near-identical labels, they do not manufacture a match.
W_RESPONDENT = 0.10
W_RESPONDENT_MISMATCH = -0.18   # a parent measure is not a youth measure
W_METRIC = 0.08
W_METRIC_MISMATCH = -0.05
W_ADMIN = -0.22                 # enough to sink an admin sibling below its measure
# A qualifier the paper never asked for (prorated, percentile, theta). Small, but
# enough to prefer the plain measure over its variants when the paper is silent —
# otherwise `fes_y_ss_fc` and `fes_y_ss_fc_pr` tie forever and neither is named.
W_UNASKED_QUALIFIER = -0.06
# Item-level rows (a single checkbox or questionnaire item) versus the derived
# summary a paper actually analyses. "Child race/ethnicity" is not
# `dim_yesno_q1` ("have you felt discriminated against because of your race...").
W_ITEM_LEVEL = -0.10
# A two-word mention must match on two words. Without this, "birth weight" matches
# a language questionnaire whose answer options start with "Birth to 1 year",
# because `birth` alone carries most of the phrase's IDF mass.
MIN_TOKEN_HITS = 2
W_CONTEXT = 0.06                # context tokens are corroboration, not evidence
STEM_DISCOUNT = 0.85            # an inflected match is worth slightly less
W_AUX = 0.12                    # extractor's own label/instrument, as corroboration
# Words that carry real meaning in English but almost none in a dictionary label:
# every scale has a "score", and a paper's "externalizing behaviors" is labelled
# "External CBCL Syndrome Scale" with no "behavior" in sight. Kept at a low weight
# rather than dropped, so they can still corroborate a match.
WEAK_WEIGHT = 0.30
WEAK_TOKENS = frozenset("""
behavior behaviors behaviour behaviours problem problems symptom symptoms
score scores scoring level levels measure measures measured index indices
assessment assessments outcome outcomes variable variables data value values
test tests task tasks scale scales subscale subscales questionnaire inventory
item items version total child children youth participant participants
""".split())
# Extra score a variable must clear to be named when the paper called the mention
# an instrument. The paper's own classification outranks our lexical hunch.
INSTRUMENT_ROLE_PENALTY = 0.20


def tokens(text: Optional[str]) -> List[str]:
    """Content tokens of `text`, order preserved, duplicates dropped."""
    seen: Dict[str, None] = {}
    for tok in _TOKEN.findall((text or "").lower()):
        if len(tok) > 2 and tok not in STOPWORDS:
            seen.setdefault(tok, None)
    return list(seen)


# Papers and dictionaries inflect the same word differently, and the difference is
# never meaningful: the CBCL scale a paper calls "externalizing behaviors" is
# labelled "External CBCL Syndrome Scale". Without this, that variable is
# unmatchable — which is how `cbcl_scr_syn_external_t` came to be missing from a
# run over three papers that all used it.
_SPELLING = {"behaviour": "behavior", "behaviours": "behavior",
             "colour": "color", "centre": "center", "grey": "gray"}
_SUFFIXES = ("izations", "ization", "isation", "izing", "ising", "ized", "ised",
             "ness", "ing", "edly", "ally", "ies", "ers", "ed", "ly", "es", "s")


def stem(token: str) -> str:
    """Crude suffix stripper — enough to bridge inflection, not a real stemmer.

    Deliberately conservative: it only fires on tokens long enough that the
    remainder is still a word, so "sex" and "raw" pass through untouched.
    """
    tok = _SPELLING.get(token, token)
    if len(tok) < 6:
        return tok
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)]
    return tok


def releases_for_paper(data_release: Optional[str],
                       available: Sequence[str]) -> List[str]:
    """Which snapshot(s) a paper's stated data release should be matched against.

    This matters more than any scoring weight. A paper analysing release 5.0 used
    `nihtbx_cryst_fc` in `abcd_tbss01`; release 6.0 renamed the same measure to
    `nc_y_nihtb__comp__cryst__fullcorr_tscore` in `nc_y_nihtb`. Matching a 5.0
    paper against every loaded release makes the two look like rival candidates in
    different tables — which reads as ambiguity when in fact the paper was
    perfectly clear. Releases 4.x and 5.x are the NDA era, so they map to the
    `nda-legacy` snapshot; 6.0+ match their own.

    Returns [] when the release is unknown or unavailable, meaning "search all" —
    a wrong release filter is worse than none.
    """
    text = str(data_release or "")
    m = re.search(r"\b([0-9]+)\.([0-9]+)\b", text)
    if not m:
        return []
    major, minor = m.group(1), m.group(2)
    exact = f"{major}.{minor}"
    if exact in available:
        return [exact]
    if major in ("4", "5") and "nda-legacy" in available:
        return ["nda-legacy"]
    # A minor release we have no snapshot for (e.g. 6.2): search its siblings
    # rather than pretending we can check that exact release.
    return sorted(r for r in available if r.startswith(f"{major}."))


def decision_rule() -> dict:
    """The thresholds every match was judged against — recorded in provenance so a
    result can be re-derived, or argued with, months later."""
    return {
        "min_score": MIN_SCORE,
        "min_coverage": MIN_COVERAGE,
        "name_margin": NAME_MARGIN,
        "table_mass": TABLE_MASS,
        "stem_discount": STEM_DISCOUNT,
        "instrument_role_penalty": INSTRUMENT_ROLE_PENALTY,
        "weights": {"coverage": 0.85, "label_precision": 0.15,
                    "respondent": W_RESPONDENT,
                    "respondent_mismatch": W_RESPONDENT_MISMATCH,
                    "metric": W_METRIC, "metric_mismatch": W_METRIC_MISMATCH,
                    "administrative": W_ADMIN,
                    "unasked_qualifier": W_UNASKED_QUALIFIER,
                    "extractor_label": W_AUX, "context": W_CONTEXT},
    }


# Wording the ABCD dictionary uses for measures papers name differently. These are
# naming conventions documented in the ABCD imaging release notes, not guesses at
# what an author meant: the DTI tables label the third and second eigenvalues
# "longitudinal" and "transverse" diffusivity, which every dMRI paper calls axial
# and radial; the rsfMRI tables report network "correlations", which papers call
# functional connectivity; and the structural tables say "cortical area" where
# papers say "surface area". Without this bridge those measures are unmatchable no
# matter how good the scoring is, because they share no content word at all.
PHRASE_SYNONYMS: Tuple[Tuple[str, str], ...] = (
    ("axial diffusivity", "longitudinal diffusivity"),
    ("radial diffusivity", "transverse diffusivity"),
    ("mean diffusivity", "mean diffusivity"),
    ("functional connectivity", "correlation between networks"),
    ("surface area", "cortical area"),
    ("sulcal depth", "cortical sulcal depth"),
    ("cortical volume", "cortical volume"),
    ("neurite density", "restricted normalized directional diffusion"),
)


def synonym_for(mention: str) -> Optional[Tuple[str, str]]:
    """(phrase, replacement) if a documented dictionary wording differs."""
    low = norm_text(mention)
    for phrase, replacement in PHRASE_SYNONYMS:
        if phrase != replacement and phrase in low:
            return phrase, replacement
    return None


def _words(text: str) -> int:
    return len([w for w in re.split(r"[^A-Za-z0-9]+", text or "") if w])


def acronym_of(phrase: str) -> str:
    """Initials of the capitalised words in `phrase` — "Child Behavior Checklist"
    -> "CBCL" needs the 'C' from Checklist, so every word counts."""
    words = [w for w in re.split(r"[^A-Za-z]+", phrase or "") if w]
    return "".join(w[0].upper() for w in words if w[0].isupper())


def _cues(text: str, table: Sequence[Tuple[str, str]]) -> List[str]:
    return [name for name, pat in table if re.search(pat, text, re.I)]


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    """One dictionary row scored against the paper's wording."""

    name: str
    label: str
    study: str
    releases: List[str]
    table: Optional[str]
    nda_or_nbdc_table: Optional[str]
    domain: Optional[str]
    sub_domain: Optional[str]
    score: float
    coverage: float
    precision: float
    matched_tokens: List[str]
    respondent: Optional[str]
    metrics: List[str]
    admin: bool

    def to_dict(self, *, brief: bool = False) -> dict:
        out = {
            "variable": self.name,
            "label": self.label,
            "score": round(self.score, 3),
            "nda_or_nbdc_table": self.nda_or_nbdc_table,
            "nbdc_domain": self.domain,
        }
        if not brief:
            out.update({
                "study": self.study,
                "dd_releases": self.releases,
                "nbdc_table": self.table,
                "nbdc_sub_domain": self.sub_domain,
                "coverage": round(self.coverage, 3),
                "label_precision": round(self.precision, 3),
                "matched_tokens": self.matched_tokens,
                "respondent": self.respondent,
                "metrics": self.metrics,
                "administrative": self.admin,
            })
        return out


@dataclass
class ContextMatch:
    """The verdict for one paper mention, with everything needed to audit it."""

    status: str                      # context_variable | context_family |
                                     # context_domain | instrument_table |
                                     # ambiguous | unmatched
    mention: str
    variable: Optional[str] = None
    label: Optional[str] = None
    nda_or_nbdc_table: Optional[str] = None
    nbdc_table: Optional[str] = None
    tables: List[str] = field(default_factory=list)
    nbdc_domain: Optional[str] = None
    nbdc_sub_domain: Optional[str] = None
    study: Optional[str] = None
    dd_releases: List[str] = field(default_factory=list)
    score: float = 0.0
    family_size: int = 0
    family_prefix: Optional[str] = None
    instrument: Optional[str] = None
    cues: Dict[str, Any] = field(default_factory=dict)
    candidates: List[Candidate] = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def matched(self) -> bool:
        return self.status in ("context_variable", "context_family",
                               "context_domain", "instrument_table")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "mention": self.mention,
            "variable": self.variable,
            "label": self.label,
            "nda_or_nbdc_table": self.nda_or_nbdc_table,
            "nbdc_table": self.nbdc_table,
            "candidate_tables": self.tables or None,
            "nbdc_domain": self.nbdc_domain,
            "nbdc_sub_domain": self.nbdc_sub_domain,
            "study": self.study,
            "dd_releases": self.dd_releases,
            "match_method": {
                "context_variable": "label_context",
                "context_family": "label_context_family",
                "context_domain": "label_context_domain",
                "instrument_table": "instrument_label",
            }.get(self.status),
            "match_score": round(self.score, 3),
            "family_size": self.family_size or None,
            "family_prefix": self.family_prefix,
            "instrument": self.instrument,
            "context_cues": self.cues,
            "candidates": [c.to_dict(brief=True) for c in self.candidates[:6]],
            "decision_rule": decision_rule(),
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #

class ContextIndex:
    """Inverted index over dictionary labels, plus an instrument index.

    Built lazily and once per run: ~540k labels across seven bundled snapshots
    tokenise in about two seconds. `Dictionary` deliberately does not do this at
    construction time, because a run that only resolves literal variable names
    should not pay for it.
    """

    def __init__(self, dictionary: Dictionary):
        self.dictionary = dictionary
        self._rows: List[dict] = []
        self._row_tokens: List[frozenset] = []
        self._row_stems: List[frozenset] = []
        self._row_meta: List[Tuple[str, str]] = []   # (study, release)
        self._postings: Dict[str, List[int]] = defaultdict(list)
        self._stem_postings: Dict[str, List[int]] = defaultdict(list)
        # normalised instrument phrase -> Counter of (study, table, nda_table,
        # domain, sub_domain)
        self._instruments: Dict[str, Counter] = defaultdict(Counter)
        self._acronyms: Dict[str, set] = defaultdict(set)
        self._build()

    # -- construction ------------------------------------------------------- #

    def _build(self) -> None:
        for snap in self.dictionary.snapshots:
            study, release = snap["study"], snap["release"]
            for row in snap["variables"]:
                label = str(row.get("label") or "")
                if not label:
                    continue
                idx = len(self._rows)
                self._rows.append(row)
                self._row_meta.append((study, release))
                toks = frozenset(tokens(label))
                self._row_tokens.append(toks)
                stems = frozenset(stem(t) for t in toks)
                self._row_stems.append(stems)
                for tok in toks:
                    self._postings[tok].append(idx)
                for st in stems - toks:
                    self._stem_postings[st].append(idx)
                self._index_instrument(row, label, study)

        n = max(len(self._rows), 1)
        self._idf = {t: math.log(n / len(p)) for t, p in self._postings.items()}

    def _index_instrument(self, row: dict, label: str, study: str) -> None:
        """Harvest the instrument name from a label.

        Both dictionary eras put the instrument first and qualify it afterwards:

            NBDC 6.x  "Child Behavior Checklist [Parent] (Syndrome Scale - ...)"
            NDA 4.x   "Conflict subscale from the Family Environment Scale, ..."

        Splitting on the first bracket/colon captures the 6.x form exactly. The
        NDA form has no reliable delimiter, so it is left to label matching — an
        instrument index built from arbitrary prose prefixes would be noise.
        """
        head = re.split(r"[\[(:]", label, maxsplit=1)[0].strip(" ,-;")
        if not (12 <= len(head) <= 90):
            return
        if ADMIN_LABEL_RE.search(head):
            return
        key = norm_text(head)
        slot = (
            study,
            row.get("table_name"),
            canonical_field(row, "nda_or_nbdc_table"),
            canonical_field(row, "nbdc_domain"),
            canonical_field(row, "nbdc_sub_domain"),
        )
        self._instruments[key][slot] += 1
        acr = acronym_of(head)
        if 3 <= len(acr) <= 8:
            self._acronyms[acr.upper()].add(key)

    # -- scoring ------------------------------------------------------------ #

    def _postings_for(self, term: str) -> List[int]:
        """Rows whose label contains `term` as a token or as a stem."""
        exact = self._postings.get(term) or []
        stems = self._stem_postings.get(term) or []
        return exact + stems if stems else exact

    def _score_row(self, idx: int, qw: Dict[str, float], qtot: float,
                   aw: Dict[str, float], ctx_tokens: frozenset,
                   want_respondent: Optional[str],
                   want_metrics: Sequence[str]) -> Optional[Candidate]:
        row = self._rows[idx]
        label = str(row.get("label") or "")
        ltoks = self._row_tokens[idx]
        lstems = self._row_stems[idx]
        matched: List[str] = []
        credit = 0.0
        for tok, weight in qw.items():
            if tok in ltoks:
                matched.append(tok)
                credit += weight
            elif stem(tok) in lstems:
                # Inflectional match ("externalizing" vs "External"): real, but
                # discounted so an exact wording always outranks it.
                matched.append(tok)
                credit += STEM_DISCOUNT * weight
        if not matched:
            return None
        strong = [t for t in qw if t not in WEAK_TOKENS]
        strong_hits = [t for t in matched if t not in WEAK_TOKENS]
        if len(strong_hits) < min(MIN_TOKEN_HITS, len(strong) or 1):
            return None
        coverage = credit / qtot
        precision = len(matched) / max(len(ltoks), 1)
        # Coverage dominates: a paper naming "fractional anisotropy of white
        # matter tracts" is fully accounted for by a 20-word ROI label, and
        # weighting precision any higher would reject exactly those long, correct
        # imaging labels while favouring short generic ones ("Total Score").
        score = 0.85 * coverage + 0.15 * precision

        if aw:
            hit = sum(w for t, w in aw.items()
                      if t in ltoks or stem(t) in lstems)
            score += W_AUX * (hit / sum(aw.values()))

        name = str(row.get("name") or "")
        respondent = self._respondent_of(name, label)
        if want_respondent:
            if respondent == want_respondent:
                score += W_RESPONDENT
            elif respondent and respondent != want_respondent:
                score += W_RESPONDENT_MISMATCH

        metrics = _cues(label, METRIC_CUES)
        if want_metrics:
            if any(m in metrics for m in want_metrics):
                score += W_METRIC
            elif metrics:
                score += W_METRIC_MISMATCH

        if _ITEM_LEVEL_NAME_RE.search(name) or label.rstrip().endswith("?"):
            score += W_ITEM_LEVEL

        admin = bool(ADMIN_LABEL_RE.search(label))
        if admin:
            score += W_ADMIN
        unasked = [m for m in metrics
                   if m in ("prorated", "percentile", "theta", "computed")
                   and m not in want_metrics]
        if unasked:
            score += W_UNASKED_QUALIFIER

        if ctx_tokens:
            extra = [t for t in ltoks if t in ctx_tokens and t not in qw]
            if extra:
                score += min(W_CONTEXT, 0.02 * len(extra))

        study, release = self._row_meta[idx]
        return Candidate(
            name=name,
            label=label,
            study=study,
            releases=[release],
            table=row.get("table_name"),
            nda_or_nbdc_table=canonical_field(row, "nda_or_nbdc_table"),
            domain=canonical_field(row, "nbdc_domain"),
            sub_domain=canonical_field(row, "nbdc_sub_domain"),
            score=score,
            coverage=coverage,
            precision=precision,
            matched_tokens=matched,
            respondent=respondent,
            metrics=metrics,
            admin=admin,
        )

    @staticmethod
    def _respondent_of(name: str, label: str) -> Optional[str]:
        for who, pat in RESPONDENT_LABEL_RE.items():
            if pat.search(label):
                return who
        for who, pat in RESPONDENT_NAME_RE.items():
            if pat.search(name.lower()):
                return who
        return None

    # -- the public call ---------------------------------------------------- #

    def match(self, mention: str, **kwargs) -> ContextMatch:
        """`_match_once`, retried with the dictionary's own wording if it failed.

        The retry is recorded (`cues.synonym_applied`) so the substitution is never
        invisible: a reader sees that "axial diffusivity" was matched as
        "longitudinal diffusivity" and can reject that reading.
        """
        first = self._match_once(mention, **kwargs)
        if first.matched:
            return first
        pair = synonym_for(mention)
        if not pair:
            return first
        phrase, replacement = pair
        retried = self._match_once(
            re.sub(re.escape(phrase), replacement, mention, flags=re.I), **kwargs)
        if not retried.matched:
            return first
        retried.mention = mention
        retried.cues["synonym_applied"] = {"paper_wording": phrase,
                                           "dictionary_wording": replacement}
        return retried

    def _match_once(self, mention: str, *, label: Optional[str] = None,
              context: Optional[str] = None, instrument: Optional[str] = None,
              respondent: Optional[str] = None, study: Optional[str] = None,
              releases: Optional[Iterable[str]] = None,
              role: Optional[str] = None,
              top_k: int = 8) -> ContextMatch:
        """Map one paper mention to a variable, a family, or a table.

        `mention` is how the paper wrote it; `label`/`instrument`/`respondent` are
        the extractor's reading of the same sentence; `context` is the surrounding
        text. All four are evidence from the paper — nothing here consults the
        model's own knowledge.
        """
        mention = (mention or "").strip()
        query_text = " ".join(p for p in (mention, label, instrument) if p)
        ctx = context or ""
        want_respondent = respondent or self._respondent_cue(query_text + " " + ctx)
        want_metrics = _cues(query_text + " " + ctx, METRIC_QUERY_CUES)
        cues = {
            "respondent": want_respondent,
            "metrics": want_metrics,
            "instrument_hint": instrument or None,
        }

        # An entry the paper itself calls an instrument ("Youth Self-Report
        # (YSR)") is not a variable, and matching it against variable labels
        # invites nonsense: "Youth Self-Report" shares "youth" and "report" with
        # the Prosocial Behavior youth-report scale and nothing else. Route it to
        # the instrument index, and only consider variables if that fails and the
        # wording is a strong match on its own.
        if (role or "").lower() == "instrument":
            hit = self.match_instrument(query_text)
            if hit:
                return self._instrument_match(mention, hit, cues)

        # The MENTION is what has to be explained by a label; the extractor's
        # own label and the instrument name are corroboration. Keeping them out of
        # the denominator matters: a helpful label ("CBCL externalizing broadband
        # T-score, parent report") adds words the dictionary label will never
        # contain, and counting them as unexplained sinks a correct match.
        qtoks = tokens(mention) or tokens(query_text)
        aux_toks = [t for t in tokens(query_text) if t not in qtoks]
        if not qtoks:
            return ContextMatch(status="unmatched", mention=mention, cues=cues,
                                reason="no content tokens in the mention")

        qw = {t: self._idf.get(t, 0.0) * (WEAK_WEIGHT if t in WEAK_TOKENS else 1.0)
              for t in qtoks}
        aw = {t: self._idf.get(t, 0.0) * (WEAK_WEIGHT if t in WEAK_TOKENS else 1.0)
              for t in aux_toks}
        qtot = sum(qw.values())
        if qtot <= 0:
            return ContextMatch(status="unmatched", mention=mention, cues=cues,
                                reason="mention has no tokens present in any label")

        # Candidate generation: follow the rare tokens (and their stems). Common
        # ones are useless as entry points — "score" appears in 100k labels.
        # Stems are entry points in their own right, not a fallback for tokens
        # that found nothing: "externalizing" does occur in 6.x labels, so it is
        # not rare-and-empty, yet the 5.0 dictionary spells the same scale
        # "External" and is only reachable through the stem.
        search_toks = [*qtoks, *aux_toks]
        terms = list(dict.fromkeys([*search_toks, *(stem(t) for t in search_toks)]))
        rare = [t for t in terms if 0 < len(self._postings_for(t)) <= RARE_DF_CAP]
        hits: Counter = Counter()
        if rare:
            for tok in dict.fromkeys(rare):
                for idx in self._postings_for(tok):
                    hits[idx] += 1
        if not rare or not hits:
            # Every token is common ("cortical thickness" — both appear in tens of
            # thousands of imaging labels). Intersect the two least common
            # postings lists instead of giving up: the pair is discriminating even
            # though neither token is.
            ranked = sorted((t for t in search_toks if self._postings_for(t)),
                            key=lambda t: len(self._postings_for(t)))
            if len(ranked) < 2:
                return ContextMatch(
                    status="unmatched", mention=mention, cues=cues,
                    reason=("every token in the mention is too common to search "
                            "on, and there is no second token to intersect with"))
            first, second = set(self._postings_for(ranked[0])), set(
                self._postings_for(ranked[1]))
            for idx in first & second:
                hits[idx] += 2
            if not hits:
                return ContextMatch(
                    status="unmatched", mention=mention, cues=cues,
                    reason=(f"no label contains both {ranked[0]!r} and "
                            f"{ranked[1]!r}"))
        common_pair_tried = not rare

        ctx_tokens = frozenset(tokens(ctx)) - set(qtoks)
        # Strongest context signal available: if the paper named the instrument,
        # only that instrument's variables are candidates. "Externalizing" appears
        # in the CBCL, the ABCL, the YSR and the Brief Problem Monitor; the paper
        # saying "Child Behavior Checklist" settles it, and no amount of lexical
        # scoring can.
        scope = None
        if instrument:
            scope = self.match_instrument(instrument)
        if scope is None and context:
            scope = self.match_instrument(context)
        scope_table = None
        if scope:
            _, slot, _ = scope
            scope_table = slot[2] or slot[1]
            cues["instrument_scope"] = {"instrument": scope[0],
                                        "nda_or_nbdc_table": scope_table}

        want_study = (study or "").lower() or None
        want_releases = {str(r) for r in releases} if releases else None

        scored: List[Candidate] = []
        out_of_scope = 0
        for idx, _ in hits.most_common(MAX_CANDIDATES):
            snap_study, snap_release = self._row_meta[idx]
            if want_study and snap_study != want_study:
                continue
            if want_releases and snap_release not in want_releases:
                continue
            cand = self._score_row(idx, qw, qtot, aw, ctx_tokens,
                                   want_respondent, want_metrics)
            if not cand:
                continue
            if scope_table and (cand.nda_or_nbdc_table or cand.table) != scope_table:
                out_of_scope += 1
                continue
            scored.append(cand)
        if scope_table and not scored and out_of_scope:
            # The instrument scope excluded everything. Say so rather than
            # silently widening: either the extractor's instrument is wrong or the
            # measure lives in another table, and both are worth seeing.
            cues["instrument_scope_empty"] = True
        if not scored and not common_pair_tried:
            # Everything the rare tokens found was in another release or study.
            # "cortical thickness" is reachable only by intersecting two common
            # tokens, and the rare token that ran first ("desikan", from the
            # extractor's label) exists in 6.x labels but not in the 5.x snapshot
            # this paper needs — so the phrase looked unsearchable when it was not.
            scored = self._intersect_scored(
                search_toks, qw, qtot, aw, ctx_tokens, want_respondent,
                want_metrics, want_study, want_releases, scope_table)
            if scored:
                cues["candidate_generation"] = "common_token_intersection"
        if not scored:
            return self._instrument_fallback(mention, query_text, cues,
                                             reason="no label shared a rare token")

        if want_respondent:
            # The respondent is a fact the paper stated, not a preference to be
            # out-scored: "children completed the FES" excludes every parent-report
            # variable. Filtering rather than penalising also keeps the parent
            # rows from diluting the table agreement below, which is what left
            # `fes_y_ss_fc` reported as a vague domain-level match.
            kept = [c for c in scored
                    if c.respondent in (None, want_respondent)]
            if kept:
                cues["respondent_filtered"] = len(scored) - len(kept)
                scored = kept

        merged = _merge_releases(scored)
        merged.sort(key=lambda c: (-c.score, c.name))
        keep = [c for c in merged
                if c.score >= MIN_SCORE and c.coverage >= MIN_COVERAGE]
        if not keep:
            best = merged[0]
            return self._instrument_fallback(
                mention, query_text, cues, candidates=merged[:top_k],
                reason=(f"best candidate {best.name} scored {best.score:.2f} "
                        f"(coverage {best.coverage:.2f}), below "
                        f"{MIN_SCORE}/{MIN_COVERAGE}"))

        floor = MIN_SCORE + INSTRUMENT_ROLE_PENALTY if (role or "").lower() == "instrument" else MIN_SCORE
        keep = [c for c in keep if c.score >= floor]
        if not keep:
            return ContextMatch(
                status="unmatched", mention=mention, cues=cues,
                candidates=merged[:top_k],
                reason=("the paper calls this an instrument and no dictionary "
                        f"instrument matched; best variable scored "
                        f"{merged[0].score:.2f}, below the {floor} required to "
                        "override the paper's own wording"))
        return self._decide(mention, keep, cues, top_k)

    def _intersect_scored(self, search_toks: List[str], qw, qtot, aw, ctx_tokens,
                          want_respondent, want_metrics, want_study,
                          want_releases, scope_table) -> List[Candidate]:
        """Score rows containing two of the query's tokens.

        Pairs are tried least-common first and the first pair with any in-release
        row wins. Trying only the single rarest pair is not enough: the extractor's
        label contributes very rare tokens ("Desikan-Killiany") that exist in the
        6.x labels and nowhere in the 5.x snapshot a given paper needs, so the one
        pair tried can come back empty while "cortical" + "thickness" would have
        matched 136 rows.
        """
        ranked = sorted((t for t in search_toks if self._postings_for(t)),
                        key=lambda t: len(self._postings_for(t)))
        if len(ranked) < 2:
            return []
        pair: set = set()
        for i in range(min(len(ranked), 6)):
            for j in range(i + 1, min(len(ranked), 6)):
                cand = (set(self._postings_for(ranked[i]))
                        & set(self._postings_for(ranked[j])))
                cand = {ix for ix in cand
                        if not want_releases or self._row_meta[ix][1] in want_releases}
                if cand:
                    pair = cand
                    break
            if pair:
                break
        out: List[Candidate] = []
        for idx in pair:
            snap_study, snap_release = self._row_meta[idx]
            if want_study and snap_study != want_study:
                continue
            if want_releases and snap_release not in want_releases:
                continue
            cand = self._score_row(idx, qw, qtot, aw, ctx_tokens,
                                   want_respondent, want_metrics)
            if not cand:
                continue
            if scope_table and (cand.nda_or_nbdc_table or cand.table) != scope_table:
                continue
            out.append(cand)
        return out

    def _decide(self, mention: str, keep: List[Candidate],
                cues: dict, top_k: int) -> ContextMatch:
        """Name a variable, a family, or nothing — see the module docstring."""
        top = keep[: max(top_k, 10)]
        # Which table do the good candidates agree on? Score-weighted, so a long
        # tail of weak hits in other tables cannot swing it.
        by_table: Dict[Tuple[Optional[str], Optional[str]], float] = defaultdict(float)
        for c in top:
            by_table[(c.nda_or_nbdc_table or c.table, c.domain)] += c.score
        total = sum(by_table.values()) or 1.0
        (best_table, best_domain), mass = max(by_table.items(), key=lambda kv: kv[1])
        table_confident = (mass / total) >= TABLE_MASS

        in_table = [c for c in keep
                    if (c.nda_or_nbdc_table or c.table) == best_table
                    and c.domain == best_domain]
        winner = in_table[0] if in_table else keep[0]
        rivals = [c for c in in_table[1:] if c.name != winner.name]
        margin_ok = (not rivals) or (winner.score - rivals[0].score) >= NAME_MARGIN

        if not table_confident:
            # Tables disagree. The domain often still agrees — every fractional
            # anisotropy variable is imaging, spread over per-atlas tables — and
            # the domain is worth reporting on its own rather than throwing the
            # match away.
            by_domain: Dict[Optional[str], float] = defaultdict(float)
            for c in top:
                by_domain[c.domain] += c.score
            dom, dmass = max(by_domain.items(), key=lambda kv: kv[1])
            if dom and dmass / total >= TABLE_MASS:
                in_domain = [c for c in keep if c.domain == dom]
                return ContextMatch(
                    status="context_domain", mention=mention,
                    nbdc_domain=dom, study=in_domain[0].study,
                    dd_releases=sorted({r for c in in_domain for r in c.releases}),
                    tables=sorted({t for c in in_domain
                                   if (t := c.nda_or_nbdc_table or c.table)})[:12],
                    score=in_domain[0].score, family_size=len(in_domain),
                    family_prefix=_common_prefix([c.name for c in in_domain]),
                    cues=cues, candidates=keep[:top_k],
                    reason=(f"{len(in_domain)} candidates across several tables "
                            f"agree on the domain {dom!r}; the paper's wording "
                            "does not identify one table"))
            return ContextMatch(
                status="ambiguous", mention=mention, score=keep[0].score,
                cues=cues, candidates=keep[:top_k],
                reason=(f"candidates span several tables (top table holds "
                        f"{mass / total:.0%} of the mass, need {TABLE_MASS:.0%})"))

        out = ContextMatch(
            status="context_variable",
            mention=mention,
            variable=winner.name,
            label=winner.label,
            nda_or_nbdc_table=winner.nda_or_nbdc_table,
            nbdc_table=winner.table,
            nbdc_domain=winner.domain,
            nbdc_sub_domain=winner.sub_domain,
            study=winner.study,
            dd_releases=winner.releases,
            score=winner.score,
            cues=cues,
            candidates=keep[:top_k],
        )
        if not margin_ok:
            # Several variables in one table fit the wording equally well. Report
            # the family and leave the variable null — the table and domain are
            # what a reader needs, and the paper genuinely did not pick one.
            out.status = "context_family"
            out.variable = None
            out.family_size = len(in_table)
            out.family_prefix = _common_prefix([c.name for c in in_table])
            out.reason = (
                f"{len(in_table)} variables in {best_table} match the wording "
                f"equally well ({winner.name}, {rivals[0].name}, ...); the paper "
                "did not name one")
        return out

    def _instrument_fallback(self, mention: str, query_text: str, cues: dict, *,
                             candidates: Optional[List[Candidate]] = None,
                             reason: str = "") -> ContextMatch:
        """No variable matched — is the paper naming an instrument?"""
        hit = self.match_instrument(query_text)
        if hit:
            return self._instrument_match(mention, hit, cues,
                                          candidates=candidates or [])
        return ContextMatch(status="unmatched", mention=mention, cues=cues,
                            candidates=candidates or [], reason=reason)

    @staticmethod
    def _instrument_match(mention: str, hit: Tuple[str, tuple, int], cues: dict, *,
                          candidates: Optional[List[Candidate]] = None
                          ) -> ContextMatch:
        key, (study, table, nda_table, domain, sub_domain), n = hit
        return ContextMatch(
            status="instrument_table", mention=mention, instrument=key,
            nda_or_nbdc_table=nda_table, nbdc_table=table,
            nbdc_domain=domain, nbdc_sub_domain=sub_domain, study=study,
            score=0.0, family_size=n, cues=cues, candidates=candidates or [],
            reason=("matched an instrument, not a variable: the paper names the "
                    "measure but not which of its variables it used"))

    def match_instrument(self, text: str) -> Optional[Tuple[str, tuple, int]]:
        """Map an instrument phrase (or its acronym) to one table.

        Matching is containment on the normalised phrase, never fuzzy, and an
        acronym is only accepted when it points at exactly one instrument — "FES"
        must not become whichever scale happens to sort first.
        """
        norm = norm_text(text)
        keys: List[str] = []
        if norm in self._instruments and _words(norm) >= 2:
            keys = [norm]
        else:
            for key in self._instruments:
                # Two words minimum. A one-word "instrument" harvested from a
                # label prefix ("Externalizing") is a scale name fragment, and
                # accepting it maps "externalizing behaviors" onto whichever table
                # happens to start a label with that word — an Adult Behavior
                # Checklist row, in the case that made this rule exist.
                if len(key) >= 12 and _words(key) >= 2 and (key in norm or norm in key):
                    keys.append(key)
        if not keys:
            for acr in re.findall(r"\b[A-Z][A-Z0-9\-]{2,7}\b", text or ""):
                cand = self._acronyms.get(acr.replace("-", "").upper())
                if cand and len(cand) == 1:
                    keys = list(cand)
                    break
        if not keys:
            return None
        # Prefer the most specific phrase, then the table it most often sits in.
        key = max(keys, key=len)
        slots = self._instruments[key]
        if not slots:
            return None
        slot, n = slots.most_common(1)[0]
        distinct_tables = {s[2] or s[1] for s in slots}
        if len(distinct_tables) > 1 and n / sum(slots.values()) < TABLE_MASS:
            return None
        return key, slot, n

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _respondent_cue(text: str) -> Optional[str]:
        found = [who for who, pat in RESPONDENT_QUERY_RE.items() if pat.search(text)]
        # An ambiguous sentence ("parent- and youth-reported") gives no cue rather
        # than an arbitrary one.
        return found[0] if len(found) == 1 else None

    @property
    def stats(self) -> dict:
        return {
            "labels_indexed": len(self._rows),
            "tokens": len(self._postings),
            "instruments": len(self._instruments),
            "acronyms": len(self._acronyms),
        }


def _merge_releases(cands: List[Candidate]) -> List[Candidate]:
    """One entry per (study, variable): releases collapse into a list.

    Without this, a variable present in 6.0/6.1/7.0 occupies the top three slots
    and looks like three rival candidates.
    """
    best: Dict[Tuple[str, str], Candidate] = {}
    for c in cands:
        key = (c.study, c.name)
        cur = best.get(key)
        if cur is None:
            best[key] = c
        else:
            cur.releases = sorted(set(cur.releases) | set(c.releases))
            if c.score > cur.score:
                c.releases = cur.releases
                best[key] = c
    return list(best.values())


def _common_prefix(names: Sequence[str]) -> Optional[str]:
    """Longest shared `_`-delimited prefix, e.g. dmri_dtifa_scts for 148 ROIs."""
    if not names:
        return None
    parts = [n.split("_") for n in names]
    out: List[str] = []
    for chunk in zip(*parts):
        if len({c for c in chunk}) != 1:
            break
        out.append(chunk[0])
    return ("_".join(out) + "_*") if out else None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("match", help="map a paper's wording to a variable")
    m.add_argument("mention")
    m.add_argument("--label", help="the extractor's reading of the measure")
    m.add_argument("--context", help="surrounding sentence(s) from the paper")
    m.add_argument("--instrument")
    m.add_argument("--respondent", choices=["parent", "youth", "teacher"])
    m.add_argument("--study", default="abcd")
    m.add_argument("--releases", help="comma-separated, e.g. 6.0,nda-legacy")
    m.add_argument("--top", type=int, default=8)

    i = sub.add_parser("instrument", help="map an instrument phrase to a table")
    i.add_argument("phrase")

    s = sub.add_parser("stats", help="what the index holds")

    a = ap.parse_args(argv)
    dictionary = Dictionary.load()
    index = ContextIndex(dictionary)

    if a.cmd == "stats":
        print(json.dumps(index.stats, indent=1))
        return 0

    if a.cmd == "instrument":
        hit = index.match_instrument(a.phrase)
        if not hit:
            print(json.dumps({"instrument": None, "reason": "no unique match"}))
            return 3
        key, slot, n = hit
        print(json.dumps({"instrument": key, "study": slot[0], "nbdc_table": slot[1],
                          "nda_or_nbdc_table": slot[2], "nbdc_domain": slot[3],
                          "nbdc_sub_domain": slot[4], "variables_in_table": n},
                         indent=1))
        return 0

    res = index.match(
        a.mention, label=a.label, context=a.context, instrument=a.instrument,
        respondent=a.respondent, study=a.study,
        releases=[r.strip() for r in a.releases.split(",")] if a.releases else None,
        top_k=a.top,
    )
    print(json.dumps(res.to_dict(), indent=1))
    return 0 if res.matched else 3


if __name__ == "__main__":
    raise SystemExit(_cli())
