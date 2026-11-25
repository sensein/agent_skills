"""Org-wide views shared by `brief --org`, `colab.py unassigned` and the nightly digest: which of the org's repos
belong to no space (sensein/lab-colab#34). Read-only, under whatever login or token the caller holds; a repo the
caller cannot read is simply not listed.
"""
from __future__ import annotations

import urllib.error
from datetime import datetime, timedelta, timezone

from colab_sync import Budget, DetailBudget

ACTIVE_DAYS = 30


def _day(value: str | None) -> str:
    return str(value or "")[:10]


def spaces_revised(gh, config: dict, budget: Budget) -> str | None:
    """When the spaces were last revised: `[org] spaces_revised` (a date), else the last commit to the meta repo's
    `.colab/config.toml` (a proxy: any config change counts). None when neither is known."""
    declared = (config.get("org") or {}).get("spaces_revised")
    if declared:
        return str(declared)
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    try:
        budget.spend()
        commits = gh.get(f"/repos/{org}/{meta}/commits?path=.colab/config.toml&per_page=1", conditional=False)
    except (urllib.error.URLError, OSError):
        return None
    if isinstance(commits, list) and commits:
        return (((commits[0].get("commit") or {}).get("committer") or {}).get("date")) or None
    return None


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
                     active_days: int = ACTIVE_DAYS, budget: Budget | None = None) -> dict:
    """Repos of `[org] name` in no space, split into `new` (created since the spaces were last revised, or `since`),
    `active` (pushed in the last `active_days`), `ignored` (listed on purpose in `[ignored] repos`, with the reason)
    and a count of the rest (`dormant`, names under `dormant_names`). Each entry: name, visibility, created, pushed,
    url. Private repos are listed by name only, which the org's members can already see."""
    from colab import declared_tiers, repo_spaces  # noqa: PLC0415
    now_t = now_t or datetime.now(timezone.utc)
    budget = budget or Budget(40)
    org = config["org"]["name"]
    out = {"since": None, "active_days": active_days, "new": [], "active": [], "ignored": [], "dormant": 0, "restricted": 0,
           "dormant_names": [], "total": 0, "in_spaces": 0, "notes": []}
    try:
        out["since"] = since or spaces_revised(gh, config, budget)
        repos = org_repos(gh, org, budget)
    except DetailBudget:
        out["notes"].append("the call budget ran out listing the org's repos; the list is partial")
        repos = []
    except (urllib.error.URLError, OSError) as err:
        out["notes"].append(f"could not list the org's repos: {str(err)[:200]}")
        repos = []
    member_of, declared = repo_spaces(config), declared_tiers(config)
    ignored = {str(k).lower(): str(v) for k, v in ((config.get("ignored") or {}).get("repos") or {}).items()}
    cutoff = (now_t - timedelta(days=active_days)).strftime("%Y-%m-%d")
    since_day = _day(out["since"])
    out["total"] = len(repos)
    for r in sorted(repos, key=lambda r: str(r.get("created_at") or ""), reverse=True):
        name = str(r.get("name") or "")
        if name.lower() in member_of:  # repo keys are lower-cased
            out["in_spaces"] += 1
            continue
        if declared.get(name.lower()) == "restricted":
            out["restricted"] += 1  # declared restricted: a count only, never the name
            continue
        entry = {"name": name, "visibility": str(r.get("visibility") or ("private" if r.get("private") else "public")),
                 "created": _day(r.get("created_at")), "pushed": _day(r.get("pushed_at")), "url": r.get("html_url", "")}
        if name.lower() in ignored:
            out["ignored"].append({**entry, "reason": ignored[name.lower()]})
        elif since_day and entry["created"] >= since_day:
            out["new"].append(entry)
        elif entry["pushed"] >= cutoff:
            out["active"].append(entry)
        else:
            out["dormant"] += 1
            out["dormant_names"].append(name)
    out["dormant_names"].sort()
    return out


def unassigned_lines(view: dict, limit: int = 15) -> list[str]:
    """Plain-text lines for a brief or a digest: new and active repos in no space, and the rest as a count."""
    lines = []
    for label, key in (("new since the spaces were last revised" + (f" ({_day(view['since'])})" if view.get("since") else ""), "new"),
                       (f"active in the last {view['active_days']} days", "active")):
        rows = view[key]
        if rows:
            shown = ", ".join(f"{r['name']} ({r['visibility']}, created {r['created']})" for r in rows[:limit])
            more = f", and {len(rows) - limit} more" if len(rows) > limit else ""
            lines.append(f"{len(rows)} {label}: {shown}{more}")
    if view.get("dormant"):
        lines.append(f"{view['dormant']} older repos in no space and not pushed in {view['active_days']} days")
    if view.get("restricted"):
        lines.append(f"{view['restricted']} restricted repos in no space (count only)")
    if view.get("ignored"):
        lines.append(f"{len(view['ignored'])} left out on purpose ([ignored] repos)")
    return lines
