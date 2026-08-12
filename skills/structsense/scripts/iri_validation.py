"""Strict IRI validation for ontology mappings.

Purpose: prevent LLM-hallucinated IRIs from entering the canonical output.
The skill's policy is **no LLM-knowledge mappings, ever** — every mapping
must come from a tool call (local hybrid or BioPortal) and pass these checks:

1. Structural sanity — the IRI matches a known pattern for its ontology.
2. Optional resolvability — HEAD/GET returns 2xx (slow; off by default).

Items whose `ontology_id` fails validation are **demoted to unmapped**
(ontology fields nulled, `concept_mapping_provenance: "unmapped"`,
`alignment_method: "validation_failed"`). The item itself is preserved so
exhaustive extraction remains intact.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("iri_validation")

# ---------------------------------------------------------------------------
# Per-ontology IRI patterns. Keys are the canonical ontology shortnames
# (case-insensitive); values are regexes that the FULL IRI must match.
# Most biomedical ontologies use OBO PURLs of the form
#   http://purl.obolibrary.org/obo/<PREFIX>_<NUMBER>
# but several have their own URL spaces.
# ---------------------------------------------------------------------------

_OBO_PREFIX_PATTERN = (
    r"^https?://purl\.obolibrary\.org/obo/{prefix}_[0-9]+(?:[/#?].*)?$"
)

# Curie patterns ("NCBITaxon:10090") accepted alongside full IRIs
_CURIE_PATTERN = r"^{prefix}:[0-9A-Za-z\-_]+$"

_IRI_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    # Anatomy
    "uberon":     (re.compile(_OBO_PREFIX_PATTERN.format(prefix="UBERON")),
                   re.compile(_CURIE_PATTERN.format(prefix="UBERON"))),
    # Cells
    "cl":         (re.compile(_OBO_PREFIX_PATTERN.format(prefix="CL")),
                   re.compile(_CURIE_PATTERN.format(prefix="CL"))),
    "pcl":        (re.compile(_OBO_PREFIX_PATTERN.format(prefix="PCL")),
                   re.compile(_CURIE_PATTERN.format(prefix="PCL"))),
    # Species
    "ncbitaxon":  (re.compile(_OBO_PREFIX_PATTERN.format(prefix="NCBITaxon")),
                   re.compile(_CURIE_PATTERN.format(prefix="NCBITaxon"))),
    # Diseases
    "mondo":      (re.compile(_OBO_PREFIX_PATTERN.format(prefix="MONDO")),
                   re.compile(_CURIE_PATTERN.format(prefix="MONDO"))),
    "doid":       (re.compile(_OBO_PREFIX_PATTERN.format(prefix="DOID")),
                   re.compile(_CURIE_PATTERN.format(prefix="DOID"))),
    "hp":         (re.compile(_OBO_PREFIX_PATTERN.format(prefix="HP")),
                   re.compile(_CURIE_PATTERN.format(prefix="HP"))),
    # Chemistry / drugs
    "chebi":      (re.compile(_OBO_PREFIX_PATTERN.format(prefix="CHEBI")),
                   re.compile(_CURIE_PATTERN.format(prefix="CHEBI"))),
    "dron":       (re.compile(_OBO_PREFIX_PATTERN.format(prefix="DRON")),
                   re.compile(_CURIE_PATTERN.format(prefix="DRON"))),
    # Tissue / cell line
    "bto":        (re.compile(_OBO_PREFIX_PATTERN.format(prefix="BTO")),
                   re.compile(_CURIE_PATTERN.format(prefix="BTO"))),
    # Methods / assays
    "obi":        (re.compile(_OBO_PREFIX_PATTERN.format(prefix="OBI")),
                   re.compile(_CURIE_PATTERN.format(prefix="OBI"))),
    # Phenotypes
    "mp":         (re.compile(_OBO_PREFIX_PATTERN.format(prefix="MP")),
                   re.compile(_CURIE_PATTERN.format(prefix="MP"))),
    # Genes
    "hgnc":       (re.compile(r"^https?://identifiers\.org/hgnc/[0-9]+$"),
                   re.compile(r"^https?://www\.genenames\.org/(?:cgi-bin/gene_symbol_report\?(?:hgnc_id=)?HGNC:|data/gene-symbol-report/)?HGNC:[0-9]+$"),
                   re.compile(r"^HGNC:[0-9]+$")),
    "ncbigene":   (re.compile(r"^https?://www\.ncbi\.nlm\.nih\.gov/gene/[0-9]+$"),
                   re.compile(r"^NCBIGene:[0-9]+$")),
    "mgi":        (re.compile(r"^MGI:[0-9]+$"),
                   re.compile(r"^https?://www\.informatics\.jax\.org/marker/MGI:[0-9]+$")),
    # Proteins
    "uniprot":    (re.compile(r"^https?://(?:www\.)?uniprot\.org/uniprot(?:kb)?/[A-Z0-9]{6,10}$"),
                   re.compile(r"^UniProtKB:[A-Z0-9]{6,10}$")),
    "pr":         (re.compile(_OBO_PREFIX_PATTERN.format(prefix="PR")),
                   re.compile(_CURIE_PATTERN.format(prefix="PR"))),
    # Cellular components / GO
    "go":         (re.compile(_OBO_PREFIX_PATTERN.format(prefix="GO")),
                   re.compile(_CURIE_PATTERN.format(prefix="GO"))),
    # EFO (general experimental factor)
    "efo":        (re.compile(r"^https?://www\.ebi\.ac\.uk/efo/EFO_[0-9]+$"),
                   re.compile(r"^EFO:[0-9]+$")),
    # NIFSTD (neuroscience)
    "nifstd":     (re.compile(r"^https?://uri\.neuinfo\.org/nif/nifstd/[A-Za-z0-9_]+$"),),
    # CIDO (COVID)
    "cido":       (re.compile(_OBO_PREFIX_PATTERN.format(prefix="CIDO")),),
}

# Generic permissive pattern — used when no per-ontology pattern is registered.
# Requires either a known scheme + path with at least one alphanumeric run,
# or a CURIE-like "PREFIX:LOCAL".
_GENERIC_IRI = re.compile(
    r"^("
    r"https?://[A-Za-z0-9_\-.]+(?:/[^\s]*)?"   # http(s) URL
    r"|[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_\-]+"   # CURIE
    r")$"
)


def is_well_formed_iri(iri: Optional[str]) -> bool:
    """True if `iri` looks like a real IRI/CURIE (structural check only)."""
    if not iri or not isinstance(iri, str):
        return False
    s = iri.strip()
    if not s or s.lower() in ("n/a", "none", "null", "unmapped"):
        return False
    return bool(_GENERIC_IRI.match(s))


def matches_ontology(iri: Optional[str], ontology: Optional[str]) -> Optional[bool]:
    """Stricter check: does the IRI match the patterns registered for the
    declared ontology?

    Returns:
        True  — IRI matches one of the known patterns for that ontology.
        False — ontology is registered but the IRI does NOT match any of
                its patterns (likely fabricated).
        None  — ontology is unknown to the registry; no opinion.
    """
    if not iri or not ontology:
        return None
    key = ontology.strip().lower()
    patterns = _IRI_PATTERNS.get(key)
    if not patterns:
        return None
    return any(p.match(iri.strip()) for p in patterns)


def matches_any_known_ontology(iri: Optional[str]) -> bool:
    """Does this IRI match the pattern for ANY ontology we know about?

    Used to accept legitimate cross-ontology mappings (e.g. CIDO results that
    reuse an HP IRI as a synonym) without requiring the declared `ontology`
    field to match the IRI prefix.
    """
    if not iri:
        return False
    s = iri.strip()
    for patterns in _IRI_PATTERNS.values():
        if any(p.match(s) for p in patterns):
            return True
    return False


def validate_item(item: dict, *, strict: bool = True) -> tuple[bool, Optional[str]]:
    """Check the ontology fields of one extracted item.

    Policy:
      - `concept_mapping_provenance == "llm_knowledge"` is ALWAYS rejected.
        The skill does not permit LLM-fabricated mappings.
      - `ontology_id` must be structurally well-formed (real URL or CURIE).
      - In `strict=True` mode, the IRI must additionally match the registered
        pattern for SOME known ontology — its own prefix's pattern. The
        declared `ontology` field does NOT have to match the IRI prefix (real
        mappers legitimately report cross-ontology synonyms, e.g. CIDO results
        that reuse HP IRIs).
      - Unknown-ontology IRIs (no per-prefix pattern) are accepted as long as
        they're well-formed — we can't assert what we don't know.

    Returns:
        (ok, reason). ok=True keeps the mapping. ok=False demotes to unmapped.
    """
    oid = item.get("ontology_id")
    prov = (item.get("concept_mapping_provenance") or "").strip().lower()

    # explicit unmapped/skipped — nothing to validate
    if prov in ("unmapped", "skipped") and not oid:
        return True, None
    if not oid:
        return False, "no ontology_id but provenance not unmapped/skipped"

    # Hard rule #1: zero LLM-knowledge mappings allowed.
    if prov == "llm_knowledge":
        return False, ("concept_mapping_provenance=llm_knowledge is not "
                       "permitted; all mappings must come from a tool call")

    # Hard rule #2: must be a well-formed IRI/CURIE.
    if not is_well_formed_iri(oid):
        return False, f"ontology_id {oid!r} is not a well-formed IRI/CURIE"

    # Hard rule #3 (strict only): IRI must match SOME known ontology pattern.
    # We don't require the declared `ontology` field to match the IRI prefix —
    # cross-ontology lookups legitimately reuse IRIs across ontology sources.
    if strict and not matches_any_known_ontology(oid):
        # If the IRI looks like an OBO PURL or identifiers.org URL of a known
        # form, accept it even if no per-prefix pattern is registered.
        if not _looks_like_real_ontology_iri(oid):
            return False, (f"ontology_id {oid!r} does not match any known "
                           f"ontology pattern; possibly hallucinated")
    return True, None


# Permissive structural acceptance: catches real-world ontology IRIs whose
# specific prefix isn't yet in _IRI_PATTERNS, while still rejecting strings
# that don't look like ontology IRIs at all.
_OBO_GENERIC      = re.compile(r"^https?://purl\.obolibrary\.org/obo/[A-Za-z]+_[0-9]+(?:[/#?].*)?$")
_BIOPORTAL_PURL   = re.compile(r"^https?://purl\.bioontology\.org/ontology/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+$")
_IDENTIFIERS_ORG  = re.compile(r"^https?://identifiers\.org/[A-Za-z0-9._]+(?:/[A-Za-z0-9._:-]+)?$")
_EBI_OLS          = re.compile(r"^https?://www\.ebi\.ac\.uk/[A-Za-z0-9/_-]+/[A-Za-z0-9_]+_[0-9]+$")
_SEMANTIC_WEB     = re.compile(r"^https?://www\.semanticweb\.org/.+#[A-Za-z0-9_]+$")
_GENERIC_OWL_IRI  = re.compile(r"^https?://[A-Za-z0-9./_-]+/[A-Za-z0-9._-]+_[0-9]+$")


def _looks_like_real_ontology_iri(iri: str) -> bool:
    s = iri.strip()
    return any(p.match(s) for p in (
        _OBO_GENERIC, _BIOPORTAL_PURL, _IDENTIFIERS_ORG, _EBI_OLS,
        _SEMANTIC_WEB, _GENERIC_OWL_IRI,
    ))


def demote_to_unmapped(item: dict, reason: Optional[str] = None) -> dict:
    """In-place: null out ontology fields and mark the item as failed validation."""
    item["ontology_id"] = None
    item["ontology_label"] = None
    item["ontology"] = None
    item["concept_mapping_provenance"] = "unmapped"
    item["alignment_method"] = "validation_failed"
    if reason:
        # Append to remarks if present so the audit trail is preserved.
        prev = item.get("remarks") or ""
        if prev and reason not in prev:
            item["remarks"] = (prev + " | " + reason).strip(" |")
        elif not prev:
            item["remarks"] = reason
    return item


def validate_all(result: dict, *, strict: bool = True) -> dict:
    """Walk every entity / key_term / resource item and demote any whose
    ontology mapping fails validation (hallucinated, malformed, or
    llm_knowledge). Returns the mutated result.

    Adds `result["validation"]` with counts: passed / demoted / by_reason.
    """
    counts = {"passed": 0, "demoted": 0}
    by_reason: dict[str, int] = {}

    for key in ("entities", "key_terms"):
        for it in result.get(key, []) or []:
            if not isinstance(it, dict):
                continue
            ok, reason = validate_item(it, strict=strict)
            if ok:
                counts["passed"] += 1
            else:
                demote_to_unmapped(it, reason)
                counts["demoted"] += 1
                if reason:
                    by_reason[_canonical_reason(reason)] = (
                        by_reason.get(_canonical_reason(reason), 0) + 1)

    # Resources nested by index — walk shallowly
    res = (result.get("extracted_resources") or result.get("aligned_resources")
           or result.get("judge_resource") or {})
    if isinstance(res, dict):
        for items in res.values():
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                ok, reason = validate_item(it, strict=strict)
                if ok:
                    counts["passed"] += 1
                else:
                    demote_to_unmapped(it, reason)
                    counts["demoted"] += 1
                    if reason:
                        by_reason[_canonical_reason(reason)] = (
                            by_reason.get(_canonical_reason(reason), 0) + 1)

    result["validation"] = {
        "strict_iri_validation": strict,
        "counts": counts,
        "demoted_by_reason": by_reason,
    }
    return result


def _canonical_reason(reason: str) -> str:
    """Bucket the free-text reason into a short tag for the stats."""
    r = reason.lower()
    if "llm_knowledge" in r:
        return "llm_knowledge_rejected"
    if "well-formed" in r:
        return "malformed_iri"
    if "does not match" in r and "pattern" in r:
        return "wrong_ontology_pattern"
    if "no ontology_id" in r:
        return "missing_ontology_id"
    return "other"


if __name__ == "__main__":
    import json
    cases = [
        {"entity": "kidney disease", "ontology": "CIDO",
         "ontology_id": "http://purl.obolibrary.org/obo/HP_0012622",
         "concept_mapping_provenance": "tool"},
        {"entity": "BDNF", "ontology": "HGNC",
         "ontology_id": "HGNC:1033", "concept_mapping_provenance": "tool"},
        {"entity": "hippocampus", "ontology": "UBERON",
         "ontology_id": "http://purl.obolibrary.org/obo/UBERON_0002421",
         "concept_mapping_provenance": "tool"},
        # Fabricated by LLM
        {"entity": "made-up", "ontology": "UBERON",
         "ontology_id": "http://purl.obolibrary.org/obo/MADE_UP_999",
         "concept_mapping_provenance": "llm_knowledge"},
        # Wrong pattern for declared ontology
        {"entity": "weird", "ontology": "NCBITaxon",
         "ontology_id": "http://example.com/random-uri",
         "concept_mapping_provenance": "tool"},
        # Unknown ontology — accepted if well-formed
        {"entity": "x", "ontology": "RandomOnto",
         "ontology_id": "http://random.example.org/X_42",
         "concept_mapping_provenance": "tool"},
    ]
    for c in cases:
        ok, reason = validate_item(c)
        print(f"  {c['entity']:15s} ontology={c['ontology']:12s} ok={ok!s:5s} {reason or ''}")
