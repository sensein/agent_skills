#!/usr/bin/env python3
"""Turn a folder of user-supplied PDFs into a SynthScholar review corpus.

This is step 1 of the bring-your-own-corpus workflow (SKILL.md → Mode 3):
the *user* did the searching and full-text collection, so there is no
discovery phase — the PDFs on disk **are** the corpus.

For each PDF this script:

  1. extracts plain text (PyMuPDF, via ``synthscholar.clients.PyMuPdfParser``
     when the package is importable so behaviour matches the app exactly),
  2. mints a stable synthetic ID (``local_<sha8>``) unless a real PMID/DOI is
     supplied via ``--manifest``,
  3. records full-text provenance (``full_text_source='user_supplied_pdf'``,
     SHA-256 content hash, retrieval timestamp),
  4. best-effort guesses ``doi`` / ``title`` / ``year`` / ``abstract``, and
  5. flags whatever is still missing in ``_needs_metadata`` so the agent can
     complete it by reading ``_head_text``.

Keys prefixed with ``_`` are skill bookkeeping — ``run_local_review.py``
strips them before building ``synthscholar.models.Article`` objects.

Usage:
    # build a corpus from a directory of PDFs
    python build_corpus.py --dir ./pdfs --out corpus.json

    # individual files + known metadata from a manifest
    python build_corpus.py --pdf a.pdf --pdf b.pdf \
        --manifest metadata.csv --out corpus.json

    # report which entries still need metadata (exit 1 if any do)
    python build_corpus.py --check corpus.json

Manifest formats (all columns optional, matched to PDFs by ``file``):
    CSV   file,pmid,doi,title,authors,journal,year,abstract,source
    JSON  [{"file": "a.pdf", "title": "...", ...}, ...]
          {"a.pdf": {"title": "...", ...}, ...}

``authors`` must be **semicolon-separated** ("Smith J; Doe A") — the RDF
exporter splits on ``;`` to emit one ``dcterms:creator`` per author.

Each PDF is read **in full** by default (``--max-chars 0``): the review pipeline
chunks every article's stored text and reads every chunk, so a truncated corpus
would hide the results and discussion sections from the analysis.

Requires: pymupdf (``pip install pymupdf``) for text extraction. Falls back
to ``pypdf`` and then the ``pdftotext`` CLI when PyMuPDF is unavailable.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Fields the review pipeline genuinely needs per article. `abstract` is not
# strictly required (title/abstract screening falls back to the full text)
# but a missing abstract measurably weakens screening, so it is requested.
REQUIRED_METADATA = ["title", "authors", "year", "abstract"]

# Text scanned for metadata guesses and handed to the agent for completion.
HEAD_CHARS = 2500

# Stand-in for "no limit" when a parser insists on a positive cap. Larger than
# any real paper; a 2 MB text layer is already ~500 pages.
_UNLIMITED = 20_000_000

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
_ABSTRACT_HEAD_RE = re.compile(r"\bA\s?B\s?S\s?T\s?R\s?A\s?C\s?T\b\s*[:.\-—]?\s*", re.I)
# Section headings that reliably terminate an abstract.
_ABSTRACT_END_RE = re.compile(
    r"\n\s*(?:Keywords?|Key\s?words|Index Terms|CCS Concepts|ACM Reference|"
    r"(?:\d+\.?\s*)?Introduction|(?:\d+\.?\s*)?Background)\b",
    re.I,
)
# Hard cap when no terminating heading is found.
_ABSTRACT_MAX = 3000
# Trailing punctuation that regularly gets glued onto a DOI in PDF text.
_DOI_TRAILING = ".,;:)]}>'\""


# ── PDF text extraction ────────────────────────────────────────────────


def _extract_text(pdf: Path, max_chars: int) -> tuple[str, str]:
    """Return ``(text, extractor_name)``; text is "" when every backend fails.

    ``max_chars=0`` means the whole document — the default, because the review
    pipeline chunks each article's full text and reads every chunk, so a
    truncated corpus would hide exactly the results sections it needs.
    """
    max_chars = max_chars if max_chars > 0 else _UNLIMITED
    # 1. The app's own parser — identical cap + control-char scrubbing.
    try:
        from synthscholar.clients import PyMuPdfParser  # type: ignore

        parser = PyMuPdfParser(max_chars=max_chars)
        if parser.available:
            text = parser.parse_path(pdf)
            if text:
                return text, "synthscholar.PyMuPdfParser"
    except ImportError:
        pass

    # 2. Bare PyMuPDF.
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        doc = fitz.open(str(pdf))
        chunks, total = [], 0
        for page in doc:
            t = page.get_text("text") or ""
            if not t:
                continue
            chunks.append(t)
            total += len(t)
            if total >= max_chars:
                break
        doc.close()
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", "\n\n".join(chunks)).strip()
        if text:
            return text[:max_chars], "pymupdf"
    except Exception:
        pass

    # 3. pypdf.
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf))
        chunks, total = [], 0
        for page in reader.pages:
            t = page.extract_text() or ""
            if not t:
                continue
            chunks.append(t)
            total += len(t)
            if total >= max_chars:
                break
        text = "\n\n".join(chunks).strip()
        if text:
            return text[:max_chars], "pypdf"
    except Exception:
        pass

    # 4. poppler's pdftotext CLI.
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf), "-"],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:max_chars], "pdftotext"
    except (OSError, subprocess.SubprocessError):
        pass

    return "", ""


def _pdf_metadata(pdf: Path) -> dict:
    """Embedded PDF metadata (title/author/keywords), best effort."""
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        doc = fitz.open(str(pdf))
        meta = dict(doc.metadata or {})
        meta["_pages"] = doc.page_count
        doc.close()
        return meta
    except Exception:
        return {}


# ── Metadata guessing ──────────────────────────────────────────────────


def _guess_doi(text: str) -> str:
    m = _DOI_RE.search(text)
    if not m:
        return ""
    return m.group(0).rstrip(_DOI_TRAILING)


def _guess_year(text: str) -> str:
    """Most recent plausible publication year mentioned in the header text."""
    years = [int(y) for y in _YEAR_RE.findall(text)]
    if not years:
        return ""
    this_year = datetime.now().year
    plausible = [y for y in years if y <= this_year]
    return str(max(plausible)) if plausible else ""


def _guess_title(text: str, pdf_meta: dict) -> str:
    """Prefer the embedded PDF title; else the first substantial header line."""
    title = (pdf_meta.get("title") or "").strip()
    # Reject the junk defaults publishers leave behind.
    if title and len(title) > 15 and not title.lower().endswith((".pdf", ".dvi", ".doc")):
        return re.sub(r"\s+", " ", title)
    for line in text.splitlines():
        line = line.strip()
        if 25 <= len(line) <= 300 and not _DOI_RE.search(line) and "@" not in line:
            return re.sub(r"\s+", " ", line)
    return ""


def _guess_abstract(text: str) -> str:
    """Text between an 'Abstract' heading and the next section heading."""
    head = _ABSTRACT_HEAD_RE.search(text[:20000])
    if not head:
        return ""
    body = text[head.end():head.end() + _ABSTRACT_MAX]
    end = _ABSTRACT_END_RE.search(body)
    if end:
        body = body[:end.start()]
    # Re-join words split across a line break by PDF hyphenation.
    body = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body if len(body) >= 80 else ""


def _guess_authors(pdf_meta: dict) -> str:
    """Embedded author string, normalised to the ';' separator the RDF export splits on."""
    raw = (pdf_meta.get("author") or "").strip()
    if not raw or len(raw) > 500:
        return ""
    if ";" in raw:
        parts = raw.split(";")
    elif " and " in raw:
        parts = raw.split(" and ")
    else:
        parts = [raw]
    return "; ".join(p.strip() for p in parts if p.strip())


# ── Manifest loading ───────────────────────────────────────────────────


def _load_manifest(path: str) -> dict[str, dict]:
    """Load a CSV/JSON manifest into ``{pdf basename: {field: value}}``."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    rows: list[dict]
    if p.suffix.lower() == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
    else:
        data = json.loads(text)
        if isinstance(data, dict):
            rows = [{"file": k, **(v or {})} for k, v in data.items()]
        else:
            rows = list(data)

    out: dict[str, dict] = {}
    for row in rows:
        fname = (row.get("file") or row.get("filename") or row.get("path") or "").strip()
        if not fname:
            continue
        fields = {
            k: v for k, v in row.items()
            if k not in ("file", "filename", "path") and v not in (None, "")
        }
        # Comma/semicolon-separated list fields.
        for list_field in ("keywords", "mesh_terms"):
            if isinstance(fields.get(list_field), str):
                fields[list_field] = [
                    s.strip() for s in re.split(r"[;,]", fields[list_field]) if s.strip()
                ]
        out[Path(fname).name] = fields
    return out


# ── Corpus building ────────────────────────────────────────────────────


def build_item(
    pdf: Path,
    *,
    max_chars: int,
    default_source: str,
    overrides: dict,
) -> dict:
    """Build one corpus entry (an ``Article``-shaped dict + ``_`` bookkeeping)."""
    raw = pdf.read_bytes()
    text, extractor = _extract_text(pdf, max_chars)
    pdf_meta = _pdf_metadata(pdf)
    head = text[:HEAD_CHARS]

    sha = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest() if text else ""
    file_sha = hashlib.sha256(raw).hexdigest()
    guesses: list[str] = []

    item: dict = {
        # Stable synthetic ID — keyed on file bytes so re-running is idempotent.
        "pmid": f"local_{file_sha[:8]}",
        "doi": "",
        "title": "",
        "authors": "",
        "journal": "",
        "year": "",
        "abstract": "",
        "keywords": [],
        "mesh_terms": [],
        "source": default_source,
        "full_text": text,
        "content_sha256": sha,
        "full_text_source": "user_supplied_pdf",
        "full_text_retrieved_at": datetime.now(timezone.utc).isoformat(),
    }

    for field, guess in (
        ("doi", _guess_doi(head)),
        ("title", _guess_title(head, pdf_meta)),
        ("year", _guess_year(head)),
        ("abstract", _guess_abstract(text)),
        ("authors", _guess_authors(pdf_meta)),
    ):
        if guess:
            item[field] = guess
            guesses.append(field)

    # Manifest values are authoritative — they came from the user.
    for k, v in (overrides or {}).items():
        if k in item or k in ("pmc_id", "hop_level", "parent_id"):
            item[k] = v
            if k in guesses:
                guesses.remove(k)

    item["_pdf_path"] = str(pdf)
    item["_pdf_sha256"] = file_sha
    item["_pdf_pages"] = pdf_meta.get("_pages", 0)
    item["_extractor"] = extractor
    item["_head_text"] = head
    item["_metadata_guesses"] = sorted(guesses)
    item["_needs_metadata"] = [f for f in REQUIRED_METADATA if not str(item.get(f) or "").strip()]
    if not text:
        item["_needs_metadata"].append("full_text")
    return item


def collect_pdfs(dirs: list[str], files: list[str]) -> list[Path]:
    found: list[Path] = []
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            raise SystemExit(f"not a directory: {d}")
        found.extend(sorted(p for p in base.rglob("*.pdf") if p.is_file()))
        found.extend(sorted(p for p in base.rglob("*.PDF") if p.is_file()))
    for f in files:
        p = Path(f)
        if not p.is_file():
            raise SystemExit(f"not a file: {f}")
        found.append(p)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for p in found:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def check(path: str) -> int:
    """Report entries with missing metadata. Exit 1 when any are incomplete."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items", data if isinstance(data, list) else [])
    incomplete = [it for it in items if it.get("_needs_metadata")]
    no_text = [it for it in items if not (it.get("full_text") or "").strip()]

    print(f"{len(items)} corpus entries")
    if no_text:
        print(f"\n  {len(no_text)} with NO extracted text (scanned/encrypted PDF?):")
        for it in no_text:
            print(f"    - {it.get('_pdf_path', it.get('pmid'))}")
    if incomplete:
        print(f"\n  {len(incomplete)} needing metadata:")
        for it in incomplete:
            missing = ", ".join(it["_needs_metadata"])
            print(f"    - {it.get('_pdf_path', it.get('pmid'))}: {missing}")
        print("\n✗ Complete the fields above (read each entry's `_head_text`) "
              "before running the review.")
        if any("full_text" in (it.get("_needs_metadata") or []) and it.get("doi")
               for it in items):
            print("  Entries with a DOI but no text: if the paper is paywalled, "
                  "retrieve it with `fetch_ezproxy.py --corpus <this file>` "
                  "(institutional access); if it is a scanned PDF, OCR it first.")
        return 1
    guessed = [it for it in items if it.get("_metadata_guesses")]
    if guessed:
        print(f"\n  {len(guessed)} entries carry auto-guessed fields — verify them:")
        for it in guessed[:20]:
            print(f"    - {it.get('pmid')}: {', '.join(it['_metadata_guesses'])}")
    print("\n✓ Every entry has title, authors, year, abstract and full text.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dir", action="append", default=[], metavar="DIR",
                    help="directory to scan recursively for PDFs (repeatable)")
    ap.add_argument("--pdf", action="append", default=[], metavar="FILE",
                    help="individual PDF to include (repeatable)")
    ap.add_argument("--manifest", default="",
                    help="CSV/JSON of known metadata, matched to PDFs by filename")
    ap.add_argument("--out", default="corpus.json", help="output corpus file")
    ap.add_argument("--max-chars", type=int, default=0,
                    help="per-article full-text cap; 0 = the whole document (default). "
                         "The review reads every chunk of the stored text, so "
                         "truncating here hides results sections from the analysis.")
    ap.add_argument("--source", default="user_supplied",
                    help="Article.source for PRISMA per-database identification counts; "
                         "override per file in the manifest (e.g. PubMed)")
    ap.add_argument("--check", metavar="CORPUS",
                    help="validate an existing corpus file instead of building one")
    args = ap.parse_args()

    if args.check:
        return check(args.check)

    if not args.dir and not args.pdf:
        ap.error("give --dir and/or --pdf (or --check CORPUS)")

    manifest = _load_manifest(args.manifest) if args.manifest else {}
    pdfs = collect_pdfs(args.dir, args.pdf)
    if not pdfs:
        raise SystemExit("no PDFs found")

    items: list[dict] = []
    for pdf in pdfs:
        item = build_item(
            pdf,
            max_chars=args.max_chars,
            default_source=args.source,
            overrides=manifest.get(pdf.name, {}),
        )
        items.append(item)
        status = "ok" if not item["_needs_metadata"] else "needs: " + ",".join(item["_needs_metadata"])
        print(f"  {pdf.name} → {item['pmid']} "
              f"[{len(item['full_text'])} chars, {item['_extractor'] or 'NO TEXT'}] {status}")

    unmatched = set(manifest) - {p.name for p in pdfs}
    for name in sorted(unmatched):
        print(f"  WARN  manifest row '{name}' matched no PDF", file=sys.stderr)

    corpus = {
        "corpus_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": "user_supplied_pdfs",
        "n_items": len(items),
        "items": items,
    }
    Path(args.out).write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    needs = sum(1 for it in items if it["_needs_metadata"])
    print(f"\nWrote {args.out} — {len(items)} entries, {needs} needing metadata.")
    if needs:
        print("Complete them (read each entry's `_head_text`), then re-check with "
              f"`--check {args.out}`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
