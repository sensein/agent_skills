"""Tests for the Extraction Core LinkML schema (schemas/extraction_core).

The schema tests need ``linkml`` and are skipped without it; CI installs it in
the dedicated "Extraction Core schema" job. They check that the schema loads,
that every ExtractionRecord carries the universal and provenance slots, that the
valid example passes and that each invalid example is rejected for the reason it
documents.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "schemas" / "extraction_core"
SCHEMA = SCHEMA_DIR / "extraction_core.yaml"
EXAMPLES = SCHEMA_DIR / "examples"

HAVE_LINKML = importlib.util.find_spec("linkml") is not None

UNIVERSAL_SLOTS = {"paper_id", "source_location", "source_excerpt", "assertion",
                   "generated_by", "extracted_by", "extracted_at"}


def load_checker():
    spec = importlib.util.spec_from_file_location("check_dataset", SCHEMA_DIR / "check_dataset.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SchemaFilesTest(unittest.TestCase):
    def test_schema_and_examples_exist(self):
        self.assertTrue(SCHEMA.is_file())
        self.assertTrue((EXAMPLES / "ExtractionDataset-hu2026.yaml").is_file())
        self.assertTrue(list((EXAMPLES / "invalid").glob("*.yaml")))


@unittest.skipUnless(HAVE_LINKML, "linkml not installed")
class ExtractionCoreSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from linkml_runtime.utils.schemaview import SchemaView

        cls.sv = SchemaView(str(SCHEMA))
        cls.checker = load_checker()

    def test_every_record_type_has_universal_slots(self):
        records = self.sv.class_descendants("ExtractionRecord", reflexive=False)
        self.assertGreaterEqual(len(records), 6)
        for cname in records:
            slots = {s.name for s in self.sv.class_induced_slots(cname)}
            self.assertLessEqual(UNIVERSAL_SLOTS, slots, cname)
            for required in ("paper_id", "generated_by"):
                self.assertTrue(self.sv.induced_slot(required, cname).required, f"{cname}.{required}")

    def test_run_pins_skill_and_model_versions(self):
        self.assertTrue(self.sv.induced_slot("skill", "ExtractionRun").required)
        self.assertTrue(self.sv.induced_slot("models", "ExtractionRun").required)
        self.assertTrue(self.sv.induced_slot("skill_version", "SkillVersion").required)
        self.assertTrue(self.sv.induced_slot("model_identifier", "ModelVersion").required)
        self.assertTrue(self.sv.induced_slot("prompt_sha256", "PromptVersion").required)

    def test_spreadsheet_provenance_vocabularies_match_ait_code(self):
        # field_provenance labels and evidence outcomes must stay in step with
        # the structsense AIT pipeline that writes them.
        sys.path.insert(0, str(REPO_ROOT / "skills" / "structsense"))
        from scripts import ait_tables

        labels = self.sv.get_enum("FieldProvenanceLabelEnum").permissible_values
        self.assertEqual({pv.title for pv in labels.values()}, set(ait_tables.PROVENANCE_LABELS))
        outcomes = set(self.sv.get_enum("EvidenceVerificationEnum").permissible_values)
        self.assertLessEqual(set(ait_tables.load_schema()["vocabularies"]["evidence_verified"]), outcomes)

    def test_every_enum_value_builds_its_enum_in_generated_python(self):
        # linkml-convert builds Python classes from the schema, and LinkML keeps the
        # matched permissible value on each enum object as `.code`. A value that
        # shadows an EnumDefinitionImpl attribute (as a value named `code` did)
        # corrupts every member: MapperTypeEnum("llm").code was the `code` value.
        from linkml.generators.pythongen import PythonGenerator

        module = PythonGenerator(str(SCHEMA)).compile_module()
        for enum_name, enum in self.sv.all_enums().items():
            cls = getattr(module, enum_name)
            for value in enum.permissible_values:
                with self.subTest(enum=enum_name, value=value):
                    member = cls(value)
                    self.assertIsInstance(member, cls)
                    self.assertEqual(member.code.text, value)

    def test_hashes_accept_structsense_format(self):
        import hashlib
        import re

        digest = hashlib.sha256(b"prompt text").hexdigest()
        for cname, slot in (("PromptVersion", "prompt_sha256"), ("RunConfiguration", "config_sha256"),
                            ("SourceDocument", "content_checksum")):
            pattern = re.compile(self.sv.induced_slot(slot, cname).pattern)
            with self.subTest(slot=slot):
                self.assertRegex("sha256:" + digest, pattern)  # what structsense writes
                self.assertRegex(digest, pattern)
                for bad in ("md5:" + digest, "sha256:" + digest[:40], digest.upper()):
                    self.assertNotRegex(bad, pattern)

    def test_jsonld_context_is_current_and_declares_1_1(self):
        spec = importlib.util.spec_from_file_location("gen_jsonld_context", SCHEMA_DIR / "gen_jsonld_context.py")
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        committed = (SCHEMA_DIR / "extraction_core.context.jsonld").read_text(encoding="utf-8")
        self.assertEqual(committed, gen.build_context(), "run gen_jsonld_context.py to regenerate")
        context = json.loads(committed)["@context"]
        self.assertEqual(context["@version"], 1.1)
        self.assertEqual(context["CL"], {"@id": "http://purl.obolibrary.org/obo/CL_", "@prefix": True})

    def test_jsonld_links_entities_to_external_ontology_terms(self):
        # The point of the SKOS slots and the OntologyClass ranges: in RDF the
        # entity links to the CL term itself, and ontology-valued fields are IRIs.
        import rdflib
        import yaml
        from rdflib import Namespace, URIRef

        data = yaml.safe_load((EXAMPLES / "ExtractionDataset-hu2026.yaml").read_text(encoding="utf-8"))
        context = json.loads((SCHEMA_DIR / "extraction_core.context.jsonld").read_text(encoding="utf-8"))
        doc = {**context, **json.loads(json.dumps(data, default=str))}
        graph = rdflib.Graph().parse(data=json.dumps(doc), format="json-ld")

        obo = "http://purl.obolibrary.org/obo/"
        skos = Namespace("http://www.w3.org/2004/02/skos/core#")
        exc = Namespace("https://brainkb.org/extraction-core/")
        entity = URIRef("https://example.org/extraction/entity/pvalb-interneuron")
        self.assertEqual(set(graph.objects(entity, skos.exactMatch)), {URIRef(obo + "CL_4023018")})
        self.assertIn(URIRef(obo + "NCBITaxon_10090"), set(graph.objects(None, exc.taxon)))
        self.assertIn(URIRef(obo + "ECO_0000006"), set(graph.objects(None, exc.evidence_code)))
        self.assertIn(URIRef(obo + "RO_0002215"), set(graph.objects(None, exc.predicate)))
        for prop in (exc.taxon, exc.mapped_curie, exc.entity_class, exc.predicate, exc.evidence_code):
            for value in graph.objects(None, prop):
                self.assertIsInstance(value, URIRef, f"{prop} -> {value!r} is not a node")

    def test_valid_example_passes(self):
        problems = self.checker.check(EXAMPLES / "ExtractionDataset-hu2026.yaml")
        self.assertEqual(problems, [])

    def test_missing_provenance_is_rejected(self):
        problems = self.checker.check(EXAMPLES / "invalid" / "ExtractionDataset-missing-provenance.yaml")
        text = "\n".join(problems)
        for slot in ("skill", "models", "paper_id", "generated_by"):
            self.assertIn(f"'{slot}' is a required property", text)

    def test_broken_references_are_rejected(self):
        problems = self.checker.check(EXAMPLES / "invalid" / "ExtractionDataset-broken-references.yaml")
        text = "\n".join(problems)
        self.assertIn("ex:paper/missing is not defined", text)
        self.assertIn("extracted_by ex:model/model-b is not among the models", text)
        self.assertIn("grounding not_found but record_status is accepted", text)
        self.assertIn("never tool-verified", text)
        self.assertIn("match_confidence exact does not follow from broad_match", text)
        self.assertIn("no_match mapping has no no_match_reason", text)
        self.assertIn("accepted exact_match mapping to UBERON:0003881 is missing from exact_match", text)
        self.assertIn("close_match asserts UBERON:0003881 without an accepted mapping", text)
        self.assertFalse(any(p.startswith("schema:") for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
