"""
Domain-specific vocabulary layer.

MeSH is authoritative but lags current method/tool jargon (RNA-seq, connectome,
optogenetics, ...). These synonym clusters are injected into the search as
free-text [tiab] terms so the query stays exhaustive for fast-moving domains.

Backed by data/domain_terms.json; you can edit that file (or pass extra user
clusters at query time) without touching code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE  # data/ sits next to the modules in the skill layout
DEFAULT_PATH = os.path.join(ROOT, "data", "domain_terms.json")


@dataclass
class VocabCluster:
    concept: str
    synonyms: list[str]
    domain: str

    def to_dict(self) -> dict:
        return {"concept": self.concept, "synonyms": self.synonyms, "domain": self.domain}


class DomainVocab:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._clusters: list[VocabCluster] = []
        self.reload()

    def reload(self) -> None:
        self._clusters = []
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        for domain, clusters in data.items():
            if domain.startswith("_") or not isinstance(clusters, list):
                continue
            for c in clusters:
                syns = [s for s in c.get("synonyms", []) if s and s.strip()]
                if syns:
                    self._clusters.append(VocabCluster(c.get("concept", syns[0]), syns, domain))

    def domains(self) -> list[str]:
        return sorted({c.domain for c in self._clusters})

    def clusters(self, domains: list[str] | None = None) -> list[VocabCluster]:
        if not domains:
            return list(self._clusters)
        wanted = set(domains)
        return [c for c in self._clusters if c.domain in wanted]

    def find(self, text: str, domains: list[str] | None = None) -> list[VocabCluster]:
        """Clusters whose concept or any synonym contains `text` (case-insensitive)."""
        t = text.lower().strip()
        out = []
        for c in self.clusters(domains):
            hay = [c.concept.lower()] + [s.lower() for s in c.synonyms]
            if any(t in h or h in t for h in hay):
                out.append(c)
        return out


_singleton: DomainVocab | None = None


def get_vocab(path: str = DEFAULT_PATH) -> DomainVocab:
    global _singleton
    if _singleton is None:
        _singleton = DomainVocab(path)
    return _singleton
