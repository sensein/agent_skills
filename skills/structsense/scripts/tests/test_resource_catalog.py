"""Offline, synthetic contract tests for the legacy resource shape; not an
extraction-accuracy benchmark.

The legacy structsense resource record (schemas/resource-output.schema.json) is still
accepted as input. Its output is now a resource KG in the BrainKB Resource Ontology
(scripts/resource_kg.py): source-stated identifiers become adms:Identifier nodes,
versions bkr:ResourceVersion nodes, and each quote a bkr:ResourceAssertion anchored by
an ner:EntityMention — all attested by the paper. Unsupported claims are removed and
reported, never published.
"""
import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import DCTERMS, PROV, RDF, SKOS

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))

from json_to_ttl import result_to_ttl  # noqa: E402
from normalize_result import normalize  # noqa: E402
from pipeline import run  # noqa: E402
from resource_kg import ground, load_config, to_records  # noqa: E402
from validate_ttl import validate_file  # noqa: E402

BKR = Namespace("https://brainkb.org/resource/")
NER = Namespace("https://brainkb.org/ner/")
ADMS = Namespace("http://www.w3.org/ns/adms#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
SCHEMA = Namespace("https://schema.org/")

FIXTURES = json.loads((SKILL / "examples" / "resource-catalog-fixtures.json").read_text())
SCHEMA_LEGACY = json.loads((SKILL / "schemas" / "resource-output.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA_LEGACY, format_checker=FormatChecker())


def result(*resources):
    return {"extracted_resources": {str(i): [res] for i, res in enumerate(resources, 1)},
            "task_type": "resource"}


def by_name(graph, name, cls):
    return next(s for s in graph.subjects(BKR.resourceName, Literal(name)) if (s, RDF.type, cls) in graph)


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


def _grounded(resource, source):
    records, _ = to_records(result(resource))
    report = ground(records, source, load_config())
    return records[0], report


def test_unsupported_claim_is_removed_not_published():
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    resource["identifiers"][0]["value"] = "999999"
    rec, report = _grounded(resource, FIXTURES["dataset"]["source"])
    assert "stable_identifiers" not in rec and "stable_identifiers" in rec["not_found_fields"]
    assert [v["version"] for v in rec["versions"]] == ["0.2.0"]  # the valid version stays
    assert any(r["field"] == "stable_identifiers[0]" for r in report["removed"])
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    resource["identifiers"][0]["value"] = "00012"  # a prefix is not the stated ID
    rec, _ = _grounded(resource, FIXTURES["dataset"]["source"])
    assert "stable_identifiers" not in rec


def test_mocked_pipeline_grounds_instead_of_publishing_fabrication(monkeypatch):
    import pipeline
    resource = copy.deepcopy(FIXTURES["dataset"]["expected"])
    monkeypatch.setattr(pipeline, "extract", lambda *a, **k: result(resource))
    output = run(FIXTURES["dataset"]["source"], task="resource", extractor_model="mock",
                 mapper_backend=None, judge_model=None, skip_judge=True)
    rec = output["extracted_resources"][0]
    assert rec["extracted_type"] == "dataset"
    assert rec["stable_identifiers"] == [{"value": "000123", "scheme": "accession", "resolves_through": "DANDI"}]
    assert normalize(copy.deepcopy(output))["extracted_resources"][0]["versions"] == [{"version": "0.2.0"}]
    assert output["resource_grounding"]["grounded"] is True
    resource["identifiers"][0]["value"] = "invented"
    output = run(FIXTURES["dataset"]["source"], task="resource", extractor_model="mock",
                 mapper_backend=None, judge_model=None, skip_judge=True)
    rec = output["extracted_resources"][0]
    assert "stable_identifiers" not in rec
    assert any(r["value"] == "invented" for r in output["resource_grounding"]["removed"])


def test_rdf_claims_survive_and_validate(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(FIXTURES["both"]["source"], encoding="utf-8")
    ttl, report = result_to_ttl(result(*FIXTURES["both"]["expected"]), source_path=source)
    assert report["kind"] == "resource_kg" and report["grounding"]["n_removed"] == 0
    graph = Graph().parse(data=ttl, format="turtle")
    dataset = by_name(graph, "Pine Maze Dataset", DCAT.Dataset)
    tool = by_name(graph, "MazeCheck", SCHEMA.SoftwareApplication)
    cited = by_name(graph, "OldScope", SCHEMA.SoftwareApplication)
    ident = graph.value(dataset, ADMS.identifier)
    assert str(graph.value(ident, SKOS.notation)) == "000123"
    assert str(graph.value(graph.value(dataset, BKR.depositedIn), BKR.resourceName)) == "DANDI"
    assert {str(graph.value(v, BKR.versionIdentifier)) for v in graph.objects(tool, BKR.hasVersion)} == {"1.4.0", "1.5.0"}
    assert list(graph.objects(dataset, BKR.hasVersion)) == []
    assert list(graph.objects(cited, ADMS.identifier)) == [] and list(graph.objects(cited, BKR.hasVersion)) == []
    assert (dataset, BKR.mentions, cited) in graph and (tool, BKR.mentions, cited) in graph
    assertions = set(graph.subjects(RDF.type, BKR.ResourceAssertion))
    assert len(assertions) == 3
    pub = next(graph.subjects(RDF.type, NER.SourceDocument))
    for a in assertions:
        assert graph.value(a, BKR.assertionAbout) in {dataset, tool}
        mention = graph.value(a, BKR.evidencedByMention)
        assert str(graph.value(mention, NER.evidenceText)) in FIXTURES["both"]["source"]
    for res in (dataset, tool):
        assert (res, DCTERMS.isReferencedBy, pub) in graph
        assert (graph.value(res, BKR.hasRecord), PROV.hadPrimarySource, pub) in graph
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
    assert report["records"] == 1  # one resource from two partial records
    graph = Graph().parse(data=ttl, format="turtle")
    tool = by_name(graph, "MazeCheck", SCHEMA.SoftwareApplication)
    assert {str(graph.value(v, BKR.versionIdentifier)) for v in graph.objects(tool, BKR.hasVersion)} == {"1.4.0", "1.5.0"}


def test_missing_source_never_emits_unverified_claim():
    ttl, report = result_to_ttl(result(FIXTURES["dataset"]["expected"]))
    graph = Graph().parse(data=ttl, format="turtle")
    assert list(graph.triples((None, ADMS.identifier, None))) == []
    assert list(graph.subjects(RDF.type, BKR.ResourceVersion)) == []
    assert any("no source text" in w for w in report["warnings"])
