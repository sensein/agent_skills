"""Cognitive Atlas concept/task/disorder mapping client — API-backed, cached.

The Cognitive Atlas (https://www.cognitiveatlas.org, API documented at
https://www.cognitiveatlas.org/api) publishes a vocabulary of cognitive
*concepts* (constructs like "working memory", "inhibitory control"), *tasks*
(the paradigms that measure them) and *disorders* (ADHD, depression, conduct
disorder — what ABCD's CBCL scales are named after). For ABCD/HBCD synthesis
this is the join key: two papers that use different variables are talking about
the same construct only if they share a construct id.

Endpoints used (no key required):

    GET /api/v-alpha/concept            whole concept vocabulary (trm_...)
    GET /api/v-alpha/task               whole task vocabulary (tsk_...)
    GET /api/v-alpha/disorder           whole disorder vocabulary (dso_...)
    GET /api/v-alpha/search?q=<term>    live search across all kinds
    GET /api/v-alpha/<kind>?id=<id>     one entry by id

How a term is mapped, in order:

1. The vocabularies are downloaded from the API and cached on disk with the
   retrieval time. A cache older than ``STRUCTSENSE_COGATLAS_MAX_AGE_DAYS``
   (default 30) is re-downloaded; if the API is unreachable the stale cache is
   used and that is recorded in the provenance.
2. Exact match on the normalised name, its singular, or a declared alias.
3. If nothing matched locally, the live ``search`` endpoint is queried and a
   concept, task or disorder is accepted only if its name or an alias equals the
   term (after the same normalisation). Contrasts (cnt_) are not constructs and
   are never accepted. Search answers are cached per term.

An alias counts only on an entry that lists at most two: "internalizing" lists
"worry, anxiety, negativity, depression", which are related terms, not synonyms,
so "depression" does not become "internalizing".

Every hit records which API URL produced it. Matching is never fuzzy and there
is no LLM-knowledge path: a fabricated ``trm_`` id is worse than an honest gap
(SKILL.md hard rule 15). A model-supplied id is checked against the API
(``lookup_id``) and kept only when the API returns it for the same term.

    python -m scripts.cognitive_atlas refresh
    python -m scripts.cognitive_atlas map "working memory" "inhibitory control"
    python -m scripts.cognitive_atlas map --kind task "flanker task"
    python -m scripts.cognitive_atlas api-search "conduct disorder"
    python -m scripts.cognitive_atlas id trm_4a3fd79d096be
"""
from __future__ import annotations

# Run either way: `python -m scripts.<mod>` from the skill directory, or
# `python /abs/path/to/scripts/<mod>.py` from anywhere.
if __package__ in (None, ""):  # executed as a file, not as part of the package
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

API_ROOT = "https://www.cognitiveatlas.org/api"
BASE = f"{API_ROOT}/v-alpha"
CACHE_DIR = Path(
    os.getenv("STRUCTSENSE_COGATLAS_DIR")
    or (Path.home() / ".cache" / "structsense" / "cognitive_atlas")
)
MAX_AGE_DAYS = float(os.getenv("STRUCTSENSE_COGATLAS_MAX_AGE_DAYS", "30"))
KINDS = ("concept", "task", "disorder")
USER_AGENT = "structsense-skill (cognitive atlas client)"
_WS = re.compile(r"\s+")
# Hyphens fold to spaces too: "obsessive-compulsive disorder" is the API's name
# for what papers write as "obsessive compulsive disorder".
_PUNCT = re.compile(r"[^\w\s]")
# Cognitive Atlas ids: trm_ concepts, tsk_ tasks, dso_ disorders, cnt_ contrasts.
ID_RE = re.compile(r"^(trm|tsk|dso|cnt)_[0-9A-Za-z]{6,}$")
_PREFIX_KIND = {"trm": "concept", "tsk": "task", "dso": "disorder", "cnt": "contrast"}
# What a construct can map to. A contrast (cnt_) is one task condition minus
# another, not a construct, so search results of that kind are never accepted.
MAPPABLE_KINDS = ("concept", "task", "disorder")
# An entry whose alias field lists more than this many terms is listing related
# terms ("worry, anxiety, negativity, depression" on "internalizing"), not
# synonyms, so an alias match on it is not accepted.
MAX_SYNONYM_ALIASES = 2


def _aliases(alias_field: str) -> List[str]:
    return [a for a in (_norm(x) for x in re.split(r"[;,/|]", alias_field or "")) if a]


class CognitiveAtlasError(RuntimeError):
    """Raised when the vocabulary cannot be obtained — never guess an id instead."""


def _norm(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    return _WS.sub(" ", _PUNCT.sub(" ", (s or "").lower())).strip()


def _singular(s: str) -> str:
    """Crude de-pluralisation so 'working memories' matches 'working memory'."""
    if s.endswith("ies") and len(s) > 4:
        return s[:-3] + "y"
    if s.endswith("ses") or s.endswith("xes"):
        return s[:-2]
    if s.endswith("s") and not s.endswith("ss"):
        return s[:-1]
    return s


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get(url: str, *, timeout: int = 60, empty_on_404: bool = False) -> Any:
    """GET a Cognitive Atlas API URL and decode the JSON body. The search endpoint
    answers 404 when nothing matches; ``empty_on_404`` turns that into []."""
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if empty_on_404 and exc.code == 404:
            return []
        raise CognitiveAtlasError(f"could not read {url} ({exc}). Construct mapping is "
                                  "tool-only, so constructs will be reported unmapped "
                                  "rather than guessed.") from exc
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError) as exc:
        raise CognitiveAtlasError(
            f"could not read {url} ({exc}). Construct mapping is tool-only, so "
            "constructs will be reported unmapped rather than guessed."
        ) from exc


def _item(row: dict, kind: str) -> Optional[dict]:
    ident = str(row.get("id") or "")
    name = str(row.get("name") or "").strip()
    if not ident or not name:
        return None
    return {
        "id": ident,
        "name": name,
        "alias": str(row.get("alias") or "").strip(),
        "definition_text": str(row.get("definition_text") or row.get("definition")
                               or row.get("description") or "").strip()[:600],
        "kind": kind,
    }


# --------------------------------------------------------------------------- #
# vocabularies: fetch + cache
# --------------------------------------------------------------------------- #

def _cache_path(kind: str) -> Path:
    return CACHE_DIR / f"{kind}s.json"


def refresh(kind: str = "concept", *, timeout: int = 60) -> Path:
    """Download one vocabulary from the API and cache it with provenance."""
    if kind not in KINDS:
        raise CognitiveAtlasError(f"kind must be one of {KINDS}, got {kind!r}")
    url = f"{BASE}/{kind}"
    payload = _get(url, timeout=timeout)
    if not isinstance(payload, list) or not payload:
        raise CognitiveAtlasError(f"{url} returned no items")
    items = [it for it in (_item(r, kind) for r in payload if isinstance(r, dict)) if it]
    doc = {
        "kind": kind,
        "count": len(items),
        "provenance": {"source": url, "retrieved_at": _now(),
                       "retrieved_epoch": time.time(), "api": "cognitiveatlas v-alpha"},
        "items": items,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = _cache_path(kind)
    out.write_text(json.dumps(doc, indent=1))
    return out


def _age_days(doc: dict, path: Path) -> float:
    t = (doc.get("provenance") or {}).get("retrieved_epoch")
    if not isinstance(t, (int, float)):
        t = path.stat().st_mtime
    return (time.time() - t) / 86400.0


def _load(kind: str, *, offline: bool) -> dict:
    """The cached vocabulary, re-downloaded from the API when missing or stale."""
    path = _cache_path(kind)
    doc = json.loads(path.read_text()) if path.is_file() else None
    if doc is not None and (offline or _age_days(doc, path) <= MAX_AGE_DAYS):
        doc.setdefault("provenance", {})["cache_state"] = "cached"
        return doc
    if offline:
        raise CognitiveAtlasError(
            f"no cached {kind} vocabulary at {path} and offline mode was set. "
            "Run: python -m scripts.cognitive_atlas refresh"
        )
    try:
        refresh(kind)
    except CognitiveAtlasError as exc:
        if doc is None:
            raise
        # Stale beats nothing, but say so.
        print(f"warning: {exc}; using the cached {kind} vocabulary from "
              f"{doc.get('provenance', {}).get('retrieved_at')}", file=sys.stderr)
        doc.setdefault("provenance", {})["cache_state"] = "stale (API unreachable)"
        return doc
    doc = json.loads(path.read_text())
    doc["provenance"]["cache_state"] = "refreshed from API"
    return doc


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

class CognitiveAtlas:
    """Exact lookup over the API vocabularies, with live API search as fallback."""

    def __init__(self, kinds: Iterable[str] = KINDS, *, offline: bool = False,
                 live_search: bool = True, timeout: int = 30):
        self.offline = offline
        self.live_search = live_search and not offline
        self.timeout = timeout
        self.docs: Dict[str, dict] = {}
        for k in kinds:
            try:
                self.docs[k] = _load(k, offline=offline)
            except CognitiveAtlasError:
                # Concepts are required; tasks and disorders only widen coverage.
                if k == "concept":
                    raise
                print(f"warning: Cognitive Atlas {k} vocabulary unavailable; "
                      f"continuing without it", file=sys.stderr)
        self._exact: Dict[str, List[dict]] = {}
        self._alias: Dict[str, List[dict]] = {}
        self._by_id: Dict[str, dict] = {}
        for kind, doc in self.docs.items():
            for it in doc["items"]:
                rec = {**it, "kind": kind}
                self._by_id[rec["id"]] = rec
                for key in {_norm(it["name"]), _singular(_norm(it["name"]))}:
                    if key:
                        self._exact.setdefault(key, []).append(rec)
                als = _aliases(it.get("alias") or "")
                if len(als) <= MAX_SYNONYM_ALIASES:
                    for a in als:
                        if len(a) >= 3:
                            self._alias.setdefault(a, []).append(rec)
        self._search_cache_path = CACHE_DIR / "search_cache.json"
        try:
            self._search_cache: Dict[str, dict] = json.loads(
                self._search_cache_path.read_text())
        except Exception:
            self._search_cache = {}
        self.api_calls: List[dict] = []

    @property
    def provenance(self) -> List[dict]:
        out = [{"kind": k, "count": d["count"], **d["provenance"]}
               for k, d in self.docs.items()]
        if self.api_calls:
            out.append({"kind": "live_search", "api": "cognitiveatlas v-alpha",
                        "source": f"{BASE}/search", "calls": len(self.api_calls),
                        "hits": sum(1 for c in self.api_calls if c.get("hit"))})
        return out

    # -- local ------------------------------------------------------------- #

    def _hit(self, rec: dict, method: str, source: str) -> dict:
        return {
            "construct_id": rec["id"],
            "construct_label": rec["name"],
            "construct_kind": rec.get("kind") or _PREFIX_KIND.get(rec["id"][:3]),
            "match_method": method,
            "definition": rec.get("definition_text") or None,
            "mapping_provenance": "tool",
            "mapping_source": source,
        }

    def _local(self, n: str, kind: Optional[str]) -> Optional[dict]:
        for table, method in ((self._exact, "exact_name"), (self._alias, "alias")):
            for key in (n, _singular(n)):
                for rec in table.get(key, []):
                    if kind and rec["kind"] != kind:
                        continue
                    src = (self.docs.get(rec["kind"], {}).get("provenance", {})
                           .get("source") or f"{BASE}/{rec['kind']}")
                    return self._hit(rec, method, src)
        return None

    # -- live API ---------------------------------------------------------- #

    def api_search(self, term: str) -> List[dict]:
        """Raw results of GET /api/v-alpha/search?q=<term> (cached per term)."""
        key = _norm(term)
        if key in self._search_cache:
            return self._search_cache[key]["results"]
        if self.offline:
            return []
        url = f"{BASE}/search?{urllib.parse.urlencode({'q': term})}"
        rows = _get(url, timeout=self.timeout, empty_on_404=True)
        results = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            kind = _PREFIX_KIND.get(str(r.get("id") or "")[:3], "unknown")
            it = _item(r, kind)
            if it and not any(x["id"] == it["id"] for x in results):
                results.append(it)
        self._search_cache[key] = {"url": url, "retrieved_at": _now(), "results": results}
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._search_cache_path.write_text(json.dumps(self._search_cache, indent=1))
        except Exception:
            pass
        return results

    def _live(self, term: str, n: str, kind: Optional[str]) -> Optional[dict]:
        try:
            results = self.api_search(term)
        except CognitiveAtlasError as exc:
            self.api_calls.append({"term": term, "error": str(exc)})
            return None
        url = self._search_cache.get(n, {}).get("url") or f"{BASE}/search"
        hit = None
        for r in results:
            if r["kind"] not in MAPPABLE_KINDS or (kind and r["kind"] != kind):
                continue
            names = {_norm(r["name"]), _singular(_norm(r["name"]))}
            als = _aliases(r.get("alias") or "")
            aliases = set(als) if len(als) <= MAX_SYNONYM_ALIASES else set()
            if n in names or _singular(n) in names:
                hit = self._hit(r, "api_search_exact_name", url)
                break
            if n in aliases:
                hit = self._hit(r, "api_search_alias", url)
                break
        self.api_calls.append({"term": term, "hit": bool(hit)})
        return hit

    def lookup_id(self, ident: str) -> Optional[dict]:
        """GET /api/v-alpha/<kind>?id=<id>. None when the API does not know it."""
        ident = (ident or "").strip()
        if not valid_id(ident):
            return None
        if ident in self._by_id:
            return self._by_id[ident]
        kind = _PREFIX_KIND.get(ident[:3])
        if self.offline or kind not in KINDS:
            return None
        try:
            row = _get(f"{BASE}/{kind}?{urllib.parse.urlencode({'id': ident})}",
                       timeout=self.timeout)
        except CognitiveAtlasError:
            return None
        if isinstance(row, list):
            row = row[0] if row else None
        it = _item(row, kind) if isinstance(row, dict) else None
        return it if it and it["id"] == ident else None

    # -- public ------------------------------------------------------------ #

    def map_term(self, term: str, *, kind: Optional[str] = None) -> Optional[dict]:
        """Map a construct phrase to one Cognitive Atlas entry, or None.

        Exact on the normalised name, its singular, or an alias — first in the
        API vocabularies, then through the live search endpoint. Deliberately NOT
        fuzzy: "memory" must not silently become "working memory".
        """
        n = _norm(term)
        if not n:
            return None
        hit = self._local(n, kind)
        if hit is None and self.live_search:
            hit = self._live(term, n, kind)
        return hit

    def map_terms(self, terms: Iterable[str], *, kind: Optional[str] = None
                  ) -> Tuple[Dict[str, dict], List[str]]:
        """Map many terms. Returns (mapped, unmapped)."""
        mapped: Dict[str, dict] = {}
        unmapped: List[str] = []
        for t in terms:
            hit = self.map_term(t, kind=kind)
            if hit:
                mapped[t] = hit
            else:
                unmapped.append(t)
        return mapped, unmapped

    def search(self, needle: str, limit: int = 15) -> List[dict]:
        """Substring search over the cached vocabularies (exploration, not mapping)."""
        n = _norm(needle)
        out = []
        for kind, doc in self.docs.items():
            for it in doc["items"]:
                if n in _norm(it["name"]):
                    out.append({**it, "kind": kind})
                    if len(out) >= limit:
                        return out
        return out


def valid_id(ident: str) -> bool:
    """True if `ident` is shaped like a Cognitive Atlas id. Shape is necessary but
    NOT sufficient: an id is trusted only if a lookup returned it."""
    return bool(ID_RE.match((ident or "").strip()))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="download + cache the vocabularies from the API")
    r.add_argument("--kind", choices=[*KINDS, "all"], default="all")

    m = sub.add_parser("map", help="map construct phrases to ids")
    m.add_argument("terms", nargs="+")
    m.add_argument("--kind", choices=KINDS)
    m.add_argument("--offline", action="store_true")
    m.add_argument("--no-live-search", action="store_true",
                   help="only the cached vocabularies, no /search calls")

    s = sub.add_parser("search", help="substring search of the cache (exploration only)")
    s.add_argument("needle")
    s.add_argument("--offline", action="store_true")

    q = sub.add_parser("api-search", help="raw GET /api/v-alpha/search?q=...")
    q.add_argument("term")

    i = sub.add_parser("id", help="look an id up through the API")
    i.add_argument("ident")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "refresh":
            for kind in (KINDS if a.kind == "all" else [a.kind]):
                p = refresh(kind)
                doc = json.loads(p.read_text())
                print(f"cached {doc['count']:>5} {kind}s -> {p}")
            return 0

        ca = CognitiveAtlas(offline=getattr(a, "offline", False),
                            live_search=not getattr(a, "no_live_search", False))
        if a.cmd == "map":
            mapped, unmapped = ca.map_terms(a.terms, kind=a.kind)
            for term, hit in mapped.items():
                print(json.dumps({"term": term, **hit}))
            for term in unmapped:
                print(json.dumps({"term": term, "construct_id": None,
                                  "mapping_provenance": "unmapped"}))
            return 0 if not unmapped else 3
        if a.cmd == "search":
            for it in ca.search(a.needle):
                print(f"{it['id']:22} {it['kind']:8} {it['name']}")
            return 0
        if a.cmd == "api-search":
            for it in ca.api_search(a.term):
                print(f"{it['id']:22} {it['kind']:8} {it['name']}")
            return 0
        if a.cmd == "id":
            it = ca.lookup_id(a.ident)
            print(json.dumps(it) if it else "not found")
            return 0 if it else 3
    except CognitiveAtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
