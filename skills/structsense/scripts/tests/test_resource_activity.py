"""Activity status of software resources. Offline: every registry answer is a fixture."""
import datetime as dt
import sys
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import resource_activity as ra  # noqa: E402
from resource_activity import FetchError, check, targets_from_code, targets_from_url  # noqa: E402
from resource_kg import build, validate_graph  # noqa: E402
from test_resource_kg import TEXT, record, res  # noqa: E402

BKR = Namespace("https://brainkb.org/resource/")
TODAY = dt.date(2026, 10, 19)
GH = "https://api.github.com/repos/"


def fake(answers: dict):
    """url -> answer; an int answer is an HTTP error; anything unlisted is a 404."""
    def fetch(url):
        for prefix, ans in answers.items():
            if url.startswith(prefix):
                if isinstance(ans, int):
                    raise FetchError(ans, f"HTTP {ans}")
                return ans
        raise FetchError(404, "HTTP 404")
    return fetch


def github(full, *, archived=False, commit="2026-09-02T15:35:42Z", release=None):
    a = {f"{GH}{full}/commits": [{"commit": {"committer": {"date": commit}}}],
         f"{GH}{full}/releases/latest": {"published_at": release, "tag_name": "v1.0"} if release else 404,
         f"{GH}{full}": {"full_name": full, "archived": archived, "default_branch": "main", "pushed_at": commit}}
    return a


@pytest.mark.parametrize("text,expected", [
    ("https://github.com/MITHeimanLab/Cross-species-single-cell-atlas-of-the-striatum",
     ("github", "MITHeimanLab/Cross-species-single-cell-atlas-of-the-striatum")),
    ("https://github.com/scverse/scanpy.git", ("github", "scverse/scanpy")),
    ("https://github.com/scverse/scanpy/tree/main/docs", ("github", "scverse/scanpy")),
    ("https://gitlab.com/group/sub/project/-/tree/main", ("gitlab", "group/sub/project")),
    ("https://pypi.org/project/scanpy/", ("pypi", "scanpy")),
    ("https://cran.r-project.org/package=Seurat", ("cran", "Seurat")),
    ("https://cran.r-project.org/web/packages/Seurat/index.html", ("cran", "Seurat")),
    ("https://bioconductor.org/packages/release/bioc/html/DESeq2.html", ("bioconductor", "DESeq2")),
    ("https://www.npmjs.com/package/@scope/tool", ("npm", "@scope/tool")),
    ("https://anaconda.org/bioconda/star", ("conda", "bioconda/star")),
])
def test_targets_from_urls(text, expected):
    assert [(t.source, t.key) for t in targets_from_url(text)] == [expected]


def test_non_repository_urls_are_not_targets():
    assert targets_from_url("https://github.com/orgs/MITHeimanLab") == []
    assert targets_from_url("https://example.org/atlas") == []


def test_targets_from_install_commands():
    code = ("pip install -U scanpy[leiden]==1.10\n"
            "pip install git+https://github.com/x/y\n"
            "install.packages(\"Seurat\")\n"
            "BiocManager::install(c('DESeq2', 'edgeR'))\n"
            "conda install -y -c bioconda star\n"
            "npm install --save @scope/tool")
    assert {(t.source, t.key) for t in targets_from_code(code)} == {
        ("pypi", "scanpy"), ("cran", "Seurat"), ("bioconductor", "DESeq2"),
        ("conda", "bioconda/star"), ("npm", "@scope/tool")}


@pytest.mark.parametrize("answers,status", [
    (github("o/r", commit="2026-09-02T00:00:00Z"), "active"),
    (github("o/r", commit="2024-06-01T00:00:00Z"), "stale"),
    (github("o/r", commit="2019-01-01T00:00:00Z"), "inactive"),
    (github("o/r", archived=True), "archived"),
    ({f"{GH}o/r": 404}, "unavailable"),
    ({f"{GH}o/r": 403}, "unknown"),          # rate limited: says nothing about the resource
])
def test_status_from_github(answers, status):
    r = check(["https://github.com/o/r"], fetch=fake(answers), today=TODAY)
    assert r["status"] == status


def test_release_counts_as_activity():
    r = check(["https://github.com/o/r"], today=TODAY,
              fetch=fake(github("o/r", commit="2020-01-01T00:00:00Z", release="2026-05-01T00:00:00Z")))
    assert (r["status"], r["last_activity"]) == ("active", "2026-05-01")


def test_registry_names_its_repository_and_it_is_checked():
    pypi = {"https://pypi.org/pypi/oldpkg/json": {
        "info": {"version": "0.3", "classifiers": [], "project_urls": {"Source": "https://github.com/o/oldpkg"}},
        "releases": {"0.3": [{"upload_time_iso_8601": "2018-03-01T00:00:00Z"}]}}}
    r = check(["pip install oldpkg"], today=TODAY, fetch=fake({**pypi, **github("o/oldpkg", commit="2018-02-01T00:00:00Z")}))
    assert [s["source"] for s in r["signals"]] == ["pypi", "github"]
    assert r["status"] == "inactive" and r["last_activity"] == "2018-03-01"


def test_registry_deprecation_wins_over_dates():
    npm = {"https://registry.npmjs.org/request": {
        "dist-tags": {"latest": "2.88.2"}, "time": {"2.88.2": "2026-02-11T00:00:00Z"},
        "versions": {"2.88.2": {"deprecated": "request has been deprecated"}}}}
    r = check(["npm install request"], fetch=fake(npm), today=TODAY, follow_repository=False)
    assert r["status"] == "deprecated"


def test_cran_archived_package_is_deprecated():
    cran = {"https://crandb.r-pkg.org/SDMTools/all": {
        "latest": "1.1-221.2", "archived": True, "versions": {"1.1-221.2": {}},
        "timeline": {"1.1-221.2": "2019-11-30T05:55:27+00:00", "archived": "2020-02-12T19:38:27+00:00"}}}
    r = check(["https://cran.r-project.org/package=SDMTools"], fetch=fake(cran), today=TODAY)
    assert r["status"] == "deprecated" and r["signals"][0]["latest_version"] == "1.1-221.2"


def test_bioconductor_version_bump_is_not_activity():
    bioc = {"https://bioc.r-universe.dev/api/packages/OldBioc": {
        "Version": "1.2.0", "URL": "https://github.com/o/OldBioc",
        "_commit": {"time": 1790897554, "message": "bump x.y.z version to even y prior to creation of RELEASE_3_22 branch"}}}
    r = check(["BiocManager::install('OldBioc')"], today=TODAY,
              fetch=fake({**bioc, **github("o/OldBioc", commit="2019-05-01T00:00:00Z")}))
    assert r["signals"][0]["last_commit"] is None
    assert r["status"] == "inactive"


def test_conda_rebuild_does_not_make_a_dead_project_active():
    conda = {"https://api.anaconda.org/package/bioconda/tool": {
        "latest_version": "1.0", "dev_url": "https://github.com/o/tool", "files": [{"upload_time": "2026-09-01 00:00:00"}]}}
    r = check(["conda install -c bioconda tool"], today=TODAY,
              fetch=fake({**conda, **github("o/tool", commit="2018-01-01T00:00:00Z")}))
    assert r["status"] == "inactive"


def test_no_target_is_no_check():
    assert check(["https://example.org/atlas", "python run.py"], fetch=fake({}), today=TODAY) is None


PROV = Namespace("http://www.w3.org/ns/prov#")
DCT = Namespace("http://purl.org/dc/terms/")
NIDM_VERSION = URIRef("http://purl.org/nidash/nidm#NIDM_0000122")


def test_build_adds_provenance_tracked_assessments_and_still_validates():
    rec = record()                       # url: https://github.com/example/cellmapper (stated in TEXT)
    ttl, rep = build(res(rec), source_text=TEXT, check_activity=True,
                     fetch=fake(github("example/cellmapper", commit="2019-01-01T00:00:00Z")))
    g = Graph().parse(data=ttl, format="turtle")
    [a] = list(g.subjects(RDF.type, BKR.ActivityAssessment))
    assert rep["activity"]["by_status"] == {"inactive": 1}
    assert (a, BKR.activityStatus, BKR["activity-status/inactive"]) in g
    assert (a, BKR.lastActivityDate, Literal("2019-01-01", datatype=XSD.date)) in g
    check_node = g.value(a, PROV.wasGeneratedBy)
    resource = g.value(a, BKR.assessedResource)
    assert (resource, BKR.hasActivityAssessment, a) in g and (check_node, PROV.used, resource) in g
    v = validate_graph(g)
    assert v["ok"], v
    assert not any("ctivity" in u for u in v.get("undeclared", {}))


def test_assessment_provenance_answers_when_by_what_and_from_where():
    g = Graph()
    r = URIRef("https://brainkb.org/kb/resource/x")
    g.add((r, RDF.type, BKR.Resource))
    g.add((r, BKR.accessURL, Literal("https://github.com/o/r", datatype=XSD.anyURI)))
    rep = ra.annotate(g, fetch=fake(github("o/r")), today=TODAY)
    [a] = list(g.subjects(RDF.type, BKR.ActivityAssessment))
    when = g.value(a, PROV.generatedAtTime)
    assert when.datatype == XSD.dateTime                     # a time, not only a day
    c = g.value(a, PROV.wasGeneratedBy)
    assert g.value(c, PROV.startedAtTime).datatype == XSD.dateTime and g.value(c, PROV.endedAtTime) is not None
    run = g.value(c, DCT.isPartOf)
    assert (run, RDF.type, BKR.ActivityCheckRun) in g and str(run) == rep["run"]["iri"]
    agent = g.value(c, PROV.wasAssociatedWith)
    assert (agent, RDF.type, PROV.SoftwareAgent) in g
    assert str(g.value(agent, NIDM_VERSION)) == ra.skill_version() != "unknown"
    [sig] = list(g.objects(a, PROV.wasDerivedFrom))
    assert (sig, PROV.wasGeneratedBy, c) in g and g.value(sig, PROV.generatedAtTime) is not None
    assert URIRef("https://api.github.com/repos/o/r") in set(g.objects(sig, PROV.hadPrimarySource))


def test_reruns_accumulate_and_the_latest_assessment_is_current():
    g = Graph()
    r = URIRef("https://brainkb.org/kb/resource/x")
    g.add((r, RDF.type, BKR.Resource))
    g.add((r, BKR.accessURL, Literal("https://github.com/o/r", datatype=XSD.anyURI)))
    for commit in ("2019-01-01T00:00:00Z", "2026-10-01T00:00:00Z"):
        ra.annotate(g, fetch=fake(github("o/r", commit=commit)), today=TODAY)
    assessments = list(g.objects(r, BKR.hasActivityAssessment))
    assert len(assessments) == 2 and len(set(g.subjects(RDF.type, BKR.ActivityCheckRun))) == 2
    latest = max(assessments, key=lambda a: str(g.value(a, PROV.generatedAtTime)))
    assert g.value(latest, BKR.activityStatus) == BKR["activity-status/active"]


def test_legacy_checks_are_upgraded_not_discarded():
    g = Graph()
    r = URIRef("https://brainkb.org/kb/resource/x")
    c = URIRef(f"{r}/activity-check/2026-04-01")
    g.add((r, RDF.type, BKR.Resource)); g.add((r, BKR.hasActivityCheck, c))
    g.add((c, RDF.type, BKR.ActivityCheck)); g.add((c, PROV.used, r))
    g.add((c, PROV.endedAtTime, Literal("2026-04-01", datatype=XSD.date)))
    g.add((c, BKR.activityStatus, BKR["activity-status/stale"]))
    rep = ra.annotate(g, fetch=fake({}), today=TODAY)
    assert rep["upgraded"] == 1
    [a] = list(g.objects(r, BKR.hasActivityAssessment))
    assert g.value(a, BKR.activityStatus) == BKR["activity-status/stale"]
    assert g.value(a, PROV.generatedAtTime) == Literal("2026-04-01", datatype=XSD.date)
    assert g.value(c, BKR.activityStatus) is None


@pytest.mark.parametrize("code,expected", [
    ("pip install -r requirements.txt", set()),
    ("python -m pip install --upgrade anndata scvi-tools>=1.0", {("pypi", "anndata"), ("pypi", "scvi-tools")}),
    ("pip install -e .", set()),
    ("conda install star", set()),                         # no channel: says nothing upstream
    ("mamba install bioconda::star=2.7", {("conda", "bioconda/star")}),
    ("npm i express@4 && node app.js", {("npm", "express")}),
    ("python run.py --install foo", set()),
])
def test_install_command_edge_cases(code, expected):
    assert {(t.source, t.key) for t in targets_from_code(code)} == expected


def test_github_pages_site_points_at_its_repository():
    [t] = targets_from_url("https://deeplabcut.github.io/DeepLabCut/docs/ModelZoo.html")
    assert (t.source, t.key, t.inferred) == ("github", "deeplabcut/DeepLabCut", True)
    r = check([t.found_in], fetch=fake({}), today=TODAY)      # guessed repo does not exist
    assert r is None                                           # a failed guess records nothing


def test_offline_stops_after_a_few_unreachable_resources():
    g = Graph()
    for i in range(6):
        r = URIRef(f"https://brainkb.org/kb/resource/r{i}")
        g.add((r, RDF.type, BKR.Resource))
        g.add((r, BKR.accessURL, Literal(f"https://github.com/o/r{i}", datatype=XSD.anyURI)))
    calls = []

    def down(url):
        calls.append(url)
        raise FetchError(None, "network unreachable")
    rep = ra.annotate(g, fetch=down, today=TODAY)
    github_calls = [u for u in calls if "api.github.com" in u]
    assert rep["checked"] == 0 and rep["skipped"] == 3 and len(github_calls) == 3
    assert rep["warnings"] and not list(g.subjects(RDF.type, BKR.ActivityCheck))


def test_entry_points_follow_the_config_library_stays_offline(monkeypatch):
    from resource_kg import activity_enabled, load_config
    assert activity_enabled(load_config()) is True
    monkeypatch.setenv("STRUCTSENSE_NO_NETWORK", "1")
    assert activity_enabled(load_config()) is False
    monkeypatch.delenv("STRUCTSENSE_NO_NETWORK")
    ttl, rep = build(res(record()), source_text=TEXT)            # library default: no network
    assert "activity" not in rep
    ttl, rep = build(res(record()), source_text=TEXT, check_activity=None,
                     fetch=fake(github("example/cellmapper")))    # entry-point default: config (on)
    assert rep["activity"]["checked"] == 1


def test_rate_limited_check_is_not_recorded_and_github_is_not_retried():
    g = Graph()
    for i in range(3):
        r = URIRef(f"https://brainkb.org/kb/resource/r{i}")
        g.add((r, RDF.type, BKR.Resource))
        g.add((r, BKR.accessURL, Literal(f"https://github.com/o/r{i}", datatype=XSD.anyURI)))
    calls = []

    def limited(url):
        calls.append(url)
        raise FetchError(403, "HTTP 403 (rate limited: set GITHUB_TOKEN)", rate_limited=True)
    rep = ra.annotate(g, fetch=limited, today=TODAY)
    assert rep["checked"] == 0 and len(rep["rate_limited"]) == 3
    assert len([u for u in calls if "api.github.com" in u]) == 1   # the host is not asked again this run
    assert not list(g.subjects(RDF.type, BKR.ActivityCheck))  # no "unknown" masking an earlier check
    assert "GITHUB_TOKEN" in rep["warnings"][0]


OBO = {"https://obofoundry.org/registry/ontologies.jsonld": {"ontologies": [
    {"id": "go", "title": "Gene Ontology", "activity_status": "active",
     "repository": "https://github.com/geneontology/go-ontology"},
    {"id": "old", "title": "Old Anatomy Ontology", "activity_status": "inactive"},
    {"id": "gone", "title": "Gone Ontology", "activity_status": "active", "is_obsolete": True}]}}
OLS_GO = {"https://www.ebi.ac.uk/ols4/api/ontologies/go": {
    "config": {"version": "2026-07-26", "versionIri": "http://purl.obolibrary.org/obo/go/releases/2026-07-26/go.owl"},
    "loaded": "2026-10-16T00:10:06"}}


def test_ontology_by_title_gets_obo_status_ols_release_and_its_repository():
    r = check([], name="Gene Ontology", today=TODAY,
              fetch=fake({**OBO, **OLS_GO, **github("geneontology/go-ontology", commit="2026-10-10T00:00:00Z")}))
    assert r["status"] == "active" and r["last_activity"] == "2026-10-10"
    by = {s["source"]: s for s in r["signals"]}
    assert set(by) == {"obofoundry", "ols", "github"}
    assert by["obofoundry"]["declared_status"] == "active" and "matched by title" in by["obofoundry"]["note"]
    assert by["ols"]["last_release"] == "2026-07-26"       # the release, not OLS's load time


def test_ontology_purl_and_obo_declared_inactive_or_obsolete():
    assert [(t.source, t.key) for t in targets_from_url("http://purl.obolibrary.org/obo/GO_0008150")] == [("obofoundry", "go")]
    assert [(t.source, t.key) for t in targets_from_url("http://purl.obolibrary.org/obo/uberon.owl")] == [("obofoundry", "uberon")]
    r = check([], name="Old Anatomy Ontology", fetch=fake(OBO), today=TODAY)
    assert r["status"] == "inactive"                         # curated status, even without dates
    r = check(["https://obofoundry.org/ontology/gone"], fetch=fake(OBO), today=TODAY)
    assert r["status"] == "deprecated"


def test_name_that_matches_nothing_records_nothing():
    assert check([], name="Adobe Illustrator", fetch=fake(OBO), today=TODAY) is None


def test_rrid_leads_to_the_repository():
    rrid = {"https://scicrunch.org/resolver/RRID:SCR_018139.json": {"hits": {"hits": [{"_source": {
        "distributions": {"current": [{"type": "landing page", "uri": "https://github.com/theislab/scanpy"}],
                          "alternate": [{"uri": "https://icb-scanpy.readthedocs-hosted.com/"}]},
        "item": {"availability": [{"keyword": "Free, Available for download"}]}}}]}}}
    r = check(["RRID:SCR_018139"], fetch=fake({**OBO, **rrid, **github("theislab/scanpy", commit="2026-10-15T00:00:00Z")}),
              today=TODAY, name="Scanpy", types=["SoftwareLibrary"])
    assert r["status"] == "active" and [s["source"] for s in r["signals"]] == ["rrid", "github"]


def test_rrid_marked_discontinued_is_deprecated():
    rrid = {"https://scicrunch.org/resolver/RRID:SCR_000001.json": {"hits": {"hits": [{"_source": {
        "item": {"availability": [{"keyword": "THIS RESOURCE IS NO LONGER IN SERVICE"}]}}}]}}}
    r = check(["RRID:SCR_000001"], fetch=fake({**OBO, **rrid}), today=TODAY)
    assert r["status"] == "deprecated"


GEO_PUBLIC = """!Series_title = Whole striatum
!Series_status = Public on Jul 17 2020
!Series_submission_date = Jun 08 2020
!Series_last_update_date = Jan 18 2023
"""


def test_geo_dataset_is_judged_by_availability_not_age():
    url = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE152058"
    r = check(["GSE152058"], fetch=fake({**OBO, url: GEO_PUBLIC}), today=TODAY)
    assert r["status"] == "available" and r["last_activity"] == "2023-01-18"   # not "inactive"
    s = r["signals"][0]
    assert (s["kind"], s["last_release"], s["embargoed"]) == ("data", "2020-07-17", False)
    url2 = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE999999"
    private = "<html>GSE999999 is currently private and is scheduled to be released on Mar 01, 2027.</html>"
    r = check(["GSE999999"], fetch=fake({**OBO, url2: private}), today=TODAY)
    assert r["status"] == "embargoed" and r["signals"][0]["last_release"] == "2027-03-01"


def test_r_package_by_exact_name_only_for_a_linkless_software_library():
    bioc = {"https://bioc.r-universe.dev/api/packages/limma": {"Version": "3.62.0",
            "_commit": {"time": 1790000000, "message": "fix voom weights"}}}
    r = check([], name="limma", types=["SoftwareLibrary"], fetch=fake({**OBO, **bioc}), today=TODAY)
    assert r["status"] == "active" and r["signals"][0]["source"] == "bioconductor" and r["signals"][0]["inferred"]
    assert check([], name="limma", types=["SoftwareResource"], fetch=fake({**OBO, **bioc}), today=TODAY) is None
    assert check([], name="Limma", types=["SoftwareLibrary"], fetch=fake({**OBO, **bioc}), today=TODAY) is None


def test_dataset_and_ontology_checks_validate():
    url = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE152058"
    g = Graph()
    d, o = URIRef("https://brainkb.org/kb/resource/d"), URIRef("https://brainkb.org/kb/resource/o")
    for r, n in ((d, "Whole striatum data"), (o, "Gene Ontology")):
        g.add((r, RDF.type, BKR.Resource)); g.add((r, BKR.resourceName, Literal(n)))
    i = URIRef("https://brainkb.org/kb/resource/d/id")
    g.add((d, URIRef("http://www.w3.org/ns/adms#identifier"), i))
    g.add((i, URIRef("http://www.w3.org/2004/02/skos/core#notation"), Literal("GSE152058")))
    rep = ra.annotate(g, today=TODAY, fetch=fake({**OBO, **OLS_GO, url: GEO_PUBLIC,
                                                  **github("geneontology/go-ontology")}))
    assert rep["by_status"] == {"available": 1, "active": 1}
    assert (None, BKR.isEmbargoed, Literal(False)) in g
    assert (None, BKR.declaredActivityStatus, Literal("active")) in g


def test_name_match_stops_at_the_first_registry_that_has_it():
    bioc = {"https://bioc.r-universe.dev/api/packages/limma": {"Version": "3.99.2",
            "_commit": {"time": 1790000000, "message": "fix"}}}
    cran = {"https://crandb.r-pkg.org/limma/all": {"latest": "2.10.7", "archived": True,
            "versions": {"2.10.7": {}}, "timeline": {"2.10.7": "2007-09-24", "archived": "2013-08-29"}}}
    r = check([], name="limma", types=["SoftwareLibrary"], fetch=fake({**OBO, **bioc, **cran}), today=TODAY)
    assert r["status"] == "active" and [s["source"] for s in r["signals"]] == ["bioconductor"]


def test_rrid_naming_a_different_resource_is_not_used():
    rrid = {"https://scicrunch.org/resolver/RRID:SCR_002001.json": {"hits": {"hits": [{"_source": {
        "item": {"name": "Neural ElectroMagnetic Ontologies (NEMO) Project", "abbreviations": [{"name": "NEMO"}],
                 "availability": [{"keyword": "THIS RESOURCE IS NO LONGER IN SERVICE"}]}}}]}}}
    r = check(["RRID:SCR_002001"], name="NIH Neuroscience Multi-omic Archive (NeMO)",
              fetch=fake({**OBO, **rrid}), today=TODAY)
    assert r["status"] == "unknown"                          # not the other project's "deprecated"
    assert "different resource" in r["signals"][0]["note"]


@pytest.mark.parametrize("name,record,ok", [
    ("Prism", ["GraphPad Prism"], True), ("R", ["R Project for Statistical Computing", "R software"], True),
    ("Seurat", ["Seurat"], True), ("NIH Neuroscience Multi-omic Archive (NeMO)", ["Neural ElectroMagnetic Ontologies (NEMO) Project", "NEMO"], False),
])
def test_names_match(name, record, ok):
    assert ra.names_match(name, record) is ok
