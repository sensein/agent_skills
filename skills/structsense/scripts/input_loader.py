"""Read a document of any supported format and return plain text with reproducible
offsets.

This is the most important "missing" piece for users running extraction on
papers: the skill assumes plain-text input, but real users hand it PDFs — and
increasingly Word manuscripts, slide decks, spreadsheets and page scans. This
module extracts text deterministically, with fallbacks so it works whether or
not the user has Docling, GROBID, PyMuPDF or pdfminer installed.

**Docling is stage 1 for every format it handles**, and it handles far more than
PDF: DOCX, PPTX, XLSX, HTML, AsciiDoc, Markdown, CSV, images (PNG/JPEG/TIFF/BMP/
WEBP) and, with its `asr` extra, audio. For everything except PDF/CSV/TXT/MD it
is the *only* backend here — nothing else in the chain can open a .docx or a page
scan at all. So the order is Docling first, then the PDF-specific backends as
fallback for when Docling is absent or fails:

1. **Docling** (https://github.com/docling-project/docling) — a layout model and
   a table-structure model over a unified document representation, exported as
   Markdown. OCR is on by default, which makes it the only backend that reads a
   *scanned* PDF or an image; everything below needs a text layer. Pure pip, no
   server, but models are downloaded on first use and it is the slowest option,
   so it is skippable (`--no-docling`).
2. **GROBID** (PDF only, if reachable) — purpose-built for academic PDFs and
   still the best at TEI section structure (Introduction, Methods, Results, …).
   Pointed at by `GROBID_SERVER_URL_OR_EXTERNAL_SERVICE` or `--grobid-url`.
3. **pymupdf4llm** (PDF only) — layout-aware Markdown, no server and no models.
4. **PyMuPDF** (`fitz`, PDF only) — fast, accurate raw-text extractor. Works on
   any PDF type (papers, questionnaires, reports, forms).
5. **pdfminer.six** (PDF only) — pure-Python, slower, no system deps. Last-resort
   fallback when none of the above is installed.

TXT/MD/CSV always read straight off disk, Docling installed or not (CSV is
round-tripped through pandas if available, otherwise read verbatim). Docling
converts those formats too, but a text file gains nothing from a document model
and the conversion would change the bytes — which would shift every span offset
computed against an earlier extraction of the same file.

For extraction pipelines: **always write the extracted text to disk first**, so
character offsets in the result file refer to a stable text. Re-extracting later
might yield different offsets if a library version changes — and switching
backends definitely does.

Adapted from structsense `utils.process_file`.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Union

logger = logging.getLogger("input_loader")


# What Docling converts, and therefore what this module accepts. Audio needs
# docling's `asr` extra; if it is missing the conversion fails and, having no
# fallback for that format, the error is raised rather than silently swallowed.
DOCLING_SUFFIXES = frozenset({
    ".pdf",
    ".docx", ".pptx", ".xlsx",
    ".html", ".htm", ".xhtml",
    ".md", ".adoc", ".asciidoc",
    ".csv",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    ".wav", ".mp3",
})

# What the non-Docling backends can read. An extension in DOCLING_SUFFIXES but
# not here has no fallback: no Docling, no text.
FALLBACK_SUFFIXES = frozenset({".pdf", ".csv", ".txt", ".md"})

# Formats that are already text and are read straight off disk, Docling installed
# or not. Docling converts these too, but routing them through a document model
# buys nothing and would *change the bytes* — and since span offsets are computed
# against whatever text this module returns, changing them silently invalidates
# every offset in a result file produced before the change.
TEXTUAL_SUFFIXES = frozenset({".txt", ".md", ".csv"})

SUPPORTED_SUFFIXES = DOCLING_SUFFIXES | FALLBACK_SUFFIXES


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def process_file(
    source_path: Union[str, Path],
    *,
    grobid_url: Optional[str] = None,
    prefer_grobid: bool = True,
    use_docling: bool = True,
) -> str:
    """Read a file and return its content as plain text.

    Docling runs first for every format it supports (see `DOCLING_SUFFIXES`);
    PDF/CSV/TXT/MD fall back to the older backends when it is absent or fails,
    and the other formats have no fallback because nothing else here can open
    them.

    Args:
        source_path: file to read.
        grobid_url: GROBID server URL. Defaults to the
            `GROBID_SERVER_URL_OR_EXTERNAL_SERVICE` env var, or
            `http://localhost:8070`.
        prefer_grobid: if True (default), try GROBID before the PyMuPDF family
            once Docling is out of the picture. Set False to skip GROBID
            entirely (useful for non-academic PDFs).
        use_docling: if True (default), Docling is stage 1. Set False to skip it
            when the model download or its per-page cost is not worth it — PDFs
            then go GROBID → pymupdf4llm → PyMuPDF → pdfminer, and the
            Docling-only formats become unreadable.

    Raises:
        FileNotFoundError: if `source_path` doesn't exist.
        ValueError: if the extension is unsupported, or all extractors fail.
    """
    p = Path(source_path)
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {p}")

    ext = p.suffix.lower()
    if ext not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported extension {ext!r} (supported: "
            f"{', '.join(sorted(SUPPORTED_SUFFIXES))})"
        )

    errors: list[str] = []

    # Stage 1, and the only stage that is format-agnostic.
    if use_docling and ext in DOCLING_SUFFIXES and ext not in TEXTUAL_SUFFIXES:
        text = _try_docling(p, errors=errors)
        if text and text.strip():
            if ext == ".pdf":
                warn_if_captions_missing(text, f"{p.name} (docling)")
            return text
        if ext not in FALLBACK_SUFFIXES:
            raise ValueError(
                f"docling could not read {p.name} and nothing else here can open "
                f"{ext} files: " + " | ".join(errors)
            )
        logger.info("docling unavailable for %s, falling back: %s",
                    p.name, " | ".join(errors))
    elif ext not in FALLBACK_SUFFIXES:
        raise ValueError(
            f"{ext} files can only be read by docling, which is disabled here "
            f"(use_docling=False / --no-docling)"
        )

    if ext == ".pdf":
        return _read_pdf(p, grobid_url=grobid_url, prefer_grobid=prefer_grobid,
                         errors=errors)
    if ext == ".csv":
        return _read_csv(p)
    return p.read_text(encoding="utf-8", errors="replace")


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
            "(3) pip install docling (layout + table models, and the only backend "
            "that OCRs a scanned PDF); (4) pip install pymupdf4llm (layout-aware, "
            "no server) and re-extract.",
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
              prefer_grobid: bool, errors: Optional[list[str]] = None) -> str:
    """The PDF-only fallback chain, reached when Docling is absent or failed.

    `errors` carries in whatever stage 1 already recorded, so the final failure
    message names every backend that was tried, Docling included.
    """
    errors = errors if errors is not None else []

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


def _try_docling(path: Path, *, errors: list[str]) -> Optional[str]:
    """Markdown via Docling — any supported format, layout + tables, OCR for scans.

    Docling converts through a unified document representation instead of a text
    layer: a layout model orders the page and a table-structure model reconstructs
    cells, so a two-column paper comes back in reading order and a table comes back
    as a pipe table. The same call handles DOCX, PPTX, XLSX, HTML, images and the
    rest of `DOCLING_SUFFIXES` — `convert()` dispatches on the format itself, which
    is why this is the general stage-1 backend rather than a PDF one.

    OCR is on by default, which makes this the one backend here that returns text
    for a scanned PDF or an image — every other one returns nothing,
    indistinguishable from a corrupt file.

    OCR is on by default (`PdfPipelineOptions.do_ocr=True`), and `pip install
    docling` bundles an engine (rapidocr) so it works with no system packages;
    `docling[easyocr]`, or `docling[ocrmac]` on macOS, swap the engine.

    The cost is real: models are downloaded on first use (hundreds of MB) and
    conversion is seconds per page, more with OCR. Failures fall through to the
    lighter backends rather than raising.
    """
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as e:
        errors.append(f"docling: not installed ({e}); pip install docling")
        return None
    try:
        result = DocumentConverter().convert(str(path))
        return result.document.export_to_markdown()
    except Exception as e:  # noqa: BLE001 - any failure just falls through
        errors.append(f"docling: {e}")
        return None


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
        description="Extract text from a document (PDF, DOCX, PPTX, XLSX, HTML, "
                    "image, CSV, TXT, MD — anything docling reads) and write a "
                    "<stem>.txt next to the source so subsequent extraction "
                    "stages have stable character offsets."
    )
    ap.add_argument("source",
                    help="path to any supported document; docling handles the "
                         "non-PDF formats, PDFs also have server-free fallbacks")
    ap.add_argument("--out", default=None, help="output text path (default: <stem>.txt)")
    ap.add_argument("--grobid-url", default=None,
                    help="GROBID server URL (default: $GROBID_SERVER_URL_OR_EXTERNAL_SERVICE or http://localhost:8070)")
    ap.add_argument("--no-grobid", action="store_true",
                    help="Skip GROBID in the PDF fallback chain.")
    ap.add_argument("--no-docling", action="store_true",
                    help="Skip Docling, the stage-1 backend (avoids its model "
                         "download and per-page cost). PDFs then go GROBID -> "
                         "pymupdf4llm -> PyMuPDF -> pdfminer; DOCX/PPTX/XLSX/HTML/"
                         "image input becomes unreadable.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out_path, text = process_file_to_text_file(
        args.source,
        out_path=args.out,
        grobid_url=args.grobid_url,
        prefer_grobid=not args.no_grobid,
        use_docling=not args.no_docling,
    )
    print(f"{out_path}\t{len(text)} chars", file=sys.stderr)
