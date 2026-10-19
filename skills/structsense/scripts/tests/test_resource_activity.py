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


def test_build_adds_dated_activity_checks_and_still_validates():
    rec = record()                       # url: https://github.com/example/cellmapper (stated in TEXT)
    ttl, rep = build(res(rec), source_text=TEXT, check_activity=True,
                     fetch=fake(github("example/cellmapper", commit="2019-01-01T00:00:00Z")))
    g = Graph().parse(data=ttl, format="turtle")
    checks = list(g.subjects(RDF.type, BKR.ActivityCheck))
    assert len(checks) == 1 and rep["activity"]["by_status"] == {"inactive": 1}
    c = checks[0]
    assert (c, BKR.activityStatus, BKR["activity-status/inactive"]) in g
    assert (c, BKR.lastActivityDate, Literal("2019-01-01", datatype=XSD.date)) in g
    resource = g.value(c, URIRef("http://www.w3.org/ns/prov#used"))
    assert (resource, BKR.hasActivityCheck, c) in g
    v = validate_graph(g)
    assert v["ok"], v
    assert not any("activity" in u.lower() for u in v.get("undeclared", {}))


def test_rerun_on_the_same_day_replaces_the_check():
    g = Graph()
    r = URIRef("https://brainkb.org/kb/resource/x")
    g.add((r, RDF.type, BKR.Resource))
    g.add((r, BKR.accessURL, Literal("https://github.com/o/r", datatype=XSD.anyURI)))
    for commit in ("2019-01-01T00:00:00Z", "2026-10-01T00:00:00Z"):
        ra.annotate(g, fetch=fake(github("o/r", commit=commit)), today=TODAY)
    assert len(set(g.subjects(RDF.type, BKR.ActivityCheck))) == 1
    assert len(set(g.subjects(RDF.type, BKR.ActivitySignal))) == 1
    assert set(g.objects(None, BKR.activityStatus)) == {BKR["activity-status/active"]}


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
    assert r["status"] == "unknown"                            # not "unavailable": it was a guess
