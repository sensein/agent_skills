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


# --------------------------------------------------------------------------- org-wide brief (sensein/lab-colab#35)

SEARCH_PAGES = 3      # pages of 100 per search query (GitHub caps a search at 1000 results)
STALE_REVIEW_DAYS = 7  # an open PR whose review was requested and that has not moved for this long is "waiting"


def search_all(gh, q: str, budget: Budget, pages: int = SEARCH_PAGES) -> tuple[list[dict], int]:
    """(items, total_count) for one issue search, newest first; items stop at `pages` pages."""
    import urllib.parse  # noqa: PLC0415
    items: list[dict] = []
    total = 0
    for page in range(1, pages + 1):
        budget.spend()
        res = gh.get(f"/search/issues?q={urllib.parse.quote(q)}&sort=updated&order=desc&per_page=100&page={page}",
                     conditional=False)
        if not isinstance(res, dict):
            break
        total = int(res.get("total_count") or 0)
        batch = res.get("items") or []
        items += batch
        if len(batch) < 100:
            break
    return items, total


def _repo_name(item: dict) -> str:
    return str(item.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1]


def org_brief(gh, config: dict, since: str, now_t: datetime | None = None, budget: Budget | None = None,
              board_items: list[dict] | None = None, unassigned: dict | None = None) -> dict:
    """What is going on across `[org] name` since `since`, read now with whatever login or token `gh` holds: one row per
    space (items updated, PRs merged, PRs open, issues opened and closed, PRs waiting on a review, items Blocked on the
    board), releases, repos in no space, and what this login could not read. Restricted repos contribute counts only:
    no titles, names or links. `tier` is the strictest tier of what is shown in detail. Writes nothing."""
    from colab import declared_tiers, field_value, repo_spaces  # noqa: PLC0415
    now_t = now_t or datetime.now(timezone.utc)
    budget = budget or Budget(60)
    org = config["org"]["name"]
    member_of, declared = repo_spaces(config), declared_tiers(config)
    day = since[:10]
    stale = (now_t - timedelta(days=STALE_REVIEW_DAYS)).strftime("%Y-%m-%d")
    queries = {
        "updated": f"org:{org} updated:>={day}",
        "merged": f"org:{org} is:pr is:merged merged:>={day}",
        "open_prs": f"org:{org} is:pr is:open",
        "opened": f"org:{org} is:issue created:>={day}",
        "closed": f"org:{org} is:issue is:closed closed:>={day}",
        "waiting_review": f"org:{org} is:pr is:open review:required updated:<={stale}",
    }
    out = {"org": org, "since": since, "spaces": {}, "no_space": {}, "releases": [], "unassigned": unassigned,
           "notes": [], "tier": "public", "truncated": []}
    counts: dict[str, dict[str, int]] = {}
    shown_tiers: list[str] = []
    try:
        for key, q in queries.items():
            items, total = search_all(gh, q, budget)
            if total > len(items):
                out["truncated"].append(f"{key}: {len(items)} of {total} read")
            for it in items:
                name = _repo_name(it).lower()
                if not name:
                    continue
                row_keys = member_of.get(name) or [None]
                for space in row_keys:
                    bucket = out["spaces"] if space else out["no_space"]
                    label = space or name
                    row = counts.setdefault(f"{'s' if space else 'r'}:{label}", {k: 0 for k in queries})
                    row[key] += 1
                    bucket[label] = row
        for name in sorted({n for n in member_of if "/" not in n}):  # releases in space repos since the period began
            if declared.get(name) == "restricted":
                continue
            budget.spend()
            try:
                rels = gh.get(f"/repos/{org}/{name}/releases?per_page=5", conditional=False)
            except (urllib.error.URLError, OSError):
                rels = None  # one unreadable repo skips its releases, not everyone's
            for rel in rels if isinstance(rels, list) else []:
                if rel.get("draft") or str(rel.get("published_at") or "") < since:
                    continue
                out["releases"].append({"repo": name, "tag": rel.get("tag_name", ""), "at": _day(rel.get("published_at")),
                                        "url": rel.get("html_url", ""), "spaces": member_of.get(name, [])})
                shown_tiers.append(declared.get(name) or "lab")
    except DetailBudget:
        out["notes"].append("the call budget ran out; the brief is partial")
    except (urllib.error.URLError, OSError) as err:
        out["notes"].append(f"stopped early: {str(err)[:200]}")
    if board_items is not None:  # Blocked items on the board, per space (the board shows only what this login can see)
        for it in board_items:
            if str(field_value(it, "Status") or "").strip().lower() != "blocked":
                continue
            spaces = field_value(it, "Spaces")
            for space in (spaces if isinstance(spaces, list) else [x.strip() for x in str(spaces or "").split(",") if x.strip()]):
                if space in out["spaces"] or space in (config.get("spaces") or {}):
                    row = out["spaces"].setdefault(space, {k: 0 for k in queries})
                    row["blocked"] = row.get("blocked", 0) + 1
    else:
        out["notes"].append("the board was not read: Blocked counts are missing")
    for space, spec in (config.get("spaces") or {}).items():
        out["spaces"].setdefault(space, {k: 0 for k in queries})
        restricted = any(declared.get(r) == "restricted" for r in member_of if space in member_of[r]) or spec.get("tier") == "restricted"
        out["spaces"][space]["restricted"] = restricted
        names = [r for r in member_of if space in member_of[r]]
        out["spaces"][space]["external_only"] = bool(names) and all("/" in r for r in names)  # other orgs' repos: not searched
        if not restricted and any(out["spaces"][space].get(k) for k in queries):
            shown_tiers.append("lab")
    for name in list(out["no_space"]):
        if declared.get(name) == "restricted":
            out["no_space"].pop(name)  # never named; restricted repos are in a space or counted by `unassigned`
    if out["no_space"]:
        shown_tiers.append("lab")
    out["tier"] = max(shown_tiers or ["public"], key=lambda t: {"public": 0, "lab": 1, "restricted": 2}[t])
    return out


def brief_lines(view: dict) -> list[str]:
    """Plain text for a person or a digest: one line per space, then releases, repos active outside any space, repos
    in no space, and notes. Restricted spaces show counts only (they never had names in the view)."""
    def fmt(row: dict) -> str:
        parts = [f"{row.get('updated', 0)} items updated", f"{row.get('merged', 0)} PRs merged",
                 f"{row.get('open_prs', 0)} PRs open", f"{row.get('opened', 0)} issues opened",
                 f"{row.get('closed', 0)} closed"]
        if row.get("waiting_review"):
            parts.append(f"{row['waiting_review']} PRs waiting on a review over {STALE_REVIEW_DAYS} days")
        if row.get("blocked"):
            parts.append(f"{row['blocked']} Blocked")
        return ", ".join(parts)
    lines = [f"What's new in {view['org']} since {view['since'][:10]} (tier: {view['tier']})"]
    active = sorted(view["spaces"].items(), key=lambda kv: -kv[1].get("updated", 0))
    for space, row in active:
        if row.get("updated", 0) or row.get("blocked") or row.get("open_prs"):
            lines.append(f"- {space}{' (restricted: counts only)' if row.get('restricted') else ''}: {fmt(row)}")
    idle = [(s, row) for s, row in active if not (row.get("updated", 0) or row.get("blocked") or row.get("open_prs"))]
    quiet = [s for s, row in idle if not row.get("external_only")]
    external = [s for s, row in idle if row.get("external_only")]
    if quiet:
        lines.append(f"- quiet: {', '.join(quiet)}")
    if external:
        lines.append(f"- not covered (their repos are in other orgs): {', '.join(external)}")
    if view["releases"]:
        lines.append("Releases: " + ", ".join(f"{r['repo']} {r['tag']} ({r['at']})" for r in view["releases"]))
    if view["no_space"]:
        top = sorted(((n, row) for n, row in view["no_space"].items() if row.get("updated", 0)), key=lambda kv: -kv[1]["updated"])[:10]
        if top:
            lines.append("Active outside any space: " + ", ".join(f"{name} ({row['updated']} updated)" for name, row in top))
    if view.get("unassigned"):
        lines += ["Repos in no space: " + x for x in unassigned_lines(view["unassigned"])]
    lines += [f"Note: {n}" for n in view["notes"] + view.get("truncated", [])]
    return lines
