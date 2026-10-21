#!/usr/bin/env python3
"""Fetch missing full texts — open access first, institutional access second.

Fills the gap in a bring-your-own-corpus review (SKILL.md → Mode 3): the user
has the DOIs but not the PDFs. This retrieves them, extracts the text, and
folds it into ``corpus.json`` with the retrieval route recorded per article.

Two stages, in the same order the application's ``FullTextResolver`` uses:

1. **Open access** — Unpaywall → OpenAlex → Semantic Scholar. Free, needs no
   institutional session, and doesn't spend the user's entitlement. Recorded as
   ``unpaywall_pdf`` / ``openalex_pdf`` / ``semanticscholar_pdf``.
2. **Institutional access** — only for what open access couldn't reach, through
   the user's **own EZproxy session** (the equivalent of them opening the paper
   in a browser). Recorded as ``ezproxy_pdf``.

Open access going first is what makes a mostly-OA corpus work with no library
setup at all, and keeps the proxy budget for the papers that actually need it.

Stage 2 signs in by itself, **at the start of the run**: the saved library
session is checked first, and if there is none or it has expired a browser
window opens at the user's library login (SSO, MFA — whatever their
institution uses); once they are through, the session is saved under
``~/.config/synthscholar/`` and reused until it expires. The proxy is worked
out from their email domain, or from ``--institution`` / ``--library-link``
(see ``ezproxy_login.py``). Nothing to configure. Power users can still set
``EZPROXY_HOST`` / ``EZPROXY_MODE`` / ``EZPROXY_COOKIE_FILE`` by hand, and
those win; ``EZPROXY_DELAY`` / ``EZPROXY_MAX_REQUESTS`` tune the pacing.

Usage:
    # what can this run reach?
    python fetch_ezproxy.py --status

    # sign in through the library now (otherwise it happens on first need)
    python fetch_ezproxy.py --login                     # proxy from your email
    python fetch_ezproxy.py --login --institution "University of Michigan"   # or umich.edu
    python fetch_ezproxy.py --logout

    # fill in every corpus entry that has a DOI but no full text
    python fetch_ezproxy.py --corpus corpus.json --pdf-dir ./pdfs

    # open access only — never touch the institutional proxy
    python fetch_ezproxy.py --corpus corpus.json --oa-only

    # fetch by DOI, then build a corpus from the downloaded PDFs
    python fetch_ezproxy.py --doi 10.1016/j.jad.2023.01.001 --doi-file dois.txt \
        --pdf-dir ./pdfs
    python build_corpus.py --dir ./pdfs --manifest ./pdfs/retrieval_manifest.json \
        --out corpus.json

    # see what would be fetched, without any requests
    python fetch_ezproxy.py --corpus corpus.json --dry-run

**Stay within your licence.** Proxied requests go one at a time,
``EZPROXY_DELAY`` apart, capped at ``EZPROXY_MAX_REQUESTS`` per run. Keep the
ceiling to the size of the review you are actually conducting: systematic bulk
downloading breaches most publisher agreements and can cost your institution
its access. You are responsible for using this within your library's terms.

Requires ``httpx``; ``pymupdf`` for text extraction. The EZproxy client comes
from ``synthscholar.ezproxy`` when importable, else the skill's vendored copy.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _vendor  # noqa: E402,F401  (the skill's own synthscholar copy, first)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_corpus import build_item  # noqa: E402
from oa_client import OAConfig, OAResolver  # noqa: E402
import ezproxy_login  # noqa: E402
from verify_citations import Registry  # noqa: E402  (preprint → published version)
from deposited_copies import DepositFinder, authors_string  # noqa: E402

# Stop after this many consecutive *proxied* failures — usually an expired
# cookie or a blocked session, and hammering the gateway makes that worse.
# Open-access misses don't count: a DOI with no free copy is the normal case,
# not a symptom, and a run of them says nothing about the next DOI.
MAX_CONSECUTIVE_FAILURES = 3


def _require() -> tuple:
    """Return ``(EZProxyConfig, EZProxyFetcher)``, from the app or the vendored copy.

    The app's ``synthscholar.ezproxy`` wins when importable, so a newer
    SynthScholar takes precedence (the skill's vendored copy has it); the skill
    also carries an equivalent standalone client
    and falls back to that, which keeps this script usable in any environment.
    """
    try:
        from synthscholar.ezproxy import EZProxyConfig, EZProxyFetcher
        return EZProxyConfig, EZProxyFetcher
    except ImportError:
        pass
    try:
        from ezproxy_client import EZProxyConfig, EZProxyFetcher
        return EZProxyConfig, EZProxyFetcher
    except ImportError as e:  # pragma: no cover — only if the skill is incomplete
        print(f"No EZproxy client available ({e}). Expected either an installed "
              "synthscholar with synthscholar/ezproxy.py, or this skill's "
              "scripts/ezproxy_client.py.", file=sys.stderr)
        raise SystemExit(2)


def proxy_config(EZProxyConfig):
    """``EZPROXY_*`` from the environment when set, else the saved browser login."""
    cfg = EZProxyConfig.from_env()
    if not cfg.enabled:
        saved = ezproxy_login.load_saved()
        if saved and (not cfg.host or cfg.host == saved["host"]):
            cfg.host, cfg.mode, cfg.cookie_file = saved["host"], saved["mode"], saved["cookie_file"]
    return cfg


class AutoLoginFetcher:
    """An ``EZProxyFetcher`` that signs the user in when access is first checked.

    ``main`` checks at the start of a run (``--lazy-login`` defers it to the first
    paper open access misses). The saved session is validated against the proxy;
    if there is none, or it has expired, a browser window opens for the user to
    sign in, once per run.
    """

    def __init__(self, EZProxyConfig, EZProxyFetcher, *, login_kwargs: dict,
                 allow_login: bool, max_chars: int = 0):
        self._Config, self._Fetcher = EZProxyConfig, EZProxyFetcher
        self._login_kwargs, self._allow = login_kwargs, allow_login
        self._max_chars = max_chars
        self._inner = self._make(proxy_config(EZProxyConfig))
        self._checked = False
        self.login_error: str = ""

    def _make(self, cfg):
        return self._Fetcher(config=cfg, max_chars=self._max_chars) if self._max_chars \
            else self._Fetcher(config=cfg)

    @property
    def available(self) -> bool:
        if not self._checked:
            self._checked = True
            self._ensure()
        return self._inner.available

    def _ensure(self) -> None:
        cfg = self._inner.config
        from_env = bool(os.environ.get("EZPROXY_COOKIE") or os.environ.get("EZPROXY_COOKIE_FILE"))
        if cfg.enabled and from_env:
            return  # the user configured it by hand: use exactly that
        if cfg.enabled and ezproxy_login.session_valid(cfg.host, cfg.cookie_file) is not False:
            return
        if not self._allow:
            return
        try:
            ezproxy_login.ensure_session(force=cfg.enabled, **self._login_kwargs)
        except Exception as e:  # a failed sign-in must not lose the rest of the run
            self.login_error = str(e)
            print(f"NOTE  institutional access unavailable: {e}", file=sys.stderr)
            return
        self._inner.close()
        self._inner = self._make(proxy_config(self._Config))

    @property
    def can_sign_in(self) -> bool:
        """Configured already, or able to open a browser to sign in."""
        import importlib.util
        return self._inner.config.enabled or (
            self._allow and importlib.util.find_spec("playwright") is not None)

    @property
    def ready(self) -> bool:
        """``available`` without the side effect: never checks or signs in."""
        return self._inner.available

    def status(self) -> str:
        if self._inner.config.enabled:
            return self._inner.status()
        return ("Institutional access: signs in through your library in a browser "
                "the first time a paywalled paper needs it")

    def __getattr__(self, name):
        return getattr(self._inner, name)


_ROUTE_LABELS = {
    "unpaywall_pdf": "Unpaywall (open access)",
    "openalex_pdf": "OpenAlex (open access)",
    "semanticscholar_pdf": "Semantic Scholar (open access)",
    "ezproxy_pdf": "institutional access",
    "zenodo_pdf": "Zenodo (deposited copy)",
    "arxiv_pdf": "arXiv (deposited copy)",
}


def _report_proxy_saving(by_route: dict[str, int], oa, fetcher) -> None:
    """Say how much institutional entitlement the OA stage preserved.

    Worth stating explicitly: the proxy budget is finite and shared with the
    rest of the institution, and a run that spent none of it on free papers is
    a materially different act from one that proxied everything.
    """
    if oa is None:
        return
    free = sum(n for route, n in by_route.items() if route != "ezproxy_pdf")
    if not free:
        return
    msg = (f"{free} of these were open access — retrieved without touching the "
           "institutional proxy")
    if fetcher is not None and fetcher.ready:
        left = max(fetcher.config.max_requests - fetcher.requests_made, 0)
        msg += f" ({left} of {fetcher.config.max_requests} proxy requests still unspent)"
    print(msg + ".")


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


def fetch_one(doi: str, pdf_dir: Path, oa=None, fetcher=None, versions=None,
              deposits=None, hint: dict | None = None) -> dict | None:
    """Fetch one paper's PDF into *pdf_dir*; None when every route missed.

    Reading order — **the peer-reviewed version first, then fallbacks**:

    1. the published version: open access, then institutional access;
    2. its registry-linked preprint, by the same two routes;
    3. a repository-deposited copy (Zenodo, arXiv), found by exact title and
       first author (``deposited_copies.py``).

    The paper is always *cited* by its published DOI (``cite_doi``); what was
    actually *read* is recorded separately (``read_doi`` / ``read_url``,
    ``version``), so a review read from a preprint or deposited copy says so.
    Within each version open access comes first, so a free copy never costs a
    proxied request.
    """
    plan = (versions.fetch_plan(doi) if versions is not None
            else {"cite": doi, "tries": [(doi, "as-given")], "title": "", "authors": []})

    def save(data: bytes, name: str) -> Path:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        path = pdf_dir / f"{_safe_name(name)}.pdf"
        path.write_bytes(data)
        return path

    for d, kind in plan["tries"]:
        data = source = None
        if oa is not None:
            hit = oa.fetch_pdf_bytes(d)
            if hit:
                data, source = hit
        if data is None and fetcher is not None and fetcher.available:
            data = fetcher.fetch_pdf_bytes(doi=d)
            source = "ezproxy_pdf"
        if data:
            return {"path": save(data, d), "source": source or "ezproxy_pdf", "cite_doi": plan["cite"],
                    "read_doi": d, "read_url": "", "version": kind}

    if deposits is not None:
        title = (hint or {}).get("title") or plan["title"]
        authors = (hint or {}).get("authors") or authors_string(plan["authors"])
        found = deposits.fetch(title, authors) if title and authors else None
        if found:
            data, source, url = found
            return {"path": save(data, plan["cite"]), "source": source, "cite_doi": plan["cite"],
                    "read_doi": "", "read_url": url, "version": "deposited-copy"}
    return None


def _record_version(entry: dict, requested: str, hit: dict, manifest: bool = False) -> dict:
    """Cite the published DOI; record what was read when it was something else.

    Corpus entries carry the extra facts as private ``_`` fields (kept out of
    the review model); the manifest carries them as plain keys, which
    ``build_corpus.py --manifest`` maps back to the same private fields.
    """
    p = "" if manifest else "_"
    entry["doi"] = hit["cite_doi"]
    if requested.lower() != hit["cite_doi"].lower():
        entry[f"{p}preprint_doi" if hit["version"] != "as-given" else f"{p}requested_doi"] = requested
    if hit["version"] in ("preprint", "deposited-copy") or manifest:
        entry[f"{p}read_version"] = hit["version"]
    if hit["read_doi"] and hit["read_doi"].lower() != hit["cite_doi"].lower():
        entry[f"{p}read_doi"] = hit["read_doi"]
    if hit["read_url"]:
        entry[f"{p}read_url"] = hit["read_url"]
    return entry


def _version_note(requested: str, hit: dict) -> str:
    kind, cite, read = hit["version"], hit["cite_doi"], hit["read_doi"]
    if kind == "deposited-copy":
        return f" (deposited copy, {hit['read_url']}; cited as {cite})"
    if kind == "preprint" and read.lower() != cite.lower():
        return f" (preprint {read} — the published version {cite} was not accessible; cited as {cite})"
    if kind == "preprint":
        return " (preprint — no peer-reviewed version found)"
    if cite.lower() != requested.lower():
        return f" (peer-reviewed version of preprint {requested})"
    return ""


def merge_into_entry(item: dict, pdf: Path, max_chars: int,
                     source: str = "ezproxy_pdf") -> dict:
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
    item["full_text_source"] = source
    item["full_text_retrieved_at"] = datetime.now(timezone.utc).isoformat()
    item["_metadata_guesses"] = sorted(
        set(item.get("_metadata_guesses") or []) | set(fresh.get("_metadata_guesses") or [])
    )
    item["_needs_metadata"] = [
        f for f in ("title", "authors", "year", "abstract")
        if not str(item.get(f) or "").strip()
    ]
    return item


def run_corpus(args: argparse.Namespace, fetcher, pdf_dir: Path, oa=None, versions=None,
               deposits=None) -> int:
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
    by_route: dict[str, int] = {}
    for it in todo:
        doi = it["doi"].strip()
        hit = fetch_one(doi, pdf_dir, oa=oa, fetcher=fetcher, versions=versions,
                        deposits=deposits, hint=it)
        if hit is None:
            failed += 1
            # Only a proxied miss is evidence of a broken session. When the
            # proxy wasn't consulted at all, a miss just means "no free copy".
            if fetcher is not None and fetcher.available:
                consecutive += 1
            print(f"  MISS  {doi}")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nStopping after {consecutive} consecutive institutional "
                      "failures — the session cookie has probably expired, or these "
                      "titles aren't in your institution's licence. Re-run with --login "
                      "to sign in again; already-fetched entries are skipped.")
                break
            continue
        pdf, source = hit["path"], hit["source"]
        consecutive = 0
        merge_into_entry(it, pdf, args.max_chars, source)
        _record_version(it, doi, hit)
        by_route[source] = by_route.get(source, 0) + 1
        ok += 1
        print(f"  OK    {hit['cite_doi']} → {pdf.name} ({len(it['full_text'])} chars) "
              f"[{_ROUTE_LABELS.get(source, source)}]{_version_note(doi, hit)}")

    out = Path(args.out) if args.out else corpus_path
    corpus["items"] = items
    corpus["full_text_fetch"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "retrieved": ok,
        "failed": failed,
        "by_route": by_route,
        "open_access": oa.config.redacted() if oa is not None else None,
        "ezproxy": fetcher.config.redacted() if fetcher is not None else None,
    }
    out.write_text(json.dumps(corpus, indent=2), encoding="utf-8")
    print(f"\nRetrieved {ok}, failed {failed}. Updated {out}")
    if by_route:
        for route, n in sorted(by_route.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>3}  {_ROUTE_LABELS.get(route, route)}")
    _report_proxy_saving(by_route, oa, fetcher)
    still = sum(1 for it in items if not (it.get("full_text") or "").strip())
    if still:
        print(f"{still} entr{'y' if still == 1 else 'ies'} still without full text — "
              "they will screen on abstract only (that is reported honestly in the "
              "PRISMA flow as abstract-only inclusions).")
    print(f"Next: python build_corpus.py --check {out}")
    return 0


def run_dois(args: argparse.Namespace, fetcher, pdf_dir: Path, dois: list[str],
             oa=None, versions=None, deposits=None) -> int:
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
    by_route: dict[str, int] = {}
    manifest: dict[str, dict] = {}
    for doi in dois:
        hit = fetch_one(doi, pdf_dir, oa=oa, fetcher=fetcher, versions=versions, deposits=deposits)
        if hit is None:
            failed += 1
            if fetcher is not None and fetcher.available:
                consecutive += 1
            print(f"  MISS  {doi}")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nStopping after {consecutive} consecutive institutional "
                      "failures — re-run with --login to sign in again.")
                break
            continue
        pdf, source = hit["path"], hit["source"]
        consecutive = 0
        ok += 1
        by_route[source] = by_route.get(source, 0) + 1
        # Keyed by filename: that is what build_corpus.py's --manifest matches on.
        manifest[pdf.name] = _record_version({"doi": doi, "full_text_source": source}, doi, hit,
                                             manifest=True)
        print(f"  OK    {hit['cite_doi']} → {pdf.name} [{_ROUTE_LABELS.get(source, source)}]"
              f"{_version_note(doi, hit)}")

    print(f"\nRetrieved {ok}, failed {failed}.")
    for route, n in sorted(by_route.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {_ROUTE_LABELS.get(route, route)}")
    _report_proxy_saving(by_route, oa, fetcher)
    if ok:
        # Written rather than described: the retrieval route is only known here,
        # and a corpus built without it would silently mislabel every one of
        # these as user_supplied_pdf.
        manifest_path = pdf_dir / "retrieval_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Wrote {manifest_path} (DOI + retrieval route per file)")
        print(f"Next: python build_corpus.py --dir {pdf_dir} "
              f"--manifest {manifest_path} --out corpus.json")
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
                    help="report which retrieval routes are usable, then exit")
    ap.add_argument("--oa-only", action="store_true",
                    help="open access only — never use institutional access")
    ap.add_argument("--no-oa", action="store_true",
                    help="skip the open-access chain (institutional access only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched; make no requests")
    ap.add_argument("--no-deposits", action="store_true",
                    help="don't fall back to repository-deposited copies (Zenodo, arXiv)")
    ap.add_argument("--as-given", action="store_true",
                    help="fetch each DOI exactly as given — don't swap a preprint for its "
                         "peer-reviewed version")
    sign = ap.add_argument_group("library sign-in (replaces the EZPROXY_* variables)")
    sign.add_argument("--login", action="store_true",
                      help="sign in through your library now, in a browser, and save the session")
    sign.add_argument("--logout", action="store_true", help="forget the saved library session")
    sign.add_argument("--institution", default="", metavar="NAME_OR_DOMAIN",
                      help="your university's full name or email domain, e.g. 'University of Michigan' "
                           "or umich.edu (default: from your email)")
    sign.add_argument("--library-link", default="", metavar="URL",
                      help="any link your library gives when you open an article off campus")
    sign.add_argument("--proxy-host", default="", metavar="HOST",
                      help="the EZproxy host itself, if you know it")
    sign.add_argument("--no-login", action="store_true",
                      help="never open a browser; skip paywalled papers instead")
    sign.add_argument("--lazy-login", action="store_true",
                      help="sign in only when the first paywalled paper needs it, not at the start")
    args = ap.parse_args()
    login_kwargs = {"proxy_host": args.proxy_host, "library_link": args.library_link,
                    "institution": args.institution}

    if args.logout:
        removed = ezproxy_login.logout()
        print("Removed: " + ", ".join(removed) if removed else "No saved library session.")
        return 0
    if args.login:
        try:
            st = ezproxy_login.ensure_session(force=True, **login_kwargs)
        except ezproxy_login.LoginError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return e.code
        ok = ezproxy_login.session_valid(st["host"], st["cookie_file"])
        print(f"Signed in via {st['host']} ({st['mode']}; proxy found from {st['host_source']}). "
              + ("Session verified." if ok else "Saved — couldn't verify it from here.")
              + f"\nSaved to {ezproxy_login.state_dir()} — later runs reuse it.")
        return 0

    if args.oa_only and args.no_oa:
        ap.error("--oa-only and --no-oa are mutually exclusive")

    EZProxyConfig, EZProxyFetcher = _require()
    fetcher = None if args.oa_only else AutoLoginFetcher(
        EZProxyConfig, EZProxyFetcher, login_kwargs=login_kwargs,
        allow_login=not (args.no_login or args.dry_run or args.status))
    oa = None if args.no_oa else OAResolver(config=OAConfig.from_env())
    # Peer-reviewed first: a preprint DOI is fetched as its published version
    # when the registry links one (Crossref is-preprint-of), the preprint after.
    mailto = os.environ.get("SYNTHSCHOLAR_EMAIL") or os.environ.get("NCBI_EMAIL") or ""
    versions = None if (args.as_given or args.dry_run or args.status) else Registry(mailto=mailto)
    # Last route, after published + preprint missed on both open access and the proxy.
    deposits = None if (args.no_deposits or args.dry_run or args.status) else DepositFinder(mailto=mailto)

    def _status_lines() -> str:
        parts = [oa.status() if oa is not None else "Open access: disabled (--no-oa)"]
        parts.append(fetcher.status() if fetcher is not None
                     else "Institutional access: disabled (--oa-only)")
        return "\n".join(parts)

    if args.status:
        print(_status_lines())
        # Usable if *either* route works — open access alone is a legitimate
        # configuration, and the common one for a first-time user.
        return 0 if (oa is not None or (fetcher is not None and fetcher.ready)) else 1

    dois = _load_dois(args)
    if not args.corpus and not dois:
        ap.error("give --corpus and/or --doi/--doi-file (or --status)")

    usable = (oa is not None) or (fetcher is not None and fetcher.can_sign_in)
    if not usable and not args.dry_run:
        print("ERROR: no retrieval route available.", file=sys.stderr)
        print(_status_lines(), file=sys.stderr)
        print("See references/byo_corpus_review.md § retrieving full texts for setup.",
              file=sys.stderr)
        return 2

    pdf_dir = Path(args.pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        if fetcher is not None and not args.no_login and not args.lazy_login:
            # Sign in before the run, not in the middle of it: a long fetch
            # should not stop at paper 40 waiting for someone to notice a
            # browser window. A still-valid saved session passes silently.
            print("Checking library access before starting…")
            fetcher.available
        print(_status_lines())
        if args.no_login and fetcher is not None and not fetcher._inner.config.enabled:
            print("NOTE  --no-login: paywalled papers will be missed")

    try:
        if args.corpus:
            rc = run_corpus(args, fetcher, pdf_dir, oa=oa, versions=versions, deposits=deposits)
            if dois:
                rc = run_dois(args, fetcher, pdf_dir, dois, oa=oa, versions=versions, deposits=deposits) or rc
            return rc
        return run_dois(args, fetcher, pdf_dir, dois, oa=oa, versions=versions, deposits=deposits)
    finally:
        if fetcher is not None:
            fetcher.close()
        if versions is not None:
            versions.close()
        if deposits is not None:
            deposits.close()
        if oa is not None:
            oa.close()


if __name__ == "__main__":
    raise SystemExit(main())
