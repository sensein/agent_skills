"""Offline, synthetic contract tests; not an extraction-accuracy benchmark."""
import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from rdflib import Graph, Literal
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, RDFS

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))

from json_to_ttl import NER, result_to_ttl  # noqa: E402
from normalize_result import normalize  # noqa: E402
from pipeline import run  # noqa: E402
from resource_claims import checked_claims  # noqa: E402
from validate_ttl import validate_file  # noqa: E402

FIXTURES = json.loads((SKILL / "examples" / "resource-catalog-fixtures.json").read_text())
SCHEMA = json.loads((SKILL / "schemas" / "resource-output.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def result(*resources):
    return {"extracted_resources": {str(i): [res] for i, res in enumerate(resources, 1)},
            "task_type": "resource"}


@pytest.mark.parametrize("case", ["dataset", "tool", "legacy"])
def test_valid_and_legacy_resources(case):
    VALIDATOR.validate(result(FIXTURES[case]["expected"]))


def test_multiple_primary_resources_and_mentions():
    data = result(*FIXTURES["both"]["expected"])
    VALIDATOR.validate(data)
    assert len(data["extracted_resources"]) == 2
    assert all("OldScope" in r["mentions"]["tools"] for r in FIXTURES["both"]["expected"])
    VALIDATOR.validate(FIXTURES["mentions_only"]["expected"])


@pytest.mark.parametrize("change", [
    {"identifiers": "DANDI:000123"},
    {"versions": [{"value": "0.2.0", "evidence": {"quote": 23}}]},
    {"versions": [{"value": "0.2.0"}]},
])
def test_schema_rejects_malformed_claims(change):
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    resource.update(change)
    assert not VALIDATOR.is_valid(result(resource))


def test_unsupported_claim_is_not_accepted():
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    resource["identifiers"][0]["value"] = "999999"
    claims, errors = checked_claims(resource, FIXTURES["dataset"]["source"])
    assert len(claims) == 1  # the valid version remains supported
    assert "identifiers[0]" in errors[0]
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    resource["identifiers"][0]["value"] = "00012"  # prefix is not the stated ID
    _, errors = checked_claims(resource, FIXTURES["dataset"]["source"])
    assert "identifiers[0]" in errors[0]


def test_mocked_pipeline_preserves_metadata_and_rejects_fabrication(monkeypatch):
    import pipeline
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    monkeypatch.setattr(pipeline, "extract", lambda *a, **k: result(resource))
    output = run(FIXTURES["dataset"]["source"], task="resource", extractor_model="mock",
                 mapper_backend=None, judge_model=None, skip_judge=True)
    assert output["extracted_resources"]["1"][0]["identifiers"] == resource["identifiers"]
    assert normalize(copy.deepcopy(output))["extracted_resources"]["1"][0]["versions"] == resource["versions"]
    resource["identifiers"][0]["value"] = "invented"
    with pytest.raises(ValueError, match="Unsupported resource metadata"):
        run(FIXTURES["dataset"]["source"], task="resource", extractor_model="mock",
            mapper_backend=None, judge_model=None, skip_judge=True)


def test_rdf_claims_survive_and_validate(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(FIXTURES["both"]["source"], encoding="utf-8")
    data = result(*FIXTURES["both"]["expected"])
    ttl, report = result_to_ttl(data, source_path=source)
    assert report["warnings"] == []
    graph = Graph().parse(data=ttl, format="turtle")
    dataset = next(s for s in graph.subjects(RDFS.label, Literal("Pine Maze Dataset"))
                   if (s, RDF.type, NER.DatasetEntity) in graph)
    tool = next(s for s in graph.subjects(RDFS.label, Literal("MazeCheck"))
                if (s, RDF.type, NER.SoftwareEntity) in graph)
    cited = next(s for s in graph.subjects(RDFS.label, Literal("OldScope"))
                 if (s, RDF.type, NER.SoftwareEntity) in graph)
    assert (dataset, DCTERMS.identifier, Literal("DANDI:000123")) in graph
    assert {str(v) for v in graph.objects(tool, OWL.versionInfo)} == {"1.4.0", "1.5.0"}
    assert list(graph.triples((dataset, OWL.versionInfo, None))) == []
    assert list(graph.triples((cited, DCTERMS.identifier, None))) == []
    assert list(graph.triples((cited, OWL.versionInfo, None))) == []
    assert len(list(graph.subjects(RDF.type, RDF.Statement))) == 3
    for statement in graph.subjects(RDF.type, RDF.Statement):
        subject = graph.value(statement, RDF.subject)
        assert subject in {dataset, tool}
        assert graph.value(statement, DCTERMS.description)
        assert graph.value(statement, PROV.hadPrimarySource)
    ttl_path = tmp_path / "out.ttl"
    ttl_path.write_text(ttl, encoding="utf-8")
    assert validate_file(ttl_path)["ok"]


def test_repeated_resource_keeps_each_stated_version(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(FIXTURES["both"]["source"], encoding="utf-8")
    resource = FIXTURES["both"]["expected"][1]
    first, second = copy.deepcopy(resource), copy.deepcopy(resource)
    first["versions"] = resource["versions"][:1]
    second["versions"] = resource["versions"][1:]
    ttl, report = result_to_ttl(result(first, second), source_path=source)
    assert report["warnings"] == []
    graph = Graph().parse(data=ttl, format="turtle")
    tool = next(s for s in graph.subjects(RDFS.label, Literal("MazeCheck"))
                if (s, RDF.type, NER.SoftwareEntity) in graph)
    assert {str(v) for v in graph.objects(tool, OWL.versionInfo)} == {"1.4.0", "1.5.0"}


def test_missing_source_never_emits_unverified_claim():
    ttl, report = result_to_ttl(result(FIXTURES["dataset"]["expected"]))
    graph = Graph().parse(data=ttl, format="turtle")
    assert list(graph.triples((None, DCTERMS.identifier, None))) == []
    assert any("not in the normalized source text" in warning for warning in report["warnings"])
