"""Serialise ABCD extraction / synthesis results as JSON, Markdown and Turtle.

Three renderings of the same verified payload, because three different consumers
need it:

  * **JSON** — the machine record. Everything, including `rejected[]`.
  * **Markdown** — tables a human reads in a PR or a lab notebook.
  * **Turtle** — triples for a graph store (e.g. BrainKB), so provenance survives
    into a queryable form.

The TTL uses PROV-O for provenance and a small local vocabulary for the domain
terms. Every claim node carries the quote, character offsets, section/page and
the mapping provenance, so "where in the paper did this come from?" is answerable
in SPARQL rather than by re-reading the PDF.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

NS = "https://structsense.skills/abcd/"
PREFIXES = f"""@prefix abcd:    <{NS}> .
@prefix prov:    <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .
@prefix cogat:   <https://www.cognitiveatlas.org/concept/id/> .
@prefix nbdc:    <https://nbdc-datahub.org/variable/> .
"""

_SLUG = re.compile(r"[^A-Za-z0-9]+")


def slug(*parts: Any) -> str:
    """A stable local-name from arbitrary text."""
    s = "-".join(str(p) for p in parts if p not in (None, ""))
    return _SLUG.sub("-", s).strip("-").lower() or "unnamed"


def esc(value: Any) -> str:
    """Turtle-escape a string literal (long form handles newlines)."""
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return s.replace("\n", "\\n").replace("\r", "")


def lit(value: Any) -> str:
    return f'"{esc(value)}"'


def md_escape(value: Any) -> str:
    """Make a value safe inside a Markdown table cell."""
    s = "" if value is None else str(value)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def md_table(headers: List[str], rows: Iterable[Iterable[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    empty = True
    for row in rows:
        empty = False
        out.append("| " + " | ".join(md_escape(c) for c in row) + " |")
    if empty:
        out.append("| " + " | ".join("—" for _ in headers) + " |")
    return "\n".join(out)


def _trunc(s: Any, n: int = 160) -> str:
    t = "" if s is None else str(s)
    return t if len(t) <= n else t[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# per-paper rendering
# --------------------------------------------------------------------------- #

def paper_markdown(doc: dict) -> str:
    meta = doc.get("source_metadata") or {}
    ver = doc.get("verification") or {}
    lines = [
        f"# ABCD extraction — {meta.get('paper_title') or meta.get('source_path') or doc.get('paper_id')}",
        "",
        f"- **Paper id**: `{doc.get('paper_id')}`",
        f"- **Source**: `{meta.get('source_path')}`",
        f"- **DOI**: {meta.get('doi') or '—'}",
        f"- **Study / release stated in paper**: {meta.get('study') or '—'}"
        f" / {meta.get('data_release') or '—'}",
        f"- **Verified**: {ver.get('variables_dictionary_verified', 0)} variables against the"
        f" data dictionary, {ver.get('constructs_mapped', 0)} constructs mapped,"
        f" {ver.get('rejected_total', 0)} claims rejected",
        "",
        "## Variables used in this study",
        "",
        md_table(
            ["Mentioned as", "Maps to variable", "nda_or_nbdc_table", "nbdc_domain",
             "Role", "Match", "Release(s)", "Section", "Quote"],
            [
                (
                    v.get("mention_as_written") or v.get("name"),
                    (v.get("dictionary_match") or {}).get("variable") or "— unverified —",
                    v.get("nda_or_nbdc_table") or "—",
                    v.get("nbdc_domain") or "—",
                    v.get("role") or "—",
                    (v.get("dictionary_match") or {}).get("match_method")
                    if v.get("dictionary_status") == "verified" else v.get("dictionary_status"),
                    ", ".join(v.get("dd_releases_containing") or []) or "—",
                    (v.get("evidence") or {}).get("section") or "—",
                    _trunc((v.get("evidence") or {}).get("quote"), 90),
                )
                for v in doc.get("variables", [])
            ],
        ),
        "",
        "## Constructs",
        "",
        md_table(
            ["Construct", "Cognitive Atlas id", "Verbatim in text", "Section", "Quote"],
            [
                (
                    c.get("construct_label") or c.get("construct"),
                    c.get("construct_id") or f"— ({c.get('mapping_provenance')})",
                    "yes" if (c.get("evidence") or {}).get("label_in_quote") else "no (mapped)",
                    (c.get("evidence") or {}).get("section") or "—",
                    _trunc((c.get("evidence") or {}).get("quote"), 110),
                )
                for c in doc.get("constructs", [])
            ],
        ),
        "",
        "## Models",
        "",
        md_table(
            ["Specification", "Predictors", "Outcomes", "Mediators", "Moderators", "Section"],
            [
                (
                    m.get("specification") or m.get("name"),
                    ", ".join(m.get("predictors") or []) or "—",
                    ", ".join(m.get("outcomes") or []) or "—",
                    ", ".join(m.get("mediators") or []) or "—",
                    ", ".join(m.get("moderators") or []) or "—",
                    (m.get("evidence") or {}).get("section") or "—",
                )
                for m in doc.get("models", [])
            ],
        ),
        "",
        "## Findings",
        "",
        md_table(
            ["Finding", "Direction", "Role", "Construct", "Effect", "Section", "Quote"],
            [
                (
                    _trunc(f.get("statement"), 90),
                    f.get("direction"),
                    f.get("role"),
                    f.get("construct_label") or f.get("construct") or "—",
                    f.get("effect_size") or f.get("estimate") or "—",
                    (f.get("evidence") or {}).get("section") or "—",
                    _trunc((f.get("evidence") or {}).get("quote"), 90),
                )
                for f in doc.get("findings", [])
            ],
        ),
    ]

    rejected = doc.get("rejected") or []
    if rejected:
        lines += [
            "",
            "## Rejected claims (failed verification)",
            "",
            "Kept deliberately: a reader can see what the model proposed and why it did not stand.",
            "",
            md_table(
                ["Section", "Reason", "Claim", "Claimed quote"],
                [
                    (r.get("section"), r.get("reason"),
                     _trunc(json.dumps(r.get("claim"), sort_keys=True), 80),
                     _trunc(r.get("claimed_quote"), 80))
                    for r in rejected
                ],
            ),
        ]

    prov = doc.get("provenance") or {}
    lines += [
        "",
        "## Provenance",
        "",
        f"- **Extractor model**: `{prov.get('llm_model')}`",
        f"- **Text extraction**: {prov.get('text_extractor')} "
        f"({prov.get('text_chars')} chars)",
        f"- **Run at**: {prov.get('run_at')}  •  **skill**: {prov.get('skill_version')}",
        "",
        "Dictionaries consulted:",
        "",
        md_table(
            ["Study", "Release", "Variables", "Method", "Retrieved"],
            [
                (d.get("study"), d.get("dd_release"), d.get("variable_count"),
                 d.get("method"), d.get("retrieved_at"))
                for d in (prov.get("dictionaries") or [])
            ],
        ),
    ]
    return "\n".join(lines) + "\n"


def paper_turtle(doc: dict) -> str:
    pid = slug(doc.get("paper_id") or (doc.get("source_metadata") or {}).get("source_path"))
    paper = f"abcd:paper-{pid}"
    meta = doc.get("source_metadata") or {}
    prov = doc.get("provenance") or {}
    out = [PREFIXES, ""]

    out.append(f"{paper} a abcd:Publication ;")
    if meta.get("paper_title"):
        out.append(f"    dcterms:title {lit(meta['paper_title'])} ;")
    if meta.get("doi"):
        out.append(f"    dcterms:identifier {lit(meta['doi'])} ;")
    if meta.get("source_path"):
        out.append(f"    abcd:sourcePath {lit(meta['source_path'])} ;")
    if meta.get("data_release"):
        out.append(f"    abcd:statedDataRelease {lit(meta['data_release'])} ;")
    out.append(f"    abcd:extractedBy {lit(prov.get('llm_model'))} ;")
    out.append(f"    prov:generatedAtTime {lit(prov.get('run_at'))}^^xsd:dateTime .")
    out.append("")

    def evidence_block(node: str, ev: dict, indent: str = "    ") -> List[str]:
        """Emit the evidence triples every claim shares."""
        rows = [
            f"{indent}abcd:quote {lit(ev.get('quote'))} ;",
            f"{indent}abcd:usedContext {lit(ev.get('used_context'))} ;",
            f"{indent}abcd:charStart {int(ev.get('start') or 0)} ;",
            f"{indent}abcd:charEnd {int(ev.get('end') or 0)} ;",
            f"{indent}abcd:anchorMethod {lit(ev.get('anchor_method'))} ;",
        ]
        if ev.get("section"):
            rows.append(f"{indent}abcd:section {lit(ev['section'])} ;")
        if ev.get("page") is not None:
            rows.append(f"{indent}abcd:page {lit(ev['page'])} ;")
        return rows

    for i, v in enumerate(doc.get("variables", [])):
        node = f"abcd:var-{pid}-{i}-{slug(v.get('name'))}"
        ev = v.get("evidence") or {}
        dm = v.get("dictionary_match") or {}
        out.append(f"{node} a abcd:VariableUse ;")
        out.append(f"    abcd:variableName {lit(v.get('name'))} ;")
        out.append(f"    abcd:mentionAsWritten "
                   f"{lit(v.get('mention_as_written') or v.get('name'))} ;")
        out.append(f"    abcd:dictionaryStatus {lit(v.get('dictionary_status'))} ;")
        if dm.get("variable"):
            out.append(f"    abcd:dictionaryVariable nbdc:{slug(dm['variable'])} ;")
            out.append(f"    abcd:ddRelease {lit(dm.get('dd_release'))} ;")
            out.append(f"    abcd:matchMethod {lit(dm.get('match_method'))} ;")
            if v.get("nda_or_nbdc_table"):
                out.append(f"    abcd:ndaOrNbdcTable {lit(v['nda_or_nbdc_table'])} ;")
            if v.get("nbdc_domain"):
                out.append(f"    abcd:nbdcDomain {lit(v['nbdc_domain'])} ;")
            if v.get("nbdc_sub_domain"):
                out.append(f"    abcd:nbdcSubDomain {lit(v['nbdc_sub_domain'])} ;")
        if v.get("role"):
            out.append(f"    abcd:role {lit(v['role'])} ;")
        out += evidence_block(node, ev)
        out.append(f"    prov:wasDerivedFrom {paper} .")
        out.append("")

    for i, c in enumerate(doc.get("constructs", [])):
        node = f"abcd:construct-{pid}-{i}-{slug(c.get('construct'))}"
        ev = c.get("evidence") or {}
        out.append(f"{node} a abcd:ConstructMention ;")
        out.append(f"    rdfs:label {lit(c.get('construct_label') or c.get('construct'))} ;")
        if c.get("construct_id"):
            out.append(f"    abcd:cognitiveAtlasConcept cogat:{c['construct_id']} ;")
            out.append(f"    abcd:mappingProvenance {lit(c.get('mapping_provenance'))} ;")
        else:
            out.append(f"    abcd:mappingProvenance {lit(c.get('mapping_provenance'))} ;")
        out.append(f"    abcd:labelVerbatimInText "
                   f"{'true' if ev.get('label_in_quote') else 'false'} ;")
        out += evidence_block(node, ev)
        out.append(f"    prov:wasDerivedFrom {paper} .")
        out.append("")

    for i, m in enumerate(doc.get("models", [])):
        node = f"abcd:model-{pid}-{i}"
        ev = m.get("evidence") or {}
        out.append(f"{node} a abcd:StatisticalModel ;")
        out.append(f"    rdfs:label {lit(m.get('specification') or m.get('name'))} ;")
        for key, pred in (("predictors", "abcd:hasPredictor"),
                          ("outcomes", "abcd:hasOutcome"),
                          ("mediators", "abcd:hasMediator"),
                          ("moderators", "abcd:hasModerator"),
                          ("covariates", "abcd:hasCovariate")):
            for name in m.get(key) or []:
                out.append(f"    {pred} {lit(name)} ;")
        out += evidence_block(node, ev)
        out.append(f"    prov:wasDerivedFrom {paper} .")
        out.append("")

    for i, f in enumerate(doc.get("findings", [])):
        node = f"abcd:finding-{pid}-{i}"
        ev = f.get("evidence") or {}
        out.append(f"{node} a abcd:Finding ;")
        out.append(f"    abcd:statement {lit(f.get('statement'))} ;")
        out.append(f"    abcd:direction {lit(f.get('direction'))} ;")
        out.append(f"    abcd:role {lit(f.get('role'))} ;")
        if f.get("construct_id"):
            out.append(f"    abcd:aboutConstruct cogat:{f['construct_id']} ;")
        for name in f.get("variables") or []:
            out.append(f"    abcd:aboutVariable {lit(name)} ;")
        if f.get("effect_size") or f.get("estimate"):
            out.append(f"    abcd:effect {lit(f.get('effect_size') or f.get('estimate'))} ;")
        out += evidence_block(node, ev)
        out.append(f"    prov:wasDerivedFrom {paper} .")
        out.append("")

    for d in (prov.get("dictionaries") or []):
        node = f"abcd:dd-{slug(d.get('study'), d.get('dd_release'))}"
        out.append(f"{node} a abcd:DataDictionary ;")
        out.append(f"    abcd:study {lit(d.get('study'))} ;")
        out.append(f"    abcd:ddRelease {lit(d.get('dd_release'))} ;")
        out.append(f"    abcd:variableCount {int(d.get('variable_count') or 0)} ;")
        out.append(f"    abcd:retrievalMethod {lit(d.get('method'))} ;")
        if d.get("source"):
            out.append(f"    prov:hadPrimarySource {lit(d['source'])} ;")
        out.append(f"    prov:generatedAtTime {lit(d.get('retrieved_at'))}^^xsd:dateTime .")
        out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# synthesis rendering
# --------------------------------------------------------------------------- #

def synthesis_markdown(doc: dict) -> str:
    tot = doc.get("totals") or {}
    lines = [
        "# ABCD cross-paper synthesis",
        "",
        f"- **Papers**: {tot.get('papers', 0)}"
        f"  •  **verified variable uses**: {tot.get('variable_uses', 0)}"
        f"  •  **findings**: {tot.get('findings', 0)}",
        f"- **Constructs with consensus**: {tot.get('consensus_constructs', 0)}"
        f"  •  **with divergence**: {tot.get('divergent_constructs', 0)}",
        "",
        "## Consensus and divergence by construct",
        "",
        "`agreement` is the share of papers backing the majority direction. A construct is",
        "*divergent* when papers report opposing directions, not merely when they differ in size.",
        "",
        md_table(
            ["Construct", "Papers", "Positive", "Negative", "Null", "Majority",
             "Agreement", "Verdict"],
            [
                (
                    c.get("construct_label") or c.get("construct_id") or c.get("construct"),
                    c.get("paper_count"),
                    (c.get("directions") or {}).get("positive", 0),
                    (c.get("directions") or {}).get("negative", 0),
                    (c.get("directions") or {}).get("null", 0),
                    c.get("majority_direction"),
                    f"{c.get('agreement', 0):.2f}",
                    c.get("verdict"),
                )
                for c in doc.get("constructs", [])
            ],
        ),
        "",
        "## Variable roles across papers",
        "",
        "Whether a variable is treated consistently as a mediator/moderator, or its role is contested.",
        "",
        md_table(
            ["Variable", "nda_or_nbdc_table", "nbdc_domain", "Papers",
             "Roles observed", "Dominant role", "Consistency", "Verdict"],
            [
                (
                    v.get("variable"),
                    (v.get("dictionary_match") or {}).get("nda_or_nbdc_table") or "—",
                    (v.get("dictionary_match") or {}).get("nbdc_domain") or "—",
                    v.get("paper_count"),
                    ", ".join(f"{k}×{n}" for k, n in (v.get("roles") or {}).items()),
                    v.get("dominant_role"),
                    f"{v.get('role_consistency', 0):.2f}",
                    v.get("verdict"),
                )
                for v in doc.get("variables", [])
            ],
        ),
    ]

    contested = [v for v in doc.get("variables", []) if v.get("verdict") == "contested_role"]
    if contested:
        lines += ["", "### Contested roles — evidence side by side", ""]
        for v in contested:
            lines.append(f"**`{v['variable']}`**")
            lines.append("")
            lines.append(md_table(
                ["Paper", "Role", "Section", "Quote"],
                [(e.get("paper_id"), e.get("role"), e.get("section"),
                  _trunc(e.get("quote"), 90)) for e in v.get("evidence") or []],
            ))
            lines.append("")

    lines += [
        "",
        "## Papers included",
        "",
        md_table(
            ["Paper id", "Title / source", "Release", "Variables", "Findings"],
            [
                (p.get("paper_id"), _trunc(p.get("title") or p.get("source_path"), 60),
                 p.get("data_release") or "—", p.get("variable_count"), p.get("finding_count"))
                for p in doc.get("papers", [])
            ],
        ),
    ]
    return "\n".join(lines) + "\n"


def synthesis_turtle(doc: dict) -> str:
    out = [PREFIXES, ""]
    sid = slug(doc.get("synthesis_id") or "synthesis")
    node = f"abcd:synthesis-{sid}"
    tot = doc.get("totals") or {}
    out.append(f"{node} a abcd:CrossPaperSynthesis ;")
    out.append(f"    abcd:paperCount {int(tot.get('papers') or 0)} ;")
    out.append(f"    prov:generatedAtTime {lit((doc.get('provenance') or {}).get('run_at'))}"
               f"^^xsd:dateTime ;")
    for p in doc.get("papers", []):
        out.append(f"    prov:used abcd:paper-{slug(p.get('paper_id'))} ;")
    out.append("    rdfs:label \"ABCD cross-paper synthesis\" .")
    out.append("")

    for c in doc.get("constructs", []):
        cn = f"abcd:consensus-{sid}-{slug(c.get('construct_id') or c.get('construct'))}"
        out.append(f"{cn} a abcd:ConstructConsensus ;")
        if c.get("construct_id"):
            out.append(f"    abcd:aboutConstruct cogat:{c['construct_id']} ;")
        out.append(f"    rdfs:label {lit(c.get('construct_label') or c.get('construct'))} ;")
        out.append(f"    abcd:paperCount {int(c.get('paper_count') or 0)} ;")
        out.append(f"    abcd:majorityDirection {lit(c.get('majority_direction'))} ;")
        out.append(f"    abcd:agreement {float(c.get('agreement') or 0):.3f} ;")
        out.append(f"    abcd:verdict {lit(c.get('verdict'))} ;")
        for e in c.get("evidence") or []:
            out.append(f"    prov:wasDerivedFrom abcd:paper-{slug(e.get('paper_id'))} ;")
        out.append(f"    prov:wasGeneratedBy {node} .")
        out.append("")

    for v in doc.get("variables", []):
        vn = f"abcd:roleprofile-{sid}-{slug(v.get('variable'))}"
        out.append(f"{vn} a abcd:VariableRoleProfile ;")
        out.append(f"    abcd:variableName {lit(v.get('variable'))} ;")
        out.append(f"    abcd:paperCount {int(v.get('paper_count') or 0)} ;")
        out.append(f"    abcd:dominantRole {lit(v.get('dominant_role'))} ;")
        out.append(f"    abcd:roleConsistency {float(v.get('role_consistency') or 0):.3f} ;")
        out.append(f"    abcd:verdict {lit(v.get('verdict'))} ;")
        for e in v.get("evidence") or []:
            out.append(f"    prov:wasDerivedFrom abcd:paper-{slug(e.get('paper_id'))} ;")
        out.append(f"    prov:wasGeneratedBy {node} .")
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def write_all(doc: dict, base: Path, *, kind: str = "paper",
              formats: Iterable[str] = ("json", "md", "ttl")) -> Dict[str, Path]:
    """Write `doc` in each requested format next to `base` (a stem path)."""
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    md_fn = paper_markdown if kind == "paper" else synthesis_markdown
    ttl_fn = paper_turtle if kind == "paper" else synthesis_turtle
    written: Dict[str, Path] = {}
    for fmt in formats:
        if fmt not in ("json", "md", "ttl"):
            raise ValueError(f"unknown format {fmt!r} (json | md | ttl)")
        # APPEND the extension; never with_suffix(). Paper filenames routinely embed
        # a DOI ("Whitmore-2023-10.1162_imag_a_00037"), and with_suffix() would treat
        # ".1162_imag_a_00037_abcd" as the suffix and replace it — truncating the name
        # to "Whitmore-2023-10.json" and silently colliding with every other paper
        # from the same year and prefix.
        out = base.parent / f"{base.name}.{fmt}"
        if fmt == "json":
            out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        elif fmt == "md":
            out.write_text(md_fn(doc))
        else:
            out.write_text(ttl_fn(doc))
        written[fmt] = out
    return written
