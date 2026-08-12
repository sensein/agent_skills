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
            return text

    text = _try_pymupdf(path, errors=errors)
    if text and text.strip():
        return text

    text = _try_pdfminer(path, errors=errors)
    if text and text.strip():
        return text

    raise ValueError(
        f"all PDF extractors failed for {path.name}: " + " | ".join(errors)
    )


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


def _tei_to_text(xml_text: str, *, errors: list[str]) -> Optional[str]:
    """Convert TEI XML from GROBID into plain text with section headings."""
    try:
        from xml.etree import ElementTree as ET
    except ImportError as e:
        errors.append(f"tei parser: {e}")
        return None

    try:
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        errors.append(f"grobid TEI parse error: {e}")
        return None

    parts: list[str] = []

    # Title
    for t in root.iter("{http://www.tei-c.org/ns/1.0}title"):
        if t.text and t.text.strip():
            parts.append(t.text.strip())
            break

    # Abstract
    for ab in root.iter("{http://www.tei-c.org/ns/1.0}abstract"):
        ab_text = " ".join((p.text or "") for p in ab.iter("{http://www.tei-c.org/ns/1.0}p") if (p.text or "").strip())
        if ab_text.strip():
            parts.append("Abstract\n" + ab_text.strip())
        break

    # Body sections
    for div in root.iter("{http://www.tei-c.org/ns/1.0}div"):
        head = div.find("tei:head", ns)
        head_text = (head.text.strip() if head is not None and head.text else "")
        paras = []
        for p in div.findall("tei:p", ns):
            if p.text and p.text.strip():
                paras.append(p.text.strip())
        if head_text and paras:
            parts.append(f"{head_text}\n" + "\n".join(paras))
        elif paras:
            parts.append("\n".join(paras))

    if not parts:
        return None

    return "\n\n".join(parts).strip()


def _try_pymupdf(path: Path, *, errors: list[str]) -> Optional[str]:
    try:
        import fitz   # PyMuPDF
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
            t = page.get_text()
            if t and t.strip():
                pages.append(t)
        return "\n\n".join(pages).strip()
    finally:
        doc.close()


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
