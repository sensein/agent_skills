"""BioPortal concept mapping client with rate limiting + LRU caching.

Maps free-text terms to ontology IRIs via the BioPortal REST API.

Usage:
    from bioportal_map import BioPortalMapper

    mapper = BioPortalMapper(api_key=os.environ["BIOPORTAL_API_KEY"])
    result = mapper.map_one("hippocampus", ontologies=["UBERON"])
    # -> {"ontology_id": "...", "ontology_label": "...", "ontology": "UBERON",
    #     "concept_mapping_provenance": "tool"}

    batch = mapper.map_batch(["hippocampus", "mouse"], ontologies=["UBERON","NCBITAXON"])

Get a free key at https://bioportal.bioontology.org/account.
Adapted from structsense conceptmappingtool.py.
"""
from __future__ import annotations

import os
import time
import threading
import logging
from functools import lru_cache
from typing import Iterable, Optional

import requests

logger = logging.getLogger("BioPortalMapper")

BIOPORTAL_SEARCH = "https://data.bioontology.org/search"
MAX_QUERY_LENGTH = 500


class BioPortalMapper:
    """Throttled BioPortal client.

    Constructor args:
        api_key: BioPortal API key (or set BIOPORTAL_API_KEY env var).
        request_interval: seconds between requests. Default 0.7 (env override:
            BIOPORTAL_REQUEST_INTERVAL).
        backoff_after_429: initial backoff seconds after a 429. Default 2.0.
        cache_size: LRU cache entries for repeat terms. Default 2000.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        request_interval: Optional[float] = None,
        backoff_after_429: Optional[float] = None,
        cache_size: int = 2000,
    ):
        self.api_key = api_key or os.environ.get("BIOPORTAL_API_KEY")
        if not self.api_key:
            raise ValueError("BIOPORTAL_API_KEY is required")
        self.request_interval = float(
            request_interval
            if request_interval is not None
            else os.environ.get("BIOPORTAL_REQUEST_INTERVAL", 0.7)
        )
        self.backoff = float(
            backoff_after_429
            if backoff_after_429 is not None
            else os.environ.get("BIOPORTAL_BACKOFF_AFTER_429", 2.0)
        )
        self._lock = threading.Lock()
        self._last_request_time = 0.0
        # Wrap _search_uncached in an LRU cache sized by user.
        self._search = lru_cache(maxsize=cache_size)(self._search_uncached)

    # ------------------------------------------------------------------
    # Throttling
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request_time
            wait = self.request_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_time = time.monotonic()

    # ------------------------------------------------------------------
    # Search (cached)
    # ------------------------------------------------------------------
    def _search_uncached(self, term: str, ontologies: tuple, max_results: int):
        params = {
            "q": term[:MAX_QUERY_LENGTH],
            "apikey": self.api_key,
            "display_context": "false",
            "include": "prefLabel,definition",
            "pagesize": max_results,
        }
        if ontologies:
            params["ontologies"] = ",".join(ontologies)
        for attempt in range(4):
            self._throttle()
            try:
                resp = requests.get(BIOPORTAL_SEARCH, params=params, timeout=15)
            except requests.RequestException as e:
                logger.warning("BioPortal request failed (%s); retrying", e)
                time.sleep(self.backoff * (2 ** attempt))
                continue
            if resp.status_code == 429:
                logger.warning("BioPortal 429; backing off")
                time.sleep(self.backoff * (2 ** attempt))
                continue
            if resp.status_code >= 500:
                logger.warning("BioPortal %s; retrying", resp.status_code)
                time.sleep(self.backoff * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.json().get("collection", []) or []
        return []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def map_one(self, term: str, ontologies: Optional[Iterable[str]] = None,
                max_results: int = 1) -> dict:
        """Map a single term. Returns the canonical mapping dict (with
        provenance) — `unmapped` when no results.
        """
        ont_tuple = tuple(ontologies or ())
        hits = self._search(term, ont_tuple, max_results)
        if not hits:
            return {
                "term": term,
                "ontology_id": None,
                "ontology_label": None,
                "ontology": None,
                "concept_mapping_provenance": "unmapped",
            }
        top = hits[0]
        return {
            "term": term,
            "ontology_id": top.get("@id"),
            "ontology_label": top.get("prefLabel"),
            "ontology": self._ontology_shortname(top),
            "concept_mapping_provenance": "tool",
        }

    def map_batch(self, terms: Iterable[str],
                  ontologies: Optional[Iterable[str]] = None,
                  max_results: int = 1) -> list[dict]:
        """Map a list of terms. Returns a parallel list of mapping dicts."""
        return [self.map_one(t, ontologies, max_results) for t in terms]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _ontology_shortname(hit: dict) -> Optional[str]:
        # BioPortal nests the source ontology under "links.ontology" as a URL.
        link = (hit.get("links") or {}).get("ontology")
        if isinstance(link, str):
            return link.rstrip("/").rsplit("/", 1)[-1]
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    m = BioPortalMapper()
    for r in m.map_batch(["hippocampus", "mouse", "BDNF"],
                         ontologies=["UBERON", "NCBITAXON", "HGNC"]):
        print(r)
