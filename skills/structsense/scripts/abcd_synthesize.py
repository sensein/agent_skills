"""Cross-paper synthesis over verified ABCD/HBCD extractions.

Answers the questions a literature review actually asks — and shows its work, so
every aggregate can be taken apart:

  * **Where is there consensus across constructs?** Group findings by Cognitive
    Atlas construct, tally which papers report which direction, and report the
    majority direction with an agreement score computed over paper-direction
    claims (not over findings).
  * **Where is there divergence?** A construct is divergent when papers report
    *opposing* directions, not merely when effect sizes differ. Disagreement about
    sign is the interesting case.
  * **Are some variables consistently mediators/moderators?** For every variable,
    tally the roles it plays across papers. Consistency needs two things: the role
    recurs across papers, AND it is the only substantive role in most of them. A
    variable that is a mediator in every paper but also an outcome in every paper
    is contested, not consistent.
  * **What measured what, in which release, on whose sample?** Every construct
    carries the variables that operationalised it; every variable carries, per
    paper, the wording used, the dictionary variable it resolved to, the table,
    the domain and the dictionary release the mapping holds in; every paper
    carries its dataset (release, sample, waves, cohort, source).

Three rules keep the aggregates honest:

  1. **Counting is by PAPER, never by finding.** One paper reporting the same
     association in six models must not outvote five other papers — that would
     turn verbosity into evidence.
  2. **Own-study only.** The per-paper verifier has already dropped variables and
     results that belonged to cited work, so a paper's summary of the literature
     cannot arrive here as independent evidence.
  3. **Nothing is asserted without provenance.** Every row carries `paper_evidence`
     — paper id, section, verbatim quote, the mention as written — so a synthesis
     claim can be checked without re-reading the corpus.

    python -m scripts.abcd_synthesize ./out/*_abcd.json --out ./out/abcd_synthesis
    python -m scripts.abcd_synthesize ./out --min-papers 3
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
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from scripts import abcd_export, abcd_roles

# A construct/variable needs this many distinct papers before we call anything a
# consensus. Below it, we report the observation without a verdict.
DEFAULT_MIN_PAPERS = 2
# Share of papers that must agree for "consistent"; below it, "contested".
AGREEMENT_THRESHOLD = 0.70

SUBSTANTIVE_DIRECTIONS = ("positive", "negative", "null")
_WS = re.compile(r"\s+")



def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm_key(value: Any) -> str:
    """Grouping key for a mention: case, spacing and plural folded.

    Without this, "family income" and "Family income" are two synthesis rows with
    one paper each instead of one row with two — which reads as no agreement.
    """
    text = _WS.sub(" ", str(value or "").strip().lower()).strip(" .,;:()[]")
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith(("ss", "us", "is")):
        return text[:-1]
    return text


def _first_int(*values: Any) -> Optional[int]:
    """Largest number appearing in any of `values` — sample sizes as printed."""
    best = None
    for value in values:
        for match in re.finditer(r"\d[\d,]{1,9}", str(value or "")):
            n = int(match.group(0).replace(",", ""))
            if best is None or n > best:
                best = n
    return best


def _ev(paper_id: str, item: dict, **extra) -> dict:
    ev = item.get("evidence") or {}
    return {
        "paper_id": paper_id,
        "section": ev.get("section"),
        "page": ev.get("page"),
        "quote": ev.get("quote"),
        "used_context": ev.get("used_context"),
        "char_start": ev.get("start"),
        "char_end": ev.get("end"),
        **extra,
    }


# --------------------------------------------------------------------------- #
# what a finding is about: the relationship, not just the construct
# --------------------------------------------------------------------------- #

# Latent growth factors are derived from one measure: "family conflict intercept"
# and "family conflict slope" are both family conflict, so a correlation between
# them is change over time in one variable, not an association between two.
_GROWTH_FACTOR = re.compile(r"\s+(?:intercepts?|slopes?|quadratic|growth factors?)$",
                            re.I)

_TREND = re.compile(r"\b(?:over time|across (?:the )?(?:waves|years|time ?points)|"
                    r"with age|over (?:the )?(?:four|three|two|\d+) (?:years|waves))\b",
                    re.I)
_ASSOCIATION = re.compile(r"predict|associat|correlat|mediat|moderat|relat(?:ed|ion)|"
                          r"\blinked\b|cross-lagged", re.I)

_MODERATION = re.compile(r"moderat|differ(?:ence|ed)?s? by|interaction", re.I)

RELATIONSHIP_KINDS = {
    "association": "association between two variables",
    "multi_variable": "path through three or more variables (e.g. mediation)",
    "change_over_time": "change or stability of one variable over time",
    "unspecified": "no variables named",
}


def _relationship(finding: dict, vindex: "VariableIndex") -> dict:
    """The relationship a finding is about, keyed on resolved variable identity.

    Grouping findings by construct alone counted "income -> lower family conflict"
    (negative) and "financial strain -> higher family conflict" (positive) as
    papers disagreeing about family conflict, and a decline in one variable over
    time as a negative association. Two findings disagree only when they are
    about the same relationship and have opposing signs.
    """
    bases: Dict[str, str] = {}      # one measure, whatever growth factor
    full: Dict[str, str] = {}       # intercept and slope kept apart
    for name in finding.get("variables") or []:
        name = str(name or "").strip()
        if not name:
            continue
        # "Externalizing (1)" is externalizing at timepoint 1: the timepoint is
        # not part of what the relationship is about.
        name = re.sub(r"\s*\((?:\d+|T\d+|time \d+|wave \d+)\)\s*$", "", name, flags=re.I) or name
        base = _GROWTH_FACTOR.sub("", name) or name
        bkey = vindex.key(base) or _norm_key(base)
        if not bkey:
            continue
        bases.setdefault(bkey, base)
        factor = _GROWTH_FACTOR.search(name)
        fkey = bkey + (f"#{factor.group(0).strip().lower()}" if factor else "")
        full.setdefault(fkey, name if factor else base)
    # Several variables each changing over time ("both family conflict and
    # externalizing decreased over time") are trends, not an association.
    text = f"{finding.get('statement') or ''} {(finding.get('evidence') or {}).get('quote') or ''}"
    trend_only = (len(bases) > 1 and _TREND.search(text)
                  and not _ASSOCIATION.search(text))
    if len(bases) == 1 or trend_only:
        keys = sorted(bases)
        names = [bases[k] for k in keys]
    else:
        keys = sorted(full)
        names = [full[k] for k in keys]
    if not keys:
        kind, label = "unspecified", "(no variables named)"
    elif len(bases) == 1 and _MODERATION.search(text):
        kind, label = "unspecified", f"{names[0]} as moderator (other variables not named)"
    elif len(bases) == 1 or trend_only:
        kind, label = "change_over_time", f"{' and '.join(names)} over time"
    elif len(keys) == 2:
        kind, label = "association", f"{names[0]} \u2194 {names[1]}"
    else:
        kind, label = "multi_variable", " / ".join(names)
    return {"key": f"{kind}|" + "|".join(keys), "kind": kind, "label": label,
            "variable_keys": keys, "variables": names}


# --------------------------------------------------------------------------- #
# per-paper dataset description
# --------------------------------------------------------------------------- #

def _paper_row(doc: dict) -> dict:
    """One paper's identity plus the dataset it analysed.

    The dataset fields are the difference between "three papers agree" and "three
    papers agree, all analysing the same 11,868 children at the same two waves" —
    which is a much weaker claim, and only visible if the sample is reported.
    """
    pid = str(doc.get("paper_id") or "?")
    meta = doc.get("source_metadata") or {}
    prov = doc.get("provenance") or {}
    ver = doc.get("verification") or {}
    cov = doc.get("coverage") or {}
    variables = doc.get("variables") or []

    tables = sorted({str(v["nda_or_nbdc_table"]) for v in variables
                     if v.get("nda_or_nbdc_table")})
    domains = sorted({str(v["nbdc_domain"]) for v in variables if v.get("nbdc_domain")})
    dd_releases = sorted({r for v in variables
                          for r in (v.get("dd_releases_containing") or [])})
    return {
        "paper_id": pid,
        "title": meta.get("paper_title"),
        "doi": meta.get("doi"),
        "source_path": meta.get("source_path"),
        "dataset": {
            "study": meta.get("study"),
            "data_release": meta.get("data_release"),
            "data_source": meta.get("data_source"),
            "cohort": meta.get("cohort"),
            "sample_size": meta.get("sample_size"),
            "analytic_sample": meta.get("analytic_sample"),
            "sample_n": _first_int(meta.get("analytic_sample"), meta.get("sample_size")),
            "design": meta.get("design"),
            "timepoints": meta.get("timepoints") or None,
            "site_count": meta.get("site_count"),
        },
        "dictionary": {
            # Which release the mapping was actually checked against, and which
            # releases the resolved variables exist in. A row that says 5.0 and a
            # row that says 7.0 are not directly comparable, and the reader has to
            # be able to see that in the table.
            "matched_against": prov.get("dd_releases_matched_against"),
            "dd_releases_of_resolved_variables": dd_releases or None,
            "tables_used": tables or None,
            "domains_used": domains or None,
        },
        "counts": {
            "variables": len(variables),
            "variables_with_table": ver.get("variables_with_table"),
            "variables_mapped": ver.get("variables_mapped_any_method"),
            "constructs": len(doc.get("constructs") or []),
            "models": len(doc.get("models") or []),
            "findings": len(doc.get("findings") or []),
            "rejected": len(doc.get("rejected") or []),
            "rejected_as_cited_work": ver.get("rejected_as_cited_work"),
            "referenced_but_not_declared": len(
                cov.get("referenced_but_not_declared") or []),
        },
        # Back-compat with the previous shape, which readers and the exporter used.
        "study": meta.get("study"),
        "data_release": meta.get("data_release"),
        "variable_count": len(variables),
        "finding_count": len(doc.get("findings") or []),
        "rejected_count": len(doc.get("rejected") or []),
    }


# --------------------------------------------------------------------------- #
# variable identity across papers
# --------------------------------------------------------------------------- #

class VariableIndex:
    """Decides when two papers are talking about the same variable.

    Precedence is deliberate:

      1. **the resolved dictionary variable** — if two papers' wordings both
         resolve to `fes_y_ss_fc`, they are the same measure however differently
         they were phrased ("family conflict", "FES-Conflict youth report").
      2. **an alias the paper declared** — "FA" is folded into "fractional
         anisotropy" when the paper says so, instead of becoming a second row.
      3. **the measure**, wave stripped — "Internalizing Time 2" and "internalizing
         behaviors" are one measure at two waves, and keying on the wording gave a
         corpus two variables where the papers used one.
      4. **the normalised mention** — case and plural folded.

    What it will NOT do is merge on similarity. `fes_y_ss_fc` and `fes_p_ss_fc`
    stay separate rows because youth-report and parent-report conflict are
    different measures, and a synthesis that merges them is reporting agreement
    between two things nobody measured together.
    """

    def __init__(self) -> None:
        self._alias_to_key: Dict[str, str] = {}
        self._var_to_key: Dict[str, str] = {}
        self._measure_to_key: Dict[str, str] = {}

    def learn(self, docs: Sequence[dict]) -> None:
        for doc in docs:
            for v in doc.get("variables") or []:
                mention_key = _norm_key(v.get("name") or v.get("variable"))
                if not mention_key:
                    continue
                resolved = ((v.get("dictionary_match") or {}).get("variable")
                            if v.get("dictionary_status") in
                            ("verified", "verified_via_nda_api", "context_variable")
                            else None)
                measure = abcd_roles.strip_timepoint(v.get("measure")
                                                     or v.get("name")
                                                     or v.get("variable"))
                key = (f"var:{str(resolved).lower()}" if resolved
                       else f"measure:{measure}" if measure else mention_key)
                if resolved:
                    self._var_to_key[str(resolved).lower()] = key
                if measure:
                    self._measure_to_key.setdefault(measure, key)
                self._alias_to_key.setdefault(mention_key, key)
                for alias in v.get("aliases") or []:
                    ak = _norm_key(alias)
                    if ak:
                        self._alias_to_key.setdefault(ak, key)

    def key(self, mention: Any) -> str:
        mk = _norm_key(mention)
        if not mk:
            return ""
        if mk in self._alias_to_key:
            return self._alias_to_key[mk]
        if mk in self._var_to_key:
            return self._var_to_key[mk]
        measure = abcd_roles.strip_timepoint(mention)
        if measure in self._measure_to_key:
            return self._measure_to_key[measure]
        return mk


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #

def synthesize(docs: List[dict], *, min_papers: int = DEFAULT_MIN_PAPERS,
               agreement_threshold: float = AGREEMENT_THRESHOLD) -> dict:
    """Build the synthesis document from per-paper extraction results."""
    papers = [_paper_row(doc) for doc in docs]
    by_paper = {p["paper_id"]: p for p in papers}
    vindex = VariableIndex()
    vindex.learn(docs)

    # A construct id found by one paper's lookup is the same id for a sibling paper
    # that failed to map the identical label — the vocabulary is shared, so the id
    # is reused rather than looked up again (and never invented).
    label_to_id: Dict[str, Tuple[str, str]] = {}
    for doc in docs:
        for c in (doc.get("constructs") or []) + (doc.get("findings") or []):
            cid = c.get("construct_id")
            label = c.get("construct_label") or c.get("construct")
            if cid and label:
                label_to_id.setdefault(_norm_key(label), (str(cid), str(label)))

    def construct_key(item: dict) -> Tuple[str, str, bool]:
        cid = item.get("construct_id")
        label = item.get("construct_label") or item.get("construct")
        if not label:
            return "unmapped:(none stated)", "(no construct stated)", False
        nk = _norm_key(label)
        if not cid and nk in label_to_id:
            cid = label_to_id[nk][0]
        if cid:
            return str(cid), str(label_to_id.get(nk, (cid, label))[1]), True
        return f"unmapped:{nk}", str(label), False

    # ---- accumulators ---------------------------------------------------- #
    c_dir: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    c_label: Dict[str, str] = {}
    c_mapped: Dict[str, bool] = {}
    c_ev: Dict[str, List[dict]] = defaultdict(list)
    # Papers that STUDIED the construct, whether or not they reported a directional
    # finding about it. Counting only papers with a direction made "attention" look
    # like it appeared in zero papers when one paper's methods section named it.
    c_papers: Dict[str, Set[str]] = defaultdict(set)
    # Declared measures (the paper said "we operationalised X with Y") are kept
    # apart from variables that merely appear in a finding about X. Conflating them
    # makes every co-mentioned variable look like an indicator of the construct —
    # "internalizing behaviors measured by financial strain", which no paper said.
    c_measures: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    c_involved: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    c_findings: Dict[str, List[dict]] = defaultdict(list)
    # Relationship-level: the unit at which two results can actually disagree.
    r_info: Dict[str, dict] = {}
    r_dir: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    r_findings: Dict[str, List[dict]] = defaultdict(list)

    v_role: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    v_paper_roles: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    v_ev: Dict[str, List[dict]] = defaultdict(list)
    v_uses: Dict[str, Dict[str, dict]] = defaultdict(dict)     # key -> paper -> use
    v_surface: Dict[str, Counter] = defaultdict(Counter)
    v_declared: Dict[str, Set[str]] = defaultdict(set)
    pair: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    for doc in docs:
        pid = str(doc.get("paper_id") or "?")
        release = (doc.get("source_metadata") or {}).get("data_release")

        for v in doc.get("variables") or []:
            mention = str(v.get("name") or v.get("variable") or "").strip()
            key = vindex.key(mention)
            if not key:
                continue
            # Every role the paper's analyses gave it, not just the headline one:
            # a variable that is an outcome in one model and a mediator in another
            # is genuinely both, and counting only one of them made the
            # cross-paper role-consistency verdict a coin toss.
            roles = [str(r).lower() for r in (v.get("roles") or [])] or [
                str(v.get("role") or "unspecified").lower()]
            role = roles[0]
            match = v.get("dictionary_match") or {}
            v_surface[key][mention] += 1
            v_declared[key].add(pid)
            for r in roles:
                v_role[key][r].add(pid)
                v_paper_roles[key][pid].add(r)
            v_ev[key].append(_ev(pid, v, role=role,
                                 mention_as_written=v.get("mention_as_written"),
                                 dictionary_status=v.get("dictionary_status")))
            use = v_uses[key].setdefault(pid, {
                "paper_id": pid,
                "doi": (doc.get("source_metadata") or {}).get("doi"),
                "data_release": release,
                "mentions": [],
                "roles": set(),
                "timepoints": set(),
                "instruments": set(),
                "respondents": set(),
                "metrics": set(),
                "dictionary_status": v.get("dictionary_status"),
                "dictionary_variable": match.get("variable"),
                "match_method": match.get("match_method"),
                "match_score": match.get("match_score"),
                "nda_or_nbdc_table": v.get("nda_or_nbdc_table"),
                "nbdc_domain": v.get("nbdc_domain"),
                "nbdc_sub_domain": v.get("nbdc_sub_domain"),
                "dd_releases": list(v.get("dd_releases_containing") or []),
                "family_prefix": match.get("family_prefix"),
                "quotes": [],
                "declared": True,
            })
            use["mentions"].append(mention)
            use["roles"].update(roles)
            for field, target in (("timepoint", "timepoints"),
                                  ("instrument", "instruments"),
                                  ("respondent", "respondents"),
                                  ("metric", "metrics")):
                if v.get(field):
                    use[target].add(str(v[field]))
            ev = v.get("evidence") or {}
            if ev.get("quote"):
                use["quotes"].append({"section": ev.get("section"),
                                      "page": ev.get("page"),
                                      "quote": ev.get("quote")})
            # Prefer the strongest mapping this paper found for the variable.
            if v.get("nda_or_nbdc_table") and not use["nda_or_nbdc_table"]:
                use.update({"nda_or_nbdc_table": v.get("nda_or_nbdc_table"),
                            "nbdc_domain": v.get("nbdc_domain"),
                            "dictionary_status": v.get("dictionary_status"),
                            "dictionary_variable": match.get("variable")})

        # Roles also come from the models — a variable can be declared once and
        # used as a covariate in one model and a mediator in another.
        for m in doc.get("models") or []:
            for field, role in (("predictors", "predictor"), ("outcomes", "outcome"),
                                ("mediators", "mediator"), ("moderators", "moderator"),
                                ("covariates", "covariate")):
                for name in m.get(field) or []:
                    key = vindex.key(name)
                    if not key:
                        continue
                    v_role[key][role].add(pid)
                    v_paper_roles[key][pid].add(role)
                    v_surface[key][str(name).strip()] += 1
                    v_ev[key].append(_ev(pid, m, role=role, from_model=True))
                    use = v_uses[key].setdefault(pid, _bare_use(pid, doc, name))
                    use["roles"].update(roles)

        for c in doc.get("constructs") or []:
            cid, label, mapped = construct_key(c)
            c_label.setdefault(cid, label)
            c_mapped[cid] = mapped
            c_papers[cid].add(pid)
            c_ev[cid].append(_ev(pid, c, mention=c.get("construct")))
            for name in c.get("measured_by") or []:
                key = vindex.key(name)
                if key:
                    c_measures[cid][key].add(pid)
                    pair[(key, cid)].add(pid)

        # Outcomes of models that adjusted for covariates: a finding about one of
        # them (and not a bivariate correlation) was estimated with confounders
        # controlled, which the quality grade credits.
        adjusted_keys: Set[str] = set()
        for m in doc.get("models") or []:
            if m.get("covariates"):
                for o in (m.get("outcomes") or []) + (m.get("mediators") or []):
                    k = vindex.key(o)
                    if k:
                        adjusted_keys.add(k)

        for f_index, f in enumerate(doc.get("findings") or []):
            # The same pair printed in the other order is the same result; it is
            # still a row in Findings but counts once here.
            if f.get("mirror_of") is not None:
                continue
            ev_text = " ".join(str(x) for x in (
                (f.get("evidence") or {}).get("quote"),
                ((f.get("evidence") or {}).get("table_context") or {}).get("group")) if x)
            f = {**f, "_adjusted": bool(
                str(f.get("claim_type") or "") not in ("correlation", "descriptive")
                and (any(vindex.key(v) in adjusted_keys for v in f.get("variables") or [])
                     or _ADJUSTED_WORDS.search(ev_text)))}
            cid, label, mapped = construct_key(f)
            c_label.setdefault(cid, label)
            c_mapped.setdefault(cid, mapped)
            direction = str(f.get("direction") or "unspecified").lower()
            # The first wording seen names the relationship everywhere, so one
            # relationship never appears under two labels.
            rel = r_info.setdefault(*(lambda r: (r["key"], r))(_relationship(f, vindex)))
            r_dir[rel["key"]][direction].add(pid)
            r_findings[rel["key"]].append({"paper_id": pid, "_finding_index": f_index,
                                           **f, "_relationship": rel})
            c_papers[cid].add(pid)
            # A trend in one variable says nothing about the direction of an
            # association, so it does not enter the construct's direction tally.
            if rel["kind"] != "change_over_time":
                c_dir[cid][direction].add(pid)
            c_ev[cid].append(_ev(pid, f, direction=direction,
                                 relationship=rel["label"],
                                 relationship_kind=rel["kind"],
                                 statement=f.get("statement"),
                                 effect_size=f.get("effect_size"),
                                 statistic=f.get("statistic"),
                                 analytic_n=f.get("analytic_n"),
                                 subgroup=f.get("subgroup"),
                                 role=f.get("role")))
            c_findings[cid].append({"paper_id": pid, **f, "_relationship": rel})
            role = str(f.get("role") or "").lower()
            for name in f.get("variables") or []:
                key = vindex.key(name)
                if not key:
                    continue
                pair[(key, cid)].add(pid)
                c_involved[cid][key].add(pid)
                v_surface[key][str(name).strip()] += 1
                v_uses[key].setdefault(pid, _bare_use(pid, doc, name))
                if role in ("mediator", "moderator"):
                    v_role[key][role].add(pid)
                    v_paper_roles[key][pid].add(role)
                    v_ev[key].append(_ev(pid, f, role=role, from_finding=True))

    # ---- rows ------------------------------------------------------------- #
    variables = [
        _variable_row(key, v_surface[key], v_role[key], v_paper_roles[key],
                      v_uses[key], v_ev[key], v_declared[key],
                      min_papers, agreement_threshold)
        for key in v_role.keys() | v_uses.keys()
    ]
    variables.sort(key=lambda r: (-r["paper_count"], r["variable"]))
    var_by_key = {r["key"]: r for r in variables}

    constructs = [
        _construct_row(cid, c_label.get(cid, cid), c_mapped.get(cid, False),
                       c_dir.get(cid, {}), c_ev[cid], c_measures.get(cid, {}),
                       c_involved.get(cid, {}), var_by_key, min_papers,
                       agreement_threshold, c_papers.get(cid, set()))
        for cid in set(c_dir) | set(c_label)
    ]
    constructs.sort(key=lambda r: (-r["paper_count"], r["construct_label"] or ""))

    claims = [_claim(row, c_findings.get(row["construct_id_or_key"], []), by_paper,
                     agreement_threshold)
              for row in constructs]
    claims = [c for c in claims if c]
    claims.sort(key=lambda c: (-c["paper_count"], c["claim"]))
    for i, claim in enumerate(claims, 1):
        claim["claim_id"] = f"C{i}"
    # A construct whose opposing signs come from different relationships is not
    # divergent; the claim found that out, so the construct row says so too.
    claim_verdict = {c["construct_label"]: c["verdict"] for c in claims}
    for row in constructs:
        if (row["verdict"] == "divergent"
                and claim_verdict.get(row["construct_label"]) == "different_relationships"):
            row["verdict"] = "different_relationships"

    relationships = [
        _relationship_row(r_info[k], r_dir[k], r_findings[k], by_paper,
                          min_papers, agreement_threshold)
        for k in r_info
    ]
    _order = {"divergent": 0, "consensus": 1, "mixed": 2, "insufficient_papers": 3,
              "no_directional_finding": 4, "change_over_time": 5}
    relationships.sort(key=lambda r: (r["kind"] == "change_over_time",
                                      _order.get(r["verdict"], 9), -r["paper_count"],
                                      r["label"]))
    for i, rel in enumerate(relationships, 1):
        rel["relationship_id"] = f"R{i}"

    links = [
        {"variable_key": var, "variable": var_by_key.get(var, {}).get("variable", var),
         "nda_or_nbdc_table": var_by_key.get(var, {}).get("nda_or_nbdc_table"),
         "construct_id": None if cid.startswith("unmapped:") else cid,
         "construct_label": c_label.get(cid, cid),
         "paper_count": len(pids), "papers": sorted(pids)}
        for (var, cid), pids in pair.items()
    ]
    links.sort(key=lambda r: -r["paper_count"])

    return {
        "synthesis_id": f"abcd-{len(papers)}papers",
        "papers": papers,
        "claims": claims,
        "relationships": relationships,
        "constructs": constructs,
        "variables": variables,
        "variable_construct_links": links,
        "totals": {
            "papers": len(papers),
            "variable_uses": sum(p["counts"]["variables"] for p in papers),
            "findings": sum(p["counts"]["findings"] for p in papers),
            "distinct_variables": len(variables),
            "distinct_constructs": len(constructs),
            "claims": len(claims),
            "relationships": len(relationships),
            "divergent_relationships": sum(1 for r in relationships
                                           if r["verdict"] == "divergent"),
            "within_paper_conflicts": sum(1 for r in relationships
                                          if r["within_paper_conflict"]),
            "variables_with_table": sum(1 for v in variables
                                        if v["nda_or_nbdc_table"]),
            "variables_mapping_disagreement": sum(
                1 for v in variables if v.get("mapping_disagreement")),
            "consensus_constructs": sum(1 for c in constructs
                                        if c["verdict"] == "consensus"),
            "divergent_constructs": sum(1 for c in constructs
                                        if c["verdict"] == "divergent"),
            "consistent_mediators": sum(
                1 for v in variables
                if v["verdict"] == "consistent_role" and v["dominant_role"] == "mediator"),
            "consistent_moderators": sum(
                1 for v in variables
                if v["verdict"] == "consistent_role" and v["dominant_role"] == "moderator"),
            "contested_roles": sum(1 for v in variables if v["verdict"] == "contested_role"),
            "data_releases": sorted({str(p["dataset"]["data_release"])
                                     for p in papers if p["dataset"]["data_release"]}),
        },
        "method": {
            "counting_unit": "paper",
            "min_papers_for_verdict": min_papers,
            "agreement_threshold": agreement_threshold,
            "divergence_rule": ("opposing directions (positive and negative) for the "
                                "SAME relationship (same resolved variables). A "
                                "construct whose opposing signs come from different "
                                "relationships is 'different_relationships', and a "
                                "change over time in one variable is never counted "
                                "as an association's direction"),
            "agreement_denominator": ("paper-direction claims — a paper reporting "
                                      "both a positive and a null result for one "
                                      "construct contributes to both"),
            "role_consistency_rule": ("the dominant role must recur in "
                                      f"{agreement_threshold:.0%} of papers AND be "
                                      "the only substantive role in that share of "
                                      "them; otherwise the role is contested"),
            "variable_identity_rule": ("papers are merged on the resolved dictionary "
                                       "variable, then on paper-declared aliases, "
                                       "then on the normalised mention. Never on "
                                       "similarity: parent-report and youth-report "
                                       "versions of a scale stay separate."),
            "scope": ("own-study only — the per-paper verifier rejects variables and "
                      "results attributed to cited work"),
            "strength_rating": {
                "framework": ("GRADE-adapted evidence quality (Atkins et al. 2004, "
                              "BMJ 328:1490): high / moderate / low / very low"),
                "start": "observational = low, randomised = high",
                "up": ["+1 longitudinal and covariate-adjusted",
                       "+1 large standardized effect (|r|,|beta| >= .30; d >= .80; ratio >= 2)"],
                "down": ["-1 no effect size or statistic", "-1 n < 1,000",
                         "-1 subgroup only"],
                "inputs": ["design", "covariate adjustment", "effect size",
                           "analytic sample n", "subgroup"],
                "note": ("Only what the papers reported; each step is listed in "
                         "the finding's reasons."),
            },
            "claim_status": {
                "scale": list(CLAIM_STATUSES),
                "source": "https://www.ncbi.nlm.nih.gov/books/NBK543866/",
                "rule": ("per relationship: evidence for the majority sign vs "
                         "against (opposite sign or null), each paper counted "
                         "once at its best quality; 'established' needs high "
                         "quality replicated in independent cohorts"),
            },
            "note": ("Findings are counted once per paper per construct, so a paper "
                     "reporting many models cannot outweigh other papers."),
        },
        "provenance": {
            "run_at": _now(),
            "inputs": [p["source_path"] or p["paper_id"] for p in papers],
            "papers": [{"paper_id": p["paper_id"], "doi": p["doi"],
                        "data_release": p["dataset"]["data_release"],
                        "dd_releases_matched_against": p["dictionary"]["matched_against"]}
                       for p in papers],
        },
    }


def _bare_use(pid: str, doc: dict, name: Any) -> dict:
    """A use record for a variable named in a model/finding but never declared.

    Flagged rather than hidden: it has no quote of its own, so nothing about it can
    be verified, and a reader should see that before trusting the row.
    """
    return {
        "paper_id": pid,
        "doi": (doc.get("source_metadata") or {}).get("doi"),
        "data_release": (doc.get("source_metadata") or {}).get("data_release"),
        "mentions": [str(name).strip()],
        "roles": set(),
        "timepoints": set(),
        "instruments": set(),
        "respondents": set(),
        "metrics": set(),
        "dictionary_status": "not_declared_in_variables",
        "dictionary_variable": None,
        "match_method": None,
        "match_score": None,
        "nda_or_nbdc_table": None,
        "nbdc_domain": None,
        "nbdc_sub_domain": None,
        "dd_releases": [],
        "family_prefix": None,
        "quotes": [],
        "declared": False,
    }


def _finalise_use(use: dict) -> dict:
    out = dict(use)
    for field in ("roles", "timepoints", "instruments", "respondents", "metrics"):
        out[field] = sorted(out.get(field) or [])
    out["mentions"] = sorted(set(out.get("mentions") or []))
    out["quotes"] = (out.get("quotes") or [])[:4]
    return out


def _variable_row(key: str, surface: Counter, roles: Dict[str, Set[str]],
                  paper_roles: Dict[str, Set[str]], uses: Dict[str, dict],
                  evidence: List[dict], declared: Set[str],
                  min_papers: int, thresh: float) -> dict:
    display = surface.most_common(1)[0][0] if surface else key
    all_papers = set(uses) | {p for pids in roles.values() for p in pids}
    n = len(all_papers)

    counts = {r: len(pids) for r, pids in roles.items()}
    claimed = {r: c for r, c in counts.items() if r != "unspecified"}
    dominant, dom_n = (max(claimed.items(), key=lambda kv: kv[1])
                       if claimed else (None, 0))
    dominant_share = (dom_n / n) if n else 0.0
    # Exclusivity: in how many papers is the dominant role the ONLY substantive
    # role? A variable that is a mediator in both papers and also an outcome in
    # both is not a consistent mediator, and share alone cannot see that.
    exclusive = 0
    for pid, rset in paper_roles.items():
        substantive = {r for r in rset if r != "unspecified"}
        if substantive == {dominant}:
            exclusive += 1
    exclusivity = (exclusive / n) if n else 0.0

    if n < min_papers:
        verdict = "insufficient_papers"
    elif dominant and dominant_share >= thresh and exclusivity >= thresh:
        verdict = "consistent_role"
    elif len(claimed) > 1:
        verdict = "contested_role"
    elif dominant:
        verdict = "mixed"
    else:
        verdict = "unspecified_role"

    finalised = [_finalise_use(u) for _, u in sorted(uses.items())]
    resolved = Counter(u["dictionary_variable"] for u in finalised
                       if u["dictionary_variable"])
    tables = Counter(u["nda_or_nbdc_table"] for u in finalised
                     if u["nda_or_nbdc_table"])
    domains = Counter(u["nbdc_domain"] for u in finalised if u["nbdc_domain"])
    releases = sorted({r for u in finalised for r in u["dd_releases"] or []})
    statuses = Counter(u["dictionary_status"] for u in finalised
                       if u["dictionary_status"])
    return {
        "key": key,
        "variable": display,
        "surface_forms": [s for s, _ in surface.most_common()],
        # A disagreement here is a real signal, not noise: two papers used the same
        # words for different variables, or our mapping was wrong for one of them.
        "dictionary_variable": resolved.most_common(1)[0][0] if resolved else None,
        "mapping_disagreement": (
            {"dictionary_variable": sorted(resolved), "tables": sorted(tables)}
            if len(resolved) > 1 or len(tables) > 1 else None),
        "nda_or_nbdc_table": tables.most_common(1)[0][0] if tables else None,
        "nbdc_domain": domains.most_common(1)[0][0] if domains else None,
        "dd_releases": releases or None,
        "mapping_statuses": dict(statuses),
        "paper_count": n,
        "papers": sorted(all_papers),
        "papers_declaring": sorted(declared),
        "papers_referencing_without_declaring": sorted(all_papers - declared) or None,
        "roles": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "roles_by_paper": {pid: sorted(rs) for pid, rs in sorted(paper_roles.items())},
        "dominant_role": dominant,
        "dominant_role_share": round(dominant_share, 3),
        "role_exclusivity": round(exclusivity, 3),
        "verdict": verdict,
        "paper_evidence": finalised,
        "evidence": evidence,
    }


def _construct_row(cid: str, label: str, mapped: bool, dirs: Dict[str, Set[str]],
                   evidence: List[dict], measures: Dict[str, Set[str]],
                   involved: Dict[str, Set[str]], var_by_key: Dict[str, dict],
                   min_papers: int, thresh: float,
                   studied_by: Optional[Set[str]] = None) -> dict:
    counts = {d: len(pids) for d, pids in dirs.items()}
    with_direction: Set[str] = set().union(*dirs.values()) if dirs else set()
    all_papers = with_direction | (studied_by or set())
    n = len(all_papers)
    n_with_direction = len(with_direction)
    substantive = {d: c for d, c in counts.items() if d in SUBSTANTIVE_DIRECTIONS}
    majority, maj_n = (max(substantive.items(), key=lambda kv: kv[1])
                       if substantive else (None, 0))
    # Denominator is paper-direction claims, not papers. With papers as the
    # denominator a construct where two papers say positive and one of them also
    # says negative scored 1.00 agreement while being reported as divergent — the
    # two numbers contradicted each other on the same row.
    claim_total = sum(substantive.values())
    agreement = (maj_n / claim_total) if claim_total else 0.0
    has_pos = counts.get("positive", 0) > 0
    has_neg = counts.get("negative", 0) > 0

    if not with_direction:
        verdict = "no_directional_finding"
    elif n_with_direction < min_papers:
        verdict = "insufficient_papers"
    elif has_pos and has_neg:
        verdict = "divergent"
    elif majority and agreement >= thresh:
        verdict = "consensus"
    else:
        verdict = "mixed"

    def _measure_rows(source: Dict[str, Set[str]]) -> List[dict]:
        rows = []
        for vkey, pids in sorted(source.items(), key=lambda kv: -len(kv[1])):
            row = var_by_key.get(vkey) or {}
            rows.append({
                "variable_key": vkey,
                "variable": row.get("variable", vkey),
                "dictionary_variable": row.get("dictionary_variable"),
                "nda_or_nbdc_table": row.get("nda_or_nbdc_table"),
                "nbdc_domain": row.get("nbdc_domain"),
                "dd_releases": row.get("dd_releases"),
                "paper_count": len(pids),
                "papers": sorted(pids),
                "surface_forms": (row.get("surface_forms") or [])[:4],
            })
        return rows

    measured_by = _measure_rows(measures)
    variables_in_findings = _measure_rows(involved)

    return {
        "construct_id": None if cid.startswith("unmapped:") else cid,
        "construct_id_or_key": cid,
        "construct_label": label,
        "construct_mapped": mapped and not cid.startswith("unmapped:"),
        "paper_count": n,
        "papers": sorted(all_papers),
        "papers_with_a_directional_finding": sorted(with_direction),
        "directions": counts,
        "papers_by_direction": {d: sorted(pids) for d, pids in sorted(dirs.items())},
        "majority_direction": majority,
        "agreement": round(agreement, 3),
        "direction_claims": claim_total,
        "verdict": verdict,
        "measured_by": measured_by,
        "measured_by_source": ("declared by the paper" if measured_by else
                               "not declared — see variables_in_findings"),
        "variables_in_findings": variables_in_findings,
        "measure_count": len(measured_by),
        "tables": sorted({m["nda_or_nbdc_table"]
                          for m in (measured_by or variables_in_findings)
                          if m["nda_or_nbdc_table"]}) or None,
        "evidence": evidence,
    }


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #

QUALITY_LEVELS = ("very low", "low", "moderate", "high")
_ADJUSTED_WORDS = re.compile(r"control(?:ling|led)? for|adjust|over and above|"
                             r"accounting for|covariate", re.I)
_STD_EFFECT = re.compile(r"(?<![A-Za-z])(?:rs?|r_s|\u03c1|rho|\u03b2|beta|d|g)\s*=\s*"
                         r"([-\u2212]?\d*\.\d+)(?:\s*[\u2013\-]\s*([-\u2212]?\d*\.\d+))?", re.I)
_RATIO = re.compile(r"(?<![A-Za-z])(?:OR|HR|RR|IRR)\s*=\s*(\d*\.?\d+)", re.I)


def _large_effect(effect: Any) -> Optional[str]:
    """'|r| = .58' when a standardized effect is large (|r|, |beta| >= .30;
    d/g >= .80; OR/HR/RR >= 2 or <= .5), else None. Unstandardized b is not
    judged: its size depends on the units."""
    text = str(effect or "")
    for m in _STD_EFFECT.finditer(text):
        vals = [abs(float(v.replace("\u2212", "-"))) for v in m.groups() if v]
        metric = m.group(0).split("=")[0].strip().lower()
        bar = 0.80 if metric in ("d", "g") else 0.30
        if vals and max(vals) >= bar:
            return f"|{metric}| = {max(vals):.2f}"
    for m in _RATIO.finditer(text):
        v = float(m.group(1))
        if v >= 2 or (0 < v <= 0.5):
            return f"ratio = {v:g}"
    return None


def _strength(paper: dict, finding: dict) -> dict:
    """Quality of the evidence one finding provides, graded like GRADE.

    Levels and meanings follow the GRADE Working Group (Atkins et al. 2004,
    BMJ 328:1490): high = further research is highly unlikely to change our
    confidence; moderate = moderately unlikely; low = moderately likely; very low
    = highly likely. As in GRADE, observational evidence starts at low and
    randomised evidence at high, then moves one level per factor:

      +1  longitudinal design AND estimated with covariates controlled
          (temporal order plus confounder adjustment)
      +1  large standardized effect (|r| or |beta| >= .30, d >= .80, ratio >= 2)
      -1  no effect size and no test statistic reported (precision unknown)
      -1  analytic n < 1,000 (imprecision)
      -1  subgroup-only result (indirectness)

    Every step is in `reasons`, so a reader can disagree with a step rather than
    with an opaque score. Only facts the paper reported are used.
    """
    dataset = (paper or {}).get("dataset") or {}
    n = _first_int(finding.get("analytic_n"), dataset.get("analytic_sample"),
                   dataset.get("sample_size"))
    design = str(dataset.get("design") or "")
    randomised = bool(re.search(r"randomi[sz]ed|\brct\b|trial", design, re.I))
    longitudinal = bool(re.search(r"longitud|wave|follow[\s-]?up|prospectiv",
                                  design, re.I))
    adjusted = bool(finding.get("_adjusted"))
    score = 3 if randomised else 1
    reasons = ["starts high (randomised design)" if randomised
               else "starts low (observational study)"]
    if longitudinal and adjusted:
        score += 1
        reasons.append("+1 longitudinal and adjusted for covariates")
    elif longitudinal:
        reasons.append("longitudinal, but this estimate is not covariate-adjusted")
    large = _large_effect(finding.get("effect_size"))
    if large:
        score += 1
        reasons.append(f"+1 large effect ({large})")
    if not finding.get("effect_size") and not finding.get("statistic"):
        score -= 1
        reasons.append("-1 no effect size or statistic reported")
    if n is not None and n < 1000:
        score -= 1
        reasons.append(f"-1 n = {n:,} (< 1,000)")
    elif n is None:
        reasons.append("sample size not reported")
    else:
        reasons.append(f"n = {n:,}")
    if finding.get("subgroup"):
        score -= 1
        reasons.append(f"-1 subgroup only: {finding['subgroup']}")
    level = QUALITY_LEVELS[max(0, min(3, score))]
    return {"level": level, "score": max(0, min(3, score)), "sample_n": n,
            "longitudinal": longitudinal, "adjusted": adjusted, "reasons": reasons,
            "framework": "GRADE-adapted (Atkins et al. 2004)"}


CLAIM_STATUSES = ("established", "provisionally established", "arguably true",
                  "speculative", "arguably false", "provisionally ruled out",
                  "ruled out")


def _claim_status(rel_label: str, evidence: List[dict], majority: Optional[str],
                  by_paper: Dict[str, dict]) -> dict:
    """Where the claim stands, on the scale established ... ruled out.

    The claim is the relationship with the majority sign ("financial strain <->
    family conflict is positive"); with no signed majority, that there is an
    association at all. Evidence for it is findings with that sign; evidence for
    its negation is the opposite sign or a reported null. Per paper, a side counts
    once, at its best quality.

      established              high-quality evidence, nothing against, and
                               replicated in independent samples (>= 2 cohorts)
      provisionally established  moderate (or unreplicated high) evidence,
                               nothing against
      arguably true            low-quality evidence with nothing against, or
                               moderate+ evidence with more papers for than against
      speculative              none of the above
      arguably false / provisionally ruled out / ruled out: the mirror image.

    Definitions after the claim-status scale at
    https://www.ncbi.nlm.nih.gov/books/NBK543866/ ; quality levels after Atkins
    et al. (2004).
    """
    # The claim's sign: the one more papers report. A tie between positive and
    # negative is not a claim either way.
    sign_papers = {d: {str(e.get("paper_id")) for e in evidence if e.get("direction") == d}
                   for d in ("positive", "negative")}
    n_pos, n_neg = len(sign_papers["positive"]), len(sign_papers["negative"])
    claim_dir = ("positive" if n_pos > n_neg else "negative" if n_neg > n_pos else None)
    if claim_dir is None and n_pos:
        return {"claim": f"{rel_label}: there is an association",
                "status": "speculative",
                "why": (f"the sign is disputed: {n_pos} paper(s) report positive and "
                        f"{n_neg} negative"),
                "papers_for": sorted(sign_papers["positive"] | sign_papers["negative"]),
                "papers_against": sorted({str(e.get("paper_id")) for e in evidence
                                          if e.get("direction") == "null"})}
    for_q: Dict[str, int] = {}
    against_q: Dict[str, int] = {}
    for e in evidence:
        d = e.get("direction")
        q = int((e.get("strength") or {}).get("score", 0))
        pid = str(e.get("paper_id"))
        if claim_dir and d == claim_dir:
            for_q[pid] = max(for_q.get(pid, 0), q)
        elif d == "null" or (claim_dir and d in ("positive", "negative") and d != claim_dir):
            against_q[pid] = max(against_q.get(pid, 0), q)
    claim = (f"{rel_label} is {claim_dir}" if claim_dir
             else f"{rel_label}: there is an association")

    def cohorts(pids) -> Set[str]:
        out = set()
        for p in pids:
            ds = (by_paper.get(p) or {}).get("dataset") or {}
            out.add(str(ds.get("cohort") or ds.get("study") or p).lower())
        return out

    def decide(side: Dict[str, int], other: Dict[str, int], pos: bool) -> Tuple[str, str]:
        best = max(side.values())
        names = (("established", "provisionally established", "arguably true")
                 if pos else ("ruled out", "provisionally ruled out", "arguably false"))
        level = QUALITY_LEVELS[best]
        if not other:
            independent = len(side) >= 2 and len(cohorts(side)) >= 2
            if best == 3 and independent:
                return names[0], f"high-quality evidence from {len(side)} independent samples, none against"
            if best >= 2:
                why = f"{level}-quality evidence, none against"
                if best == 3:
                    why += ("; not established: " + ("one paper only" if len(side) < 2
                            else "the papers analyse the same cohort, so it is not an independent replication"))
                return names[1], why
            if best == 1:
                return names[2], "low-quality evidence, none against"
            return "speculative", "only very low-quality evidence"
        if len(side) > len(other) and best >= 2:
            return names[2], (f"{len(side)} paper(s) for vs {len(other)} against, "
                              f"best evidence {level}")
        return "speculative", f"{len(side)} paper(s) for vs {len(other)} against"

    if for_q and (len(for_q) >= len(against_q) or not against_q):
        status, why = decide(for_q, against_q, True)
    elif against_q and not for_q:
        status, why = decide(against_q, for_q, False)
    elif against_q:
        status, why = decide(against_q, for_q, False)
    else:
        status, why = "speculative", "no finding with a direction"
    if status == "speculative" and for_q and against_q:
        why = f"{len(for_q)} paper(s) for vs {len(against_q)} against: evidence on both sides"
    return {"claim": claim, "status": status, "why": why,
            "papers_for": sorted(for_q), "papers_against": sorted(against_q)}


def _claim(construct: dict, findings: List[dict], by_paper: Dict[str, dict],
           thresh: float) -> Optional[dict]:
    """One claim per construct, with per-paper evidence and contradictions.

    Shaped for reading: a statement, the evidence behind it paper by paper with a
    strength rating, and — separately — whatever in the corpus contradicts it. The
    contradiction section is not an afterthought; a claim supported by two papers
    and contradicted by one is a different thing from a claim supported by three,
    and collapsing them into "2 of 3 agree" hides it.
    """
    if not findings:
        return None
    label = construct["construct_label"]
    majority = construct["majority_direction"]
    n = construct["paper_count"]
    verdict = construct["verdict"]

    direction_words = {"positive": "a positive association",
                       "negative": "a negative association",
                       "null": "no association",
                       "mixed": "mixed associations",
                       "unspecified": "an association of unstated direction"}
    if verdict == "divergent":
        statement = (f"Papers disagree on the direction of the association "
                     f"involving {label}: both positive and negative effects are "
                     f"reported across {n} paper(s).")
    elif verdict == "consensus":
        statement = (f"{label} shows {direction_words.get(majority, 'an association')} "
                     f"in {construct['agreement']:.0%} of the direction claims across "
                     f"{n} papers.")
    elif verdict == "insufficient_papers":
        statement = (f"{label}: {direction_words.get(majority, 'an association')} "
                     f"reported, but by only {n} paper — not replicated in this "
                     f"corpus.")
    elif verdict == "no_directional_finding":
        statement = f"{label}: studied, but no direction reported."
    else:
        statement = (f"Evidence on {label} is mixed across {n} papers; no direction "
                     f"reaches the {thresh:.0%} agreement threshold.")

    supporting, contradicting, not_counted = [], [], []
    for f in findings:
        pid = f["paper_id"]
        paper = by_paper.get(pid) or {}
        ev = f.get("evidence") or {}
        row = {
            "paper_id": pid,
            "doi": (paper.get("doi") or None),
            "data_release": (paper.get("dataset") or {}).get("data_release"),
            "direction": f.get("direction"),
            "statement": f.get("statement"),
            "effect_size": f.get("effect_size"),
            "statistic": f.get("statistic"),
            "analytic_n": f.get("analytic_n"),
            "subgroup": f.get("subgroup"),
            "section": ev.get("section"),
            "page": ev.get("page"),
            "quote": ev.get("quote"),
            "strength": _strength(paper, f),
        }
        rel = f.get("_relationship") or {}
        row["relationship"] = rel.get("label")
        row["relationship_kind"] = rel.get("kind")
        if rel.get("kind") == "change_over_time":
            row["not_counted_because"] = ("change over time in one variable, not "
                                          "an association")
            not_counted.append(row)
            continue
        opposing = (majority in ("positive", "negative")
                    and f.get("direction") in ("positive", "negative")
                    and f.get("direction") != majority)
        (contradicting if opposing else supporting).append(row)

    # Where exactly do the signs differ? By relationship, so a reader sees
    # whether the "disagreement" is two papers on one question or two questions.
    by_dir: Dict[str, Set[str]] = defaultdict(set)
    rel_dirs: Dict[str, Set[str]] = defaultdict(set)
    for r in supporting + contradicting:
        if r["direction"] in ("positive", "negative"):
            by_dir[r["direction"]].add(r["relationship"])
            rel_dirs[r["relationship"]].add(r["direction"])
    conflicting = sorted(k for k, d in rel_dirs.items() if len(d) > 1)
    if verdict == "divergent" and not conflicting:
        verdict = "different_relationships"
        statement = (f"No disagreement on {label}: results go both ways, but never "
                     f"for the same relationship. Positive: "
                     f"{'; '.join(sorted(by_dir['positive']))}. Negative: "
                     f"{'; '.join(sorted(by_dir['negative']))}.")
    elif verdict == "divergent":
        statement = (f"Papers disagree on {label}: "
                     f"{'; '.join(conflicting)} reported both positive and negative.")

    caveats = []
    if n < 2:
        caveats.append("single paper — no independent replication in this corpus")
    releases = {r["data_release"] for r in supporting + contradicting if r["data_release"]}
    if len(releases) > 1:
        caveats.append("papers analysed different data releases: "
                       + ", ".join(sorted(map(str, releases))))
    samples = {r["paper_id"]: (by_paper.get(r["paper_id"], {}).get("dataset") or {})
               .get("sample_n") for r in supporting + contradicting}
    if len({v for v in samples.values() if v}) == 1 and len(samples) > 1:
        caveats.append("papers report the same sample size — likely the same "
                       "children, so agreement is not independent")
    if not construct["construct_mapped"]:
        caveats.append("construct not mapped to Cognitive Atlas — grouping rests on "
                       "the papers' wording")
    if construct["measure_count"] == 0:
        caveats.append("no paper declared which variables measure this construct; "
                       "the variables listed are the ones its findings mention")

    return {
        "claim": statement,
        "construct_id": construct["construct_id"],
        "construct_label": label,
        "verdict": verdict,
        "paper_count": n,
        "papers": construct["papers"],
        "agreement": construct["agreement"],
        "measured_by": [m["variable"] for m in construct["measured_by"][:8]],
        "variables_in_findings": [m["variable"]
                                  for m in construct["variables_in_findings"][:8]],
        "evidence": supporting,
        "contradictions": contradicting,
        "not_counted": not_counted,
        "relationships_by_direction": {d: sorted(v) for d, v in sorted(by_dir.items())},
        "conflicting_relationships": conflicting,
        "caveats": caveats,
    }


def _relationship_row(rel: dict, dirs: Dict[str, Set[str]], findings: List[dict],
                      by_paper: Dict[str, dict], min_papers: int,
                      thresh: float) -> dict:
    """One relationship across papers: the claim, then each side's evidence."""
    counts = {d: len(p) for d, p in dirs.items()}
    papers = set().union(*dirs.values()) if dirs else set()
    substantive = {d: c for d, c in counts.items() if d in SUBSTANTIVE_DIRECTIONS}
    majority, maj_n = (max(substantive.items(), key=lambda kv: kv[1])
                       if substantive else (None, 0))
    total = sum(substantive.values())
    agreement = maj_n / total if total else 0.0
    pos, neg = dirs.get("positive", set()), dirs.get("negative", set())
    with_dir = set().union(*(dirs[d] for d in dirs if d in SUBSTANTIVE_DIRECTIONS)) \
        if substantive else set()
    if not with_dir:
        verdict = "no_directional_finding"
    elif rel["kind"] == "change_over_time":
        # A stability coefficient (prior X predicts later X, positive) and a mean
        # decline (X decreases, negative) are different quantities with the same
        # sign vocabulary; setting them against each other reports a conflict
        # nobody found. Trends are listed, not judged.
        verdict = "change_over_time"
    elif len(with_dir) < min_papers:
        verdict = "insufficient_papers"
    elif pos and neg:
        verdict = "divergent"
    elif agreement >= thresh:
        verdict = "consensus"
    else:
        verdict = "mixed"
    label = rel["label"]
    words = {"positive": "positive", "negative": "negative", "null": "no effect",
             "mixed": "mixed", "unspecified": "direction not stated"}
    if verdict == "divergent":
        statement = (f"Papers disagree on {label}: {len(pos)} paper(s) report "
                     f"positive, {len(neg)} report negative.")
    elif verdict == "consensus":
        statement = (f"Papers agree on {label}: {words.get(majority)} in "
                     f"{agreement:.0%} of results across {len(with_dir)} papers.")
    elif verdict == "insufficient_papers":
        statement = f"{label}: {words.get(majority, 'reported')} \u2014 one paper only."
    elif verdict == "mixed":
        statement = f"{label}: mixed across {len(with_dir)} papers."
    elif verdict == "change_over_time":
        statement = (f"{label}: " + "; ".join(
            f"{words.get(d, d)} in {len(p)} paper(s)" for d, p in sorted(dirs.items()))
            + ". Trends and stability are listed, not compared across papers.")
    else:
        statement = f"{label}: no direction reported."

    evidence = []
    for f in findings:
        pid = f["paper_id"]
        paper = by_paper.get(pid) or {}
        ev = f.get("evidence") or {}
        evidence.append({
            "paper_id": pid,
            "finding_index": f.get("_finding_index"),
            "direction": f.get("direction"),
            "claim_type": f.get("claim_type"),
            "directionality": f.get("directionality"),
            "statement": f.get("statement"),
            "effect_size": f.get("effect_size"),
            "statistic": f.get("statistic"),
            "analytic_n": f.get("analytic_n"),
            "subgroup": f.get("subgroup"),
            "section": ev.get("section"),
            "page": ev.get("page"),
            "quote": ev.get("quote"),
            "table_context": ev.get("table_context"),
            "summary_not_in_evidence": f.get("summary_not_in_evidence"),
            "strength": _strength(paper, f),
        })
    order = {"positive": 0, "negative": 1, "null": 2, "mixed": 3, "unspecified": 4}
    evidence.sort(key=lambda e: (order.get(e["direction"], 9), e["paper_id"]))

    # One paper stating both signs for one relationship is usually an abstract
    # contradicting its own results — worth a reader's attention on its own.
    within = sorted(pos & neg)
    caveats = []
    if within:
        caveats.append("the same paper reports both positive and negative for this "
                       "relationship (check abstract against results)")
    if len(papers) > 1:
        samples = {(by_paper.get(p, {}).get("dataset") or {}).get("sample_n")
                   for p in papers}
        if len(samples) == 1 and None not in samples:
            caveats.append("papers report the same sample size \u2014 likely the same "
                           "children, so agreement is not independent")
    if rel["kind"] == "change_over_time":
        caveats.append("change over time in one variable, not an association")
    status = (None if rel["kind"] == "change_over_time"
              else _claim_status(label, evidence, majority, by_paper))
    constructs = sorted({str(f.get("construct_label") or f.get("construct"))
                         for f in findings
                         if f.get("construct_label") or f.get("construct")})
    return {
        "label": label,
        "kind": rel["kind"],
        "claim_status": status,
        "constructs": constructs,
        "variables": rel["variables"],
        "verdict": verdict,
        "claim": statement,
        "paper_count": len(papers),
        "papers": sorted(papers),
        "papers_by_direction": {d: sorted(p) for d, p in sorted(dirs.items())},
        "majority_direction": majority,
        "agreement": round(agreement, 3),
        "within_paper_conflict": within,
        "evidence": evidence,
        "caveats": caveats,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _load_inputs(paths: List[Path]) -> List[dict]:
    files: List[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("*_abcd.json")))
        else:
            files.append(p)
    docs = []
    for f in files:
        try:
            doc = json.loads(f.read_text())
        except Exception as exc:
            print(f"skipping {f}: {exc}", file=sys.stderr)
            continue
        if "variables" not in doc and "findings" not in doc:
            print(f"skipping {f}: not an ABCD extraction", file=sys.stderr)
            continue
        docs.append(doc)
    return docs


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="*_abcd.json files, or directories containing them")
    ap.add_argument("--out", type=Path, default=Path("abcd_synthesis"),
                    help="output stem (default: ./abcd_synthesis)")
    ap.add_argument("--formats", default="json,ttl,xlsx",
                    help="comma list of json, ttl, xlsx, md, codebook; xlsx writes one "
                         "workbook (<out>.xlsx) with every paper and the synthesis")
    ap.add_argument("--min-papers", type=int, default=DEFAULT_MIN_PAPERS)
    ap.add_argument("--agreement-threshold", type=float, default=AGREEMENT_THRESHOLD)
    a = ap.parse_args(argv)

    docs = _load_inputs(a.inputs)
    if not docs:
        print("no ABCD extractions found", file=sys.stderr)
        return 1

    syn = synthesize(docs, min_papers=a.min_papers,
                     agreement_threshold=a.agreement_threshold)
    formats = [f.strip() for f in a.formats.split(",") if f.strip()]
    written = abcd_export.write_all(syn, a.out, kind="synthesis", formats=formats)
    t = syn["totals"]
    print(f"{t['papers']} papers | {t['claims']} claims | "
          f"{t['distinct_constructs']} constructs "
          f"({t['consensus_constructs']} consensus, {t['divergent_constructs']} divergent) "
          f"| {t['distinct_variables']} variables "
          f"({t['variables_with_table']} with a table, "
          f"{t['consistent_mediators']} consistent mediators, "
          f"{t['consistent_moderators']} moderators, {t['contested_roles']} contested)")
    if "xlsx" in formats:
        from scripts import abcd_xlsx

        written["xlsx"] = abcd_xlsx.write_workbook(
            docs, a.out.parent / f"{a.out.name}.xlsx", syn)
    for fmt, path in written.items():
        print(f"  {fmt}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
