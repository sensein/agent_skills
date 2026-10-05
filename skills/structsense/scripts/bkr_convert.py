#!/usr/bin/env python3
"""Convert BrainKB resource-extraction JSON into BKR Turtle.

Vendored from the BrainKB Resource Ontology bundle (bkr-0.5.6,
scripts/extraction_to_bkr.py). The ontology itself is bundled as
default_ontology/brainkb_resource_ontology.owl (the BKR standalone, every module
merged, no owl:imports) and is the default --vocab. structsense drives this
converter through scripts/resource_kg.py, which grounds and maps the records first;
call it directly only for an extraction that is already grounded.

    python -m scripts.bkr_convert extracted.json --out extracted.ttl

Changes from the bundle, all opt-in through the Converter constructor so the
command line behaves as before:
  * the vocabulary may be the OWL (RDF/XML) as well as Turtle;
  * resource_key: a callable giving a record's resource a key independent of the
    record, so one resource described by two papers is one node (records, scopes and
    assumptions stay per record);
  * documents: identifier -> IRI aliases, so a record's source work is the same node
    the NER graph of that paper uses (its ner:Publication).

The extracted_type -> OWL class mapping is read from the vocabulary at run time
(bkr:instantiatesClass), not hard-coded here: adding a type to the vocabulary is
enough to make the converter emit it.

Instance IRIs are UUIDs by default: a version-5 UUID derived from the ontology base
and a stable local key (record id + node path), so re-running the converter on the same
extraction yields the same IRIs, two records with the same resource name never collide,
and no human-readable string is baked into an identifier. The derivation key is kept on
each node as dcterms:identifier so a graph stays traceable, and --id-style slug restores
readable paths for debugging. Class and property IRIs are unaffected: those stay
human-readable, as in the Named Entity Ontology.

Policy enforced during conversion, mirroring bkr-shapes.ttl:
  * a mapping whose method is llm_judgment and whose status is accepted is
    demoted to proposed and its concept IRI is dropped. The extracted label
    survives; the unverifiable IRI does not.
  * a critical assumption with no stated consequence is reported on stderr.
  * every field listed in not_found_fields is emitted as bkr:notFoundField, so
    absence is explicit rather than inferred from a missing triple.
"""
import argparse, json, re, sys, uuid
from pathlib import Path
from rdflib import Graph, Namespace, URIRef, Literal, BNode, RDF, RDFS, XSD

BKR  = Namespace("https://brainkb.org/resource/")
NER  = Namespace("https://brainkb.org/ner/")
PROV = Namespace("http://www.w3.org/ns/prov#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DQV  = Namespace("http://www.w3.org/ns/dqv#")
DCT  = Namespace("http://purl.org/dc/terms/")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
ADMS = Namespace("http://www.w3.org/ns/adms#")
SCHEMA = Namespace("https://schema.org/")
SEPIO= Namespace("http://purl.obolibrary.org/obo/SEPIO_")
OBO  = Namespace("http://purl.obolibrary.org/obo/")
LS   = Namespace("https://brainkb.org/resource/lifesci/")
BRG  = Namespace("https://brainkb.org/resource/bridge/brainkb/")

SCOPE_CLASS = {"declared":BKR.DeclaredScope, "validated":BKR.ValidatedScope,
               "observed":BKR.ObservedScope, "out_of_scope":BKR.OutOfScope}
SCOPE_PROP  = {"declared":BKR.hasDeclaredScope, "validated":BKR.hasValidatedScope,
               "observed":BKR.hasObservedScope, "out_of_scope":BKR.hasExcludedScope}
DIMENSION = {  # extraction field -> scope property
 "species":LS.appliesToTaxon, "anatomical_structures":LS.appliesToAnatomicalStructure,
 "cell_types":LS.appliesToCellType, "developmental_stages":LS.appliesToDevelopmentalStage,
 "assays":LS.appliesToAssay, "modalities":BKR.appliesToModality,
 "conditions":LS.appliesToCondition, "tasks":BKR.appliesToTask,
 "variables":BKR.appliesToVariable, "topics":BKR.appliesToTopic}
ASSUMPTION_CLASS = {"statistical":BKR.StatisticalAssumption, "distributional":BKR.DistributionalAssumption,
 "independence":BKR.IndependenceAssumption, "biological":LS.BiologicalAssumption,
 "technical":BKR.TechnicalAssumption, "preprocessing":BKR.PreprocessingAssumption,
 "batch_effect":LS.BatchEffectAssumption, "data":BKR.DataAssumption,
 "reference":BKR.ReferenceAssumption, "scope":BKR.ScopeAssumption}
AGENT_CLASS = {"person":PROV.Person, "organization":PROV.Organization,
 "consortium":BKR.Consortium, "working_group":BKR.WorkingGroup, "research_group":BKR.ResearchGroup}
AGENT_PROP = {"owner":BKR.hasOwner, "maintainer":BKR.maintainer, "creator":DCT.creator,
 "contact":BKR.contactPoint, "publisher":BKR.publisher, "rights_holder":BKR.rightsHolder,
 "steward":BKR.stewardedBy, "host":BKR.hostedBy}

# External published classes are reused directly, but BKR no longer changes their
# global class hierarchy.  Catalogued instances therefore receive an explicit BKR
# profile type as well as the published type.
PROFILE_TYPE = {
    DCAT.Dataset: BKR.DigitalResource,
    URIRef("https://schema.org/SoftwareApplication"): BKR.SoftwareResource,
    OBO.IAO_0000310: BKR.DigitalResource,
}

def term(path, value):
    return URIRef(f"https://brainkb.org/resource/{path}/{value}")

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_VOCAB = SKILL_DIR / "default_ontology" / "brainkb_resource_ontology.owl"
DEFAULT_SCHEMA = SKILL_DIR / "schemas" / "bkr-resource-extraction.schema.json"


def load_type_map(vocab_path):
    g = Graph()
    vocab_path = str(vocab_path)
    g.parse(vocab_path, format="turtle" if vocab_path.endswith((".ttl", ".turtle")) else "xml")
    out = {}
    for s, o in g.subject_objects(BKR.instantiatesClass):
        out[str(s).rsplit("/", 1)[1]] = o
    if not out:
        sys.exit(f"no bkr:instantiatesClass statements found in {vocab_path}")
    return out


class Converter:
    def __init__(self, base, type_map, id_style="uuid", emit_skos=False,
                 resource_key=None, documents=None):
        self.base = Namespace(base)
        self.resource_key = resource_key      # rec -> key, or None: one node per record
        self.documents = dict(documents or {})  # work identifier -> existing IRI
        self.types = type_map
        self.id_style = id_style
        self.ns_uuid = uuid.uuid5(uuid.NAMESPACE_URL, base)
        self._keyed = set()
        self.g = Graph()
        for p, ns in [("bkr",BKR),("bkrls",LS),("bkrb",BRG),("ner",NER),("prov",PROV),
                      ("dcat",DCAT),("dqv",DQV),("dcterms",DCT),("skos",SKOS),("adms",ADMS),("schema",SCHEMA),("sepio",SEPIO),
                      ("obo",OBO),("ex",self.base)]:
            self.g.bind(p, ns)
        self.warnings = []
        self.record_sources = {}
        self.emit_skos = emit_skos
        self.skos_count = 0
        self._map_activities = {}

    def mapping_activity(self, method, record_id):
        key = (record_id, method)
        if key not in self._map_activities:
            act = self.iri(record_id, "mapping-activity", method)
            self.g.add((act, RDF.type, BKR.ConceptMappingActivity))
            self.g.add((act, NER.usedMappingMethod,
                        URIRef(f"https://brainkb.org/ner/mapping-method/{method}")))
            self._map_activities[key] = act
        return self._map_activities[key]

    def iri(self, *parts):
        key = "/".join(slug(p) for p in parts)
        if self.id_style == "slug":
            return self.base[key]
        node = self.base[str(uuid.uuid5(self.ns_uuid, key))]
        if node not in self._keyed:          # keep the derivation key, once
            self.g.add((node, DCT.identifier, Literal(key)))
            self._keyed.add(node)
        return node

    def lit(self, v, dt=None):
        return Literal(v, datatype=dt) if dt else Literal(v)

    def named_resource(self, value, kind="resource", cls=BKR.Resource):
        """Return a stable IRI for an extracted resource name/IRI and type it explicitly."""
        text = str(value)
        node = URIRef(text) if re.match(r"^https?://", text) else self.iri(kind, text)
        self.g.add((node, RDF.type, cls))
        self.g.add((node, RDF.type, BKR.Resource))
        self.g.add((node, BKR.resourceName, self.lit(text)))
        return node

    def source_document(self, value, default_doc=None):
        """Resolve a document reference to a canonical document, plus a locator part.

        Evidence in an extraction cites a place as well as a work: "10.1016/j.cell.2026.08.006#page=33",
        or just "page 33" when the record names its source separately. Minting one node per such string
        fragments the work into dozens of unrelated documents, so a graph can say where a statement came
        from but not which paper. This splits the reference:

          * the work gets ONE canonical node per identifier, carrying its DOI, title and citation;
          * a locator becomes its own node, dcterms:isPartOf and prov:specializationOf the work.

        A bare locator ("page 33", "Figure S11D") attaches to default_doc, the record's own source
        document, when one is known. Neither node is typed bkr:Resource: a cited work is not thereby a
        catalogued resource.
        """
        text = str(value).strip()
        if re.match(r"^https?://", text) and "#" not in text:
            node = URIRef(text)
            self.g.add((node, RDF.type, OBO.IAO_0000310))
            self.g.add((node, RDF.type, PROV.Entity))
            self.g.add((node, BKR.resourceName, self.lit(text)))
            return node
        work, locator = (text.split("#", 1) + [None])[:2] if "#" in text else (None, text)
        if work is None and re.search(r"10\.\d{4,}/", text):
            work, locator = text, None
        work_node = self.canonical_document(work) if work else default_doc
        if locator is None:
            return work_node
        key = f"{work or 'unknown-work'}#{locator}"
        node = self.iri("document-locator", key)
        self.g.add((node, RDF.type, OBO.IAO_0000310))
        self.g.add((node, RDF.type, PROV.Entity))
        self.g.add((node, BKR.resourceName, self.lit(locator)))
        if work_node is not None:
            self.g.add((node, DCT.isPartOf, work_node))
            self.g.add((node, PROV.specializationOf, work_node))
        return node

    def canonical_document(self, identifier):
        """One node per cited work, keyed on its identifier, created once and reused."""
        alias = self.documents.get(str(identifier).strip()) or self.documents.get(
            re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", str(identifier).strip(), flags=re.I).lower())
        node = URIRef(alias) if alias else self.iri("document", identifier)
        if (node, RDF.type, OBO.IAO_0000310) not in self.g:
            self.g.add((node, RDF.type, OBO.IAO_0000310))
            self.g.add((node, RDF.type, PROV.Entity))
            self.g.add((node, BKR.resourceName, self.lit(identifier)))
            if re.search(r"10\.\d{4,}/", identifier):
                self.g.add((node, RDF.type, SCHEMA.ScholarlyArticle))
                ident = self.iri("document-identifier", identifier)
                self.g.add((ident, RDF.type, ADMS.Identifier))
                self.g.add((ident, SKOS.notation, self.lit(identifier)))
                self.g.add((ident, BKR.identifierScheme, term("identifier-type", "DOI")))
                self.g.add((node, ADMS.identifier, ident))
        return node

    def describe_source_document(self, doc):
        """Attach a cited work's own metadata: title, identifier, citation, landing page."""
        ident = doc.get("identifier") or doc.get("url") or doc.get("title")
        if not ident:
            return None
        node = self.canonical_document(str(ident))
        if doc.get("title"):      self.g.add((node, DCT.title, self.lit(doc["title"])))
        if doc.get("url"):        self.g.add((node, BKR.accessURL, self.lit(doc["url"], XSD.anyURI)))
        if doc.get("version"):    self.g.add((node, DCT.bibliographicCitation, self.lit(doc["version"])))
        return node

    # ---------- concepts and mappings ------------------------------------
    # SKOS mapping properties are a shortcut, never the record of a mapping: the reified
    # ner:ConceptMappingDecision carries the method, status, confidence and provenance a
    # binary skos triple cannot hold. Only an 'exact' decision becomes skos:exactMatch, and
    # only accepted mappings are published at all.
    SKOS_PREDICATE = {"exact": SKOS.exactMatch, "close": SKOS.closeMatch,
                      "broader": SKOS.broadMatch, "narrower": SKOS.narrowMatch,
                      "related": SKOS.relatedMatch}

    def label_scheme(self):
        scheme = self.iri("label-scheme")
        if (scheme, RDF.type, SKOS.ConceptScheme) not in self.g:
            self.g.add((scheme, RDF.type, SKOS.ConceptScheme))
            self.g.add((scheme, RDFS.label, self.lit(
                "surface labels extracted from the source documents of this batch")))
        return scheme

    def add_skos_mapping(self, label, iri, m, dec):
        if not label:
            return
        pred = self.SKOS_PREDICATE.get(m.get("relation") or "", SKOS.closeMatch)
        local = self.iri("label-concept", label)
        if (local, RDF.type, SKOS.Concept) not in self.g:
            self.g.add((local, RDF.type, SKOS.Concept))
            self.g.add((local, SKOS.prefLabel, self.lit(label)))
            self.g.add((local, SKOS.inScheme, self.label_scheme()))
        self.g.add((local, pred, URIRef(iri)))
        self.g.add((local, BKR.hasConceptMapping, dec))
        self.skos_count += 1

    def add_concept(self, subject, prop, c, field, record_id, i):
        label = c.get("label")
        kept_iri = None
        for j, m in enumerate(c.get("mapped") or []):
            method, status = m.get("method"), m.get("status")
            iri, demoted = m.get("concept_iri"), False
            if method == "llm_judgment" and status == "accepted":
                status, iri, demoted = "proposed", None, True
                self.warnings.append(
                    f"{record_id}: demoted llm_judgment mapping for {field} '{label}' "
                    f"(accepted -> proposed, concept IRI dropped)")
            dec = self.iri(record_id, "mapping", field, i, j)
            self.g.add((dec, RDF.type, NER.ConceptMappingDecision))
            self.g.add((dec, RDFS.label, self.lit(f'"{label}" mapping')))
            self.g.add((dec, BKR.mappedField, self.lit(field)))
            self.g.add((dec, NER.mappingStatus, URIRef(f"https://brainkb.org/ner/mapping-status/{status}")))
            self.g.add((dec, NER.alignmentMethodRaw, self.lit(method)))
            self.g.add((dec, PROV.wasGeneratedBy, self.mapping_activity(method, record_id)))
            if m.get("relation"):
                self.g.add((dec, NER.mappingRelationType,
                            URIRef(f"https://brainkb.org/ner/mapping-relation/{m['relation']}")))
            if iri:
                self.g.add((dec, NER.conceptIRI, self.lit(iri, XSD.anyURI)))
                if status == "accepted":  # a proposed mapping is recorded, not asserted
                    kept_iri = kept_iri or iri
            if m.get("concept_id"):    self.g.add((dec, NER.conceptIdentifier, self.lit(m["concept_id"])))
            if m.get("concept_label"): self.g.add((dec, RDFS.comment, self.lit(m["concept_label"])))
            if m.get("ontology"):      self.g.add((dec, NER.ontologyAcronym, self.lit(m["ontology"])))
            if m.get("ontology_version"): self.g.add((dec, NER.ontologyVersionString, self.lit(m["ontology_version"])))
            if m.get("provenance_raw"):self.g.add((dec, NER.conceptMappingProvenanceRaw, self.lit(m["provenance_raw"])))
            if m.get("confidence") is not None:
                self.g.add((dec, NER.decisionConfidence, self.lit(m["confidence"], XSD.decimal)))
            if demoted:
                self.g.add((dec, RDFS.comment, self.lit(
                    "Demoted by the converter: a language-model judgement alone cannot publish an ontology IRI.")))
            self.g.add((subject, BKR.hasConceptMapping, dec))
            if self.emit_skos and iri and status == "accepted":
                self.add_skos_mapping(label, iri, m, dec)
        if kept_iri:
            self.g.add((subject, prop, URIRef(kept_iri)))
        elif label:
            # every concept hangs on a scope: an unmapped label is still what the scope
            # says (bkr:scopeStatement), or the scope would pin down nothing at all
            self.g.add((subject, BKR.scopeStatement, self.lit(f"unmapped {field}: {label}")))

    # ---------- scopes ---------------------------------------------------
    def add_scope(self, res, kind, sc, record_id, i):
        node = self.iri(record_id, "scope", kind, i)
        self.g.add((node, RDF.type, SCOPE_CLASS[kind]))
        self.g.add((res, SCOPE_PROP[kind], node))
        if sc.get("statement"):  self.g.add((node, BKR.scopeStatement, self.lit(sc["statement"], None)))
        if sc.get("target_label"): self.g.add((node, BKR.scopeTargetLabel, self.lit(sc["target_label"])))
        if sc.get("specific_target_label"): self.g.add((node, BKR.scopeSpecificTargetLabel, self.lit(sc["specific_target_label"])))
        lvl = sc.get("evidence_level") or ("extracted_from_text" if kind != "validated" else "benchmarked")
        self.g.add((node, BKR.scopeEvidenceLevel, term("evidence-level", lvl)))
        if sc.get("sample_size") is not None:
            self.g.add((node, BKR.scopeSampleSize, self.lit(sc["sample_size"], XSD.integer)))
        if sc.get("spatial_resolution"):
            self.g.add((node, BKR.scopeSpatialResolution, self.lit(sc["spatial_resolution"])))
        if sc.get("asserted_in"):
            self.g.add((node, BKR.scopeAssertedIn, self.source_document(sc["asserted_in"], self.default_document(record_id))))
        for field, prop in DIMENSION.items():
            for k, c in enumerate(sc.get(field) or []):
                self.add_concept(node, prop, c, field, record_id, f"{kind}{i}-{k}")
        mentions = [self.add_evidence(node, ev, record_id, f"scope-{kind}{i}-{k}")
                    for k, ev in enumerate(sc.get("evidence") or [])]
        # A bkr:ValidatedScope must cite the validation activity or its result, else it is only
        # a declared scope. Where the source reports evidence for a validated scope, reify that
        # as a bkr:Validation rather than leaving the scope unsupported or weakening the shape.
        if kind == "validated" and mentions:
            val = self.iri(record_id, "validation", kind, i)
            self.g.add((val, RDF.type, BKR.Validation))
            self.g.add((val, RDFS.label, self.lit(
                f"validation reported for scope: {(sc.get('statement') or '')[:120]}")))
            for mention in mentions:
                if mention is not None:
                    self.g.add((val, BKR.evidencedByMention, mention))
            self.g.add((node, BKR.scopeValidatedBy, val))
        return node

    # ---------- evidence -------------------------------------------------
    def default_document(self, record_id):
        srcs = self.record_sources.get(record_id) or []
        return srcs[0] if srcs else None

    def add_evidence(self, subject, ev, record_id, key):
        mention = self.iri(record_id, "mention", key)
        self.g.add((mention, RDF.type, NER.EntityMention))
        self.g.add((mention, NER.evidenceText, self.lit(ev["quote"])))
        if ev.get("start") is not None: self.g.add((mention, NER.mentionStartInSentence, self.lit(ev["start"], XSD.integer)))
        if ev.get("end") is not None:   self.g.add((mention, NER.mentionEndInSentence, self.lit(ev["end"], XSD.integer)))
        if ev.get("sentence"):
            sent = self.iri(record_id, "sentence", key)
            self.g.add((sent, RDF.type, NER.Sentence))
            self.g.add((sent, NER.sentenceText, self.lit(ev["sentence"])))
            if ev.get("sentence_index") is not None:
                self.g.add((sent, NER.sentenceIndex, self.lit(ev["sentence_index"], XSD.integer)))
            self.g.add((mention, NER.inSentence, sent))
        if ev.get("document"):
            doc = self.source_document(ev["document"], self.default_document(record_id))
            self.g.add((mention, PROV.hadPrimarySource, doc))
        if ev.get("location"):
            loc = self.iri(record_id, "mention-location", key)
            self.g.add((loc, RDF.type, PROV.Location))
            self.g.add((loc, RDFS.label, self.lit(ev["location"])))
            self.g.add((mention, PROV.atLocation, loc))
        self.g.add((subject, BKR.evidencedByMention, mention))
        return mention

    def add_assertion(self, res, field, quote, ev, record_id, key, extraction, agent):
        a = self.iri(record_id, "assertion", key)
        line = self.iri(record_id, "evidence-line", key)
        item = self.iri(record_id, "evidence-item", key)
        self.g.add((a, RDF.type, BKR.ResourceAssertion))
        self.g.add((a, BKR.assertionAbout, res))
        self.g.add((a, BKR.assertedProperty, self.lit(field)))
        self.g.add((a, BKR.assertionMethod, term("assertion-method", "automated_extraction")))
        self.g.add((a, BKR.hasEvidenceLine, line))
        self.g.add((line, RDF.type, BRG.EvidenceLine))
        self.g.add((line, BRG.hasEvidenceItem, item))
        self.g.add((item, RDF.type, BRG.EvidenceItem))
        mention = self.add_evidence(a, {**ev, "quote": quote}, record_id, key)
        self.g.add((item, BRG.evidenceItemMention, mention))
        if extraction: self.g.add((a, PROV.wasGeneratedBy, extraction))
        if agent:      self.g.add((a, PROV.wasAttributedTo, agent))
        return a

    # ---------- record ---------------------------------------------------
    def assert_source(self, node, source, rid, agent_fallback):
        if source:
            doc = self.source_document(source, self.default_document(rid))
            self.g.add((node, BKR.assumptionStatedIn, doc))
        elif agent_fallback is not None:
            self.g.add((node, BKR.assumptionAssertedBy, agent_fallback))

    def convert(self, rec):
        rid = rec["record_id"]
        etype = rec["extracted_type"]
        cls = self.types.get(etype)
        if cls is None:
            sys.exit(f"{rid}: extracted_type '{etype}' is not in the vocabulary")
        res = self.iri("resource", self.resource_key(rec)) if self.resource_key else self.iri(rid)
        g = self.g
        # Register the record's cited works FIRST: evidence quotes carry bare locators
        # ("page 23", "Limitations of the study") that must attach to the work they came from,
        # and evidence is emitted before the provenance block below.
        for _i, _doc in enumerate((rec.get("provenance") or {}).get("source_documents") or []):
            _n = self.describe_source_document(_doc) or self.canonical_document(f"{rid}-src{_i}")
            if _n not in self.record_sources.setdefault(rid, []):
                self.record_sources[rid].append(_n)
        _ext = (rec.get("provenance") or {}).get("extraction") or {}
        extracting_agent = self.iri("agent", _ext["model_name"]) if _ext.get("model_name") else (
            self.iri("agent", _ext["pipeline"]) if _ext.get("pipeline") else None)
        g.add((res, RDF.type, cls))
        g.add((res, RDF.type, BKR.Resource))
        if cls in PROFILE_TYPE:
            g.add((res, RDF.type, PROFILE_TYPE[cls]))
        g.add((res, BKR.extractedType, term("extracted-type", etype)))
        g.add((res, DCT.title, self.lit(rec["name"])))
        g.add((res, BKR.resourceName, self.lit(rec["name"])))  # legacy compatibility
        if rec.get("description"):
            desc = self.iri(rid, "description")
            g.add((desc, RDF.type, BKR.Description))
            g.add((desc, BKR.descriptionText, self.lit(rec["description"])))
            g.add((res, DCT.description, self.lit(rec["description"])))
            g.add((desc, BKR.descriptionType, term("description-type", "Abstract")))
            g.add((res, BKR.hasDescription, desc))
        if rec.get("url"):     g.add((res, BKR.accessURL, self.lit(rec["url"], XSD.anyURI)))
        if rec.get("license") and re.match(r"^https?://\S+$", rec["license"]):
            g.add((res, DCT.license, URIRef(rec["license"])))
            g.add((res, BKR.license, URIRef(rec["license"])))  # legacy compatibility
        elif rec.get("license"):  # "CC BY 4.0" as written: a statement, not an IRI to invent
            g.add((res, BKR.rightsStatement, self.lit(rec["license"])))
        for i, ident in enumerate(rec.get("stable_identifiers") or []):
            node = self.iri(rid, "id", i)
            g.add((node, RDF.type, ADMS.Identifier))
            g.add((node, SKOS.notation, self.lit(ident["value"])))
            g.add((res, ADMS.identifier, node))
            if ident.get("scheme"):
                g.add((node, BKR.identifierScheme, term("identifier-type", ident["scheme"])))
            g.add((res, BKR.primaryIdentifier if i == 0 else BKR.alternateIdentifier, node))
            if ident.get("resolves_through"):
                arc = self.iri("archive", ident["resolves_through"])
                g.add((arc, RDF.type, BKR.Archive))
                g.add((arc, BKR.resourceName, self.lit(ident["resolves_through"])))
                g.add((res, BKR.depositedIn, arc))
        # topics / tasks / modalities / species given outside an explicit scope
        loose = {"topics":BKR.appliesToTopic, "tasks":BKR.appliesToTask,
                 "modalities":BKR.appliesToModality, "species":LS.appliesToTaxon}
        if any(rec.get(f) for f in loose):
            sc = self.iri(rid, "scope", "declared-root")
            g.add((sc, RDF.type, BKR.DeclaredScope))
            g.add((sc, BKR.scopeEvidenceLevel, term("evidence-level", "extracted_from_text")))
            g.add((res, BKR.hasDeclaredScope, sc))
            for f, prop in loose.items():
                for i, c in enumerate(rec.get(f) or []):
                    self.add_concept(sc, prop, c, f, rid, i)
        validated_scopes = []
        for kind in SCOPE_CLASS:
            for i, sc in enumerate((rec.get("applicability") or {}).get(kind) or []):
                node = self.add_scope(res, kind, sc, rid, i)
                if kind == "validated":
                    validated_scopes.append((node, sc))
        for i, r in enumerate(rec.get("requirements") or []):
            n = self.iri(rid, "requirement", i)
            g.add((n, RDF.type, BKR.Requirement))
            g.add((n, BKR.requirementStatement, self.lit(r["statement"])))
            if r.get("type"): g.add((n, BKR.requirementType, term("requirement-type", r["type"])))
            if r.get("minimum_version"): g.add((n, BKR.minimumVersion, self.lit(r["minimum_version"])))
            if r.get("required_resource"):
                g.add((n, BKR.requiredResource, self.named_resource(r["required_resource"], "required-resource")))
            g.add((res, BKR.hasRequirement, n))
        assumption_nodes = {}
        for i, a in enumerate(rec.get("assumptions") or []):
            n = self.iri(rid, "assumption", i)
            g.add((n, RDF.type, ASSUMPTION_CLASS.get(a.get("kind"), BKR.Assumption)))
            g.add((n, BKR.assumptionStatement, self.lit(a["statement"])))
            if a.get("criticality"): g.add((n, BKR.assumptionCriticality, term("criticality", a["criticality"])))
            if a.get("status"):      g.add((n, BKR.assumptionStatus, term("assumption-status", a["status"])))
            if a.get("consequence"): g.add((n, BKR.violationConsequence, self.lit(a["consequence"])))
            elif a.get("criticality") == "critical":
                self.warnings.append(f"{rid}: critical assumption {i} has no consequence (SHACL will reject it)")
            self.assert_source(n, a.get("source"), rid, extracting_agent)
            if a.get("testable_by"):
                t = self.named_resource(a["testable_by"], "resource")
                g.add((n, BKR.isTestableBy, t))
            for k, ev in enumerate(a.get("evidence") or []):
                self.add_evidence(n, ev, rid, f"assumption{i}-{k}")
            g.add((res, BKR.hasAssumption, n))
            assumption_nodes[a["statement"]] = n
        for i, l in enumerate(rec.get("limitations") or []):
            n = self.iri(rid, "limitation", i)
            g.add((n, RDF.type, BKR.KnownIssue if l.get("issue_tracker_item") else BKR.Limitation))
            g.add((n, BKR.limitationStatement, self.lit(l["statement"])))
            if l.get("issue_tracker_item"):
                g.add((n, BKR.issueTrackerItem, self.lit(l["issue_tracker_item"], XSD.anyURI)))
            g.add((res, BKR.hasLimitation, n))
        for i, f in enumerate(rec.get("failure_modes") or []):
            n = self.iri(rid, "failure", i)
            g.add((n, RDF.type, BKR.FailureMode))
            g.add((n, BKR.limitationStatement, self.lit(f["statement"])))
            for key, prop, dt in [("condition",BKR.failureCondition,None),("symptom",BKR.failureSymptom,None),
                                  ("mitigation",BKR.failureMitigation,None)]:
                if f.get(key): g.add((n, prop, self.lit(f[key], dt)))
            if f.get("silent") is not None:
                g.add((n, BKR.isSilentFailure, self.lit(bool(f["silent"]), XSD.boolean)))
            if f.get("severity"): g.add((n, BKR.failureSeverity, term("criticality", f["severity"])))
            if f.get("detected_by"):
                g.add((n, BKR.failureDetectedBy, self.named_resource(f["detected_by"], "diagnostic-resource")))
            if f.get("related_assumption") in assumption_nodes:
                g.add((n, BKR.relatedAssumption, assumption_nodes[f["related_assumption"]]))
            for k, ev in enumerate(f.get("evidence") or []):
                self.add_evidence(n, ev, rid, f"failure{i}-{k}")
            g.add((res, BKR.hasLimitation, n))
        for i, b in enumerate(rec.get("benchmark_evidence") or []):
            n = self.iri(rid, "benchmark-result", i)
            g.add((n, RDF.type, BKR.BenchmarkResult))
            g.add((n, BKR.assessmentOf, res))
            g.add((n, DQV.computedOn, res))
            if b.get("metric"):
                g.add((n, BKR.assessmentMetric, self.lit(b["metric"])))
                metric = self.iri("metric", b["metric"])
                g.add((metric, RDF.type, DQV.Metric))
                g.add((metric, SKOS.prefLabel, self.lit(b["metric"])))
                g.add((n, DQV.isMeasurementOf, metric))
            if b.get("value") is not None:
                try: g.add((n, BKR.assessmentScore, self.lit(float(b["value"]), XSD.decimal)))
                except (TypeError, ValueError): g.add((n, BKR.assessmentRemarks, self.lit(str(b["value"]))))
            if b.get("method"): g.add((n, BKR.assessmentMethod, self.lit(b["method"])))
            if b.get("source"):
                g.add((n, PROV.hadPrimarySource, self.source_document(b["source"], self.default_document(rid))))
            if b.get("benchmark"):
                bm = self.iri("benchmark", b["benchmark"])
                g.add((bm, RDF.type, BKR.Benchmark)); g.add((bm, BKR.resourceName, self.lit(b["benchmark"])))
                g.add((n, BKR.onBenchmark, bm))
            if b.get("scope_statement"):
                sc = self.iri(rid, "scope", f"validated-bench{i}")
                g.add((sc, RDF.type, BKR.ValidatedScope))
                g.add((sc, BKR.scopeStatement, self.lit(b["scope_statement"])))
                g.add((sc, BKR.scopeEvidenceLevel, term("evidence-level", "benchmarked")))
                g.add((sc, BKR.scopeEvidence, n))
                g.add((res, BKR.hasValidatedScope, sc))
            for k, ev in enumerate(b.get("evidence") or []):
                self.add_evidence(n, ev, rid, f"bench{i}-{k}")
            g.add((res, BKR.hasAssessment, n))
            g.add((res, DQV.hasQualityMeasurement, n))
            for node, sc in validated_scopes:
                if not sc.get("evidence"):
                    g.add((node, BKR.scopeEvidence, n))
        for i, u in enumerate(rec.get("usage_examples") or []):
            n = self.iri(rid, "usage-example", i)
            g.add((n, RDF.type, BKR.UsageExample))
            if u.get("description"): g.add((n, BKR.exampleDescription, self.lit(u["description"])))
            if u.get("code"):        g.add((n, BKR.exampleCode, self.lit(u["code"])))
            if u.get("url"):         g.add((n, BKR.exampleURL, self.lit(u["url"], XSD.anyURI)))
            if u.get("demonstrates"):
                cap = self.iri(rid, "capability", u["demonstrates"])
                g.add((cap, RDF.type, BKR.Capability))
                g.add((cap, BKR.capabilityStatement, self.lit(u["demonstrates"])))
                g.add((res, BKR.hasCapability, cap))
                g.add((n, BKR.demonstratesCapability, cap))
            if u.get("scope_statement"):
                sc = self.iri(rid, "scope", f"example{i}")
                g.add((sc, RDF.type, BKR.ObservedScope))
                g.add((sc, BKR.scopeStatement, self.lit(u["scope_statement"])))
                g.add((sc, BKR.scopeEvidenceLevel, term("evidence-level", "inferred_from_usage")))
                g.add((n, BKR.exampleScope, sc)); g.add((res, BKR.hasObservedScope, sc))
            g.add((res, BKR.hasUsageExample, n))
        for direction, prop, klass in [("inputs", BKR.expectsInput, BKR.InputSpecification),
                                       ("outputs", BKR.producesOutput, BKR.OutputSpecification)]:
            for i, spec in enumerate(rec.get(direction) or []):
                n = self.iri(rid, direction[:-1], i)
                g.add((n, RDF.type, klass))
                g.add((n, BKR.ioName, self.lit(spec["name"])))
                for key, p in [("description",BKR.ioDescription),("format",BKR.ioFormat),
                               ("cardinality",BKR.ioCardinality),("unit",BKR.ioUnit),("example",BKR.ioExample)]:
                    if spec.get(key): g.add((n, p, self.lit(spec[key])))
                if spec.get("modality"): g.add((n, BKR.ioModality, term("modality", spec["modality"])))
                if spec.get("role"):     g.add((n, BKR.ioRole, term("io-role", spec["role"])))
                if spec.get("required") is not None and direction == "inputs":
                    g.add((n, BKR.ioRequired, self.lit(bool(spec["required"]), XSD.boolean)))
                if spec.get("schema"):
                    sch = self.iri("schema", spec["schema"])
                    g.add((sch, RDF.type, BKR.Schema)); g.add((sch, BKR.resourceName, self.lit(spec["schema"])))
                    g.add((n, BKR.ioSchema, sch))
                for k, cst in enumerate(spec.get("constraints") or []):
                    cn = self.iri(rid, direction[:-1], i, "constraint", k)
                    g.add((cn, RDF.type, BKR.DataAssumption))
                    g.add((cn, BKR.assumptionStatement, self.lit(cst)))
                    g.add((cn, BKR.assumptionCriticality, term("criticality", "moderate")))
                    g.add((cn, BKR.assumptionStatus, term("assumption-status", "stated")))
                    self.assert_source(cn, None, rid, extracting_agent)
                    g.add((n, BKR.ioConstraint, cn))
                g.add((res, prop, n))
        for i, o in enumerate(rec.get("owners") or []):
            n = self.iri("agent", o["name"])
            g.add((n, RDF.type, AGENT_CLASS.get(o.get("agent_type"), PROV.Agent)))
            g.add((n, RDFS.label, self.lit(o["name"])))
            if o.get("identifier"):
                idn = self.iri("agent", o["name"], "id")
                g.add((idn, RDF.type, ADMS.Identifier)); g.add((idn, SKOS.notation, self.lit(o["identifier"])))
                g.add((n, ADMS.identifier, idn))
                g.add((n, BKR.agentIdentifier, idn))  # legacy compatibility
            if o.get("affiliation"):
                org = self.iri("organization", o["affiliation"])
                g.add((org, RDF.type, PROV.Organization)); g.add((org, RDF.type, PROV.Agent))
                g.add((org, RDFS.label, self.lit(o["affiliation"])))
                g.add((n, SCHEMA.affiliation, org))
            role = o.get("role")
            g.add((res, AGENT_PROP.get(role, BKR.hasOwner), n))
            if role == "creator": g.add((res, DCT.creator, n))
            elif role == "publisher": g.add((res, DCT.publisher, n))
            elif role == "rights_holder": g.add((res, DCT.rightsHolder, n))
        prev = None
        for i, v in enumerate(rec.get("versions") or []):
            n = self.iri(rid, "version", v["version"])
            g.add((n, RDF.type, BKR.ResourceVersion))
            g.add((n, BKR.versionOf, res)); g.add((res, BKR.hasVersion, n))
            g.add((n, BKR.versionIdentifier, self.lit(v["version"])))
            if v.get("release_date"): g.add((n, BKR.releaseDate, self.lit(v["release_date"], XSD.date)))
            if v.get("status"):       g.add((n, BKR.lifecycleStatus, term("lifecycle-status", v["status"])))
            if v.get("commit"):       g.add((n, BKR.commitHash, self.lit(v["commit"])))
            if v.get("deprecation_reason"): g.add((n, BKR.deprecationReason, self.lit(v["deprecation_reason"])))
            if v.get("migration_guidance"): g.add((n, BKR.migrationGuidance, self.lit(v["migration_guidance"])))
            if v.get("supersedes"):
                sup = self.iri(rid, "version", v["supersedes"])
                g.add((n, BKR.supersedes, sup)); g.add((sup, BKR.isSupersededBy, n))
            if prev is not None: g.add((n, BKR.previousVersion, prev))
            prev = n
            for k, ch in enumerate(v.get("changes") or []):
                c = self.iri(rid, "change", v["version"], k)
                g.add((c, RDF.type, BRG.ResourceChangeRecord))
                g.add((c, BKR.changeAffectsResource, n))
                if ch.get("type"):    g.add((c, BKR.changeType, term("change-type", ch["type"])))
                if ch.get("trigger"): g.add((c, NER.changeTrigger, URIRef(f"https://brainkb.org/ner/change-trigger/{ch['trigger']}")))
                if ch.get("reason"):  g.add((c, NER.changeReason, self.lit(ch["reason"])))
                if ch.get("changed_field"): g.add((c, NER.changedField, self.lit(ch["changed_field"])))
                if ch.get("breaking") is not None:
                    g.add((c, BKR.isBreakingChange, self.lit(bool(ch["breaking"]), XSD.boolean)))
                if v.get("release_date"):
                    g.add((c, PROV.generatedAtTime, self.lit(v["release_date"] + "T00:00:00Z", XSD.dateTime)))
                for o in rec.get("owners") or []:
                    if o.get("role") in (None, "maintainer", "owner"):
                        g.add((c, PROV.wasAttributedTo, self.iri("agent", o["name"]))); break
                g.add((n, BKR.hasChangeRecord, c))
        if prev is not None: g.add((res, BKR.latestVersion, prev))
        acc = rec.get("access") or {}
        if acc:
            n = self.iri(rid, "access")
            g.add((n, RDF.type, BKR.AccessCondition))
            if acc.get("level"):       g.add((n, BKR.accessLevel, term("access-level", acc["level"])))
            if acc.get("request_url"): g.add((n, BKR.accessRequestURL, self.lit(acc["request_url"], XSD.anyURI)))
            if acc.get("embargo_end"): g.add((n, BKR.embargoEndDate, self.lit(acc["embargo_end"], XSD.date)))
            if acc.get("committee"):
                dac = self.iri("agent", acc["committee"])
                g.add((dac, RDF.type, BKR.DataAccessCommittee)); g.add((dac, RDFS.label, self.lit(acc["committee"])))
                g.add((n, BKR.managedByCommittee, dac))
            g.add((res, BKR.hasAccessCondition, n))
            if acc.get("data_use_permissions") or acc.get("data_use_modifiers"):
                duc = self.iri(rid, "data-use")
                g.add((duc, RDF.type, LS.DataUseCondition))
                for p in acc.get("data_use_permissions") or []:
                    g.add((duc, LS.hasDataUsePermission, OBO[p.replace(":", "_")]))
                for m in acc.get("data_use_modifiers") or []:
                    g.add((duc, LS.hasDataUseModifier, OBO[m.replace(":", "_")]))
                g.add((res, LS.hasDataUseCondition, duc))
        if rec.get("deposited_in"):
            arc = self.iri("archive", rec["deposited_in"])
            g.add((arc, RDF.type, BKR.Archive)); g.add((arc, BKR.resourceName, self.lit(rec["deposited_in"])))
            g.add((res, BKR.depositedIn, arc))
        if rec.get("conforms_to_schema"):
            sch = self.iri("schema", rec["conforms_to_schema"])
            g.add((sch, RDF.type, BKR.Schema)); g.add((sch, BKR.resourceName, self.lit(rec["conforms_to_schema"])))
            g.add((res, BKR.conformsToSchema, sch))
        for i, m in enumerate(rec.get("mentions") or []):
            n = self.iri("mentioned", m["name"])
            g.add((n, RDF.type, self.types.get(m.get("extracted_type"), BKR.Resource)))
            g.add((n, BKR.resourceName, self.lit(m["name"])))
            g.add((res, BKR.mentions, n))
        # ---- record, extraction provenance, review, field evidence ------
        prov_blk = rec.get("provenance") or {}
        ext = prov_blk.get("extraction") or {}
        record = self.iri(rid, "record")
        g.add((record, RDF.type, DCAT.CatalogRecord))
        g.add((record, BKR.describesResource, res)); g.add((res, BKR.hasRecord, record))
        extraction = model = None
        if ext:
            extraction = self.iri(rid, "extraction")
            g.add((extraction, RDF.type, BKR.AutomatedExtraction))
            if ext.get("timestamp"): g.add((extraction, PROV.endedAtTime, self.lit(ext["timestamp"], XSD.dateTime)))
            if ext.get("pipeline"):
                pipe = self.iri("agent", ext["pipeline"])
                g.add((pipe, RDF.type, NER.PipelineAgent)); g.add((pipe, RDFS.label, self.lit(ext["pipeline"])))
                if ext.get("pipeline_version"): g.add((pipe, NER.agentVersion, self.lit(ext["pipeline_version"])))
                g.add((extraction, PROV.wasAssociatedWith, pipe))
            if ext.get("model_name"):
                model = self.iri("agent", ext["model_name"])
                g.add((model, RDF.type, NER.LanguageModelAgent))
                g.add((model, NER.modelName, self.lit(ext["model_name"])))
                if ext.get("model_version"):  g.add((model, NER.modelVersion, self.lit(ext["model_version"])))
                if ext.get("model_provider"): g.add((model, NER.modelProvider, self.lit(ext["model_provider"])))
                g.add((extraction, PROV.wasAssociatedWith, model))
            if ext.get("snapshot_id"):
                snap = self.iri("snapshot", ext["snapshot_id"])
                g.add((snap, RDF.type, NER.ExtractionSnapshot))
                g.add((record, BKR.recordFromSnapshot, snap))
            g.add((record, PROV.wasGeneratedBy, extraction))
            g.add((record, PROV.wasAttributedTo, model or self.iri("agent", ext.get("pipeline", "pipeline"))))
        # Which work, not only where in it. Each cited source becomes one canonical document
        # carrying its own DOI, title and citation; the record derives from it, and the resource
        # is linked to it directly with dcterms:isReferencedBy so "which papers attest this
        # resource" is one hop and many-valued — a resource may be referenced by several papers.
        for i, doc in enumerate(prov_blk.get("source_documents") or []):
            n = self.describe_source_document(doc) or self.canonical_document(f"{rid}-src{i}")
            if extraction: g.add((extraction, BKR.usedResource, n))
            g.add((record, PROV.hadPrimarySource, n))
            g.add((record, DCT.source, n))
            g.add((res, DCT.isReferencedBy, n))
        rev = prov_blk.get("review") or {}
        if rev:
            n = self.iri(rid, "review")
            g.add((n, RDF.type, NER.ReviewDecision))
            g.add((n, NER.reviewStatus, URIRef(f"https://brainkb.org/ner/review-status/{rev.get('status','unreviewed')}")))
            if rev.get("comment"):   g.add((n, NER.reviewComment, self.lit(rev["comment"])))
            if rev.get("confidence") is not None:
                g.add((n, NER.reviewConfidence, self.lit(rev["confidence"], XSD.decimal)))
            if rev.get("reviewer"):
                r = self.iri("agent", rev["reviewer"])
                g.add((r, RDF.type, PROV.Person)); g.add((r, RDFS.label, self.lit(rev["reviewer"])))
                g.add((n, PROV.wasAttributedTo, r))
            g.add((record, BKR.hasReviewDecision, n))
        for i, fe in enumerate(prov_blk.get("field_evidence") or []):
            self.add_assertion(res, fe["field"], fe["quote"], fe, rid, f"field{i}", extraction, model)
        for f in rec.get("not_found_fields") or []:
            g.add((record, BKR.notFoundField, self.lit(f)))
        if rec.get("field_completeness") is not None:
            g.add((record, BKR.fieldCompleteness, self.lit(rec["field_completeness"], XSD.decimal)))
        judge = rec.get("judge") or {}
        if judge:
            n = self.iri(rid, "judge")
            g.add((n, RDF.type, DQV.QualityMeasurement))
            g.add((n, BKR.assessmentOf, record))
            g.add((n, DQV.computedOn, record))
            if judge.get("score") is not None: g.add((n, BKR.assessmentScore, self.lit(judge["score"], XSD.decimal)))
            if judge.get("method"):  g.add((n, BKR.assessmentMethod, self.lit(judge["method"])))
            if judge.get("remarks"): g.add((n, BKR.assessmentRemarks, self.lit(judge["remarks"])))
            if model: g.add((n, BKR.assessedBy, model))
            g.add((record, BKR.hasAssessment, n))
            g.add((record, DQV.hasQualityMeasurement, n))
        return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="extraction JSON conforming to schemas/bkr-resource-extraction.schema.json")
    ap.add_argument("--vocab", default=str(DEFAULT_VOCAB))
    ap.add_argument("--base", default="https://brainkb.org/kb/")
    ap.add_argument("--out", default="-")
    ap.add_argument("--id-style", choices=["uuid", "slug"], default="uuid",
                    help="uuid (default): deterministic UUIDv5 instance IRIs; slug: readable paths")
    ap.add_argument("--emit-skos", action="store_true",
                    help="also emit SKOS mapping shortcuts from a document-scoped skos:Concept per "
                         "surface label to the external term, for accepted mappings only; the "
                         "reified ner:ConceptMappingDecision stays authoritative")
    ap.add_argument("--validate-json", action="store_true",
                    help="validate the input against the extraction schema first (needs jsonschema)")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    args = ap.parse_args(argv)

    doc = json.load(open(args.input))
    if args.validate_json:
        import jsonschema
        jsonschema.validate(doc, json.load(open(args.schema)))
    conv = Converter(args.base, load_type_map(args.vocab), id_style=args.id_style,
                     emit_skos=args.emit_skos)
    for rec in doc["extracted_resources"]:
        conv.convert(rec)
    ttl = conv.g.serialize(format="turtle")
    if args.out == "-":
        print(ttl)
    else:
        open(args.out, "w").write(ttl)
    for w in conv.warnings:
        print("warning:", w, file=sys.stderr)
    print(f"{len(conv.g)} triples from {len(doc['extracted_resources'])} record(s)"
          + (f"; {conv.skos_count} SKOS mapping shortcut(s)" if args.emit_skos else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
