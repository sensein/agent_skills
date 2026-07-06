"""Client for a local hybrid (BM25 + dense) concept mapping service.

The pipeline pattern in this skill is designed around batched ontology lookups.
For thousands of terms, a single HTTP POST to a local service is orders of
magnitude faster than per-term API calls.

Reference service: https://github.com/sensein/search_hybrid
OpenAPI / interactive docs (default): http://localhost:8000/docs

Endpoint (default port 8000):
    POST /map/batch
    { "terms": ["hippocampus", "mouse", ...], "max_results": 1 }

Health check tried in order: GET /health, then GET /docs (every FastAPI-based
service exposes /docs by default, so this is a reliable "service is up" probe
even when /health hasn't been wired up).

If the default URL doesn't work, set LOCAL_CONCEPT_MAPPING_URL or pass
base_url=... to the constructor. **Ask the user** for their URL if neither
the default nor BioPortal fallback succeeds — the port and host can vary
based on how they deployed the service.

Usage:
    from local_hybrid_map import LocalHybridMapper
    mapper = LocalHybridMapper("http://localhost:8000")
    mappings = mapper.map_batch(["hippocampus", "mouse"], max_results=1)
"""
from __future__ import annotations

import os
import logging
from typing import Iterable, Optional

import requests

logger = logging.getLogger("LocalHybridMapper")

DEFAULT_URL = os.environ.get("LOCAL_CONCEPT_MAPPING_URL", "http://localhost:8000")
DEFAULT_TIMEOUT = float(os.environ.get("LOCAL_CONCEPT_MAPPING_TIMEOUT", 30))


class LocalHybridMapper:
    def __init__(self, base_url: str = DEFAULT_URL,
                 timeout: float = DEFAULT_TIMEOUT,
                 api_key: Optional[str] = None,
                 rerank_model: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("LOCAL_CONCEPT_MAPPING_API_KEY") \
            or os.environ.get("OPENROUTER_API_KEY")
        self.rerank_model = rerank_model or os.environ.get("LOCAL_CONCEPT_MAPPING_MODEL") \
            or os.environ.get("OPENROUTER_MODEL")

    def health(self) -> bool:
        """Return True if the service is reachable.

        Tries /health first; falls back to /docs (always present on
        FastAPI-based services like search_hybrid).
        """
        for path in ("/health", "/docs"):
            try:
                r = requests.get(f"{self.base_url}{path}",
                                 timeout=min(self.timeout, 5))
                if r.ok:
                    return True
            except requests.RequestException:
                continue
        return False

    def map_batch(self, terms, max_results: int = 5,
                  ontologies: Optional[Iterable[str]] = None,
                  contexts: Optional[Iterable[Optional[str]]] = None
                  ) -> list[dict]:
        """One POST to /map/batch.

        Accepts either:
          - a flat iterable of strings (e.g. ["kidney disease", "T2DM", ...]),
            plus an optional parallel `contexts` iterable, OR
          - an iterable of dicts already shaped as the API expects:
            [{"text": "kidney disease", "context": "progressive decline in GFR"},
             {"text": "T2DM", "context": "type 2 diabetes with insulin resistance"},
             {"text": "astrocyte"}]

        API contract (verified against the reference search_hybrid deployment):

            POST /map/batch
            {
              "max_results": 5,
              "text": [{"text": "...", "context": "..."}, ...]
            }

        Returns parallel list of canonical mapping dicts:
            {"term", "ontology_id", "ontology_label", "ontology",
             "score", "concept_mapping_provenance"}
        """
        # normalize input to the API's expected shape
        items: list[dict] = []
        plain_terms: list[str] = []
        terms = list(terms)
        if terms and isinstance(terms[0], dict):
            # already shaped — pass through, capture surface forms for the
            # output's "term" field.
            for it in terms:
                if not isinstance(it, dict) or not it.get("text"):
                    continue
                items.append({k: v for k, v in it.items()
                              if k in ("text", "context") and v is not None})
                plain_terms.append(it["text"])
        else:
            ctx_list = list(contexts) if contexts is not None else []
            for i, t in enumerate(terms):
                if not t:
                    continue
                entry = {"text": str(t)}
                if i < len(ctx_list) and ctx_list[i]:
                    entry["context"] = str(ctx_list[i])
                items.append(entry)
                plain_terms.append(str(t))

        if not items:
            return []

        body: dict = {
            "text": items,
            "max_results": max(1, min(20, int(max_results))),
        }
        if ontologies:
            body["ontologies"] = list(ontologies)
        if self.rerank_model:
            body["rerank_model"] = self.rerank_model
        if self.api_key:
            body["api_key"] = self.api_key  # used only for LLM re-ranking

        r = requests.post(f"{self.base_url}/map/batch", json=body, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json() or {}

        # Real server shape (verified against the user's deployment):
        #   {
        #     "query": "...",
        #     "type":  "batch",
        #     "results": {
        #         "<input text>": [
        #             {"rank": 1, "ontology_id": ..., "ontology_label": ...,
        #              "ontology": ..., "final_score": ..., "llm_score": ...,
        #              "late_interaction_score": ..., "original_score": ...},
        #             ...
        #         ],
        #         ...
        #     }
        #   }
        #
        # Also tolerated for forward/backward compatibility:
        #   results: list[{text, candidates: [...]}]
        #   results: list[{...top candidate flat...}]
        #   mappings: ...

        results = payload.get("results")
        if results is None:
            results = payload.get("mappings")

        out: list[dict] = []

        if isinstance(results, dict):
            # Dict keyed by the input surface form (the user-verified shape).
            for term in plain_terms:
                cands = results.get(term) or []
                if not cands:
                    out.append(_unmapped(term))
                    continue
                out.append(_to_canonical(term, cands[0]))
            return out

        # Fallback: list shape, parallel to inputs
        if isinstance(results, list):
            for term, row in zip(plain_terms, results):
                if not isinstance(row, dict):
                    out.append(_unmapped(term))
                    continue
                cands = (row.get("candidates") or row.get("matches")
                         or row.get("mappings") or [])
                if not cands:
                    if row.get("ontology_id") or row.get("id"):
                        out.append(_to_canonical(term, row))
                        continue
                    out.append(_unmapped(term))
                    continue
                out.append(_to_canonical(term, cands[0]))
            return out

        # Unknown shape — mark everything unmapped but don't crash.
        return [_unmapped(t) for t in plain_terms]

    def map_one(self, term, context: Optional[str] = None, **kwargs) -> dict:
        if isinstance(term, dict):
            return self.map_batch([term], **kwargs)[0]
        return self.map_batch([term],
                              contexts=[context] if context else None,
                              **kwargs)[0]


def _to_canonical(term: str, cand: dict) -> dict:
    """Map an API candidate dict to the skill's canonical mapping shape.

    Tolerant of common field-name variations across server versions:
    - ontology_id / id / iri
    - ontology_label / label / prefLabel
    - ontology / source / ontology_name
    - score / final_score (preferred when available)
    """
    score = cand.get("final_score")
    if score is None:
        score = cand.get("score")
    return {
        "term": term,
        "ontology_id": (cand.get("ontology_id") or cand.get("id")
                        or cand.get("iri")),
        "ontology_label": (cand.get("ontology_label") or cand.get("label")
                           or cand.get("prefLabel")),
        "ontology": (cand.get("ontology") or cand.get("source")
                     or cand.get("ontology_name")),
        "score": score,
        "concept_mapping_provenance": "tool",
    }


def _unmapped(term: str) -> dict:
    return {
        "term": term,
        "ontology_id": None,
        "ontology_label": None,
        "ontology": None,
        "concept_mapping_provenance": "unmapped",
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    m = LocalHybridMapper()
    if not m.health():
        print(f"Service not reachable at {m.base_url}")
        raise SystemExit(1)

    # Identical to the user-verified curl payload
    items = [
        {"text": "kidney disease", "context": "progressive decline in GFR"},
        {"text": "T2DM",           "context": "type 2 diabetes with insulin resistance"},
        {"text": "astrocyte"},
    ]
    out = m.map_batch(items, max_results=5)
    print(json.dumps(out, indent=2, default=str))
