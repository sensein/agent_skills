"""Roles belong to an analysis, not to a variable — and waves are not names.

Two things went wrong in the first ABCD runs, and both come from the same place:
the extractor emits one flat `variables[]` list, so a variable gets one `role` and
one free-text `timepoint`, while the paper actually assigns roles per analysis and
measures the same thing at three waves.

    A paper regresses Y3 on X1 controlling for Y1 and Y2. The extractor writes
    "internalizing behaviors" (outcome), "Internalizing Time 2" (covariate) and
    "Internalizing problems year 1" (unspecified) — three names, three roles, one
    measure. A reader cannot tell they are the same instrument, and the last one
    looks like a variable nobody used.

    A paper runs brain metrics as OUTCOMES of preterm birth, then as MEDIATORS of
    gestational age on cognition. One `role` field has to pick, and whichever it
    picks makes the other analysis disappear.

So this module does three deterministic things, after verification and before
export, using only what the payload already contains:

1. **Waves get an order.** "baseline (Time 1)", "1-year follow-up (Time 2)" and
   "year 3" become comparable integers, so instances of one measure can be sorted
   and compared instead of string-matched.
2. **Wordings collapse into measures.** Names and aliases are stripped of their
   wave tokens; entries whose *name* appears among another entry's stripped
   wordings are the same measure. One measure, several timepoints, one label.
3. **Roles come from `models[]`.** Every model already lists its predictors,
   outcomes, mediators, moderators and covariates — the paper's own assignment,
   per analysis. Cross-referencing those against the variable list yields
   `role_assignments[]` (which role, in which model, on what evidence),
   `roles[]` and `role_varies_by_analysis`. `role` stays a single value for
   readers that expect one, but it is no longer the whole story.

Nothing here invents a role. The one inference is `prior_wave_control`, and it
fires only when the paper itself declares an earlier wave of the same measure as a
covariate — see `_infer_prior_wave_controls`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROLE_FIELDS = (
    ("predictors", "predictor"),
    ("outcomes", "outcome"),
    ("mediators", "mediator"),
    ("moderators", "moderator"),
    ("covariates", "covariate"),
)

# Which single role to print when a variable plays several and none of them is the
# one the paper stated. Ordered by how much a reader loses by not seeing it: an
# outcome or predictor defines what the study is about, a covariate is background.
ROLE_PRECEDENCE = ("outcome", "predictor", "mediator", "moderator", "instrument",
                   "confounder", "control", "covariate", "unspecified")

SUBSTANTIVE_ROLES = ("predictor", "outcome", "mediator", "moderator")

_WS = re.compile(r"\s+")

# Wave wording, most specific first. "1-year follow-up" is unambiguous; "Time 2"
# means the second wave, which is the FIRST follow-up; a bare "year 2" follows
# ABCD's own event naming (`2_year_follow_up_y_arm_1`) and means follow-up 2.
_FOLLOWUP = re.compile(
    r"(\d+)\s*[-‐-―]?\s*(?:year|yr|month|mo|wave)s?\s*[-‐-―]?\s*"
    r"follow\s*[-‐-―]?\s*up", re.I)
_FOLLOWUP_AT = re.compile(
    r"follow\s*[-‐-―]?\s*up\s*(?:at|after)?\s*(\d+)\s*(?:year|yr)s?", re.I)
_BASELINE = re.compile(r"\bbaseline\b|\bt0\b|\bwave\s*0\b|\byear\s*0\b", re.I)
_ORDINAL_ONE_BASED = re.compile(r"\b(?:time|wave|visit|t)\s*[-_]?\s*(\d+)\b", re.I)
_YEAR_N = re.compile(r"\byear\s*[-_]?\s*(\d+)\b", re.I)

# Tokens that name a wave rather than a measure. Stripped before two wordings are
# compared, so "Internalizing Time 2" and "internalizing behaviors" can meet.
_TP_TOKENS = re.compile(
    r"(?:\b(?:at|in|from|during)\s+)?"
    r"(?:\b\d+\s*[-‐-―]?\s*(?:year|yr|month|mo)s?\s*"
    r"[-‐-―]?\s*follow\s*[-‐-―]?\s*up\b"
    r"|\bfollow\s*[-‐-―]?\s*up\b"
    r"|\bbaseline\b"
    r"|\b(?:time|wave|visit)\s*[-_]?\s*\d+\b"
    r"|\byears?\s*[-_]?\s*\d+\b"
    r"|\bt\s*[-_]?\s*[0-9]\b"
    r"|\b(?:y|w)[0-9]\b)", re.I)
_EMPTY_PARENS = re.compile(r"\(\s*[\d\s,;.‐-―-]*\s*\)")
# A dangling preposition left behind by a stripped wave: "age at baseline" -> "age".
_DANGLING = re.compile(r"^(?:the|a)\s+|\s+(?:at|in|on|during|from|of|for)$", re.I)

# Words that make a DERIVED quantity out of a measure. A growth intercept is not
# the repeated measure it was estimated from, and a residualised change score is
# not the raw score — so a wording that adds one of these is a different measure,
# however much of the parent name it repeats.
_DERIVATION = frozenset((
    "intercept", "slope", "change", "residual", "residualised", "residualized",
    "difference", "delta", "latent", "factor", "composite", "ratio", "growth",
    "trajectory", "variability", "average", "mean", "sum", "total", "z-score",
    "zscore", "percentile", "rank", "quartile", "tertile", "decile"))

# Analyses that describe the data rather than model it assign no roles: every
# variable in a correlation matrix is on both axes, and reading that as
# "predictor" and "outcome" made family income a predictor of itself.
_NON_ASSIGNING = re.compile(
    r"bivariate correlation|correlation (?:analys|matrix|among)|descriptive "
    r"statistic|measurement invariance|invariance testing|attrition analys|"
    r"missing data analys", re.I)
_NON_ASSIGNING_KINDS = frozenset(("descriptive", "correlational"))


# --------------------------------------------------------------------------- #
# waves
# --------------------------------------------------------------------------- #

def normalize_timepoint(value: Any) -> Dict[str, Any]:
    """Parse a paper's wave wording into a comparable order.

    Returns `{label, order, cues, ambiguous, multiple}`. `order` is waves since
    baseline (0 = baseline) and is None whenever the wording does not pin one
    down — a range ("baseline and 1-year follow-up") or nothing recognisable.
    Never guessed: an unparsed wave is reported as unparsed, because a wrong order
    silently rewrites which measurement was the outcome.
    """
    text = _WS.sub(" ", str(value or "")).strip()
    if not text:
        return {"label": None, "order": None, "span": None, "cues": [],
                "ambiguous": False, "multiple": False}

    cues: List[Tuple[str, int]] = []
    for m in _FOLLOWUP.finditer(text):
        cues.append(("follow_up", int(m.group(1))))
    for m in _FOLLOWUP_AT.finditer(text):
        cues.append(("follow_up", int(m.group(1))))
    if _BASELINE.search(text):
        cues.append(("baseline", 0))
    for m in _ORDINAL_ONE_BASED.finditer(text):
        n = int(m.group(1))
        if n >= 1:
            cues.append(("ordinal", n - 1))
    for m in _YEAR_N.finditer(text):
        n = int(m.group(1))
        if n >= 1:
            cues.append(("year", n))

    order: Optional[int] = None
    # A baseline cue beats everything: "baseline (year 1)" is a paper numbering its
    # own waves from one, not a follow-up. Then explicit follow-ups, then Time/wave
    # ordinals, and only then a bare "year N".
    for kind in ("baseline", "follow_up", "ordinal", "year"):
        vals = sorted({v for k, v in cues if k == kind})
        if vals:
            order = vals[0]
            break

    orders = {v for _, v in cues}
    multiple = len(orders) > 1 and bool(re.search(r"\band\b|,|through|\bto\b|&", text))
    # Cues that disagree without the wording joining two waves — "Time 2" plus
    # "2-year follow-up" in one string cannot both be right.
    ambiguous = len(orders) > 1 and not multiple and not any(
        k == "baseline" for k, _ in cues)
    span = (min(orders), max(orders)) if multiple and orders else None
    return {"label": text, "order": None if multiple else order, "span": span,
            "cues": [f"{k}:{v}" for k, v in cues], "ambiguous": ambiguous,
            "multiple": multiple}


def wave_key(v: dict) -> str:
    """The wave part of a variable's identity, as a comparable string.

    A span counts as one wave-identity: a paper that writes "baseline through
    3-year follow-up" in one place and "baseline through 3-year follow-up (all
    four waves)" in another has named the same quantity twice, and keying on the
    wording left it standing as two measures.
    """
    if v.get("timepoint_order") is not None:
        return f"w{v['timepoint_order']}"
    span = v.get("timepoint_span")
    if span:
        return f"w{span[0]}-{span[1]}"
    return norm_key(v.get("timepoint"))


def wave_label(order: Optional[int]) -> Optional[str]:
    if order is None:
        return None
    return "baseline" if order == 0 else f"{order}-year follow-up"


# --------------------------------------------------------------------------- #
# measures
# --------------------------------------------------------------------------- #

def strip_timepoint(value: Any) -> str:
    """A wording with its wave tokens removed, normalized for comparison."""
    text = _WS.sub(" ", str(value or "").strip().lower())
    text = _TP_TOKENS.sub(" ", text)
    text = _EMPTY_PARENS.sub(" ", text)
    text = _WS.sub(" ", text).strip(" .,;:-()[]")
    text = _WS.sub(" ", _DANGLING.sub(" ", text)).strip(" .,;:-()[]")
    return _fold_plural(text)


def _fold_plural(text: str) -> str:
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith(("ss", "us", "is")):
        return text[:-1]
    return text


def norm_key(value: Any) -> str:
    """Comparison key that keeps the wave — for matching a model's exact string."""
    text = _WS.sub(" ", str(value or "").strip().lower()).strip(" .,;:()[]")
    return _fold_plural(text)


def _wordings(v: dict) -> List[str]:
    out = [v.get("name"), v.get("mention_as_written"), v.get("variable")]
    for key in ("aliases", "also_written_as"):
        val = v.get(key)
        if isinstance(val, (list, tuple)):
            out.extend(val)
    return [str(w) for w in out if w]


# Same ranking abcd_verify uses to pick a merge winner: how far the mapping got.
_STATUS_RANK = {s: i for i, s in enumerate(
    ("verified", "verified_via_nda_api", "context_variable", "context_family",
     "context_domain", "instrument_table", "ambiguous", "unverified_variable",
     "not_a_variable_name", "no_dictionary_loaded"))}


def _derivation_differs(a: str, b: str) -> bool:
    """True when one wording derives a new quantity from the other."""
    ta, tb = set(a.split()), set(b.split())
    return bool((ta ^ tb) & _DERIVATION)


def assigns_roles(model: dict) -> bool:
    """Whether this analysis actually assigns roles, or merely describes."""
    kind = str(model.get("kind") or "").strip().lower()
    if kind:
        return kind not in _NON_ASSIGNING_KINDS
    return not _NON_ASSIGNING.search(str(model.get("specification") or ""))


def _resolved_variable(v: dict) -> Optional[str]:
    return (v.get("dictionary_match") or {}).get("variable")


def assign_measures(variables: List[dict]) -> List[dict]:
    """Group per-wave entries of one instrument under a shared `measure`.

    Two entries are the same measure when one entry's *name*, stripped of wave
    tokens, is among the other's stripped wordings. Keying on a name rather than on
    any shared alias is what stops a generic alias ("income", "sex") chaining two
    genuinely different measures into one.
    """
    n = len(variables)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    stripped_all: List[set] = []
    stripped_name: List[str] = []
    for v in variables:
        tp = normalize_timepoint(v.get("timepoint"))
        v["timepoint_order"] = tp["order"]
        v["timepoint_span"] = list(tp["span"]) if tp["span"] else None
        v["timepoint_normalized"] = (
            wave_label(tp["order"]) if tp["span"] is None
            else f"{wave_label(tp['span'][0])} to {wave_label(tp['span'][1])}")
        if tp["ambiguous"] or tp["multiple"]:
            v["timepoint_parse"] = tp
        stripped_all.append({s for s in (strip_timepoint(w) for w in _wordings(v)) if s})
        stripped_name.append(strip_timepoint(v.get("name") or v.get("variable")))

    for i in range(n):
        for j in range(i + 1, n):
            if not stripped_name[i] or not stripped_name[j]:
                continue
            linked = (stripped_name[i] in stripped_all[j]
                      or stripped_name[j] in stripped_all[i])
            if not linked or _derivation_differs(stripped_name[i], stripped_name[j]):
                continue
            # Two entries that each resolved to a *different* dictionary variable
            # are not one measure however alike they read — fes_y_ss_fc and
            # fes_p_ss_fc are different respondents, not a naming variant.
            ri, rj = _resolved_variable(variables[i]), _resolved_variable(variables[j])
            if ri and rj and ri != rj:
                continue
            union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    for root, members in groups.items():
        # The label is a NAME the paper printed, from whichever entry resolved
        # furthest — never an alias. Ranking by wording frequency instead made
        # "Crystallized Cognition Composite Score" export as "crystallized
        # intelligence composite score", a name the paper never used.
        best = min(members, key=lambda i: (
            _STATUS_RANK.get(str(variables[i].get("dictionary_status")), 99), i))
        label = stripped_name[best]
        if not label:
            counts: Dict[str, int] = {}
            for i in members:
                for w in stripped_all[i]:
                    counts[w] = counts.get(w, 0) + 1
            label = max(counts, key=lambda w: (counts[w], len(w))) if counts else ""
        key = label or f"measure-{root}"
        waves = sorted({variables[i].get("timepoint_order") for i in members
                        if variables[i].get("timepoint_order") is not None})
        for i in members:
            variables[i]["measure"] = label or variables[i].get("name")
            variables[i]["measure_key"] = key
            if len(members) > 1:
                variables[i]["measure_instances"] = len(members)
                variables[i]["measure_waves"] = waves
    return variables


# --------------------------------------------------------------------------- #
# roles from models
# --------------------------------------------------------------------------- #

def label_models(models: List[dict]) -> List[dict]:
    """Give each analysis a stable id, so a role can point at one."""
    for i, m in enumerate(models, 1):
        m.setdefault("model_id", f"M{i}")
    return models


def assign_roles(variables: List[dict], models: List[dict]) -> Dict[str, Any]:
    """Attach each model's own role declarations to the variables they name.

    A model string is matched to a variable entry by exact wording first. When one
    wording covers several waves of a measure ("family conflict" is both the Time 1
    covariate and the Time 2 mediator), the entry whose stated role matches the
    model's is the one meant; when that still does not single one out, the
    assignment is recorded against the measure instead of guessing a wave.
    """
    label_models(models)

    by_wording: Dict[str, List[int]] = {}
    by_measure: Dict[str, List[int]] = {}
    for i, v in enumerate(variables):
        for w in _wordings(v):
            by_wording.setdefault(norm_key(w), []).append(i)
        if v.get("measure_key"):
            by_measure.setdefault(v["measure_key"], []).append(i)

    for v in variables:
        v["role_assignments"] = []
    unresolved: List[dict] = []

    descriptive = []
    for m in models:
        mid = m.get("model_id")
        if not assigns_roles(m):
            m["assigns_roles"] = False
            descriptive.append(mid)
            continue
        section = (m.get("evidence") or {}).get("section")
        for field, role in ROLE_FIELDS:
            for name in m.get(field) or []:
                key = norm_key(name)
                cands = sorted(set(by_wording.get(key, [])))
                if not cands:
                    cands = sorted(set(by_measure.get(strip_timepoint(name), [])))
                    if len(cands) > 1:
                        cands = []           # measure-level fallback must be exact
                if len(cands) > 1:
                    stated = [i for i in cands
                              if str(variables[i].get("role") or "") == role]
                    cands = stated if len(stated) == 1 else cands
                entry = {"model_id": mid, "role": role, "as_written": str(name),
                         "section": section}
                if len(cands) == 1:
                    variables[cands[0]]["role_assignments"].append(entry)
                else:
                    unresolved.append({
                        **entry,
                        "measure": strip_timepoint(name) or None,
                        "reason": ("ambiguous_across_waves" if cands
                                   else "not_declared_in_variables"),
                        "candidate_timepoints": [variables[i].get("timepoint")
                                                 for i in cands],
                    })

    for v in variables:
        _finalise_variable_roles(v)
    _infer_prior_wave_controls(variables)

    varies = [v for v in variables if v.get("role_varies_by_analysis")]
    return {
        "models_indexed": len(models),
        "models_descriptive_only": descriptive,
        "variables_with_model_roles": sum(
            1 for v in variables if v.get("role_assignments")),
        "variables_role_varies_by_analysis": len(varies),
        "variables_role_from_model_only": sum(
            1 for v in variables if v.get("role_basis") == "model_declaration"),
        "variables_role_inferred_prior_wave": sum(
            1 for v in variables if v.get("role_basis") == "prior_wave_control"),
        "variables_role_unresolved": sum(
            1 for v in variables if v.get("role") == "unspecified"),
        "unresolved_model_role_mentions": unresolved,
        "note": ("A role is a property of one analysis. `roles` lists every role a "
                 "variable plays across `models[]`, `role_assignments` says in "
                 "which; `role` is the single headline value and loses that "
                 "detail whenever `role_varies_by_analysis` is true."),
    }


def _finalise_variable_roles(v: dict) -> None:
    stated = str(v.get("role") or "unspecified").lower()
    model_roles: List[str] = []
    for a in v.get("role_assignments") or []:
        if a["role"] not in model_roles:
            model_roles.append(a["role"])
    ordered = [r for r in ROLE_PRECEDENCE if r in model_roles]

    if not ordered:
        v["roles"] = [stated] if stated != "unspecified" else []
        v["role_varies_by_analysis"] = False
        v["role_basis"] = "paper_statement" if stated != "unspecified" else "unresolved"
        v["role_summary"] = stated
        return

    v["roles"] = ordered
    v["role_varies_by_analysis"] = len(ordered) > 1
    if stated in ordered:
        v["role"] = stated
        v["role_basis"] = "paper_statement_confirmed_by_model"
    else:
        if stated != "unspecified":
            v["role_stated_in_text"] = stated
            v["role_conflict"] = {
                "stated": stated, "declared_in_models": ordered,
                "note": ("The prose called it one thing and the model arrays "
                         "another; the model arrays win, both are kept."),
            }
        v["role"] = ordered[0]
        v["role_basis"] = "model_declaration"

    by_role: Dict[str, List[str]] = {}
    per_model: Dict[str, set] = {}
    for a in v["role_assignments"]:
        by_role.setdefault(a["role"], []).append(a["model_id"])
        per_model.setdefault(a["model_id"], set()).add(a["role"])
    v["role_summary"] = "; ".join(
        f"{r} ({', '.join(by_role[r])})" for r in ordered)
    # Both sides of one model is not a contradiction — it is a cross-lagged or
    # bidirectional design, and flattening it to one role hides the whole point of
    # the analysis.
    both = sorted(mid for mid, rs in per_model.items()
                  if {"predictor", "outcome"} <= rs)
    if both:
        v["bidirectional_in"] = both


def _infer_prior_wave_controls(variables: List[dict]) -> None:
    """Earlier waves of an outcome the paper already controls for are controls.

    An autoregressive design measures Y at three waves, models the last one and
    adjusts for the earlier ones. The wave that only ever surfaces in Table 1 then
    has no model to take a role from and lands as `unspecified`, reading as a
    variable the study never used.

    The inference fires only where the paper has already shown its hand: some
    *other* wave of the same measure is a declared covariate, and an outcome wave
    sits later than the entry in question. Without a declared covariate wave
    nothing is inferred — the alternative is turning every descriptive-table row
    into a control the paper never mentioned.
    """
    groups: Dict[str, List[dict]] = {}
    for v in variables:
        key = v.get("measure_key")
        if key:
            groups.setdefault(key, []).append(v)

    for members in groups.values():
        if len(members) < 2:
            continue
        outcome_orders = [v["timepoint_order"] for v in members
                          if v.get("timepoint_order") is not None
                          and "outcome" in (v.get("roles") or [])]
        cov_orders = [v["timepoint_order"] for v in members
                      if v.get("timepoint_order") is not None
                      and "covariate" in (v.get("roles") or [])]
        if not outcome_orders or not cov_orders:
            continue
        last_outcome = max(outcome_orders)
        if not any(c < last_outcome for c in cov_orders):
            continue
        for v in members:
            order = v.get("timepoint_order")
            if (order is None or order >= last_outcome
                    or v.get("roles") or v.get("role") != "unspecified"):
                continue
            v["role"] = "covariate"
            v["roles"] = ["covariate"]
            v["role_varies_by_analysis"] = False
            v["role_basis"] = "prior_wave_control"
            v["role_summary"] = "covariate (prior wave of the outcome)"
            v["role_inference"] = {
                "rule": "prior_wave_control",
                "why": (f"{v.get('measure')} is the outcome at wave {last_outcome} "
                        f"and an earlier wave of it is a declared covariate, so "
                        f"this wave {order} instance is an autoregressive control."),
                "declared_covariate_waves": sorted(set(cov_orders)),
                "outcome_waves": sorted(set(outcome_orders)),
            }


# --------------------------------------------------------------------------- #
# the matrix a reader actually wants
# --------------------------------------------------------------------------- #

_ABBREV = {"predictor": "pred", "outcome": "out", "mediator": "med",
           "moderator": "mod", "covariate": "cov", "confounder": "conf",
           "control": "ctrl", "instrument": "instr", "unspecified": "—"}


def role_matrix(variables: List[dict], models: List[dict]) -> Dict[str, Any]:
    """Variables x analyses, each cell the role that analysis gave it."""
    rows = []
    for v in variables:
        cells: Dict[str, List[str]] = {}
        for a in v.get("role_assignments") or []:
            cells.setdefault(a["model_id"], [])
            if a["role"] not in cells[a["model_id"]]:
                cells[a["model_id"]].append(a["role"])
        if not cells and v.get("role") in (None, "unspecified"):
            continue
        rows.append({
            "variable": v.get("measure") or v.get("name"),
            "as_written": v.get("mention_as_written") or v.get("name"),
            "timepoint": v.get("timepoint_normalized") or v.get("timepoint"),
            "cells": {mid: "/".join(_ABBREV.get(r, r) for r in rs)
                      for mid, rs in cells.items()},
            "role_basis": v.get("role_basis"),
        })
    return {
        # Only analyses that assign roles get a column; a correlation matrix would
        # contribute a column of blanks and imply the variables were left out of it.
        "models": [{"model_id": m.get("model_id"),
                    "specification": m.get("specification"),
                    "section": (m.get("evidence") or {}).get("section")}
                   for m in models if assigns_roles(m)],
        "descriptive_models": [
            {"model_id": m.get("model_id"), "specification": m.get("specification")}
            for m in models if not assigns_roles(m)],
        "rows": rows,
        "legend": {v: k for k, v in _ABBREV.items() if k != "unspecified"},
    }


def annotate(doc: dict) -> dict:
    """Run the whole pass over a verified document, in place."""
    variables = doc.get("variables") or []
    models = doc.get("models") or []
    assign_measures(variables)
    summary = assign_roles(variables, models)
    doc["role_analysis"] = summary
    doc["role_matrix"] = role_matrix(variables, models)
    return doc


# --------------------------------------------------------------------------- #
# self-check
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    """`python -m scripts.abcd_roles` — the wave and role rules, asserted.

    These are the cases that were wrong in the first corpus run, kept as tests so
    a future change to the regexes has to face them again.
    """
    def order(text):
        return normalize_timepoint(text)["order"]

    # A paper numbering its waves from one still means baseline.
    assert order("baseline") == 0
    assert order("baseline (Time 1)") == 0
    assert order("baseline (year 1)") == 0
    assert order("Time 1") == 0
    assert order("1-year follow-up (Time 2)") == 1
    assert order("2-year follow-up (Time 3)") == 2
    assert order("Year 2") == 2
    assert order("wave 3") == 2
    assert order("") is None
    span = normalize_timepoint("baseline through 3-year follow-up (all four waves)")
    assert span["order"] is None and span["span"] == (0, 3), span

    assert strip_timepoint("Internalizing Time 2") == "internalizing"
    assert strip_timepoint("Internalizing problems year 1") == "internalizing problem"
    assert strip_timepoint("age at baseline") == "age"
    assert _derivation_differs("family conflict", "family conflict intercept")
    assert not _derivation_differs("family income", "income")

    # One measure, three waves, three roles — the case the flat list flattened.
    variables = [
        {"name": "internalizing behaviors", "timepoint": "2-year follow-up (Time 3)",
         "role": "outcome", "aliases": ["Internalizing Time 3"]},
        {"name": "Internalizing Time 2", "timepoint": "1-year follow-up (Time 2)",
         "role": "covariate", "aliases": ["internalizing behaviors"]},
        {"name": "Internalizing problems year 1", "timepoint": "baseline (year 1)",
         "role": "unspecified", "aliases": ["internalizing behaviors"]},
    ]
    models = [{"specification": "mediation model", "outcomes": ["internalizing behaviors"],
               "covariates": ["Internalizing Time 2"], "evidence": {"section": "Methods"}},
              {"specification": "bivariate correlations among all study variables",
               "predictors": ["internalizing behaviors"], "evidence": {}}]
    assign_measures(variables)
    assert {v["measure"] for v in variables} == {"internalizing behavior"}, variables
    summary = assign_roles(variables, models)
    assert summary["models_descriptive_only"] == ["M2"], summary
    assert variables[0]["role"] == "outcome"
    assert variables[1]["role"] == "covariate"
    # Inferred, and only because wave 1 of the same measure is a declared covariate.
    assert variables[2]["role"] == "covariate", variables[2]
    assert variables[2]["role_basis"] == "prior_wave_control"

    # A brain metric that is an outcome in one analysis and a mediator in another.
    v = [{"name": "fractional anisotropy", "role": "mediator"}]
    m = [{"specification": "mixed linear model", "outcomes": ["fractional anisotropy"],
          "evidence": {}},
         {"specification": "mediation analysis", "mediators": ["fractional anisotropy"],
          "evidence": {}}]
    assign_measures(v)
    assign_roles(v, m)
    assert v[0]["roles"] == ["outcome", "mediator"], v[0]["roles"]
    assert v[0]["role_varies_by_analysis"]
    assert v[0]["role_summary"] == "outcome (M1); mediator (M2)"

    # Cross-lagged: both sides of the same model is a design, not a contradiction.
    v = [{"name": "externalizing behavior", "role": "predictor"}]
    m = [{"specification": "bivariate LCM-SR", "predictors": ["externalizing behavior"],
          "outcomes": ["externalizing behavior"], "evidence": {}}]
    assign_measures(v)
    assign_roles(v, m)
    assert v[0]["bidirectional_in"] == ["M1"], v[0]

    print("abcd_roles: all self-checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
