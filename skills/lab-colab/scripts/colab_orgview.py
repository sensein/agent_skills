"""Org-wide views shared by `brief --org`, `colab.py unassigned`, `unblock` and the nightly digest: which of the org's
repos belong to no space (sensein/lab-colab#34). Read-only, under whatever login or token the caller holds; a repo the
caller cannot read is simply not listed.

"Unassigned" = a non-archived repo of `[org] name` in no space, not in `[ignored] repos`, and not dormant (pushed in
the last `active_days`). It stays listed until it is put in a space or ignored: being reported does not clear it.
When each one was first seen is tracked automatically, with no hand-kept date: the nightly digest keeps a small
state block (repo -> first_seen, last_reported) in its own issue body; a member's run reads that block when it can,
else uses each repo's creation date. Restricted repos are never named, in the output or in the state block.
"""
from __future__ import annotations

import json
import re
import urllib.error
from datetime import date, datetime, timedelta, timezone

from colab_sync import Budget, DetailBudget

ACTIVE_DAYS = 30
NEW_DAYS = 7          # first seen this recently: "New"
ASK_DAYS = 14         # unassigned this long: an `unblock` ask for the triage space's stewards
TRIAGE_SPACE = "lab-infra"
MARKER = "<!-- colab-digest -->"
STATE_RE = re.compile(r"<!-- colab-digest-state (\{.*?\}) -->", re.S)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _day(value: str | None) -> str:
    return str(value or "")[:10]


def _days_between(earlier: str, now_t: datetime) -> int:
    try:
        return max(0, (now_t.date() - date.fromisoformat(earlier[:10])).days)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- the state block

def parse_state(body: str | None) -> dict:
    """The digest's state block from an issue body, validated: {"repos": {name: {"first_seen", "last_reported"}}}.
    Missing, malformed or hand-edited blocks give a fresh state (entries that don't validate are dropped)."""
    m = STATE_RE.search(str(body or ""))
    if not m:
        return {"repos": {}}
    try:
        raw = json.loads(m[1])
    except ValueError:
        return {"repos": {}}
    repos = raw.get("repos") if isinstance(raw, dict) else None
    out: dict[str, dict] = {}
    for name, entry in (repos.items() if isinstance(repos, dict) else []):
        if not isinstance(name, str) or not NAME_RE.match(name) or not isinstance(entry, dict):
            continue
        first = str(entry.get("first_seen") or "")
        last = str(entry.get("last_reported") or first)
        if DAY_RE.match(first) and DAY_RE.match(last):
            out[name.lower()] = {"first_seen": first, "last_reported": last}
    return {"repos": out}


def render_state(state: dict) -> str:
    """The state block as an HTML comment. Every "-" is written as \\u002d, so no repo name can close the comment."""
    body = json.dumps({"v": 1, "repos": dict(sorted((state.get("repos") or {}).items()))}, sort_keys=True,
                      ensure_ascii=True, separators=(",", ":"))
    return f"<!-- colab-digest-state {body.replace('-', chr(92) + 'u002d')} -->"


def next_state(view: dict, previous: dict, now_t: datetime) -> dict:
    """The state after this run: every repo listed now keeps its first_seen (or gets one) and is stamped
    last_reported today; repos no longer unassigned (now in a space, ignored, archived or dormant) drop out.
    Only names in the view are written, and the view never names restricted repos."""
    today = now_t.strftime("%Y-%m-%d")
    old = previous.get("repos") or {}
    repos = {}
    for r in view.get("unassigned") or []:
        name = r["name"].lower()
        repos[name] = {"first_seen": (old.get(name) or {}).get("first_seen") or r["first_seen"], "last_reported": today}
    return {"repos": repos}


def read_digest_state(gh, config: dict, budget: Budget | None = None) -> tuple[dict | None, dict | None]:
    """(issue, state) for the open digest issue in the meta repo, if this login can read it, preferring the one the
    scheduled Action opened. (None, None) when there is none or it cannot be read."""
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    found = []
    try:
        for page in range(1, 4):
            if budget:
                budget.spend()
            batch = gh.get(f"/repos/{org}/{meta}/issues?state=open&sort=updated&direction=desc&per_page=100&page={page}",
                           conditional=False)
            batch = batch if isinstance(batch, list) else []
            found += [it for it in batch if "pull_request" not in it and MARKER in str(it.get("body") or "")]
            if len(batch) < 100:
                break
    except (DetailBudget, urllib.error.URLError, OSError):
        return None, None
    if not found:
        return None, None
    bot = [it for it in found if str((it.get("user") or {}).get("login") or "").lower() == "github-actions[bot]"]
    issue = (bot or found)[0]
    return issue, parse_state(issue.get("body"))


# --------------------------------------------------------------------------- repos in no space

def spaces_revised(config: dict) -> str | None:
    """`[org] spaces_revised`, optional: a floor for "new" (repos first seen before it are never new)."""
    value = (config.get("org") or {}).get("spaces_revised")
    return str(value)[:10] if value else None


def org_repos(gh, org: str, budget: Budget, pages: int = 20) -> list[dict]:
    """The org's repos this login can see, archived ones left out."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        budget.spend()
        batch = gh.get(f"/orgs/{org}/repos?type=all&per_page=100&page={page}", conditional=False)
        batch = batch if isinstance(batch, list) else []
        out += [r for r in batch if isinstance(r, dict) and not r.get("archived")]
        if len(batch) < 100:
            break
    return out


def unassigned_repos(gh, config: dict, now_t: datetime | None = None, since: str | None = None,
                     active_days: int = ACTIVE_DAYS, budget: Budget | None = None, state: dict | None = None) -> dict:
    """The org's repos in no space. `unassigned` (each: name, visibility, created, pushed, first_seen, age_days, url,
    new) is every non-archived, non-ignored, non-dormant repo in no space; `new` names those first seen in the last
    NEW_DAYS (or since `since`; never before `[org] spaces_revised` when set). first_seen comes from `state` (the
    digest's block) when given, else the repo's creation date. `ignored` lists the repos left out on purpose, with the
    reason; `restricted` and `dormant` are counts (dormant names under `dormant_names`)."""
    from colab import declared_tiers, repo_spaces  # noqa: PLC0415
    now_t = now_t or datetime.now(timezone.utc)
    budget = budget or Budget(40)
    org = config["org"]["name"]
    out = {"active_days": active_days, "new_days": NEW_DAYS, "unassigned": [], "new": [], "ignored": [], "restricted": 0,
           "dormant": 0, "dormant_names": [], "total": 0, "in_spaces": 0,
           "first_seen_from": "digest state" if state is not None else "creation dates", "notes": []}
    try:
        repos = org_repos(gh, org, budget)
    except DetailBudget:
        out["notes"].append("the call budget ran out listing the org's repos; the list is partial")
        repos = []
    except (urllib.error.URLError, OSError) as err:
        out["notes"].append(f"could not list the org's repos: {str(err)[:200]}")
        repos = []
    member_of, declared = repo_spaces(config), declared_tiers(config)
    ignored = {str(k).lower(): str(v) for k, v in ((config.get("ignored") or {}).get("repos") or {}).items()}
    seen = (state or {}).get("repos") or {}
    cutoff = (now_t - timedelta(days=active_days)).strftime("%Y-%m-%d")
    new_from = max(filter(None, [_day(since) if since else (now_t - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%d"),
                                 spaces_revised(config)]))
    out["total"] = len(repos)
    for r in repos:
        name = str(r.get("name") or "")
        key = name.lower()
        if key in member_of:  # repo keys are lower-cased
            out["in_spaces"] += 1
            continue
        if declared.get(key) == "restricted":
            out["restricted"] += 1  # declared restricted: a count only, never the name
            continue
        entry = {"name": name, "visibility": str(r.get("visibility") or ("private" if r.get("private") else "public")),
                 "created": _day(r.get("created_at")), "pushed": _day(r.get("pushed_at")), "url": r.get("html_url", "")}
        if key in ignored:
            out["ignored"].append({**entry, "reason": ignored[key]})
        elif entry["pushed"] < cutoff:
            out["dormant"] += 1
            out["dormant_names"].append(name)
        else:
            first = (seen.get(key) or {}).get("first_seen") or entry["created"]
            entry.update(first_seen=first, age_days=_days_between(first, now_t), new=first >= new_from)
            out["unassigned"].append(entry)
    out["unassigned"].sort(key=lambda e: e["first_seen"], reverse=True)
    out["new"] = [e["name"] for e in out["unassigned"] if e["new"]]
    out["dormant_names"].sort()
    return out


def unassigned_lines(view: dict, limit: int = 15) -> list[str]:
    """Plain-text lines for a brief or a digest: new repos in no space, the ones still unassigned with their age, and
    the rest as counts."""
    lines = []
    new = [e for e in view["unassigned"] if e["new"]]
    still = sorted((e for e in view["unassigned"] if not e["new"]), key=lambda e: -e["age_days"])
    if new:
        shown = ", ".join(f"{e['name']} ({e['visibility']}, created {e['created']})" for e in new[:limit])
        lines.append(f"New (first seen in the last {view['new_days']} days), {len(new)}: {shown}"
                     + (f", and {len(new) - limit} more" if len(new) > limit else ""))
    if still:
        shown = ", ".join(f"{e['name']} ({e['age_days']} days)" for e in still[:limit])
        lines.append(f"Still unassigned, {len(still)}: {shown}" + (f", and {len(still) - limit} more" if len(still) > limit else ""))
    if view.get("dormant"):
        lines.append(f"{view['dormant']} older repos in no space and not pushed in {view['active_days']} days")
    if view.get("restricted"):
        lines.append(f"{view['restricted']} restricted repos in no space (count only)")
    if view.get("ignored"):
        lines.append(f"{len(view['ignored'])} left out on purpose ([ignored] repos)")
    return lines


def triage_asks(view: dict, config: dict, login: str) -> list[dict]:
    """For a steward of the triage space (`[org] triage_space`, default lab-infra): one ask per repo unassigned for
    more than ASK_DAYS, "assign or ignore this repo". Empty for anyone else."""
    from colab import item_stewards  # noqa: PLC0415
    space = (config.get("org") or {}).get("triage_space") or TRIAGE_SPACE
    if space not in (config.get("spaces") or {}) or login.lower() not in item_stewards(config, [space]):
        return []
    return [{"kind": "unassigned_repo", "item": e["name"], "url": e["url"], "tier": "lab", "summary": "",
             "who": [], "since": e["first_seen"], "age_days": float(e["age_days"]), "holds_up": 0, "untrusted": False,
             "visibility": e["visibility"]}
            for e in view["unassigned"] if e["age_days"] > ASK_DAYS]
