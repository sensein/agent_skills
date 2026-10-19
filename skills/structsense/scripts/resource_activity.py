#!/usr/bin/env python3
"""Activity status of resources: is the code or ontology still maintained, is the data released?

A paper says where its code lives; it cannot say whether anyone still works on it.
This step asks the hosting service and the package registries, and records what they
answered as a dated observation — never as a claim of the paper.

Targets come from what the resource graph already states: a resource's
`bkr:accessURL`, URL-valued identifiers, usage-example URLs, and install commands in
usage-example code (`pip install X`, `install.packages("X")`,
`BiocManager::install("X")`, `conda install -c bioconda X`, `npm install X`). A
package registry that names its own source repository (PyPI `project_urls`, npm
`repository`, conda `dev_url`) adds that repository as a target too. Identifiers:
an RRID of a tool (`RRID:SCR_…`) is resolved for its links, a GEO series (`GSE…`)
for its release. Ontology URLs (`purl.obolibrary.org/obo/…`, OBO Foundry, OLS) name
the ontology. Guesses from the name, marked inferred and dropped when they find
nothing: an exact OBO Foundry title, and — for a software library with no link at
all — an exact-case Bioconductor, then CRAN, package name.

Sources, all public GETs (set GITHUB_TOKEN to lift GitHub's 60 requests/hour limit):

| source        | what is read                                                     |
|---------------|------------------------------------------------------------------|
| GitHub        | archived flag, last commit on the default branch, latest release |
| GitLab.com    | archived flag, last activity, latest release                     |
| PyPI          | latest version, last upload, "Development Status :: 7 - Inactive" |
| CRAN (crandb) | latest version and date, archived from CRAN                      |
| Bioconductor  | (r-universe mirror) version, last non-bump commit, repository    |
| npm           | latest version and date, deprecated flag                         |
| conda         | latest version, last upload (a packaging signal: rebuilds by     |
|               | channel bots, so used only when nothing else dates the resource) |
| OBO Foundry   | curated activity status (active/inactive/orphaned), obsolete,    |
|               | repository (followed)                                            |
| OLS           | version of the loaded release; its date is the release date      |
| RRID resolver | the resource's links (followed), "no longer in service"          |
| GEO           | public / private, release and last-update dates (a dataset)      |

Status (`https://brainkb.org/resource/activity-status/`), first rule that applies:
archived (the host says so) > deprecated (a registry says so) > inactive by a
registry's curated status (OBO Foundry inactive/orphaned) > unavailable (every target
is gone: 404) > for a dataset, available / embargoed (data is not stale for being
unchanged) > unknown (no date could be read) > active (last activity within
`stale_after_days`) > stale (within `inactive_after_days`) > inactive.

The paper-stated `bkr:lifecycleStatus` of a version is left alone. Every run is
provenance-tracked (PROV-O, structured like NIDM-Experiment): an `ActivityCheckRun`
(cf. nidm:Session) with start/end times and a `prov:SoftwareAgent` carrying its NIDM
software version; per resource an `ActivityCheck` (cf. nidm:Acquisition, part of the
run) that `prov:used` the resource; per source an `ActivitySignal` with the time the
answer arrived and the API URLs queried (`prov:hadPrimarySource`); and the result, an
`ActivityAssessment` (cf. nidm:AcquisitionObject) `prov:wasGeneratedBy` the check at
`prov:generatedAtTime`, `prov:wasDerivedFrom` the signals. Runs accumulate, never
overwrite; the current status is the latest assessment. Checks written before this
are upgraded to assessments in place (`upgrade_legacy`), keeping their date.

    python scripts/resource_activity.py check https://github.com/org/repo "pip install scanpy"
    python scripts/resource_activity.py annotate resources.ttl --out resources.ttl --report activity.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import rdflib
from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

BKR = Namespace("https://brainkb.org/resource/")
PROV = Namespace("http://www.w3.org/ns/prov#")
ADMS = Namespace("http://www.w3.org/ns/adms#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
DCT = Namespace("http://purl.org/dc/terms/")
NIDM = Namespace("http://purl.org/nidash/nidm#")
NIDM_SOFTWARE_VERSION = NIDM.NIDM_0000122       # NIDM-Results "software Version" (domain prov:SoftwareAgent)
KB = "https://brainkb.org/kb/"


def skill_version() -> str:
    """structsense's version, from SKILL.md (one source of truth)."""
    try:
        text = (Path(__file__).resolve().parent.parent / "SKILL.md").read_text()
        m = re.search(r'^\s*version:\s*"?([\w.+-]+)"?\s*$', text, re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _now() -> str:
    """UTC, always to the microsecond: one fixed width, so ISO strings order correctly as
    text (the MCP queries compare STR()) and back-to-back runs never tie."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

STALE_AFTER_DAYS = 365
INACTIVE_AFTER_DAYS = 3 * 365
_TIMEOUT = 20
_DELAY_SECONDS = 0.2
STATUSES = ("active", "stale", "inactive", "archived", "deprecated", "unavailable", "unknown",
            "available", "embargoed")
PACKAGING = {"conda"}  # dates that reflect rebuilds, not development
OBO_REGISTRY = "https://obofoundry.org/registry/ontologies.jsonld"
OFFLINE_AFTER = 3     # resources in a row with no answer at all: stop, the network is down


# ---------------------------------------------------------------------------
# Targets: where to ask
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    source: str          # github | gitlab | pypi | cran | bioconductor | npm | conda
    key: str             # owner/repo, package name, channel/name
    found_in: str        # the URL or code it was read from
    inferred: bool = False   # a guess (GitHub Pages address, a name): a failed guess records nothing

    @property
    def page(self) -> str:
        return {
            "github": f"https://github.com/{self.key}",
            "gitlab": f"https://gitlab.com/{self.key}",
            "pypi": f"https://pypi.org/project/{self.key}/",
            "cran": f"https://cran.r-project.org/package={self.key}",
            "bioconductor": f"https://bioconductor.org/packages/{self.key}",
            "npm": f"https://www.npmjs.com/package/{self.key}",
            "conda": f"https://anaconda.org/{self.key}",
            "obofoundry": f"https://obofoundry.org/ontology/{self.key}",
            "ols": f"https://www.ebi.ac.uk/ols4/ontologies/{self.key}",
            "rrid": f"https://scicrunch.org/resolver/{self.key}",
            "geo": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={self.key}",
        }[self.source]


_GITHUB_RESERVED = {"orgs", "topics", "sponsors", "features", "marketplace", "settings", "collections",
                    "about", "pricing", "login", "search", "explore", "users", "apps", "site", "enterprise"}
_URL_RULES: list[tuple[str, re.Pattern]] = [
    ("github", re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.I)),
    ("gitlab", re.compile(r"gitlab\.com/((?:[\w.-]+/)+?[\w.-]+?)(?:\.git)?(?:/-/|[?#]|/?$)", re.I)),
    ("pypi", re.compile(r"pypi(?:\.python)?\.org/(?:project|pypi)/([\w.-]+)", re.I)),
    ("cran", re.compile(r"cran\.(?:r-project\.org|rstudio\.com)/(?:web/packages/|package=)([\w.]+)", re.I)),
    ("cran", re.compile(r"cran\.r-project\.org/web/packages/([\w.]+)", re.I)),
    ("bioconductor", re.compile(r"bioconductor\.org/packages/(?:(?:release|devel|[\d.]+)/bioc/html/)?([\w.]+?)(?:\.html)?(?:[/?#]|$)", re.I)),
    ("npm", re.compile(r"npmjs\.(?:com|org)/package/((?:@[\w.-]+/)?[\w.-]+)", re.I)),
    ("conda", re.compile(r"anaconda\.org/([\w-]+/[\w.-]+)", re.I)),
    ("obofoundry", re.compile(r"purl\.obolibrary\.org/obo/([A-Za-z][A-Za-z0-9]*)(?:_\d|\.owl|\.obo|\.json|/|$)", re.I)),
    ("obofoundry", re.compile(r"obofoundry\.org/ontology/([\w-]+)", re.I)),
    ("ols", re.compile(r"ebi\.ac\.uk/ols4?/ontologies/([\w-]+)", re.I)),
    ("geo", re.compile(r"ncbi\.nlm\.nih\.gov/geo/query/acc\.cgi\?(?:[^#]*&)?acc=(GSE\d+)", re.I)),
    ("rrid", re.compile(r"(?:scicrunch\.org/resolver/|identifiers\.org/|n2t\.net/)(RRID:SCR_\d+)", re.I)),
]
_BIOC_NOT_PACKAGES = {"release", "devel", "json", "stats", "html"}

# install commands; a package name is a plain token, never a path, URL or flag
_NAME = r"([A-Za-z][\w.-]*)"
_R_RULES: list[tuple[str, re.Pattern]] = [
    ("cran", re.compile(r"\binstall\.packages\(\s*(?:c\()?\s*[\"']" + _NAME + r"[\"']")),
    ("bioconductor", re.compile(r"\bBiocManager::install\(\s*(?:c\()?\s*[\"']" + _NAME + r"[\"']")),
]
_SHELL = re.compile(r"(?:^|[;&|`(]|\s)(pip3?|python3?\s+-m\s+pip|conda|mamba|micromamba|npm)\s+(install|i)\s+([^\n;&|`)]*)")
# flags whose next token is their value, not a package
_VALUE_FLAGS = {"pip": {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
                        "--extra-index-url", "-t", "--target", "--prefix", "-f", "--find-links", "--root"},
                "conda": {"-c", "--channel", "-n", "--name", "-p", "--prefix", "--file"},
                "npm": {"--prefix", "--registry", "--tag"}}


def _pkg_token(tok: str, tool: str) -> Optional[str]:
    tok = tok.strip("\"'")
    if re.match(r"^(git\+|https?:|\.|/|~|\$)", tok) or tok.endswith((".whl", ".txt", ".tar.gz", ".zip", ".yml")):
        return None
    if tool == "npm":
        m = re.match(r"^((?:@[\w.-]+/)?[A-Za-z][\w.-]*?)(?:@[\w.^~<>=*-]+)?$", tok)
    else:  # pip extras/specifiers, conda specs (star=2.7, bioconda::star)
        tok = tok.split("::")[-1]
        m = re.match(r"^([A-Za-z0-9][\w.-]*?)(?:\[[^\]]*\])?(?:[=<>!~;@].*)?$", tok)
    return m.group(1) if m else None


def _shell_targets(code: str) -> list[Target]:
    out = []
    for m in _SHELL.finditer(code or ""):
        tool = "pip" if "pip" in m.group(1) else ("npm" if m.group(1) == "npm" else "conda")
        if tool != "npm" and m.group(2) != "install":
            continue
        toks, channel, names, k = m.group(3).split(), None, [], 0
        while k < len(toks):
            t = toks[k]
            if t.startswith("-"):
                flag = t.split("=")[0]
                if flag in _VALUE_FLAGS[tool]:
                    value = t.split("=", 1)[1] if "=" in t else (toks[k + 1] if k + 1 < len(toks) else "")
                    if tool == "conda" and flag in ("-c", "--channel") and not channel:
                        channel = value
                    k += 1 if "=" in t else 2
                    continue
                k += 1
                continue
            name = _pkg_token(t, tool)
            if name:
                if tool == "conda" and "::" in t:
                    channel = channel or t.split("::")[0]
                names.append(name)
            k += 1
        for name in names:
            if tool == "pip":
                out.append(Target("pypi", name, m.group(0).strip()))
            elif tool == "npm":
                out.append(Target("npm", name, m.group(0).strip()))
            elif channel:          # no channel: defaults, which says nothing about upstream
                out.append(Target("conda", f"{channel}/{name}", m.group(0).strip()))
    return out


_PAGES = re.compile(r"https?://([\w-]+)\.github\.io/([\w.-]+)", re.I)


def targets_from_url(url: str) -> list[Target]:
    m = _PAGES.match(url.strip())
    if m:   # a GitHub Pages project site is usually published from github.com/<owner>/<repo>
        return [Target("github", f"{m.group(1)}/{m.group(2)}", url, inferred=True)]
    out = []
    for source, rx in _URL_RULES:
        m = rx.search(url)
        if not m:
            continue
        if source == "github":
            owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
            if owner.lower() in _GITHUB_RESERVED or not repo.strip("."):
                continue
            key = f"{owner}/{repo}"
        elif source == "bioconductor" and m.group(1).lower() in _BIOC_NOT_PACKAGES:
            continue
        elif source in ("obofoundry", "ols"):
            key = m.group(1).lower()
        else:
            key = m.group(1).rstrip("/")
        out.append(Target(source, key, url))
        break
    return out


_IDENT_RULES: list[tuple[str, re.Pattern]] = [
    ("rrid", re.compile(r"^\s*(?:RRID:\s*)?(SCR_\d+)\s*$", re.I)),
    ("geo", re.compile(r"^\s*(GSE\d+)\s*$", re.I)),
]


def targets_from_identifier(value: str) -> list[Target]:
    """RRIDs of tools (SCR_) and GEO series; other identifiers say nothing about activity."""
    for source, rx in _IDENT_RULES:
        m = rx.match(value or "")
        if m:
            key = f"RRID:{m.group(1).upper()}" if source == "rrid" else m.group(1).upper()
            return [Target(source, key, value)]
    return []


# Bioconductor first: a package that moved there (limma, 2013) can linger on CRAN as archived
_R_NAME_ORDER = ("bioconductor", "cran")


def targets_from_name(name: str, types: Iterable[str], have_targets: bool) -> list[Target]:
    """Guesses from the resource's name, marked inferred: an exact OBO Foundry title
    (any resource), and — only when nothing else points anywhere — an exact-case
    Bioconductor or CRAN package name for a software library."""
    name = (name or "").strip()
    if not name:
        return []
    out = [Target("obofoundry", f"title:{name}", f"name: {name}", inferred=True)]
    if not have_targets and "SoftwareLibrary" in set(types) and re.fullmatch(r"[A-Za-z][A-Za-z0-9.]*", name):
        out += [Target("bioconductor", name, f"name: {name}", inferred=True),
                Target("cran", name, f"name: {name}", inferred=True)]
    return out


def targets_from_code(code: str) -> list[Target]:
    out = _shell_targets(code)
    for source, rx in _R_RULES:
        out += [Target(source, m.group(1), m.group(0)) for m in rx.finditer(code or "")]
    return out


def _dedupe(targets: Iterable[Target]) -> list[Target]:
    seen, out = set(), []
    for t in targets:
        k = (t.source, t.key.lower())
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Fetching: what each source says
# ---------------------------------------------------------------------------

class FetchError(Exception):
    def __init__(self, status: Optional[int], message: str, rate_limited: bool = False):
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited


class _RateLimits:
    """Per-run memory of hosts that rate-limited us: later requests to the same host
    fail at once instead of spending more of an exhausted quota."""

    def __init__(self, fetch: "Fetch"):
        self.fetch, self.hosts, self.cache = fetch, set(), {}

    def __call__(self, url: str) -> Any:
        host = url.split("/")[2] if "://" in url else url
        if host in self.hosts:
            raise FetchError(429, f"skipped: {host} rate limit reached earlier in this run", rate_limited=True)
        if url in self.cache:              # the OBO registry is read once per run
            if isinstance(self.cache[url], FetchError):
                raise self.cache[url]
            return self.cache[url]
        try:
            self.cache[url] = self.fetch(url)
            return self.cache[url]
        except FetchError as e:
            if e.rate_limited:
                self.hosts.add(host)
            elif url == OBO_REGISTRY:
                self.cache[url] = e
            raise


Fetch = Callable[[str], Any]   # url -> parsed JSON (or text for .html); raises FetchError


def http_fetch(url: str) -> Any:
    headers = {"User-Agent": "structsense-skill/1.0", "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if "api.github.com" in url and token:
        headers["Authorization"] = f"Bearer {token}"
    time.sleep(_DELAY_SECONDS)
    try:
        with urlopen(Request(url, headers=headers), timeout=_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace").lower()
        except Exception:
            detail = ""
        limited = e.code == 429 or (e.code == 403 and (
            (e.headers.get("X-RateLimit-Remaining") == "0") or "rate limit" in detail))
        hint = " (rate limited: set GITHUB_TOKEN)" if limited and "api.github.com" in url else (
            " (rate limited)" if limited else "")
        raise FetchError(e.code, f"HTTP {e.code}{hint}", rate_limited=limited) from None
    except (URLError, TimeoutError, OSError) as e:
        raise FetchError(None, str(e)) from None
    if url.endswith(".html") or "form=text" in url:
        return body
    return json.loads(body)


def _date(value: Any) -> Optional[str]:
    """ISO date (YYYY-MM-DD) of a timestamp in any of the registries' formats."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else None


def _latest(*dates: Optional[str]) -> Optional[str]:
    ds = [d for d in dates if d]
    return max(ds) if ds else None


def _signal(t: Target, **kw) -> dict:
    s = {"source": t.source, "key": t.key, "page": t.page, "found_in": t.found_in, "inferred": t.inferred,
         "available": True, "archived": False, "deprecated": False,
         "last_commit": None, "last_release": None, "latest_version": None, "last_activity": None,
         "repository": None, "note": None, "error": None, "http_status": None, "rate_limited": False,
         "kind": "code", "embargoed": False, "declared_status": None, "declared_inactive": False, "links": []}
    s.update(kw)
    return s


def _github(t: Target, fetch: Fetch) -> dict:
    api = f"https://api.github.com/repos/{t.key}"
    repo = fetch(api)
    note = None
    if repo.get("full_name") and repo["full_name"].lower() != t.key.lower():
        note = f"moved to {repo['full_name']}"
    branch = repo.get("default_branch") or "HEAD"
    last_commit = None
    try:
        commits = fetch(f"{api}/commits?per_page=1&sha={quote(branch)}")
        if commits:
            c = commits[0]["commit"]
            last_commit = _date((c.get("committer") or {}).get("date") or (c.get("author") or {}).get("date"))
    except FetchError as e:
        if e.status != 409:      # 409: empty repository
            raise
    release = version = None
    try:
        rel = fetch(f"{api}/releases/latest")
        release, version = _date(rel.get("published_at")), rel.get("tag_name")
    except FetchError as e:
        if e.status != 404:      # no releases
            raise
    return _signal(t, archived=bool(repo.get("archived")), last_commit=last_commit or _date(repo.get("pushed_at")),
                   last_release=release, latest_version=version, note=note,
                   last_activity=_latest(last_commit or _date(repo.get("pushed_at")), release))


def _gitlab(t: Target, fetch: Fetch) -> dict:
    api = f"https://gitlab.com/api/v4/projects/{quote(t.key, safe='')}"
    p = fetch(api)
    release = version = None
    try:
        rels = fetch(f"{api}/releases?per_page=1")
        if rels:
            release, version = _date(rels[0].get("released_at")), rels[0].get("tag_name")
    except FetchError as e:
        if e.status not in (403, 404):
            raise
    last = _date(p.get("last_activity_at"))
    return _signal(t, archived=bool(p.get("archived")), last_commit=last, last_release=release,
                   latest_version=version, last_activity=_latest(last, release))


def _repo_link(*urls: Any) -> Optional[str]:
    for u in urls:
        if isinstance(u, str) and re.search(r"(github|gitlab)\.com/", u, re.I):
            return re.sub(r"^git\+|\.git$", "", u).replace("git://", "https://").replace("ssh://git@", "https://")
    return None


def _pypi(t: Target, fetch: Fetch) -> dict:
    d = fetch(f"https://pypi.org/pypi/{quote(t.key)}/json")
    info = d.get("info") or {}
    uploads = [f.get("upload_time_iso_8601") or f.get("upload_time")
               for files in (d.get("releases") or {}).values() for f in files if not f.get("yanked")]
    last = _latest(*map(_date, uploads))
    latest_files = (d.get("releases") or {}).get(info.get("version")) or d.get("urls") or []
    release = _latest(*(_date(f.get("upload_time_iso_8601") or f.get("upload_time")) for f in latest_files))
    inactive = any("Development Status :: 7 - Inactive" in c for c in info.get("classifiers") or [])
    urls = info.get("project_urls") or {}
    return _signal(t, deprecated=inactive, last_release=release, latest_version=info.get("version"),
                   last_activity=last, note="classifier: Development Status :: 7 - Inactive" if inactive else None,
                   repository=_repo_link(*urls.values(), info.get("home_page")))


def _cran(t: Target, fetch: Fetch) -> dict:
    d = fetch(f"https://crandb.r-pkg.org/{quote(t.key)}/all")
    timeline = {k: v for k, v in (d.get("timeline") or {}).items() if k != "archived"}
    latest = d.get("latest")
    release = _date(timeline.get(latest)) if latest else _latest(*map(_date, timeline.values()))
    archived_on = _date((d.get("timeline") or {}).get("archived"))
    desc = ((d.get("versions") or {}).get(latest) or {}) if latest else {}
    return _signal(t, deprecated=bool(d.get("archived")), last_release=release, latest_version=latest,
                   last_activity=_latest(release, archived_on),
                   note=f"archived on CRAN {archived_on}" if d.get("archived") else None,
                   repository=_repo_link(*re.split(r"[\s,]+", str(desc.get("URL") or "")),
                                         desc.get("BugReports")))


_BIOC_BUMP = re.compile(r"bump x\.y\.z version|after creation of RELEASE_", re.I)


def _bioconductor(t: Target, fetch: Fetch) -> dict:
    """Bioconductor through its r-universe mirror (JSON; the package pages are not).
    The core team bumps every package's version twice a year; such a commit is not
    activity, so a package whose last commit is a bump gets no commit date and is
    dated by its own repository (URL/BugReports) instead."""
    d = fetch(f"https://bioc.r-universe.dev/api/packages/{quote(t.key)}")
    c = d.get("_commit") or {}
    bump = bool(_BIOC_BUMP.search(c.get("message") or ""))
    last = None
    if c.get("time") and not bump:
        last = dt.datetime.fromtimestamp(int(c["time"]), dt.timezone.utc).date().isoformat()
    return _signal(t, latest_version=d.get("Version"), last_commit=last, last_activity=last,
                   note="last commit is a Bioconductor release version bump" if bump else None,
                   repository=_repo_link(*re.split(r"[\s,]+", str(d.get("URL") or "")), d.get("BugReports")))


def _npm(t: Target, fetch: Fetch) -> dict:
    name = t.key.replace("/", "%2F") if t.key.startswith("@") else quote(t.key)
    d = fetch(f"https://registry.npmjs.org/{name}")
    latest = (d.get("dist-tags") or {}).get("latest")
    times = d.get("time") or {}
    meta = (d.get("versions") or {}).get(latest) or {}
    repo = d.get("repository") or meta.get("repository")
    return _signal(t, deprecated=bool(meta.get("deprecated")), latest_version=latest,
                   last_release=_date(times.get(latest)),
                   last_activity=_latest(*(_date(v) for k, v in times.items() if k not in ("created", "modified"))),
                   note=f"deprecated: {meta['deprecated']}" if meta.get("deprecated") else None,
                   repository=_repo_link(repo.get("url") if isinstance(repo, dict) else repo))


def _conda(t: Target, fetch: Fetch) -> dict:
    d = fetch(f"https://api.anaconda.org/package/{t.key}")
    uploads = [_date(f.get("upload_time")) for f in d.get("files") or []]
    return _signal(t, latest_version=d.get("latest_version"), last_release=_latest(*uploads),
                   last_activity=_latest(*uploads), repository=_repo_link(d.get("dev_url"), d.get("home")),
                   note="packaging signal: channel rebuilds, not upstream development")


def _date_in(value: Any) -> Optional[str]:
    m = re.search(r"((?:19|20)\d{2}-\d{2}-\d{2})", str(value or ""))
    return m.group(1) if m else None


def _obo(t: Target, fetch: Fetch) -> dict:
    """OBO Foundry registry: its curated activity status (active / inactive /
    orphaned), obsolescence, and the repository; OLS is added for the release date."""
    onts = (fetch(OBO_REGISTRY) or {}).get("ontologies") or []
    if t.key.startswith("title:"):
        want = t.key[6:].strip().lower()
        hit = next((o for o in onts if (o.get("title") or "").strip().lower() == want), None)
    else:
        hit = next((o for o in onts if (o.get("id") or "").lower() == t.key.lower()), None)
    if not hit:
        raise FetchError(404, "not in the OBO Foundry registry")
    oid = hit["id"]
    status = (hit.get("activity_status") or "").lower() or None
    obsolete = bool(hit.get("is_obsolete"))
    note = f"OBO Foundry activity status: {status or 'not given'}" + ("; obsolete" if obsolete else "")
    if t.inferred:
        note += f"; matched by title ({hit.get('title')})"
    return _signal(Target("obofoundry", oid, t.found_in, t.inferred), deprecated=obsolete,
                   declared_status=status, declared_inactive=status in ("inactive", "orphaned"), note=note,
                   links=[u for u in (hit.get("repository"),) if u] + [f"https://www.ebi.ac.uk/ols4/ontologies/{oid}"])


def _ols(t: Target, fetch: Fetch) -> dict:
    """OLS: the loaded release's version; its date (from the version or version IRI)
    is the release date. OLS's own `loaded`/`updated` times are not releases."""
    d = fetch(f"https://www.ebi.ac.uk/ols4/api/ontologies/{quote(t.key)}")
    c = d.get("config") or {}
    version = c.get("version") or d.get("version")
    released = _date_in(version) or _date_in(c.get("versionIri"))
    return _signal(t, latest_version=version, last_release=released, last_activity=released,
                   note=None if released else "OLS gives no release date for this version")


def _rrid(t: Target, fetch: Fetch) -> dict:
    """SciCrunch RRID record: the resource's links (followed: a repository or a
    package often appears) and whether it is marked as no longer available. An RRID
    carries no dates itself."""
    d = fetch(f"https://scicrunch.org/resolver/{quote(t.key, safe=':')}.json")
    hits = (d.get("hits") or {}).get("hits") or []
    if not hits:
        raise FetchError(404, "RRID not found")
    src = hits[0].get("_source") or {}
    dist = src.get("distributions") or {}
    links = [x.get("uri") for k in ("current", "alternate") for x in dist.get(k) or [] if x.get("uri")]
    avail = " ".join(f"{a.get('keyword', '')} {a.get('description', '')}"
                     for a in (src.get("item") or {}).get("availability") or [])
    gone = bool(re.search(r"no longer (?:in service|available|maintained|supported)|discontinued|defunct|retired",
                          avail, re.I))
    item = src.get("item") or {}
    names = [item.get("name")] + [x.get("name") for k in ("synonyms", "abbreviations")
                                  for x in item.get(k) or [] if isinstance(x, dict)]
    return _signal(t, deprecated=gone, links=list(dict.fromkeys(links)),
                   note=f"RRID record: {' '.join(avail.split())[:160]}" if gone else None,
                   record_names=[n for n in names if n])


_STOP = {"the", "of", "for", "and", "a", "an"}


def _words(name: str) -> set:
    core = re.sub(r"\([^)]*\)", " ", name or "")      # an acronym in brackets alone proves nothing
    return {w for w in re.findall(r"[a-z0-9]+", core.lower()) if w not in _STOP}


def names_match(name: str, record_names: Iterable[str]) -> bool:
    """The RRID record is about this resource: one name's words contain the other's
    (Prism / GraphPad Prism, R / R software), ignoring bracketed acronyms."""
    mine = _words(name)
    for other in record_names:
        theirs = _words(other)
        if mine and theirs and (mine <= theirs or theirs <= mine):
            return True
    return False


def _geo_date(text: Optional[str]) -> Optional[str]:
    for fmt in ("%b %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime((text or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _geo(t: Target, fetch: Fetch) -> dict:
    """GEO series: a dataset, so availability and dates, not development activity."""
    text = fetch(f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={quote(t.key)}&targ=self&form=text&view=brief")
    text = text if isinstance(text, str) else ""
    m = re.search(r"scheduled to be released on (\w+ \d{1,2},? \d{4})", text)
    if "!Series_" not in text:
        if m or re.search(r"is currently private", text, re.I):
            rel = _geo_date(m.group(1)) if m else None
            return _signal(t, kind="data", embargoed=True, last_release=rel,
                           note=f"GEO: private until {rel}" if rel else "GEO: private")
        raise FetchError(404, "GEO series not found")

    def field(n):
        f = re.search(rf"^!Series_{n} = (.+)$", text, re.M)
        return f.group(1).strip() if f else None
    status = field("status") or ""
    sm = re.match(r"(Public|Private)\s+(?:on|until)\s+(\w{3} \d{1,2} \d{4})", status)
    public = not sm or sm.group(1) == "Public"
    released = _geo_date(sm.group(2)) if sm else None
    updated = _geo_date(field("last_update_date"))
    return _signal(t, kind="data", embargoed=not public, last_release=released,
                   last_activity=_latest(released, updated), note=f"GEO: {status}" if status else None)


FETCHERS = {"github": _github, "gitlab": _gitlab, "pypi": _pypi, "cran": _cran,
            "bioconductor": _bioconductor, "npm": _npm, "conda": _conda,
            "obofoundry": _obo, "ols": _ols, "rrid": _rrid, "geo": _geo}


def observe(t: Target, fetch: Fetch = http_fetch) -> dict:
    """One source's answer, with the URLs queried and the time it was received
    (provenance: `prov:hadPrimarySource`, `prov:generatedAtTime`)."""
    queried: list[str] = []

    def tracked(url: str) -> Any:
        queried.append(url)
        return fetch(url)
    try:
        s = FETCHERS[t.source](t, tracked)
    except FetchError as e:
        gone = e.status in (404, 410) and not t.inferred   # anything else says nothing about the resource
        note = "no repository at the address the GitHub Pages site suggests" if t.inferred and e.status == 404 else None
        s = _signal(t, available=False if gone else None, error=str(e), http_status=e.status, note=note,
                    rate_limited=e.rate_limited)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        s = _signal(t, available=None, error=f"unexpected response: {e}")
    s["queried"] = list(dict.fromkeys(queried))
    s["observed_at"] = _now()
    return s


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(signals: list[dict], today: dt.date, stale_after: int = STALE_AFTER_DAYS,
             inactive_after: int = INACTIVE_AFTER_DAYS) -> dict:
    """Combine one resource's signals into a status. See the module docstring."""
    ok = [s for s in signals if s["available"]]
    code = [s for s in ok if s["kind"] != "data"]
    data = [s for s in ok if s["kind"] == "data"]
    primary = [s for s in code if s["source"] not in PACKAGING and s["last_activity"]]
    dated = primary or [s for s in code if s["last_activity"]]
    is_data = bool(data) and not dated   # a dataset: judged by availability, not by age
    if is_data:
        dated = [s for s in data if s["last_activity"]]
    last = _latest(*(s["last_activity"] for s in dated))
    out = {"last_activity": last, "basis": [s["page"] for s in dated if s["last_activity"] == last],
           "days_since": (today - dt.date.fromisoformat(last)).days if last else None}
    if any(s["archived"] for s in ok):
        status = "archived"
    elif any(s["deprecated"] for s in ok):
        status = "deprecated"
    elif any(s["declared_inactive"] for s in ok):     # a registry's curated status (OBO Foundry)
        status = "inactive"
    elif signals and all(s["available"] is False for s in signals):
        status = "unavailable"
    elif is_data:
        status = "embargoed" if any(s["embargoed"] for s in data) else "available"
    elif last is None:
        status = "unknown"
    elif out["days_since"] <= stale_after:
        status = "active"
    elif out["days_since"] <= inactive_after:
        status = "stale"
    else:
        status = "inactive"
    out["status"] = status
    return out


def check(found_in: Iterable[str], *, fetch: Fetch = http_fetch, today: Optional[dt.date] = None,
          stale_after: int = STALE_AFTER_DAYS, inactive_after: int = INACTIVE_AFTER_DAYS,
          follow_repository: bool = True, name: Optional[str] = None,
          types: Iterable[str] = ()) -> Optional[dict]:
    """Check every target found in these URLs, identifiers and code snippets (and, as
    marked guesses, the resource's name). None when nothing answered about it."""
    today = today or dt.date.today()
    started_at = _now()
    found = []
    for x in found_in:
        if not x:
            continue
        found += targets_from_url(x) if re.match(r"^\s*https?://", x) else (
            targets_from_identifier(x) or targets_from_code(x))
    targets = _dedupe(found + (targets_from_name(name, types, bool(found)) if name else []))
    if not targets:
        return None
    signals, queue, seen = [], list(targets), set()
    while queue:
        t = queue.pop(0)
        if t.inferred and t.source in _R_NAME_ORDER and any(
                x["inferred"] and x["source"] in _R_NAME_ORDER for x in signals):
            continue                        # the first registry that has the name wins
        s = observe(t, fetch)
        if t.inferred and s["error"]:
            continue                        # a guess that found nothing says nothing
        if s["source"] == "rrid" and name and s.get("record_names") and not names_match(name, s["record_names"]):
            # a wrong RRID (in the paper or the extraction): keep the finding, use nothing from it
            s.update(available=None, deprecated=False, links=[],
                     note=f"RRID names a different resource ({s['record_names'][0]}); not used")
        if (s["source"], s["key"].lower()) in seen:
            continue                        # e.g. an ontology reached by URL and by title
        seen.add((s["source"], s["key"].lower()))
        signals.append(s)
        follow = ([s["repository"]] if s.get("repository") else []) + list(s.get("links") or [])
        for url in follow if follow_repository else []:
            for r in targets_from_url(url):
                if (r.source, r.key.lower()) not in {(x.source, x.key.lower()) for x in targets}:
                    r = Target(r.source, r.key, f"{s['page']} (linked by {t.source})")
                    targets.append(r)
                    queue.append(r)
    if not signals:
        return None
    return {"checked_on": today.isoformat(), "started_at": started_at, "ended_at": _now(),
            "stale_after_days": stale_after,
            "inactive_after_days": inactive_after, **classify(signals, today, stale_after, inactive_after),
            "signals": signals}


# ---------------------------------------------------------------------------
# Graph: read targets from a resource KG, write the observation back
# ---------------------------------------------------------------------------

def _found_in(g: rdflib.Graph, res: URIRef) -> list[str]:
    out = [str(o) for o in g.objects(res, BKR.accessURL)]
    for ident in g.objects(res, ADMS.identifier):
        out += [str(n) for n in g.objects(ident, SKOS.notation)]
    for ex in g.objects(res, BKR.hasUsageExample):
        out += [str(o) for o in g.objects(ex, BKR.exampleURL)]
        out += [str(o) for o in g.objects(ex, BKR.exampleCode)]
    return list(dict.fromkeys(out))


def _types(g: rdflib.Graph, res: URIRef) -> list[str]:
    return [str(t)[len(str(BKR)):] for t in g.objects(res, RDF.type) if str(t).startswith(str(BKR))]


def _name(g: rdflib.Graph, res: URIRef) -> str:
    return str(g.value(res, BKR.resourceName) or g.value(res, RDFS.label) or res)


def _agent(g: rdflib.Graph) -> URIRef:
    """The versioned software agent (NIDM-Results: prov:SoftwareAgent + software version)."""
    version = skill_version()
    agent = URIRef(f"{KB}agent/structsense-resource-activity/{version}")
    g.add((agent, RDF.type, PROV.SoftwareAgent))
    g.add((agent, RDFS.label, Literal(f"structsense resource_activity {version}")))
    g.add((agent, NIDM_SOFTWARE_VERSION, Literal(version)))
    return agent


def new_run(g: rdflib.Graph, *, stale_after: int, inactive_after: int) -> dict:
    """One invocation of the checker: a `bkr:ActivityCheckRun` (cf. nidm:Session). Every
    check in it is `dcterms:isPartOf` the run; nothing earlier is overwritten."""
    started = _now()
    run_id = re.sub(r"[-:]|\.\d+", "", started) + "-" + uuid.uuid4().hex[:6]
    return {"iri": URIRef(f"{KB}activity-check-run/{run_id}"), "id": run_id, "started_at": started,
            "agent": _agent(g), "stale_after_days": stale_after, "inactive_after_days": inactive_after}


def close_run(g: rdflib.Graph, run: dict, n_checks: int) -> None:
    node, lit = run["iri"], (lambda v, t=None: Literal(v, datatype=t))
    g.add((node, RDF.type, BKR.ActivityCheckRun))
    g.add((node, RDFS.label, Literal(f"resource activity check run {run['id']}")))
    g.add((node, PROV.startedAtTime, lit(run["started_at"], XSD.dateTime)))
    g.add((node, PROV.endedAtTime, lit(_now(), XSD.dateTime)))
    g.add((node, PROV.wasAssociatedWith, run["agent"]))
    g.add((node, BKR.staleAfterDays, lit(run["stale_after_days"], XSD.integer)))
    g.add((node, BKR.inactiveAfterDays, lit(run["inactive_after_days"], XSD.integer)))
    g.add((node, BKR.checkCount, lit(n_checks, XSD.integer)))


def add_to_graph(g: rdflib.Graph, res: URIRef, result: dict, run: Optional[dict] = None) -> URIRef:
    """Write one check with full provenance, NIDM-style:

        run (ActivityCheckRun, cf. nidm:Session)
          └ check (ActivityCheck, cf. nidm:Acquisition)  prov:used the resource
               ├ signal (ActivitySignal) per source      prov:wasGeneratedBy check,
               │                                         prov:generatedAtTime, prov:hadPrimarySource API URL
               └ assessment (ActivityAssessment, cf. nidm:AcquisitionObject)
                                                         prov:wasGeneratedBy check, prov:generatedAtTime,
                                                         prov:wasDerivedFrom signals, the status

    Returns the assessment."""
    if run is None:                       # a single check outside annotate(): its own run
        run = new_run(g, stale_after=result["stale_after_days"], inactive_after=result["inactive_after_days"])
        close_run(g, run, 1)
    lit = lambda v, t=None: Literal(v, datatype=t)  # noqa: E731
    check_node = URIRef(f"{res}/activity-check/{run['id']}")
    assessment = URIRef(f"{res}/activity-assessment/{run['id']}")
    g.add((check_node, RDF.type, BKR.ActivityCheck))
    g.add((check_node, DCT.isPartOf, run["iri"]))
    g.add((check_node, PROV.used, res))
    g.add((check_node, PROV.wasAssociatedWith, run["agent"]))
    g.add((check_node, PROV.startedAtTime, lit(result["started_at"], XSD.dateTime)))
    g.add((check_node, PROV.endedAtTime, lit(result["ended_at"], XSD.dateTime)))
    g.add((res, BKR.hasActivityCheck, check_node))

    g.add((assessment, RDF.type, BKR.ActivityAssessment))
    g.add((assessment, PROV.wasGeneratedBy, check_node))
    g.add((assessment, PROV.generatedAtTime, lit(result["ended_at"], XSD.dateTime)))
    g.add((assessment, PROV.wasAttributedTo, run["agent"]))
    g.add((assessment, BKR.assessedResource, res))
    g.add((assessment, BKR.activityStatus, BKR[f"activity-status/{result['status']}"]))
    if result["last_activity"]:
        g.add((assessment, BKR.lastActivityDate, lit(result["last_activity"], XSD.date)))
    g.add((assessment, BKR.staleAfterDays, lit(result["stale_after_days"], XSD.integer)))
    g.add((assessment, BKR.inactiveAfterDays, lit(result["inactive_after_days"], XSD.integer)))
    g.add((res, BKR.hasActivityAssessment, assessment))

    for s in result["signals"]:
        n = URIRef(f"{check_node}/{s['source']}/{re.sub(r'[^A-Za-z0-9._-]+', '-', s['key'])}")
        g.add((n, RDF.type, BKR.ActivitySignal))
        g.add((n, PROV.wasGeneratedBy, check_node))
        if s.get("observed_at"):
            g.add((n, PROV.generatedAtTime, lit(s["observed_at"], XSD.dateTime)))
        for url in s.get("queried") or []:
            g.add((n, PROV.hadPrimarySource, URIRef(url)))
        g.add((check_node, BKR.hasActivitySignal, n))
        g.add((assessment, PROV.wasDerivedFrom, n))
        g.add((n, BKR.activitySource, BKR[f"activity-source/{s['source']}"]))
        g.add((n, BKR.checkedURL, lit(s["page"], XSD.anyURI)))
        g.add((n, BKR.isArchived, lit(bool(s["archived"]))))
        g.add((n, BKR.isDeprecated, lit(bool(s["deprecated"]))))
        if s["kind"] == "data":
            g.add((n, BKR.isEmbargoed, lit(bool(s["embargoed"]))))
        if s["declared_status"]:
            g.add((n, BKR.declaredActivityStatus, Literal(s["declared_status"])))
        if s["available"] is not None:
            g.add((n, BKR.isAvailable, lit(bool(s["available"]))))
        for key, prop in (("last_commit", BKR.lastCommitDate), ("last_release", BKR.lastReleaseDate),
                          ("last_activity", BKR.lastActivityDate)):
            if s[key]:
                g.add((n, prop, lit(s[key], XSD.date)))
        if s["latest_version"]:
            g.add((n, BKR.latestVersion, lit(str(s["latest_version"]))))
        for text in (s["note"], s["error"]):
            if text:
                g.add((n, RDFS.comment, Literal(text)))
    return assessment


def upgrade_legacy(g: rdflib.Graph) -> int:
    """Checks written before assessments existed carried the status on the activity
    and only a date. Move it to a generated ActivityAssessment, keeping the date; the
    history is preserved, not discarded."""
    n = 0
    for c in list(g.subjects(RDF.type, BKR.ActivityCheck)):
        status = g.value(c, BKR.activityStatus)
        if status is None:
            continue
        res = g.value(c, PROV.used)
        when = g.value(c, PROV.endedAtTime)
        a = URIRef(f"{c}/assessment")
        g.add((a, RDF.type, BKR.ActivityAssessment))
        g.add((a, PROV.wasGeneratedBy, c))
        if when is not None:
            g.add((a, PROV.generatedAtTime, when))
        if res is not None:
            g.add((a, BKR.assessedResource, res))
            g.add((res, BKR.hasActivityAssessment, a))
        for p in (BKR.activityStatus, BKR.lastActivityDate, BKR.staleAfterDays, BKR.inactiveAfterDays):
            for o in list(g.objects(c, p)):
                g.add((a, p, o))
                g.remove((c, p, o))
        for sig in g.objects(c, BKR.hasActivitySignal):
            g.add((sig, PROV.wasGeneratedBy, c))
            g.add((a, PROV.wasDerivedFrom, sig))
        old_agent = URIRef(f"{KB}agent/structsense-resource-activity")
        if (c, PROV.wasAssociatedWith, old_agent) in g:
            g.add((old_agent, RDFS.comment, Literal("checker before versioned agents (structsense ≤ 0.12.0)")))
        g.add((a, RDFS.comment, Literal("upgraded from a check recorded before assessments; time is a date only")))
        n += 1
    return n


def annotate(g: rdflib.Graph, *, fetch: Fetch = http_fetch, today: Optional[dt.date] = None,
             cfg: Optional[dict] = None) -> dict:
    """Check every resource in `g` that names a repository or package; add the checks."""
    cfg = cfg or {}
    stale = int(cfg.get("stale_after_days", STALE_AFTER_DAYS))
    inactive = int(cfg.get("inactive_after_days", INACTIVE_AFTER_DAYS))
    report: dict[str, Any] = {"checked": 0, "by_status": {}, "resources": [], "warnings": [],
                              "rate_limited": [], "upgraded": upgrade_legacy(g)}
    unreachable = 0
    fetch = _RateLimits(fetch)
    run = new_run(g, stale_after=stale, inactive_after=inactive)
    for res in sorted(set(g.subjects(RDF.type, BKR.Resource)), key=str):
        targets = _found_in(g, res)
        if unreachable >= OFFLINE_AFTER:
            if any(targets_from_url(x) or targets_from_identifier(x) or targets_from_code(x) for x in targets):
                report["skipped"] = report.get("skipped", 0) + 1
            continue
        result = check(targets, fetch=fetch, today=today, stale_after=stale, inactive_after=inactive,
                       follow_repository=bool(cfg.get("follow_repository", True)),
                       name=_name(g, res), types=_types(g, res))
        if result is None:
            continue
        # no answer at all (no HTTP status): the network, not the resource
        if all(s["error"] and s["http_status"] is None for s in result["signals"]):
            unreachable += 1
            if unreachable >= OFFLINE_AFTER:
                report["warnings"].append(
                    f"activity check stopped: no code host or registry reachable ({OFFLINE_AFTER} resources "
                    "in a row); the rest are unchecked — re-run resource_activity annotate when online")
            continue
        unreachable = 0
        if any(s["rate_limited"] for s in result["signals"]):
            # a partial answer could hide an archived repository: record nothing, so the
            # latest check on record stays the last complete one
            report["rate_limited"].append(_name(g, res))
            continue
        assessment = add_to_graph(g, res, result, run)
        result["assessment"] = str(assessment)
        report["checked"] += 1
        report["by_status"][result["status"]] = report["by_status"].get(result["status"], 0) + 1
        report["resources"].append({"resource": str(res), "name": _name(g, res), **result})
    if report["checked"]:
        close_run(g, run, report["checked"])
        report["run"] = {"iri": str(run["iri"]), "started_at": run["started_at"], "agent": str(run["agent"])}
    if report["rate_limited"]:
        report["warnings"].append(
            f"{len(report['rate_limited'])} resource(s) not checked: rate limited "
            f"({', '.join(sorted(fetch.hosts)) or 'a registry'}). Nothing was recorded for them. Set GITHUB_TOKEN "
            "(5000 requests/hour) and re-run: python -m scripts.resource_activity annotate <ttl>")
    return report


def summary_line(r: dict) -> str:
    since = f", last activity {r['last_activity']} ({r['days_since']} days ago)" if r.get("last_activity") else ""
    return f"{r.get('name', '?')}: {r['status']}{since}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check URLs or install commands; JSON to stdout")
    c.add_argument("found_in", nargs="+")
    a = sub.add_parser("annotate", help="add activity checks to a resource KG (Turtle)")
    a.add_argument("ttl", type=Path)
    a.add_argument("--out", type=Path, help="default: overwrite the input")
    a.add_argument("--report", type=Path)
    for p in (c, a):
        p.add_argument("--stale-after-days", type=int, default=STALE_AFTER_DAYS)
        p.add_argument("--inactive-after-days", type=int, default=INACTIVE_AFTER_DAYS)
    args = ap.parse_args(argv)
    if args.cmd == "check":
        r = check(args.found_in, stale_after=args.stale_after_days, inactive_after=args.inactive_after_days)
        print(json.dumps(r, indent=2) if r else "no repository or package found")
        return 0 if r else 1
    g = rdflib.Graph().parse(args.ttl, format="turtle")
    rep = annotate(g, cfg={"stale_after_days": args.stale_after_days,
                           "inactive_after_days": args.inactive_after_days})
    (args.out or args.ttl).write_text(g.serialize(format="turtle"))
    if args.report:
        args.report.write_text(json.dumps(rep, indent=2))
    for r in rep["resources"]:
        print(summary_line(r))
    print(f"{rep['checked']} resource(s) checked: {rep['by_status']}", file=sys.stderr)
    for w in rep["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
