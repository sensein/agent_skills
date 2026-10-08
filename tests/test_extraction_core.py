"""Tests for the Extraction Core LinkML schema (schemas/extraction_core).

The schema tests need ``linkml`` and are skipped without it; CI installs it in
the dedicated "Extraction Core schema" job. They check that the schema loads,
that every ExtractionRecord carries the universal and provenance slots, that the
valid example passes and that each invalid example is rejected for the reason it
documents.
"""
from __future__ import annotations

import importlib.util
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
        self.assertFalse(any(p.startswith("schema:") for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
