"""NDA data-element API — a live second opinion on what a variable is.

The bundled snapshots are the primary source and work offline. This module adds
the two things a snapshot cannot give you:

  * **confirmation of a name a paper actually printed**, straight from NDA,
    including the element's `dataStructures` and declared aliases —
    `GET /api/datadictionary/dataelement/<name>`
  * **full-text search over element descriptions** for wording that the offline
    matcher could not place — `POST /api/search/nda/dataelement/full`, body
    `text/plain`. Undocumented but public, no key, and the only free-text search
    NDA exposes (the `GET ?q=` form returns 405).

One hard rule: NDA is the whole archive, not ABCD. A search for "parent reported
financial adversity" returns CRISYS and Adolescent Stress Questionnaire elements
from unrelated collections, and attaching one of those to an ABCD paper would be
a fabricated mapping dressed up as an API result. So every hit is intersected with
the tables in the loaded ABCD/HBCD dictionary, and anything outside them is
dropped and counted. What survives is a real ABCD element or nothing.

Results are cached under ~/.cache/structsense/nda_api so a bulk run over hundreds
of papers hits the network once per distinct query, and a rerun is offline.

    python -m scripts.abcd_nda_api element nihtbx_flanker_uncorrected
    python -m scripts.abcd_nda_api search "family conflict subscale" --limit 5
"""
from __future__ import annotations

# Run either way: `python -m scripts.<mod>` from the skill directory, or
# `python /abs/path/to/scripts/<mod>.py` from anywhere. Without this, running the
# file directly fails with ModuleNotFoundError: scripts — which forces callers to
# cd into the skill first, for no reason.
if __package__ in (None, ""):  # executed as a file, not as part of the package
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Set

ELEMENT_URL = "https://nda.nih.gov/api/datadictionary/dataelement/{name}"
SEARCH_URL = "https://nda.nih.gov/api/search/nda/dataelement/full"
CACHE_DIR = Path(
    os.getenv("STRUCTSENSE_NDA_API_DIR")
    or (Path.home() / ".cache" / "structsense" / "nda_api")
)
DEFAULT_TIMEOUT = 20
# Cached responses older than this are refetched. The NDA dictionary changes on
# release boundaries, not daily.
CACHE_TTL_SEC = 30 * 24 * 3600


class NdaApiError(RuntimeError):
    """Network or protocol failure. Callers degrade to offline matching."""


def _cache_path(kind: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{kind}-{digest}.json"


def _cached(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return None
    fetched = doc.get("_fetched_at_epoch") or 0
    if time.time() - fetched > CACHE_TTL_SEC:
        return None
    return doc


def _store(path: Path, payload: dict, *, url: str, query: str) -> dict:
    doc = dict(payload)
    doc["_source"] = url
    doc["_query"] = query
    doc["_fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc["_fetched_at_epoch"] = int(time.time())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1))
    return doc


def _request(url: str, *, data: Optional[bytes] = None,
             content_type: Optional[str] = None,
             timeout: int = DEFAULT_TIMEOUT) -> Any:
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError) as exc:
        raise NdaApiError(f"{url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# element lookup
# --------------------------------------------------------------------------- #

def element(name: str, *, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """One element by exact name, or None if NDA does not have it."""
    name = (name or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.\-]{2,120}", name):
        return None
    path = _cache_path("element", name.lower())
    doc = _cached(path)
    if doc is None:
        try:
            payload = _request(ELEMENT_URL.format(name=urllib.parse.quote(name)),
                               timeout=timeout)
        except NdaApiError:
            raise
        if not isinstance(payload, dict) or payload.get("message"):
            payload = {"_absent": True}
        doc = _store(path, payload, url=ELEMENT_URL.format(name=name), query=name)
    if doc.get("_absent"):
        return None
    return {
        "name": doc.get("name") or name,
        "description": doc.get("description"),
        "type": doc.get("type"),
        "value_range": doc.get("valueRange"),
        "notes": doc.get("notes"),
        "data_structures": list(doc.get("dataStructures") or []),
        "aliases": list(doc.get("aliases") or []),
        "source": doc.get("_source"),
        "retrieved_at": doc.get("_fetched_at"),
    }


# --------------------------------------------------------------------------- #
# full-text search
# --------------------------------------------------------------------------- #

def search(query: str, *, limit: int = 10, timeout: int = DEFAULT_TIMEOUT
           ) -> List[dict]:
    """Elements whose name/description match `query`, best score first.

    Unfiltered — the whole NDA archive. Use `search_in_study()` unless you
    genuinely want that.
    """
    query = " ".join((query or "").split())
    if len(query) < 3:
        return []
    path = _cache_path("search", f"{query}|{limit}")
    doc = _cached(path)
    if doc is None:
        payload = _request(f"{SEARCH_URL}?size={max(limit, 10)}&from=0",
                           data=query.encode("utf-8"),
                           content_type="text/plain", timeout=timeout)
        if not isinstance(payload, dict):
            payload = {}
        doc = _store(path, payload, url=SEARCH_URL, query=query)
    results = ((doc.get("datadict") or {}).get("results")) or []
    out = []
    for row in results[:limit]:
        out.append({
            "name": row.get("name"),
            "description": row.get("description"),
            "notes": row.get("notes"),
            "score": row.get("_score"),
            "data_structures": [
                {"short_name": s.get("shortName"), "title": s.get("title"),
                 "category": s.get("category")}
                for s in (row.get("dataStructures") or [])
            ],
            "source": doc.get("_source"),
            "retrieved_at": doc.get("_fetched_at"),
        })
    return out


def known_tables(dictionary) -> Set[str]:
    """Every NDA structure short name the loaded dictionary knows about.

    Both eras contribute: NDA-era rows carry the structure as `table_nda`, and
    NBDC 6.x rows keep the old NDA name in the same column, which is what lets an
    API hit be checked against a 6.x snapshot at all.
    """
    from scripts.abcd_dictionary import canonical_field

    names: Set[str] = set()
    for snap in getattr(dictionary, "snapshots", []) or []:
        for row in snap.get("variables") or []:
            for value in (canonical_field(row, "nda_or_nbdc_table"),
                          row.get("table_nda"), row.get("table_name")):
                if value:
                    names.add(str(value).strip().lower())
    return names


def search_in_study(query: str, dictionary, *, limit: int = 10,
                    timeout: int = DEFAULT_TIMEOUT) -> dict:
    """`search()` restricted to structures present in the loaded dictionary.

    Returns {"hits": [...], "dropped": n, "checked_against": m}. `hits` are safe
    to attach to an ABCD/HBCD paper; a hit from some other NDA collection is not,
    however well it scored.
    """
    tables = known_tables(dictionary)
    raw = search(query, limit=max(limit * 4, 20), timeout=timeout)
    hits, dropped = [], 0
    for row in raw:
        inside = [s for s in row["data_structures"]
                  if (s.get("short_name") or "").lower() in tables]
        if not inside:
            dropped += 1
            continue
        hits.append({**row, "data_structures": inside,
                     "matched_tables": [s["short_name"] for s in inside]})
        if len(hits) >= limit:
            break
    return {"hits": hits, "dropped_outside_study": dropped,
            "checked_against_tables": len(tables), "query": query}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("element", help="look up one element by exact name")
    e.add_argument("name")

    s = sub.add_parser("search", help="full-text search over element descriptions")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--all-nda", action="store_true",
                   help="do NOT restrict hits to the loaded ABCD/HBCD tables")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "element":
            hit = element(a.name)
            print(json.dumps(hit or {"name": a.name, "found": False}, indent=1))
            return 0 if hit else 3
        if a.all_nda:
            print(json.dumps(search(a.query, limit=a.limit), indent=1))
            return 0
        from scripts.abcd_dictionary import Dictionary

        out = search_in_study(a.query, Dictionary.load(), limit=a.limit)
        print(json.dumps(out, indent=1))
        return 0 if out["hits"] else 3
    except NdaApiError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
