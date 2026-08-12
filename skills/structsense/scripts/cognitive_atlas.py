"""Cognitive Atlas concept/task mapping client — tool-only, cached.

The Cognitive Atlas (https://www.cognitiveatlas.org) publishes a vocabulary of
cognitive *concepts* (constructs like "working memory", "inhibitory control") and
*tasks* (the paradigms that measure them). For ABCD/HBCD synthesis this is the
join key: two papers that use different variables — `nihtbx_flanker_uncorrected`
and a Stroop score — are talking about the same construct, and only a shared
construct id lets you ask "where do these papers agree?".

API (no key required):
    GET /api/v-alpha/concept   -> [{id: trm_..., name, alias, definition_text}, ...]
    GET /api/v-alpha/task      -> [{id: tsk_..., name, alias, definition_text}, ...]

The whole vocabulary is ~1MB of JSON, so this fetches once and caches to disk;
lookups are then local and offline. Same discipline as the skill's other mappers
(`bioportal_map.py`, `ols_map.py`): a construct id is only ever attached when a
real lookup returned it. There is no LLM-knowledge path — an unmatched construct
stays unmatched, because a fabricated `trm_` id is worse than an honest gap
(SKILL.md hard rule 15).

    python -m scripts.cognitive_atlas refresh
    python -m scripts.cognitive_atlas map "working memory" "inhibitory control"
    python -m scripts.cognitive_atlas map --kind task "flanker task"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

BASE = "https://www.cognitiveatlas.org/api/v-alpha"
CACHE_DIR = Path(
    os.getenv("STRUCTSENSE_COGATLAS_DIR")
    or (Path.home() / ".cache" / "structsense" / "cognitive_atlas")
)
KINDS = ("concept", "task")
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s-]")
# Cognitive Atlas ids: trm_<hex> for concepts, tsk_<hex> for tasks.
ID_RE = re.compile(r"^(trm|tsk)_[0-9a-f]{8,}$")


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


# --------------------------------------------------------------------------- #
# fetch + cache
# --------------------------------------------------------------------------- #

def _cache_path(kind: str) -> Path:
    return CACHE_DIR / f"{kind}s.json"


def refresh(kind: str = "concept", *, timeout: int = 60) -> Path:
    """Download one vocabulary and cache it with provenance. Returns the path."""
    if kind not in KINDS:
        raise CognitiveAtlasError(f"kind must be one of {KINDS}, got {kind!r}")
    url = f"{BASE}/{kind}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise CognitiveAtlasError(
            f"could not reach {url} ({exc}). Construct mapping is tool-only, so "
            "constructs will be reported unmapped rather than guessed. Retry when "
            "the network is available, or pass --offline to proceed with gaps."
        ) from exc

    if not isinstance(payload, list) or not payload:
        raise CognitiveAtlasError(f"{url} returned no items")

    items = []
    for row in payload:
        ident = str(row.get("id") or "")
        name = str(row.get("name") or "").strip()
        if not ident or not name:
            continue
        items.append(
            {
                "id": ident,
                "name": name,
                "alias": str(row.get("alias") or "").strip(),
                "definition_text": str(row.get("definition_text") or "").strip()[:600],
            }
        )

    doc = {
        "kind": kind,
        "count": len(items),
        "provenance": {
            "source": url,
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "api": "cognitiveatlas v-alpha",
        },
        "items": items,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = _cache_path(kind)
    out.write_text(json.dumps(doc, indent=1))
    return out


def _load(kind: str, *, offline: bool) -> dict:
    path = _cache_path(kind)
    if path.is_file():
        return json.loads(path.read_text())
    if offline:
        raise CognitiveAtlasError(
            f"no cached {kind} vocabulary at {path} and --offline was set. "
            "Run: python -m scripts.cognitive_atlas refresh"
        )
    refresh(kind)
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

class CognitiveAtlas:
    """Local index over the cached vocabularies."""

    def __init__(self, kinds: Iterable[str] = KINDS, *, offline: bool = False):
        self.docs = {k: _load(k, offline=offline) for k in kinds}
        self._exact: Dict[str, List[dict]] = {}
        self._alias: Dict[str, List[dict]] = {}
        for kind, doc in self.docs.items():
            for it in doc["items"]:
                rec = {**it, "kind": kind}
                for key in {_norm(it["name"]), _singular(_norm(it["name"]))}:
                    if key:
                        self._exact.setdefault(key, []).append(rec)
                for al in re.split(r"[;,/|]", it.get("alias") or ""):
                    a = _norm(al)
                    if len(a) >= 3:
                        self._alias.setdefault(a, []).append(rec)

    @property
    def provenance(self) -> List[dict]:
        return [
            {"kind": k, "count": d["count"], **d["provenance"]}
            for k, d in self.docs.items()
        ]

    def map_term(self, term: str, *, kind: Optional[str] = None) -> Optional[dict]:
        """Map a construct phrase to one vocabulary entry, or None.

        Matching is exact on the normalised name, then on the singularised name,
        then on a declared alias. Deliberately NOT fuzzy: "memory" must not
        silently become "working memory", because the whole point of the construct
        id is that two papers agreeing on it really are talking about one thing.
        """
        n = _norm(term)
        if not n:
            return None
        for table, method in ((self._exact, "exact_name"), (self._alias, "alias")):
            for key in (n, _singular(n)):
                for rec in table.get(key, []):
                    if kind and rec["kind"] != kind:
                        continue
                    return {
                        "construct_id": rec["id"],
                        "construct_label": rec["name"],
                        "construct_kind": rec["kind"],
                        "match_method": method,
                        "definition": rec.get("definition_text") or None,
                        "mapping_provenance": "tool",
                        "mapping_source": BASE,
                    }
        return None

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
        """Substring search — for exploring the vocabulary, not for mapping."""
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
    """True if `ident` is shaped like a real Cognitive Atlas id.

    Used to demote fabricated ids the way `iri_validation.py` demotes bad IRIs:
    shape is necessary but NOT sufficient — an id is only trusted if it came back
    from `map_term()`.
    """
    return bool(ID_RE.match((ident or "").strip()))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="download + cache the vocabularies")
    r.add_argument("--kind", choices=[*KINDS, "all"], default="all")

    m = sub.add_parser("map", help="map construct phrases to ids")
    m.add_argument("terms", nargs="+")
    m.add_argument("--kind", choices=KINDS)
    m.add_argument("--offline", action="store_true")

    s = sub.add_parser("search", help="substring search (exploration only)")
    s.add_argument("needle")
    s.add_argument("--offline", action="store_true")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "refresh":
            for kind in (KINDS if a.kind == "all" else [a.kind]):
                p = refresh(kind)
                doc = json.loads(p.read_text())
                print(f"cached {doc['count']:>5} {kind}s -> {p}")
            return 0

        ca = CognitiveAtlas(offline=getattr(a, "offline", False))
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
    except CognitiveAtlasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
