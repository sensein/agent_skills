"""Shared RDF namespace constants and URI-minting helpers for the SLR Ontology."""

from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import quote

from rdflib import Namespace, URIRef, BNode
from rdflib.namespace import RDF, RDFS, OWL, XSD  # noqa: F401 — re-exported for callers

# A real PubMed ID is purely numeric. Synthetic IDs minted by the OA fetcher
# (e.g. ``doaj_<title slug>``, ``biorxiv_<doi tail>``, ``openalex_W123…``)
# fail this check and route through the DOI / hash branch below — they were
# never valid PMIDs and constructing ``https://pubmed.ncbi.nlm.nih.gov/<x>/``
# from them produces malformed URLs (spaces, slashes in the path, etc.).
_NUMERIC_PMID_RE = re.compile(r"^\d+$")

# C0-set + space + a few RFC 3986 sub-delims that rdflib/pyoxigraph reject in
# IRIs. Used as a final sanity gate before returning a constructed URI; if a
# URI we built somehow still contains one of these, fall back to a hash-based
# URN rather than emit broken Turtle that aborts the whole serialisation.
_INVALID_IRI_CHARS_RE = re.compile(r"[\x00-\x20\"<>\\^`{|}]")

# ── Ontology namespaces ────────────────────────────────────────────────────────
SLR    = Namespace("https://w3id.org/slr-ontology/")
PROV   = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
FABIO  = Namespace("http://purl.org/spar/fabio/")
BIBO   = Namespace("http://purl.org/ontology/bibo/")
OA     = Namespace("http://www.w3.org/ns/oa#")
SCHEMA = Namespace("http://schema.org/")
FOAF   = Namespace("http://xmlns.com/foaf/0.1/")
# NIDM (incf-nidash): the review's provenance is structured like NIDM-Experiment
# (Project / Session / Acquisition / AcquisitionObject; skos:closeMatch in the
# ontology) and the software agent carries NIDM-Results' "software Version".
NIDM   = Namespace("http://purl.org/nidash/nidm#")
NIDM_SOFTWARE_VERSION = NIDM.NIDM_0000122

# Convenience alias used widely in the schema
DCT = DCTERMS


def _hash_fallback_uri(article) -> URIRef:
    """Last-resort stable URN for an article when no usable real ID exists."""
    seed = (
        getattr(article, "pmid", None)
        or getattr(article, "doi", None)
        or getattr(article, "title", None)
        or uuid.uuid4().hex
    )
    slug = hashlib.md5(str(seed).encode("utf-8", errors="replace")).hexdigest()[:12]
    return URIRef(f"urn:slr:article:{slug}")


def article_uri(article) -> URIRef:
    """Mint a stable, RFC 3987-valid IRI for an article.

    Resolution priority:

    1. **Numeric PMID** → ``https://pubmed.ncbi.nlm.nih.gov/<pmid>/``.
       Synthetic IDs from non-PubMed sources (``doaj_<slug>``,
       ``biorxiv_<doi-tail>``, ``openalex_W…``) are NOT valid PMIDs and
       fall through to (2)/(3) — building a pubmed.ncbi URL from them
       produced malformed IRIs that crashed the whole RDF serialiser.
    2. **DOI** → ``https://doi.org/<doi>`` with the DOI percent-encoded so
       characters that are illegal in IRIs (spaces, ``<``, ``>``, etc.) are
       safely escaped.
    3. **Title hash fallback** → ``urn:slr:article:<md5>``.

    A final pass through ``_INVALID_IRI_CHARS_RE`` catches anything that
    still slipped through and downgrades to the URN fallback so a single
    dirty article can never abort the whole review serialisation.
    """
    pmid = (getattr(article, "pmid", None) or "").strip()
    if pmid and _NUMERIC_PMID_RE.match(pmid):
        candidate = URIRef(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        if not _INVALID_IRI_CHARS_RE.search(str(candidate)):
            return candidate

    doi = (getattr(article, "doi", None) or "").strip()
    if doi:
        # Strip any URL-form prefix the DOI may have arrived with, then
        # percent-encode the remainder. ``safe='/'`` keeps the legal slash
        # delimiters that DOIs use as the prefix/suffix separator.
        clean = doi
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
                break
        encoded = quote(clean, safe="/")
        candidate = URIRef(f"https://doi.org/{encoded}")
        if not _INVALID_IRI_CHARS_RE.search(str(candidate)):
            return candidate

    return _hash_fallback_uri(article)


def review_uri(protocol) -> URIRef:
    """Return an absolute IRI for the review.

    Resolution rules:

    - If ``protocol.review_id`` is already an absolute IRI (starts with a
      recognised scheme like ``http``/``https``/``urn``/``doi``/``info``),
      use it verbatim.
    - If it's a bare ID like ``review_0001_20260505234027``, wrap it in a
      ``urn:synthscholar:review:`` namespace so the resulting IRI is
      RFC 3987-compliant (rdflib accepts a bare string as a relative IRI,
      but strict RDF parsers — pyoxigraph, Oxigraph's Turtle parser —
      reject relative IRIs with "No scheme found in an absolute IRI").
    - If no ``review_id`` is set, mint a fresh ``urn:uuid:…``.
    """
    rid = (protocol.review_id or "").strip() if hasattr(protocol, "review_id") else ""
    if rid:
        if rid.startswith(("http://", "https://", "urn:", "doi:", "info:")):
            return URIRef(rid)
        return URIRef(f"urn:synthscholar:review:{rid}")
    return URIRef(f"urn:uuid:{uuid.uuid4()}")


def bind_namespaces(graph) -> None:
    """Bind all SLR ontology prefixes onto an rdflib Graph."""
    graph.bind("slr",     SLR)
    graph.bind("prov",    PROV)
    graph.bind("dcterms", DCTERMS)
    graph.bind("fabio",   FABIO)
    graph.bind("bibo",    BIBO)
    graph.bind("oa",      OA)
    graph.bind("schema",  SCHEMA)
    graph.bind("foaf",    FOAF)
    graph.bind("nidm",    NIDM)
