#!/usr/bin/env python3
"""Fetch paywalled full texts through the user's institutional EZproxy access.

Fills the gap in a bring-your-own-corpus review (SKILL.md → Mode 3): the user
has the DOIs, and their institution licenses the journals, but the PDFs aren't
on disk. This retrieves them with **their own institutional session** — the
equivalent of them opening each paper — extracts the text, and folds it into
``corpus.json`` with proper provenance (``full_text_source='ezproxy_pdf'``).

Authentication is done in a browser, not here: institutional login is SSO with
MFA. Sign in to your library's proxy, export the cookies, and point this at
them. Credentials are read from the environment only — never a flag — so they
can't be captured in a shell history or a provenance record:

    export EZPROXY_HOST=ezproxy.myuniversity.edu
    export EZPROXY_MODE=hostname-suffix        # or login-url
    export EZPROXY_COOKIE_FILE=~/ezproxy-cookies.txt
    export EZPROXY_DELAY=3                     # seconds between requests
    export EZPROXY_MAX_REQUESTS=100            # per-run ceiling

Usage:
    # is it usable?
    python fetch_ezproxy.py --status

    # fill in every corpus entry that has a DOI but no full text
    python fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs

    # fetch by DOI, then build a corpus from the downloaded PDFs
    python fetch_ezproxy.py --doi 10.1016/j.jad.2023.01.001 --doi-file dois.txt \
        --pdf-dir ./pdfs
    python build_corpus.py --dir ./pdfs --out corpus.json

    # see what would be fetched, without any requests
    python fetch_ezproxy.py --corpus corpus.json --dry-run

**Stay within your licence.** Requests go one at a time, ``EZPROXY_DELAY``
apart, capped at ``EZPROXY_MAX_REQUESTS`` per run. Keep the ceiling to the size
of the review you are actually conducting: systematic bulk downloading breaches
most publisher agreements and can cost your institution its access. You are
responsible for using this within your library's terms.

Requires the ``synthscholar`` package importable (for ``synthscholar.ezproxy``)
and ``pymupdf`` for text extraction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_corpus import build_item  # noqa: E402

# Stop after this many consecutive failures — usually an expired cookie or a
# blocked session, and hammering the gateway makes that worse.
MAX_CONSECUTIVE_FAILURES = 3


def _require() -> tuple:
    try:
        from synthscholar.ezproxy import EZProxyConfig, EZProxyFetcher
    except ImportError as e:
        print(f"synthscholar is not importable ({e}). "
              "`pip install 'synthscholar[fulltext]'`", file=sys.stderr)
        raise SystemExit(2)
    return EZProxyConfig, EZProxyFetcher


def _safe_name(doi: str) -> str:
    """Filesystem-safe filename stem for a DOI."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi.strip()) or hashlib.md5(
        doi.encode()).hexdigest()[:10]


def _load_dois(args: argparse.Namespace) -> list[str]:
    dois = [d.strip() for d in (args.doi or []) if d.strip()]
    if args.doi_file:
        for line in Path(args.doi_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                dois.append(line)
    seen: set[str] = set()
    return [d for d in dois if not (d in seen or seen.add(d))]


def _needs_fetch(item: dict) -> bool:
    return not (item.get("full_text") or "").strip() and bool(
        (item.get("doi") or "").strip())


def fetch_one(fetcher, doi: str, pdf_dir: Path) -> Path | None:
    """Fetch one DOI's PDF into *pdf_dir*; return the path, or None."""
    data = fetcher.fetch_pdf_bytes(doi=doi)
    if not data:
        return None
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path = pdf_dir / f"{_safe_name(doi)}.pdf"
    path.write_bytes(data)
    return path


def merge_into_entry(item: dict, pdf: Path, max_chars: int) -> dict:
    """Re-derive text/hashes from the fetched PDF, keeping known metadata.

    Metadata already in the corpus (from the user's manifest, or completed by
    the agent) always wins over anything guessed from the PDF — the fetch adds
    the body, it doesn't overwrite curated fields.
    """
    fresh = build_item(pdf, max_chars=max_chars,
                       default_source=item.get("source") or "user_supplied",
                       overrides={})
    for key in ("full_text", "content_sha256", "_pdf_path", "_pdf_sha256",
                "_pdf_pages", "_extractor", "_head_text"):
        item[key] = fresh[key]
    for key in ("title", "authors", "year", "journal", "abstract", "doi"):
        if not str(item.get(key) or "").strip() and fresh.get(key):
            item[key] = fresh[key]
    item["full_text_source"] = "ezproxy_pdf"
    item["full_text_retrieved_at"] = datetime.now(timezone.utc).isoformat()
    item["_metadata_guesses"] = sorted(
        set(item.get("_metadata_guesses") or []) | set(fresh.get("_metadata_guesses") or [])
    )
    item["_needs_metadata"] = [
        f for f in ("title", "authors", "year", "abstract")
        if not str(item.get(f) or "").strip()
    ]
    return item


def run_corpus(args: argparse.Namespace, fetcher, pdf_dir: Path) -> int:
    corpus_path = Path(args.corpus)
    raw = json.loads(corpus_path.read_text(encoding="utf-8"))
    # Accept both shapes build_corpus.py has written: {"items": [...]} and a
    # bare list. Normalise so the file we write back is always the dict form.
    corpus: dict = {"items": raw} if isinstance(raw, list) else raw
    items = corpus.get("items", [])

    todo = [it for it in items if _needs_fetch(it)]
    no_doi = [it for it in items
              if not (it.get("full_text") or "").strip() and not (it.get("doi") or "").strip()]
    print(f"{len(items)} corpus entries — {len(todo)} missing full text with a DOI")
    if no_doi:
        print(f"  {len(no_doi)} missing full text with NO DOI — cannot be fetched; "
              "add the DOI or supply the PDF manually")
    if args.limit and len(todo) > args.limit:
        print(f"  --limit {args.limit}: fetching the first {args.limit} of {len(todo)}")
        todo = todo[: args.limit]
    if not todo:
        print("Nothing to fetch.")
        return 0

    if args.dry_run:
        for it in todo:
            print(f"  would fetch {it['doi']}  ({it.get('title', '')[:55]})")
        print("\n--dry-run: no requests made.")
        return 0

    ok = failed = 0
    consecutive = 0
    for it in todo:
        doi = it["doi"].strip()
        pdf = fetch_one(fetcher, doi, pdf_dir)
        if pdf is None:
            failed += 1
            consecutive += 1
            print(f"  MISS  {doi}")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nStopping after {consecutive} consecutive failures — the "
                      "session cookie has probably expired, or these titles aren't "
                      "in your institution's licence. Re-export cookies and re-run; "
                      "already-fetched entries are skipped.")
                break
            continue
        consecutive = 0
        merge_into_entry(it, pdf, args.max_chars)
        ok += 1
        print(f"  OK    {doi} → {pdf.name} ({len(it['full_text'])} chars)")

    out = Path(args.out) if args.out else corpus_path
    corpus["items"] = items
    corpus["ezproxy_fetch"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "retrieved": ok,
        "failed": failed,
        "config": fetcher.config.redacted(),   # never the cookie itself
    }
    out.write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    print(f"\nRetrieved {ok}, failed {failed}. Updated {out}")
    still = sum(1 for it in items if not (it.get("full_text") or "").strip())
    if still:
        print(f"{still} entr{'y' if still == 1 else 'ies'} still without full text — "
              "they will screen on abstract only (that is reported honestly in the "
              "PRISMA flow as abstract-only inclusions).")
    print(f"Next: python build_corpus.py --check {out}")
    return 0


def run_dois(args: argparse.Namespace, fetcher, pdf_dir: Path, dois: list[str]) -> int:
    if args.limit:
        dois = dois[: args.limit]
    print(f"{len(dois)} DOI(s) to fetch into {pdf_dir}")
    if args.dry_run:
        for d in dois:
            print(f"  would fetch {d}")
        print("\n--dry-run: no requests made.")
        return 0

    ok = failed = 0
    consecutive = 0
    for doi in dois:
        pdf = fetch_one(fetcher, doi, pdf_dir)
        if pdf is None:
            failed += 1
            consecutive += 1
            print(f"  MISS  {doi}")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nStopping after {consecutive} consecutive failures — "
                      "re-export your session cookies and re-run.")
                break
            continue
        consecutive = 0
        ok += 1
        print(f"  OK    {doi} → {pdf.name}")
    print(f"\nRetrieved {ok}, failed {failed}.")
    if ok:
        print(f"Next: python build_corpus.py --dir {pdf_dir} --out corpus.json")
        print("      (then set each entry's full_text_source to 'ezproxy_pdf' if you "
              "want the provenance to record institutional access)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--corpus", default="",
                    help="corpus.json to fill in (entries with a DOI but no full text)")
    ap.add_argument("--out", default="",
                    help="write the updated corpus here instead of in place")
    ap.add_argument("--doi", action="append", default=[], metavar="DOI",
                    help="fetch this DOI (repeatable)")
    ap.add_argument("--doi-file", default="", metavar="PATH",
                    help="file of DOIs, one per line (# comments allowed)")
    ap.add_argument("--pdf-dir", default="./pdfs", help="where to save fetched PDFs")
    ap.add_argument("--max-chars", type=int, default=0,
                    help="per-article text cap; 0 = the whole document (default)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="fetch at most N articles this run")
    ap.add_argument("--status", action="store_true",
                    help="report whether institutional access is usable, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched; make no requests")
    args = ap.parse_args()

    EZProxyConfig, EZProxyFetcher = _require()
    fetcher = EZProxyFetcher(config=EZProxyConfig.from_env())

    if args.status:
        print(fetcher.status())
        return 0 if fetcher.available else 1

    dois = _load_dois(args)
    if not args.corpus and not dois:
        ap.error("give --corpus and/or --doi/--doi-file (or --status)")
    if not fetcher.available and not args.dry_run:
        print(f"ERROR: {fetcher.status()}", file=sys.stderr)
        print("See references/byo_corpus_review.md § institutional access for setup.",
              file=sys.stderr)
        return 2

    pdf_dir = Path(args.pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        print(fetcher.status())

    try:
        if args.corpus:
            rc = run_corpus(args, fetcher, pdf_dir)
            if dois:
                rc = run_dois(args, fetcher, pdf_dir, dois) or rc
            return rc
        return run_dois(args, fetcher, pdf_dir, dois)
    finally:
        fetcher.close()


if __name__ == "__main__":
    raise SystemExit(main())
