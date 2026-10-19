#!/usr/bin/env python3
"""Activity status of software resources: is the code still maintained?

A paper says where its code lives; it cannot say whether anyone still works on it.
This step asks the hosting service and the package registries, and records what they
answered as a dated observation — never as a claim of the paper.

Targets come from what the resource graph already states: a resource's
`bkr:accessURL`, URL-valued identifiers, usage-example URLs, and install commands in
usage-example code (`pip install X`, `install.packages("X")`,
`BiocManager::install("X")`, `conda install -c bioconda X`, `npm install X`). A
package registry that names its own source repository (PyPI `project_urls`, npm
`repository`, conda `dev_url`) adds that repository as a target too.

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

Status (`https://brainkb.org/resource/activity-status/`), first rule that applies:
archived (the host says so) > deprecated (a registry says so) > unavailable (every
target is gone: 404) > unknown (no date could be read) > active (last activity within
`stale_after_days`) > stale (within `inactive_after_days`) > inactive.

The paper-stated `bkr:lifecycleStatus` of a version is left alone: this is a separate,
dated `bkr:ActivityCheck` on the resource, one per check date, so re-running later
adds a new observation instead of overwriting the old one.

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
AGENT = URIRef("https://brainkb.org/kb/agent/structsense-resource-activity")

STALE_AFTER_DAYS = 365
INACTIVE_AFTER_DAYS = 3 * 365
_TIMEOUT = 20
_DELAY_SECONDS = 0.2
STATUSES = ("active", "stale", "inactive", "archived", "deprecated", "unavailable", "unknown")
PACKAGING = {"conda"}  # dates that reflect rebuilds, not development
OFFLINE_AFTER = 3     # resources in a row with no answer at all: stop, the network is down


# ---------------------------------------------------------------------------
# Targets: where to ask
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    source: str          # github | gitlab | pypi | cran | bioconductor | npm | conda
    key: str             # owner/repo, package name, channel/name
    found_in: str        # the URL or code it was read from
    inferred: bool = False   # derived from another address (GitHub Pages): absence is not "gone"

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
        else:
            key = m.group(1).rstrip("/")
        out.append(Target(source, key, url))
        break
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
    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status


Fetch = Callable[[str], Any]   # url -> parsed JSON (or text for .html); raises FetchError


def http_fetch(url: str) -> Any:
    headers = {"User-Agent": "structsense-skill/1.0", "Accept": "application/json"}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    time.sleep(_DELAY_SECONDS)
    try:
        with urlopen(Request(url, headers=headers), timeout=_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except HTTPError as e:
        hint = ""
        if e.code == 403 and "api.github.com" in url:
            hint = " (rate limited? set GITHUB_TOKEN)"
        raise FetchError(e.code, f"HTTP {e.code}{hint}") from None
    except (URLError, TimeoutError, OSError) as e:
        raise FetchError(None, str(e)) from None
    return body if url.endswith(".html") else json.loads(body)


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
         "repository": None, "note": None, "error": None, "http_status": None}
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


FETCHERS = {"github": _github, "gitlab": _gitlab, "pypi": _pypi, "cran": _cran,
            "bioconductor": _bioconductor, "npm": _npm, "conda": _conda}


def observe(t: Target, fetch: Fetch = http_fetch) -> dict:
    try:
        return FETCHERS[t.source](t, fetch)
    except FetchError as e:
        gone = e.status in (404, 410) and not t.inferred   # anything else says nothing about the resource
        note = "no repository at the address the GitHub Pages site suggests" if t.inferred and e.status == 404 else None
        return _signal(t, available=False if gone else None, error=str(e), http_status=e.status, note=note)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        return _signal(t, available=None, error=f"unexpected response: {e}")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(signals: list[dict], today: dt.date, stale_after: int = STALE_AFTER_DAYS,
             inactive_after: int = INACTIVE_AFTER_DAYS) -> dict:
    """Combine one resource's signals into a status. See the module docstring."""
    ok = [s for s in signals if s["available"]]
    primary = [s for s in ok if s["source"] not in PACKAGING and s["last_activity"]]
    dated = primary or [s for s in ok if s["last_activity"]]
    last = _latest(*(s["last_activity"] for s in dated))
    out = {"last_activity": last, "basis": [s["page"] for s in dated if s["last_activity"] == last],
           "days_since": (today - dt.date.fromisoformat(last)).days if last else None}
    if any(s["archived"] for s in ok):
        status = "archived"
    elif any(s["deprecated"] for s in ok):
        status = "deprecated"
    elif signals and all(s["available"] is False for s in signals):
        status = "unavailable"
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
          follow_repository: bool = True) -> Optional[dict]:
    """Check every target found in these URLs / code snippets. None when there is none."""
    today = today or dt.date.today()
    targets = _dedupe(t for x in found_in if x
                      for t in (targets_from_url(x) if re.match(r"^\s*https?://", x) else targets_from_code(x)))
    if not targets:
        return None
    signals, queue = [], list(targets)
    while queue:
        t = queue.pop(0)
        s = observe(t, fetch)
        signals.append(s)
        if follow_repository and s.get("repository"):
            for r in targets_from_url(s["repository"]):
                if (r.source, r.key.lower()) not in {(x.source, x.key.lower()) for x in targets}:
                    r = Target(r.source, r.key, f"{t.page} (repository named by {t.source})")
                    targets.append(r)
                    queue.append(r)
    return {"checked_on": today.isoformat(), "stale_after_days": stale_after,
            "inactive_after_days": inactive_after, **classify(signals, today, stale_after, inactive_after),
            "signals": signals}


# ---------------------------------------------------------------------------
# Graph: read targets from a resource KG, write the observation back
# ---------------------------------------------------------------------------

def _found_in(g: rdflib.Graph, res: URIRef) -> list[str]:
    out = [str(o) for o in g.objects(res, BKR.accessURL)]
    for ident in g.objects(res, ADMS.identifier):
        out += [str(n) for n in g.objects(ident, SKOS.notation) if str(n).startswith("http")]
    for ex in g.objects(res, BKR.hasUsageExample):
        out += [str(o) for o in g.objects(ex, BKR.exampleURL)]
        out += [str(o) for o in g.objects(ex, BKR.exampleCode)]
    return list(dict.fromkeys(out))


def _name(g: rdflib.Graph, res: URIRef) -> str:
    return str(g.value(res, BKR.resourceName) or g.value(res, RDFS.label) or res)


def add_to_graph(g: rdflib.Graph, res: URIRef, result: dict) -> URIRef:
    day = result["checked_on"]
    node = URIRef(f"{res}/activity-check/{day}")
    for p, o in list(g.predicate_objects(node)):           # same-day re-run replaces
        if p == BKR.hasActivitySignal:
            g.remove((o, None, None))
    g.remove((node, None, None))
    lit = lambda v, t=None: Literal(v, datatype=t)  # noqa: E731
    g.add((node, RDF.type, BKR.ActivityCheck))
    g.add((node, PROV.used, res))
    g.add((node, PROV.wasAssociatedWith, AGENT))
    g.add((node, PROV.endedAtTime, lit(day, XSD.date)))
    g.add((node, BKR.activityStatus, BKR[f"activity-status/{result['status']}"]))
    if result["last_activity"]:
        g.add((node, BKR.lastActivityDate, lit(result["last_activity"], XSD.date)))
    g.add((node, BKR.staleAfterDays, lit(result["stale_after_days"], XSD.integer)))
    g.add((node, BKR.inactiveAfterDays, lit(result["inactive_after_days"], XSD.integer)))
    g.add((res, BKR.hasActivityCheck, node))
    g.add((AGENT, RDF.type, PROV.SoftwareAgent))
    g.add((AGENT, RDFS.label, Literal("structsense resource_activity")))
    for s in result["signals"]:
        n = URIRef(f"{node}/{s['source']}/{re.sub(r'[^A-Za-z0-9._-]+', '-', s['key'])}")
        g.add((n, RDF.type, BKR.ActivitySignal))
        g.add((node, BKR.hasActivitySignal, n))
        g.add((n, BKR.activitySource, BKR[f"activity-source/{s['source']}"]))
        g.add((n, BKR.checkedURL, lit(s["page"], XSD.anyURI)))
        g.add((n, BKR.isArchived, lit(bool(s["archived"]))))
        g.add((n, BKR.isDeprecated, lit(bool(s["deprecated"]))))
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
    return node


def annotate(g: rdflib.Graph, *, fetch: Fetch = http_fetch, today: Optional[dt.date] = None,
             cfg: Optional[dict] = None) -> dict:
    """Check every resource in `g` that names a repository or package; add the checks."""
    cfg = cfg or {}
    stale = int(cfg.get("stale_after_days", STALE_AFTER_DAYS))
    inactive = int(cfg.get("inactive_after_days", INACTIVE_AFTER_DAYS))
    report: dict[str, Any] = {"checked": 0, "by_status": {}, "resources": [], "warnings": []}
    unreachable = 0
    for res in sorted(set(g.subjects(RDF.type, BKR.Resource)), key=str):
        targets = _found_in(g, res)
        if unreachable >= OFFLINE_AFTER:
            if check(targets, fetch=lambda _u: {}, today=today) is not None:
                report["skipped"] = report.get("skipped", 0) + 1
            continue
        result = check(targets, fetch=fetch, today=today, stale_after=stale, inactive_after=inactive,
                       follow_repository=bool(cfg.get("follow_repository", True)))
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
        add_to_graph(g, res, result)
        report["checked"] += 1
        report["by_status"][result["status"]] = report["by_status"].get(result["status"], 0) + 1
        report["resources"].append({"resource": str(res), "name": _name(g, res), **result})
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
