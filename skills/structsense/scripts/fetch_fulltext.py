#!/usr/bin/env python3
"""Fetch structured full text for an open-access paper — no PDF, no GROBID.

Why this exists
---------------
PDF text extraction is the recall ceiling for figure captions and tables. Measured
against a gold standard: 19 of 101 passages could not be located in PDF-derived text,
costing 155 annotations, and 7 papers had **no figure-caption text at all**. Captions
are in scope for cell extraction and among the densest passages in a paper, so that is
a large loss and none of it is the extractor's fault.

GROBID reconstructs captions well, but it is a Java server on port 8070 that most
people do not have and cannot always install. For an open-access paper there is a
better answer than parsing the PDF at all: fetch the publisher's own structured XML,
which has captions and tables as first-class elements because that is how it was
typeset.

Sources, in the order tried
---------------------------
1. **Europe PMC** ``fullTextXML`` — JATS, open, no key, no registration. Covers PMC
   open-access plus additional Europe PMC content.
2. **NCBI BioC (PMC OA)** — returns *passage-structured* text with
   ``section_type=FIG`` / ``TABLE`` passages. For a corpus whose gold standard is
   BioC, this is the strongest option available: the passage segmentation matches the
   annotations, so "passage not found" stops being possible.
3. **NCBI efetch (db=pmc)** — JATS for PMC records Europe PMC does not serve.

All three are HTTP GETs against public endpoints. Nothing here needs a key; set
``NCBI_API_KEY`` only to raise NCBI's rate limit.

What you get
------------
Plain text with section headings, **including** ``Figure N`` captions and
``Table N`` captions with their cell contents — the parts the PDF path loses. Written
to disk so offsets are stable, exactly like ``input_loader.process_file_to_text_file``.

Limits, stated plainly
----------------------
This only works for **open-access** papers. A paywalled paper has no fullTextXML and
no BioC record, and this script says so rather than silently falling back — a silent
fallback to a caption-less PDF parse is how the ceiling stayed invisible. For those,
GROBID or the ``input_loader`` cascade remains the answer, and the coverage report in
``input_loader.caption_coverage`` tells you what it cost.

Usage
-----
    python -m scripts.fetch_fulltext PMC7077007 --out paper.txt
    python -m scripts.fetch_fulltext 32183906 --out paper.txt      # PMID works too
    python -m scripts.fetch_fulltext PMC7077007 --source bioc      # force a source
    python -m scripts.fetch_fulltext --id-file ids.txt --out-dir ./text
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

_EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
_BIOC = ("https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/"
         "BioC_xml/{pmcid}/unicode")
_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_ID_CONV = ("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
            "?ids={ident}&format=json&tool=structsense&email={email}")

# Public endpoints; be a good citizen even when unauthenticated.
_DELAY_SECONDS = 0.4
_TIMEOUT = 60

SOURCES = ("bioc", "europepmc", "efetch")


def _get(url: str) -> Optional[bytes]:
    req = Request(url, headers={"User-Agent": "structsense-skill/1.0"})
    try:
        with urlopen(req, timeout=_TIMEOUT) as r:
            return r.read()
    except HTTPError as e:
        # 404 just means this source does not have it — the caller tries the next.
        if e.code != 404:
            print(f"  {url.split('/')[2]}: HTTP {e.code}", file=sys.stderr)
        return None
    except (URLError, TimeoutError) as e:
        print(f"  {url.split('/')[2]}: {e}", file=sys.stderr)
        return None


def normalise_id(ident: str) -> str:
    """Return a `PMC…` id. A bare number is treated as a PMID and converted."""
    ident = ident.strip()
    if re.fullmatch(r"(?i)pmc\d+", ident):
        return "PMC" + re.sub(r"(?i)^pmc", "", ident)
    if re.fullmatch(r"\d+", ident):
        email = os.getenv("SYNTHSCHOLAR_EMAIL") or os.getenv("NCBI_EMAIL") or ""
        raw = _get(_ID_CONV.format(ident=ident, email=email))
        if raw:
            import json
            try:
                recs = json.loads(raw).get("records") or []
                if recs and recs[0].get("pmcid"):
                    return recs[0]["pmcid"]
            except (ValueError, KeyError):
                pass
        raise SystemExit(
            f"{ident}: could not resolve to a PMCID. A PMID only has full text here "
            "if the paper is in PMC — pass the PMCID directly if you know it."
        )
    return ident


# --------------------------------------------------------------------------- #
# BioC — passage-structured, matches a BioC gold standard's segmentation
# --------------------------------------------------------------------------- #

def bioc_to_text(xml_bytes: bytes) -> Optional[str]:
    """Flatten BioC passages to text, keeping FIG and TABLE passages.

    Section types are preserved as headings so `paper_location` can record them and
    so a caption is recognisable as a caption downstream.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    out: list[str] = []
    for passage in root.iter("passage"):
        infons = {i.get("key"): (i.text or "") for i in passage.findall("infon")}
        text_el = passage.find("text")
        text = (text_el.text or "").strip() if text_el is not None else ""
        if not text:
            continue
        section = (infons.get("section_type") or infons.get("type") or "").strip()
        out.append(f"[{section}]\n{text}" if section else text)
    return "\n\n".join(out).strip() or None


# --------------------------------------------------------------------------- #
# JATS — Europe PMC fullTextXML and NCBI efetch
# --------------------------------------------------------------------------- #

def _flat(el) -> str:
    """All descendant text, whitespace-collapsed.

    Same reason as the TEI converter: `el.text` stops at the first inline child, and
    JATS body text is full of <xref>, <italic> and <sup>, so reading only `.text`
    truncates most paragraphs at their first citation.
    """
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def jats_to_text(xml_bytes: bytes) -> Optional[str]:
    """Convert JATS to text including <fig> captions and <table-wrap> content."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    parts: list[str] = []

    title = root.find(".//article-title")
    if title is not None:
        t = _flat(title)
        if t:
            parts.append(t)

    abstract = root.find(".//abstract")
    if abstract is not None:
        a = _flat(abstract)
        if a:
            parts.append("[ABSTRACT]\n" + a)

    body = root.find(".//body")
    if body is not None:
        for el in body.iter():
            if el.tag == "sec":
                head = el.find("title")
                label = _flat(head) if head is not None else ""
                paras = [p for p in (_flat(p) for p in el.findall("p")) if p]
                if paras:
                    parts.append((f"[{label}]\n" if label else "") + "\n".join(paras))
            elif el.tag == "fig":
                parts.append(_jats_fig(el, "FIG"))
            elif el.tag == "table-wrap":
                parts.append(_jats_fig(el, "TABLE", with_cells=True))

    # Captions can also sit outside <body> (floats group).
    floats = root.find(".//floats-group")
    if floats is not None:
        for el in floats.iter():
            if el.tag == "fig":
                parts.append(_jats_fig(el, "FIG"))
            elif el.tag == "table-wrap":
                parts.append(_jats_fig(el, "TABLE", with_cells=True))

    parts = [p for p in parts if p and p.strip()]
    return "\n\n".join(parts).strip() or None


def _jats_fig(el, kind: str, *, with_cells: bool = False) -> str:
    label_el = el.find("label")
    cap_el = el.find("caption")
    label = _flat(label_el) if label_el is not None else ""
    caption = _flat(cap_el) if cap_el is not None else ""

    rows: list[str] = []
    if with_cells:
        for tr in el.iter("tr"):
            cells = [c for c in (_flat(c) for c in list(tr)) if c]
            if cells:
                rows.append(" | ".join(cells))

    body = "\n".join(filter(None, [label or kind.title(), caption, "\n".join(rows)]))
    return f"[{kind}]\n{body}" if body.strip() else ""


# --------------------------------------------------------------------------- #

def fetch(ident: str, sources: Iterable[str] = SOURCES) -> Tuple[Optional[str], str]:
    """Return (text, source_used). Tries each source in order."""
    pmcid = normalise_id(ident)
    for src in sources:
        time.sleep(_DELAY_SECONDS)
        if src == "bioc":
            raw = _get(_BIOC.format(pmcid=pmcid))
            text = bioc_to_text(raw) if raw else None
        elif src == "europepmc":
            raw = _get(_EPMC.format(pmcid=pmcid))
            text = jats_to_text(raw) if raw else None
        elif src == "efetch":
            params = {"db": "pmc", "id": pmcid, "retmode": "xml"}
            if os.getenv("NCBI_API_KEY"):
                params["api_key"] = os.environ["NCBI_API_KEY"]
            raw = _get(f"{_EFETCH}?{urlencode(params)}")
            text = jats_to_text(raw) if raw else None
        else:
            raise SystemExit(f"unknown source {src!r} (known: {', '.join(SOURCES)})")
        if text:
            return text, src
    return None, ""


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_fulltext",
        description="Fetch structured full text (with figure captions and tables) "
                    "for an open-access paper — no PDF and no GROBID needed.",
    )
    ap.add_argument("ids", nargs="*", help="PMCID or PMID, one or more")
    ap.add_argument("--id-file", type=Path, help="file with one id per line")
    ap.add_argument("--out", type=Path, help="output .txt (single id only)")
    ap.add_argument("--out-dir", type=Path, default=Path("."),
                    help="output directory when several ids are given")
    ap.add_argument("--source", action="append", choices=SOURCES,
                    help="restrict/order sources (repeatable; default: "
                         + " then ".join(SOURCES) + ")")
    a = ap.parse_args(argv)

    ids = list(a.ids)
    if a.id_file:
        ids += [ln.strip() for ln in a.id_file.read_text().splitlines() if ln.strip()]
    if not ids:
        ap.error("give at least one id, or --id-file")
    if a.out and len(ids) > 1:
        ap.error("--out takes a single id; use --out-dir for several")

    sources = a.source or list(SOURCES)
    ok = failed = 0
    for ident in ids:
        text, used = fetch(ident, sources)
        if not text:
            print(f"FAILED {ident}: no full text from {', '.join(sources)}. The paper "
                  f"is probably not open access — use GROBID or the input_loader "
                  f"cascade, and check caption coverage.", file=sys.stderr)
            failed += 1
            continue
        out = a.out if a.out else a.out_dir / f"{normalise_id(ident)}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        caps = len(re.findall(r"^\[(FIG|TABLE)\]", text, re.M))
        print(f"wrote {out} ({len(text):,} chars, {caps} caption/table block(s), "
              f"via {used})")
        ok += 1

    if failed:
        print(f"{failed} of {len(ids)} failed", file=sys.stderr)
        return 2 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
