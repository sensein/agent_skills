#!/usr/bin/env python3
"""Source-grounded evidence for everything a review asserts about a paper.

Every value the review reports about a study — each charting answer, each
critical-appraisal item rating, each risk-of-bias judgement and extracted
field, each narrative-table cell, each GRADE domain rating — must rest on
verbatim text of the paper. This script extracts that evidence, verifies it
mechanically, and removes what it cannot ground. Ungrounded values are not
delivered.

Commands (``REVIEW`` is a ``PRISMAReviewResult`` JSON):

``spans REVIEW [--only ID …]``
    Run the app's own evidence extraction (``synthscholar.evidence``: the whole
    text of each paper read in chunks, first-party findings only, each span
    fuzzy-grounded) and replace those studies' ``evidence_spans``. Each span is
    stamped with the DOI of the version read. Needs ``OPENROUTER_API_KEY``.

``refresh REVIEW --only ID …``
    For studies whose source changed (a preprint replaced by its published
    version): re-run spans, risk of bias, charting, appraisal and the narrative
    row from the *current* text, and log the run against that text's hash.
    ``audit_grounding.py`` clears a version change only with this log entry.

``items REVIEW``
    List every structured value in ``<stem>.evidence-ledger.json`` with the
    candidate spans of its study. Values already in the ledger keep their
    evidence.

``quote REVIEW [--only ID …]``
    Ask the model, paper by paper, for the verbatim sentences that establish
    each value (GRADE domains: the evidence spans that support the rating).
    Without an API key the agent fills ``evidence`` in the ledger itself, in
    the same format; it is verified the same way.

``verify REVIEW``
    Mark each value ``grounded`` (at least one quote is verbatim text of its
    paper, and every number in the value — sign, comparator and % included —
    appears in a quote), ``absence`` (the value states nothing was reported),
    or ``ungrounded``. Exit 1 while anything is ungrounded.

``locate REVIEW``
    Replace each span's and each ledger quote's section/page *label* with where
    the quote actually is in the stored paper: the detected section it sits in,
    the page it starts on (from ``<stem>.pages.json``), its character offset and
    the source text's SHA-256. A quote found in several sections, or in none
    with a detectable heading, is left as is and reported.

``apply REVIEW --out GROUNDED``
    Write a copy of the review with every included study that has no full text
    removed (moved to "reports not retrieved": a study read only as an abstract
    cannot be evaluated) and every ungrounded value removed: text
    fields become "NR (not source-grounded)", list entries, appraisal items,
    risk-of-bias domains and GRADE domains are dropped, and a GRADE outcome
    with a dropped domain is "Not assessed". What was removed is listed in
    ``<GROUNDED stem>.ungrounded.json``. Export and deliver the copy.

Every command writes ``<stem>.extraction.json``, the log of what was extracted
from which text (SHA-256) and when.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grounding_core import (  # noqa: E402
    UNGROUNDED, check_page, check_section, is_absent, missing_quantities, norm, rubric_for,
    source_text, text_sha256, unpaired, verbatim,
)

# Charting fields that are the rubric's own publication metadata, not findings.
META_FIELDS = {"source_id", "title", "authors", "year", "journal_conference", "doi",
               "database_retrieved", "section_outputs", "custom_fields"}
ROB_FIELDS = ("study_design", "sample_size", "population", "intervention", "comparator",
              "outcomes", "key_findings", "effect_measures")
NARRATIVE_FIELDS = ("study_design_sample_dataset", "methods", "outcomes", "key_limitations",
                    "relevance_notes", "review_specific_questions")
MAX_TEXT = 150_000          # characters of a paper sent to the model in one call


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def item_id(kind: str, path: list, value: str) -> str:
    return "E" + hashlib.sha1(json.dumps([kind, path, value], ensure_ascii=False).encode()).hexdigest()[:12]


# ─────────────────────────── enumerate ────────────────────────────

def structured_items(review: dict) -> list[dict]:
    """Every value the review states about a paper, with where it lives."""
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    rub = rubric_for(review, arts)
    study_of_rubric = {id(r): pid for pid, r in rub.items()}
    study_of_source = {str(r.get("source_id")): pid for pid, r in rub.items() if r.get("source_id")}
    items: list[dict] = []

    def add(kind, study, path, label, value, context=""):
        v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        items.append({"id": item_id(kind, path, v), "kind": kind, "study": study, "path": path,
                      "label": label, "value": v, **({"context": context} if context else {})})

    for i, r in enumerate(review.get("data_charting_rubrics") or []):
        pid = study_of_rubric.get(id(r))
        for f, v in r.items():
            if f in META_FIELDS or isinstance(v, (dict, list)) or v is None or isinstance(v, bool):
                continue
            add("charting", pid, ["data_charting_rubrics", i, f], f, str(v))
        for q, v in (r.get("custom_fields") or {}).items():
            add("charting", pid, ["data_charting_rubrics", i, "custom_fields", q], q, str(v))
        for title, so in (r.get("section_outputs") or {}).items():
            if isinstance(so, dict) and so.get("formatted_answer"):
                add("charting", pid, ["data_charting_rubrics", i, "section_outputs", title, "formatted_answer"],
                    f"section: {title}", so["formatted_answer"])

    for i, res in enumerate(review.get("structured_appraisal_results") or []):
        pid = study_of_source.get(str(res.get("source_id")))
        for j, d in enumerate(res.get("domains") or []):
            for k, it in enumerate(d.get("item_ratings") or []):
                add("appraisal", pid, ["structured_appraisal_results", i, "domains", j, "item_ratings", k, "rating"],
                    f"{d.get('domain_name')}: {it.get('item_text')}", str(it.get("rating") or ""))
    for i, res in enumerate(review.get("critical_appraisals") or []):
        pid = study_of_source.get(str(res.get("source_id")))
        for j, d in enumerate(res.get("domains") or []):
            for k, it in enumerate(d.get("items") or []):
                val = " — ".join(x for x in (str(it.get("rating") or ""), str(it.get("notes") or "")) if x)
                add("appraisal", pid, ["critical_appraisals", i, "domains", j, "items", k, "rating"],
                    f"{d.get('domain_name')}: {it.get('item_text')}", val)

    for i, a in enumerate(review.get("included_articles") or []):
        rob = a.get("risk_of_bias") or {}
        pid = str(a.get("pmid"))
        for j, d in enumerate(rob.get("assessments") or []):
            add("risk_of_bias", pid, ["included_articles", i, "risk_of_bias", "assessments", j, "judgment"],
                f"RoB {d.get('domain')}", str(d.get("judgment") or ""), context=str(d.get("support") or ""))
        for f in ROB_FIELDS:
            v = rob.get(f)
            if isinstance(v, list):
                for k, x in enumerate(v):
                    add("risk_of_bias", pid, ["included_articles", i, "risk_of_bias", f, k], f, str(x))
            elif v:
                add("risk_of_bias", pid, ["included_articles", i, "risk_of_bias", f], f, str(v))

    for i, row in enumerate(review.get("narrative_rows") or []):
        pid = study_of_source.get(str(row.get("source_id")))
        for f in NARRATIVE_FIELDS:
            if row.get(f):
                add("narrative", pid, ["narrative_rows", i, f], f, str(row[f]))

    for outcome, g in (review.get("grade_assessments") or {}).items():
        for name, d in (g.get("domains") or {}).items():
            val = " — ".join(x for x in (str(d.get("rating") or ""), str(d.get("explanation") or "")) if x)
            add("grade", None, ["grade_assessments", outcome, "domains", name], f"GRADE {outcome}: {name}", val)
    return items


# ─────────────────────────── verify ────────────────────────────

def verify_items(review: dict, ledger: dict, pages: Optional[dict] = None) -> list[dict]:
    """Current items with status ``grounded`` / ``absence`` / ``ungrounded`` and a reason.

    A quote counts only if it is verbatim text of the right study and, where the
    evidence states a section or page, lies in that section / on that page.
    Every number of the value (comparator, sign, unit, %) must be in a quote, and
    every "N of M" in it must be stated together in one quote.
    """
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    cache: dict[str, tuple[str, str]] = {}
    prior = {x["id"]: x for x in ledger.get("items") or []}
    out = []
    for it in structured_items(review):
        rec = {**it, "evidence": (prior.get(it["id"]) or {}).get("evidence") or [],
               "source": (prior.get(it["id"]) or {}).get("source", "")}
        val = it["value"].split(" — ")[0] if it["kind"] in ("appraisal", "grade") else it["value"]
        if is_absent(val) or it["value"] == UNGROUNDED:
            rec.update(status="absence", reason="states that nothing was reported")
            out.append(rec)
            continue
        good, bad = [], []
        for e in rec["evidence"]:
            sid = str(e.get("study") or it["study"] or "")
            if sid not in arts:
                bad.append(f"{sid or 'no study'} is not included")
                continue
            if it["study"] and sid != it["study"]:
                bad.append(f"quote is from {sid}, but the value is about {it['study']}")
                continue
            src, ns = cache.setdefault(sid, (source_text(arts[sid]), norm(source_text(arts[sid]))))
            if verbatim(e.get("quote", ""), src, ns):
                sec = check_section(str(e.get("section") or ""), e.get("quote", ""), arts[sid])
                pg = check_page(e.get("page"), e.get("quote", ""), (pages or {}).get(sid))
                if "mismatch" in (sec, pg) or "unverifiable" in (sec, pg):
                    bad.append(f"quote from {sid} is not verifiably in its stated "
                               f"{'section ' + str(e.get('section')) if sec != 'ok' and sec != 'skip' else 'page ' + str(e.get('page'))}")
                else:
                    good.append(e.get("quote", ""))
            else:
                bad.append(f"not text of {sid}: “{str(e.get('quote'))[:90]}”")
        if not good:
            rec.update(status="ungrounded", reason="; ".join(bad) or "no evidence quoted")
        elif (miss := missing_quantities(it["value"], good)):
            rec.update(status="ungrounded", reason=f"{', '.join(miss)} in the value but in no quote")
        elif (up := unpaired(it["value"], good)):
            rec.update(status="ungrounded", reason=f"{', '.join(up)} not stated together in one quote")
        else:
            rec.update(status="grounded", reason="")
        out.append(rec)
    return out


# ─────────────────────────── locate ────────────────────────────

def where_is(quote: str, a: dict, page_texts: Optional[list[str]]) -> dict:
    """Section (detected), page, offset and source hash of a verbatim quote."""
    from grounding_core import check_page, locate, sections_of
    out: dict = {}
    names = [n for n, t in sections_of(a) if t and verbatim(quote, t)]
    specific = {x for n in names for x in n} - {"front"}
    body = [n for n in names if n - {"title", "abstract", "front"}]
    if len(body) == 1:
        out["section"] = "/".join(sorted(body[0]))
        out["section_basis"] = "nearest preceding standard heading in the stored text"
    elif not body and "abstract" in specific:
        out["section"] = "abstract"
    elif not body and "title" in specific:
        out["section"] = "title"
    else:
        out["section_ambiguous"] = sorted("/".join(sorted(n)) for n in names) or ["no detected section"]
    if page_texts:
        for i in range(1, len(page_texts) + 1):
            if check_page(i, quote, page_texts) == "ok":
                out["page"] = i
                break
    for field in ("full_text", "abstract", "title"):     # offset into the field that holds it
        loc = locate(quote, a.get(field) or "")
        if "offset" in loc:
            out["offset"], out["offset_in"] = loc["offset"], field
            break
    out["source_sha256"] = text_sha256(a)
    return out


def locate_all(review: dict, ledger: dict, pages: dict) -> dict:
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    stats = {"relabelled": 0, "ambiguous": 0, "not_verbatim": 0}

    def fix(rec: dict, study: str, text_key: str) -> None:
        a = arts.get(study)
        if not a or not verbatim(rec.get(text_key, ""), source_text(a)):
            stats["not_verbatim"] += 1
            return
        w = where_is(rec.get(text_key, ""), a, pages.get(study))
        if "section" in w:
            if str(rec.get("section") or "").lower() != w["section"]:
                stats["relabelled"] += 1
            rec["section"] = w["section"]
            rec.pop("section_ambiguous", None)
        else:
            rec["section_ambiguous"] = w["section_ambiguous"]
            stats["ambiguous"] += 1
        for k in ("page", "offset", "offset_in", "source_sha256", "section_basis"):
            if k in w:
                rec[k] = w[k]

    for sp in review.get("evidence_spans") or []:
        fix(sp, str(sp.get("paper_pmid") or ""), "text")
    for it in ledger.get("items") or []:
        for e in it.get("evidence") or []:
            fix(e, str(e.get("study") or it.get("study") or ""), "quote")
    return stats


# ─────────────────────────── apply ────────────────────────────

def apply_grounding(review: dict, verified: list[dict]) -> tuple[dict, list[dict]]:
    """A copy of ``review`` without its ungrounded values; and what was removed.

    Evidence spans that are not verbatim text of their paper (paraphrases,
    invented text, unknown papers) are dropped too: they are not evidence.
    """
    out = copy.deepcopy(review)
    removed, drops = [], []
    arts = {str(a.get("pmid")): a for a in out.get("included_articles") or []}
    keep = []
    for sp in out.get("evidence_spans") or []:
        a = arts.get(str(sp.get("paper_pmid") or ""))
        if a and verbatim(sp.get("text", ""), source_text(a)):
            keep.append(sp)
        else:
            removed.append({"kind": "evidence_span", "study": sp.get("paper_pmid"), "label": sp.get("section", ""),
                            "value": sp.get("text", ""), "reason": "not verbatim text of the paper"})
    out["evidence_spans"] = keep
    for it in verified:
        if it["status"] != "ungrounded":
            continue
        removed.append({k: it[k] for k in ("id", "kind", "study", "label", "value", "reason")})
        p = it["path"]
        if it["kind"] == "grade":
            drops.append(("grade", p))
        elif it["kind"] == "appraisal" or (it["kind"] == "risk_of_bias" and "assessments" in p) \
                or (it["kind"] == "risk_of_bias" and isinstance(p[-1], int)):
            drops.append(("list", p[:-1] if p[-1] in ("rating", "judgment") else p))
        else:
            parent = _get(out, p[:-1])
            if isinstance(parent, (dict, list)):
                parent[p[-1]] = UNGROUNDED
    by_parent: dict[str, tuple[list, set[int]]] = {}
    for kind, p in drops:
        if kind == "grade":
            g = _get(out, p[:2])
            if isinstance(g, dict) and p[3] in (g.get("domains") or {}):
                del g["domains"][p[3]]
                g["overall_certainty"] = "Not assessed"
                g["summary"] = (f"Certainty not assessed: the {p[3]} rating was not source-grounded. "
                                + str(g.get("summary") or "")).strip()
        elif isinstance(p[-1], int):
            key = json.dumps(p[:-1])
            by_parent.setdefault(key, (p[:-1], set()))[1].add(p[-1])
    for parent_path, idxs in by_parent.values():
        parent = _get(out, parent_path)
        if isinstance(parent, list):
            for i in sorted(idxs, reverse=True):     # from the end, so indices stay valid
                if i < len(parent):
                    parent.pop(i)
    removed += drop_without_full_text(out)
    return out, removed


def drop_without_full_text(review: dict) -> list[dict]:
    """Full evaluation on full text only: remove included studies without it.

    Title/abstract screening may use abstracts. Eligibility and everything after
    it — charting, appraisal, synthesis — needs the report itself, so a study
    read only as an abstract is a report not retrieved (PRISMA 2020), not an
    included study. Its rubric, appraisal, narrative row and spans go with it,
    and the flow is recomputed (``full_text_flow``). Mutates ``review``;
    returns what was removed.
    """
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    gone = {pid for pid, a in arts.items() if not (a.get("full_text") or "").strip()}
    if gone:
        rub = rubric_for(review, arts)
        sids = {str(rub[p].get("source_id")) for p in gone if p in rub and rub[p].get("source_id")}
        gone_rubrics = {id(rub[p]) for p in gone if p in rub}
        review["included_articles"] = [a for a in review["included_articles"] if str(a.get("pmid")) not in gone]
        review["evidence_spans"] = [s for s in review.get("evidence_spans") or []
                                    if str(s.get("paper_pmid")) not in gone]
        review["data_charting_rubrics"] = [r for r in review.get("data_charting_rubrics") or []
                                           if id(r) not in gone_rubrics]
        for key in ("structured_appraisal_results", "critical_appraisals", "narrative_rows"):
            if review.get(key):
                review[key] = [x for x in review[key] if str(x.get("source_id")) not in sids]
    full_text_flow(review, dropped=len(gone))
    return [{"kind": "study", "study": pid, "label": "included study",
             "value": arts[pid].get("title", ""),
             "reason": "no full text: judged on its abstract only, so it was never fully evaluated; "
                       "counted as a report not retrieved"} for pid in sorted(gone)]


def full_text_flow(review: dict, dropped: int = 0) -> None:
    """Recompute the eligibility half of the PRISMA flow for full-text-only evaluation.

    An eligibility decision made on an abstract was not a full evaluation, so
    that report counts as not retrieved, whether the decision was include or
    exclude. Only decisions on the full text count as assessed / excluded.
    sought − not retrieved = assessed; assessed − excluded = included.
    """
    flow = review.get("flow")
    if not isinstance(flow, dict):
        return
    log = review.get("screening_log") or []
    ft = [e for e in log if str(e.get("stage", "")).endswith("full_text")]
    on_text = [e for e in ft if e.get("assessed_on", "full_text") != "abstract_only"]
    excluded = [e for e in on_text if str(e.get("decision", "")).lower() == "exclude"]
    n_inc = len(review.get("included_articles") or [])
    if ft:
        flow["assessed_eligibility"] = len(on_text)
        flow["excluded_eligibility"] = len(excluded)
        flow["assessed_on_full_text"] = len(on_text)
        reasons: dict[str, int] = {}
        for e in excluded:
            r = (e.get("reason") or "Unspecified")[:80]
            reasons[r] = reasons.get(r, 0) + 1
        flow["excluded_reasons_full_text"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:12])
    else:
        flow["assessed_eligibility"] = n_inc + int(flow.get("excluded_eligibility") or 0)
    flow["assessed_on_abstract_only"] = 0
    flow["included_synthesis"] = n_inc
    flow["included_with_full_text"] = n_inc
    flow["included_abstract_only"] = 0
    sought = int(flow.get("sought_fulltext") or 0)
    if sought:
        flow["not_retrieved"] = max(0, sought - flow["assessed_eligibility"])
    else:   # no sought count to derive it from: the dropped studies were not retrieved
        flow["not_retrieved"] = int(flow.get("not_retrieved") or 0) + dropped


def _get(node: Any, path: list) -> Any:
    for k in path:
        try:
            node = node[k]
        except (KeyError, IndexError, TypeError):
            return None
    return node


# ─────────────────────────── model calls ────────────────────────────

def _deps(review: dict, args):
    # The app scores every span as grounded when rapidfuzz is missing: refuse.
    try:
        import rapidfuzz  # noqa: F401
    except ImportError:
        sys.exit("rapidfuzz is not installed. Without it the app's evidence-span filter cannot check "
              "spans and accepts every one as grounded, so extraction does not run. Install it "
              "(pip install 'rapidfuzz>=3.0'; see check_deps.py) and re-run.")
    from synthscholar.agents import AgentDeps, build_model
    from synthscholar.models import ReviewProtocol
    key = args.api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set. Set it (ask the user for it), or fill "
                 "the ledger's evidence yourself and run `verify`.")
    proto = ReviewProtocol(**(review.get("protocol") or {}))
    return AgentDeps(protocol=proto, api_key=key, model_name=args.model,
                     model=build_model(key, args.model))


async def _quote_study(sid: str, article: dict, items: list[dict], deps, spans: list[dict]) -> dict:
    """Ask the model for verbatim quotes establishing each item of one study."""
    from pydantic import BaseModel
    from pydantic_ai import Agent

    class Q(BaseModel):
        item_id: str
        quotes: list[str]

    class Out(BaseModel):
        answers: list[Q]

    if sid == "__grade__":
        body = "\n".join(f"[{i}] ({s.get('paper_pmid')}) {s.get('text')}" for i, s in enumerate(spans))
        task = ("For each GRADE domain rating below, list the indices of the evidence spans that "
                "justify the rating (as quotes: copy each chosen span's text exactly). Choose only "
                "spans that bear on that outcome and domain. Return an empty list if none do.")
    else:
        body = source_text(article)[:MAX_TEXT]
        task = ("For each item below, copy from the paper the sentence(s) that establish the value — "
                "verbatim, character for character, without ellipses or edits. Every number in the "
                "value must appear in what you copy. If the paper does not establish the value, "
                "return an empty list for it. Do not paraphrase.")
    listing = "\n".join(f"- {it['id']}: {it['label']} = {it['value'][:400]}" for it in items)
    agent = Agent(deps.model, output_type=Out, system_prompt=task)
    res = await agent.run(f"PAPER {sid}:\n{body}\n\nITEMS:\n{listing}")
    return {a.item_id: a.quotes for a in res.output.answers}


async def quote_all(review: dict, ledger: dict, only: set[str], deps, concurrency: int = 4) -> int:
    verified = {x["id"]: x for x in verify_items(review, ledger)}
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    todo: dict[str, list[dict]] = {}
    for it in verified.values():
        if it["status"] == "ungrounded" and (not only or it["study"] in only or it["kind"] == "grade"):
            todo.setdefault(it["study"] or "__grade__", []).append(it)
    spans = review.get("evidence_spans") or []
    sem = asyncio.Semaphore(concurrency)
    got: dict[str, list[str]] = {}

    async def one(sid, its):
        async with sem:
            try:
                got.update({k: [(sid, q) for q in v] for k, v in
                            (await _quote_study(sid, arts.get(sid, {}), its, deps, spans)).items()})
            except Exception as e:  # one paper's failure must not stop the others
                print(f"  {sid}: quoting failed ({e})", file=sys.stderr)

    await asyncio.gather(*(one(s, its) for s, its in todo.items()))
    by_text = {s.get("text"): str(s.get("paper_pmid")) for s in spans}
    items = {x["id"]: x for x in ledger.get("items") or []}
    n = 0
    for iid, quotes in got.items():
        ev = []
        for sid, q in quotes:
            study = by_text.get(q, sid) if sid == "__grade__" else sid
            ev.append({"study": study, "quote": q})
        if iid in verified and ev:
            items[iid] = {**verified[iid], "evidence": ev, "source": f"model:{deps.model_name}"}
            n += 1
    ledger["items"] = list(items.values())
    return n


async def extract_spans(review: dict, pmids: set[str], deps, stages: list[str]) -> dict:
    """Re-run the app's extraction stages for ``pmids`` on their current text."""
    from synthscholar.agents import (default_appraisal_config, default_charting_template,
                                     run_critical_appraisal, run_data_charting, run_narrative_row,
                                     run_risk_of_bias)
    from synthscholar.evidence import extract_evidence
    from synthscholar.models import Article
    from synthscholar.pipeline import _resolve_section_config

    arts = [Article(**a) for a in review.get("included_articles") or [] if str(a.get("pmid")) in pmids]
    spans = await extract_evidence(arts, deps)
    by_doi = {a.pmid: a.doi for a in arts}
    new = [{**s.model_dump(), "doi": s.doi or by_doi.get(s.paper_pmid, "")} for s in spans]
    review["evidence_spans"] = [s for s in review.get("evidence_spans") or []
                                if str(s.get("paper_pmid")) not in pmids] + new
    if "charting" not in stages:
        return review
    proto = deps.protocol
    rub = rubric_for(review, {str(a.get("pmid")): a for a in review.get("included_articles") or []})
    for art in arts:
        old = rub.get(art.pmid) or {}
        sid = old.get("source_id", "")
        art.risk_of_bias = await run_risk_of_bias(art, deps)
        rubric = await run_data_charting(art, deps, proto.charting_question_prompts() or None,
                                         resolved_section_config=_resolve_section_config(proto),
                                         charting_template=proto.charting_template or default_charting_template())
        rubric.source_id = sid or rubric.source_id
        ap_rubric, ap_result = await run_critical_appraisal(
            art, rubric, deps, None,
            appraisal_config=proto.critical_appraisal_config or default_appraisal_config())
        ap_rubric.source_id = ap_result.source_id = rubric.source_id
        row = await run_narrative_row(rubric, ap_rubric, deps)
        row.source_id = rubric.source_id

        def put(key, obj):
            lst = review.setdefault(key, [])
            dumped = obj.model_dump(mode="json")
            for i, x in enumerate(lst):
                if str(x.get("source_id")) == rubric.source_id:
                    lst[i] = dumped
                    return
            lst.append(dumped)

        put("data_charting_rubrics", rubric)
        put("critical_appraisals", ap_rubric)
        put("structured_appraisal_results", ap_result)
        put("narrative_rows", row)
        for a in review["included_articles"]:
            if str(a.get("pmid")) == art.pmid:
                a["risk_of_bias"] = art.risk_of_bias.model_dump(mode="json")
    return review


# ─────────────────────────── files ────────────────────────────

def side(review_json: Path, name: str) -> Path:
    return review_json.with_name(f"{review_json.stem}.{name}.json")


def load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
    except (OSError, ValueError):
        return default


def log_extraction(review_json: Path, review: dict, pmids: set[str], stages: list[str], model: str) -> None:
    log = load(side(review_json, "extraction"), {"runs": []})
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    for pid in sorted(pmids):
        a = arts.get(pid) or {}
        log["runs"].append({"pmid": pid, "doi": a.get("doi", ""), "text_sha256": text_sha256(a),
                            "extracted_at": now(), "stages": stages, "model": model,
                            "spans": sum(1 for s in review.get("evidence_spans") or []
                                         if str(s.get("paper_pmid")) == pid)})
    side(review_json, "extraction").write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")


def write_review(review_json: Path, review: dict, out: Optional[str]) -> Path:
    dest = Path(out) if out else review_json
    if dest == review_json:
        shutil.copy(review_json, review_json.with_name(f"{review_json.stem}.before-extraction.json"))
    dest.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


def summarise(verified: list[dict]) -> dict:
    from collections import Counter
    c = Counter((x["kind"], x["status"]) for x in verified)
    return {f"{k}:{s}": n for (k, s), n in sorted(c.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["spans", "refresh", "items", "quote", "locate", "verify", "apply"])
    ap.add_argument("review", help="review JSON (PRISMAReviewResult)")
    ap.add_argument("--only", nargs="+", default=[], metavar="ID", help="limit to these study ids")
    ap.add_argument("--out", help="output review JSON (spans/refresh: default in place, with a backup; "
                                  "apply: required)")
    ap.add_argument("--model", default=os.environ.get("SYNTHSCHOLAR_MODEL", "anthropic/claude-sonnet-4"))
    ap.add_argument("--api-key", default="")
    args = ap.parse_args()
    src = Path(args.review)
    review = load(src, None)
    if review is None:
        print(f"{src}: unreadable", file=sys.stderr)
        return 2
    only = set(args.only)
    pages = {k: v.get("pages") if isinstance(v, dict) else v for k, v in load(side(src, "pages"), {}).items()}
    ledger_path = side(src, "evidence-ledger")
    ledger = load(ledger_path, {"items": []})

    if args.command in ("spans", "refresh"):
        if args.command == "refresh" and not only:
            ap.error("refresh needs --only (the studies whose source changed)")
        pmids = only or {str(a.get("pmid")) for a in review.get("included_articles") or []}
        stages = ["spans"] if args.command == "spans" else ["spans", "risk_of_bias", "charting",
                                                              "appraisal", "narrative"]
        deps = _deps(review, args)
        review = asyncio.run(extract_spans(review, pmids, deps, stages))
        dest = write_review(src, review, args.out)
        log_extraction(dest, review, pmids, stages, args.model)
        print(f"{args.command}: {len(pmids)} stud{'y' if len(pmids) == 1 else 'ies'} → {dest}", file=sys.stderr)
        return 0

    if args.command == "items":
        verified = verify_items(review, ledger)
        spans = review.get("evidence_spans") or []
        for it in verified:
            if it["status"] == "ungrounded" and not it["evidence"] and it["study"]:
                words = set(norm(it["value"]).split())
                cands = sorted((s for s in spans if str(s.get("paper_pmid")) == it["study"]),
                               key=lambda s: -len(words & set(norm(s.get("text", "")).split())))[:3]
                it["candidates"] = [s.get("text", "") for s in cands]
        ledger = {"review": str(src.resolve()), "generated_at": now(),
                  "instructions": "For each item give evidence [{study, quote}] with quotes copied verbatim "
                                  "from that study's text; every number in the value must be in a quote. "
                                  "Then run `verify`; `apply` removes whatever stays ungrounded.",
                  "items": verified}
        ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"items: {len(verified)} → {ledger_path}  {summarise(verified)}", file=sys.stderr)
        return 0

    if args.command == "locate":
        stats = locate_all(review, ledger, pages)
        dest = write_review(src, review, args.out)
        ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"locate: {stats['relabelled']} label(s) corrected to where the text is, "
              f"{stats['ambiguous']} ambiguous, {stats['not_verbatim']} not verbatim → {dest}", file=sys.stderr)
        return 0

    if args.command == "quote":
        if not ledger.get("items"):
            ledger["items"] = verify_items(review, ledger)
        n = asyncio.run(quote_all(review, ledger, only, _deps(review, args)))
        ledger["items"] = verify_items(review, ledger)
        ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"quote: evidence proposed for {n} item(s); {summarise(ledger['items'])}", file=sys.stderr)
        return 0

    verified = verify_items(review, ledger, pages)
    ledger["items"] = verified
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    bad = [x for x in verified if x["status"] == "ungrounded"]
    print(f"{len(verified)} value(s): {summarise(verified)}", file=sys.stderr)
    if args.command == "verify":
        for x in bad[:20]:
            print(f"  UNGROUNDED {x['kind']} {x['study'] or ''} {x['label']}: {x['reason']}", file=sys.stderr)
        return 1 if bad else 0
    if not args.out:
        ap.error("apply needs --out (the grounded copy to export and deliver)")
    grounded, removed = apply_grounding(review, verified)
    out = Path(args.out)
    out.write_text(json.dumps(grounded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out.with_name(f"{out.stem}.ungrounded.json").write_text(
        json.dumps({"from": str(src), "removed": removed}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copy(ledger_path, side(out, "evidence-ledger"))
    print(f"apply: removed {len(removed)} ungrounded value(s) → {out} "
          f"(listed in {out.stem}.ungrounded.json)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
