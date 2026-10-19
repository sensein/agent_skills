"""Tests never reach the network: registry lookups for paper metadata (Crossref,
bioRxiv, doi.org) are stubbed. Activity checks are faked per test (fetch=...)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _offline_registries(monkeypatch):
    import doc_metadata
    for name in ("from_crossref", "from_biorxiv", "doi_resolves"):   # the real ones, for parser tests
        setattr(doc_metadata, f"_real_{name}", getattr(doc_metadata, f"_real_{name}", getattr(doc_metadata, name)))
    monkeypatch.setattr(doc_metadata, "from_crossref", lambda *a, **k: {})
    monkeypatch.setattr(doc_metadata, "from_biorxiv", lambda *a, **k: {})
    monkeypatch.setattr(doc_metadata, "doi_resolves", lambda *a, **k: False)
