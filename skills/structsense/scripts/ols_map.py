"""OLS (EBI Ontology Lookup Service) concept mapping client.

No API key required. Good free fallback / alternative to BioPortal.

Endpoint: https://www.ebi.ac.uk/ols4/api/search
Docs: https://www.ebi.ac.uk/ols4/help

Usage:
    from ols_map import OlsMapper
    mapper = OlsMapper()
    print(mapper.map_one("hippocampus", ontologies=["uberon"]))
    # -> {"ontology_id": "...", "ontology_label": "...", "ontology": "UBERON",
    #     "concept_mapping_provenance": "tool"}
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Iterable, Optional

import requests

logger = logging.getLogger("OlsMapper")

OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"


class OlsMapper:
    def __init__(self, cache_size: int = 2000, request_interval: float = 0.1,
                 timeout: float = 15.0):
        self.timeout = timeout
        self.request_interval = request_interval
        self._last_request = 0.0
        self._search = lru_cache(maxsize=cache_size)(self._search_uncached)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.request_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _search_uncached(self, term: str, ontologies: tuple, rows: int,
                         exact: bool, kind: str):
        params = {
            "q": term,
            "rows": rows,
            "exact": "true" if exact else "false",
            "type": kind,  # "class" | "individual" | "property"
        }
        if ontologies:
            params["ontology"] = ",".join(o.lower() for o in ontologies)
        for attempt in range(4):
            self._throttle()
            try:
                resp = requests.get(OLS_SEARCH, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                logger.warning("OLS request failed (%s); retrying", e)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return ((resp.json() or {}).get("response") or {}).get("docs", []) or []
        return []

    def map_one(self, term: str, ontologies: Optional[Iterable[str]] = None,
                max_results: int = 1, exact: bool = False,
                kind: str = "class") -> dict:
        hits = self._search(term, tuple(ontologies or ()), max_results, exact, kind)
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
            "ontology_id": top.get("iri"),
            "ontology_label": top.get("label"),
            "ontology": (top.get("ontology_name") or "").upper() or None,
            "concept_mapping_provenance": "tool",
        }

    def map_batch(self, terms: Iterable[str],
                  ontologies: Optional[Iterable[str]] = None,
                  max_results: int = 1, **kwargs) -> list[dict]:
        return [self.map_one(t, ontologies, max_results, **kwargs) for t in terms]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    m = OlsMapper()
    for r in m.map_batch(["hippocampus", "Mus musculus", "BDNF"],
                         ontologies=["uberon", "ncbitaxon", "hgnc"]):
        print(r)
