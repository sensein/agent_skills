"""Read PDF / CSV / TXT input and return plain text with reproducible offsets.

This is the most important "missing" piece for users running NER on papers:
the skill assumes plain-text input, but real users hand it PDFs. This module
extracts text deterministically, with multiple fallbacks so it works whether
or not the user has GROBID, PyMuPDF, or pdfminer installed.

Backend selection order:

1. **GROBID** (if reachable) — best for academic PDFs. Preserves section
   structure (Introduction, Methods, Results, …). Pointed at by the
   `GROBID_SERVER_URL_OR_EXTERNAL_SERVICE` env var or `--grobid-url` flag.
2. **PyMuPDF** (`fitz`) — fast, accurate raw-text extractor. Works on any
   PDF type (papers, questionnaires, reports, forms). Falls back to it when
   GROBID is unreachable or returns no sections.
3. **pdfminer.six** — pure-Python, slower, no system deps. Last-resort
   fallback when neither GROBID nor PyMuPDF is available.

CSV/TXT just read the bytes (CSV is round-tripped through pandas if available,
otherwise read verbatim).

For NER pipelines: **always write the extracted text to disk before
extraction**, so character offsets in the result file refer to a stable text.
Re-extracting the PDF later might yield different offsets if a library
version changes.

Adapted from structsense `utils.process_file`.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Union

logger = logging.getLogger("input_loader")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def process_file(
    source_path: Union[str, Path],
    *,
    grobid_url: Optional[str] = None,
    prefer_grobid: bool = True,
) -> str:
    """Read a file and return its content as plain text.

    Supported extensions: `.pdf`, `.csv`, `.txt`, `.md`.

    Args:
        source_path: file to read.
        grobid_url: GROBID server URL. Defaults to the
            `GROBID_SERVER_URL_OR_EXTERNAL_SERVICE` env var, or
            `http://localhost:8070`.
        prefer_grobid: if True (default), try GROBID first for PDFs.
            Set False to skip GROBID and go straight to PyMuPDF/pdfminer
            (useful for non-academic PDFs).

    Raises:
        FileNotFoundError: if `source_path` doesn't exist.
        ValueError: if the extension is unsupported, or all extractors fail.
    """
    p = Path(source_path)
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {p}")

    ext = p.suffix.lower()
    if ext == ".pdf":
        return _read_pdf(p, grobid_url=grobid_url, prefer_grobid=prefer_grobid)
    if ext == ".csv":
        return _read_csv(p)
    if ext in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="replace")

    raise ValueError(f"unsupported extension {ext!r} (supported: .pdf, .csv, .txt, .md)")


# Caption / table markers, covering every producer: BioC and JATS emit "[FIG]" /
# "[TABLE]" section tags (scripts/fetch_fulltext.py), GROBID TEI yields a "Figure N" /
# "Table N" head, and a raw PDF text layer usually keeps the printed label.
_CAPTION_RE = re.compile(
    r"^\s*(\[(?:FIG|TABLE)\]|(?:Figure|Fig\.?|Table|Supplementary\s+(?:Figure|Table))"
    r"\s*(?:S?\d+|[IVX]+)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def caption_coverage(text: str) -> dict:
    """Report how much figure/table text survived extraction.

    This exists because caption loss is silent and expensive. Measured against a gold
    standard: 19 of 101 passages could not be located in PDF-derived text, costing 155
    annotations, and **7 papers had no figure-caption text at all** — captions are in
    scope and among the densest passages, so it is a large recall ceiling that looks
    exactly like an extractor failure.

    A count of 0 on a research paper is the signal worth acting on. It almost never
    means the paper has no figures; it means this extraction path dropped them.
    """
    blocks = _CAPTION_RE.findall(text or "")
    return {
        "caption_blocks": len(blocks),
        "chars": len(text or ""),
        "looks_empty": len(blocks) == 0,
    }


def warn_if_captions_missing(text: str, label: str = "input") -> dict:
    """`caption_coverage`, but it says something when the answer is bad."""
    cov = caption_coverage(text)
    if cov["looks_empty"] and cov["chars"] > 2000:
        logger.warning(
            "%s: no figure/table captions found in %d chars of extracted text. "
            "Captions are in scope and dense, so this is probably an extraction "
            "ceiling rather than a paper without figures. Options, best first: "
            "(1) open access? `python -m scripts.fetch_fulltext <PMCID>` needs no PDF "
            "and no GROBID; (2) run GROBID and pass --grobid-url; "
            "(3) pip install pymupdf4llm (layout-aware, no server) and re-extract.",
            label, cov["chars"],
        )
    return cov


def process_file_to_text_file(
    source_path: Union[str, Path],
    out_path: Optional[Union[str, Path]] = None,
    **kwargs,
) -> Tuple[Path, str]:
    """Extract text and write it to `<source_stem>.txt` for stable offsets.

    Returns ``(out_path, text)``. Useful as the first step of an NER pipeline
    so every subsequent stage references the same canonical text file.
    """
    src = Path(source_path)
    text = process_file(src, **kwargs)
    out = Path(out_path) if out_path else src.with_suffix(".txt")
    out.write_text(text, encoding="utf-8")
    logger.info("wrote %d chars to %s", len(text), out)
    return out, text


# ---------------------------------------------------------------------------
# PDF backends
# ---------------------------------------------------------------------------

def _read_pdf(path: Path, *, grobid_url: Optional[str],
              prefer_grobid: bool) -> str:
    errors: list[str] = []

    if prefer_grobid:
        text = _try_grobid(path, grobid_url=grobid_url, errors=errors)
        if text and text.strip():
            warn_if_captions_missing(text, f"{path.name} (grobid)")
            return text

    # pymupdf4llm before plain PyMuPDF: it is layout-aware and keeps figure captions
    # and table structure, which is the whole difference that matters here, and unlike
    # GROBID it is a pip install with no server. This is the best option for anyone who
    # cannot run GROBID.
    text = _try_pymupdf4llm(path, errors=errors)
    if text and text.strip():
        warn_if_captions_missing(text, f"{path.name} (pymupdf4llm)")
        return text

    text = _try_pymupdf(path, errors=errors)
    if text and text.strip():
        warn_if_captions_missing(text, f"{path.name} (pymupdf)")
        return text

    text = _try_pdfminer(path, errors=errors)
    if text and text.strip():
        warn_if_captions_missing(text, f"{path.name} (pdfminer)")
        return text

    raise ValueError(
        f"all PDF extractors failed for {path.name}: " + " | ".join(errors)
    )


def _try_pymupdf4llm(path: Path, *, errors: list[str]) -> Optional[str]:
    """Layout-aware Markdown via pymupdf4llm — captions and tables preserved.

    Markdown rather than plain text is fine and slightly better here: a table becomes
    a pipe table on contiguous lines instead of a column of stray cells, so a caption
    or a row stays locatable as one passage. The `#`/`|` characters are inert for span
    offsets, which are computed against whatever text is written to disk.
    """
    try:
        import pymupdf4llm
    except ImportError as e:
        errors.append(f"pymupdf4llm: not installed ({e}); pip install pymupdf4llm")
        return None
    try:
        return pymupdf4llm.to_markdown(str(path))
    except Exception as e:  # noqa: BLE001 - any failure just falls through
        errors.append(f"pymupdf4llm: {e}")
        return None


def _try_grobid(path: Path, *, grobid_url: Optional[str],
                errors: list[str]) -> Optional[str]:
    url = (grobid_url
           or os.environ.get("GROBID_SERVER_URL_OR_EXTERNAL_SERVICE")
           or "http://localhost:8070")
    try:
        import requests
    except ImportError as e:
        errors.append(f"grobid: requests not installed ({e})")
        return None
    # Quick probe — if not reachable, fall back fast.
    try:
        r = requests.get(f"{url.rstrip('/')}/api/isalive", timeout=2)
        if not r.ok:
            errors.append(f"grobid {url}: /isalive returned {r.status_code}")
            return None
    except requests.RequestException as e:
        errors.append(f"grobid {url}: unreachable ({e})")
        return None

    # Submit the PDF
    try:
        with open(path, "rb") as fh:
            files = {"input": (path.name, fh, "application/pdf")}
            r = requests.post(
                f"{url.rstrip('/')}/api/processFulltextDocument",
                files=files, timeout=120,
            )
        if not r.ok:
            errors.append(f"grobid: HTTP {r.status_code}")
            return None
        xml_text = r.text
    except requests.RequestException as e:
        errors.append(f"grobid: request failed ({e})")
        return None

    # Parse the TEI XML to plain text (preserving section headings + paragraphs)
    return _tei_to_text(xml_text, errors=errors)


_TEI_NS = "http://www.tei-c.org/ns/1.0"


def _tei_text(el) -> str:
    """All descendant text of a TEI element, whitespace-collapsed.

    `el.text` alone is only the text BEFORE the first child, so a paragraph like
    ``<p>Astrocytes <ref>[12]</ref> were labelled in mPFC.</p>`` yielded just
    "Astrocytes" — the rest lives in the ``tail`` of the inline element. TEI from
    GROBID is dense with inline <ref>/<hi>/<formula>, so reading only `.text`
    truncated nearly every paragraph at its first citation. itertext() walks the
    whole subtree.
    """
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _tei_to_text(xml_text: str, *, errors: list[str]) -> Optional[str]:
    """Convert TEI XML from GROBID into plain text with section headings.

    Includes figure captions and table content. They were previously dropped
    outright: GROBID puts them in <figure>/<figDesc>/<table>, which this function
    never visited, so a caption-dense paper came out with no caption text at all.
    Captions are in scope for cell extraction and among the densest passages in a
    paper, so losing them is a recall ceiling that looks like an extractor failure
    (measured on a gold corpus: 19 of 101 passages unlocatable, 155 annotations,
    7 papers with zero caption text).
    """
    try:
        from xml.etree import ElementTree as ET
    except ImportError as e:
        errors.append(f"tei parser: {e}")
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        errors.append(f"grobid TEI parse error: {e}")
        return None

    def q(tag: str) -> str:
        return f"{{{_TEI_NS}}}{tag}"

    parts: list[str] = []

    for t in root.iter(q("title")):
        text = _tei_text(t)
        if text:
            parts.append(text)
            break

    for ab in root.iter(q("abstract")):
        ab_text = " ".join(
            filter(None, (_tei_text(p) for p in ab.iter(q("p"))))
        )
        if ab_text:
            parts.append("Abstract\n" + ab_text)
        break

    # Walk the body in DOCUMENT order so a caption stays near the section that
    # refers to it. Emitting all divs and then all figures would put every caption
    # at the end, which breaks nothing mechanically but makes paper_location and any
    # section-aware chunking wrong.
    body = root.find(f".//{q('body')}")
    scope = body if body is not None else root
    seen_div: set[int] = set()

    for el in scope.iter():
        if el.tag == q("div"):
            if id(el) in seen_div:
                continue
            seen_div.add(id(el))
            head = el.find(q("head"))
            head_text = _tei_text(head) if head is not None else ""
            paras = [t for t in (_tei_text(p) for p in el.findall(q("p"))) if t]
            if not paras:
                continue
            parts.append(f"{head_text}\n" + "\n".join(paras) if head_text
                         else "\n".join(paras))

        elif el.tag == q("figure"):
            # type="table" marks a table; everything else is a figure.
            is_table = (el.get("type") or "").lower() == "table"
            head = el.find(q("head"))
            label = _tei_text(head) if head is not None else ""
            desc_el = el.find(q("figDesc"))
            desc = _tei_text(desc_el) if desc_el is not None else ""
            # GROBID often repeats the label at the head of figDesc ("Figure 3" +
            # "Figure 3Pvalb basket cells…"). Emitting both duplicates the label and,
            # worse, glues it to the first word of the caption.
            if label and desc.startswith(label):
                desc = desc[len(label):].lstrip(" .:—-")

            # Table cells row by row, so a cell's row-mates stay adjacent and a
            # sentence splitter does not turn each cell into its own "sentence".
            rows: list[str] = []
            for row in el.iter(q("row")):
                cells = [t for t in (_tei_text(c) for c in row.findall(q("cell"))) if t]
                if cells:
                    rows.append(" | ".join(cells))

            chunk = "\n".join(filter(None, [
                label or ("Table" if is_table else "Figure"),
                desc,
                "\n".join(rows),
            ]))
            if chunk.strip():
                parts.append(chunk)

    if not parts:
        return None

    return "\n\n".join(parts).strip()


def _try_pymupdf(path: Path, *, errors: list[str]) -> Optional[str]:
    try:
        # PyMuPDF 1.28 deprecated the `fitz` alias ("will be removed in future") and
        # emits a warning on import. Prefer the real module name, keep `fitz` for
        # older installs.
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz   # PyMuPDF < 1.24-ish
    except ImportError as e:
        errors.append(f"pymupdf: not installed ({e}); pip install pymupdf")
        return None
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        errors.append(f"pymupdf: open failed ({e})")
        return None
    try:
        pages = []
        for page in doc:
            t = _pymupdf_page_text(page)
            if t and t.strip():
                pages.append(t)
        return "\n\n".join(pages).strip()
    finally:
        doc.close()


def _pymupdf_page_text(page) -> str:
    """Page text in reading order, keeping caption and table blocks.

    `page.get_text()` with default flags returns blocks in the PDF's internal
    order, which on a two-column paper interleaves the columns and scatters a
    figure caption into the middle of unrelated body text. That is the mechanism
    behind "the caption is in the PDF but I cannot locate the passage": the words
    are all present, just not contiguous, so a passage-level match fails.

    `sort=True` orders blocks top-to-bottom / left-to-right, which keeps a caption
    together as one block. It is not column-aware — GROBID still does that better —
    but it turns scrambled text into merely mis-sequenced text, and a caption that
    survives as one contiguous block is locatable.
    """
    try:
        return page.get_text("text", sort=True)
    except TypeError:
        # Older PyMuPDF without the sort kwarg.
        return page.get_text()


def _try_pdfminer(path: Path, *, errors: list[str]) -> Optional[str]:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as e:
        errors.append(f"pdfminer: not installed ({e}); pip install pdfminer.six")
        return None
    try:
        return extract_text(str(path))
    except Exception as e:
        errors.append(f"pdfminer: extract failed ({e})")
        return None


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> str:
    """Round-trip the CSV through pandas (if available) for clean re-serialization.

    Falls back to raw file read if pandas isn't installed.
    """
    try:
        import pandas as pd
        return pd.read_csv(path).to_csv(index=False)
    except ImportError:
        return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(
        description="Extract text from a PDF / CSV / TXT and write a "
                    "<stem>.txt next to the source so subsequent NER stages "
                    "have stable character offsets."
    )
    ap.add_argument("source", help="path to .pdf / .csv / .txt / .md")
    ap.add_argument("--out", default=None, help="output text path (default: <stem>.txt)")
    ap.add_argument("--grobid-url", default=None,
                    help="GROBID server URL (default: $GROBID_SERVER_URL_OR_EXTERNAL_SERVICE or http://localhost:8070)")
    ap.add_argument("--no-grobid", action="store_true",
                    help="Skip GROBID; use PyMuPDF / pdfminer only.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out_path, text = process_file_to_text_file(
        args.source,
        out_path=args.out,
        grobid_url=args.grobid_url,
        prefer_grobid=not args.no_grobid,
    )
    print(f"{out_path}\t{len(text)} chars", file=sys.stderr)
