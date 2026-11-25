"""The nightly digest: one issue in the meta repo, rebuilt each night from the org-wide brief, what is waiting on whom
(review requests left for over a week), and the repos in no space. The same code runs anywhere: a scheduled GitHub
Action on the default GITHUB_TOKEN, the same Action with a supplied token, or a member running
`colab.py digest` with their own login.

Token fallback: whatever `gh` holds decides what is covered. A repo the token cannot read is listed as "not readable
by this token" (a count, for restricted repos), never treated as quiet, and never an error.

Safety: the digest goes to the meta repo (lab tier), so restricted repos appear as counts only, and the digest is
refused if anything restricted would be shown in detail. The text is leak-scanned before it is posted, and the only
write is the digest issue itself (created once, then its body is replaced).
"""
from __future__ import annotations

import re
import urllib.error
from datetime import datetime, timedelta, timezone

from colab_sync import Budget, DetailBudget, _repo_vis

MARKER = "<!-- colab-digest -->"
SINCE_MARK = re.compile(r"<!-- colab-digest-at (\S+) -->")
TITLE = "Nightly digest"
WAIT_DAYS = 7
STALE_DAYS = 180  # older review waits are counted, not listed: they need parking, not a nudge
NOBODY = "review required, nobody requested"


def coverage(gh, config: dict, budget: Budget) -> dict:
    """Which of the lab's own space repos this token can read: {"readable": n, "unreadable": [names], "restricted": n}.
    Repos declared restricted are counted, never named."""
    from colab import declared_tiers, is_external, repo_spaces  # noqa: PLC0415
    org = config["org"]["name"]
    declared = declared_tiers(config)
    out = {"readable": 0, "unreadable": [], "unreadable_restricted": 0}
    for key in sorted(k for k in repo_spaces(config) if not is_external(k)):
        try:
            vis = _repo_vis(gh, f"{org}/{key}", budget)
        except DetailBudget:
            raise
        except (urllib.error.URLError, OSError):
            vis = None
        if vis:
            out["readable"] += 1
        elif declared.get(key) == "restricted":
            out["unreadable_restricted"] += 1
        else:
            out["unreadable"].append(key)
    return out


def waiting_reviews(gh, config: dict, budget: Budget, now_t: datetime) -> dict:
    """Open PRs in the org with a review requested and no movement for WAIT_DAYS, grouped by the requested reviewer:
    {login: [ "repo#n (d days)" ...]}. Restricted repos are counted under "restricted". No titles."""
    import urllib.parse  # noqa: PLC0415
    from colab import declared_tiers  # noqa: PLC0415
    org = config["org"]["name"]
    declared = declared_tiers(config)
    stale = (now_t - timedelta(days=WAIT_DAYS)).strftime("%Y-%m-%d")
    budget.spend()
    res = gh.get(f"/search/issues?q={urllib.parse.quote(f'org:{org} is:pr is:open review:required updated:<={stale}')}"
                 "&sort=updated&order=asc&per_page=50&page=1", conditional=False)
    out: dict[str, list[str]] = {}
    restricted = stale_n = 0
    for it in ((res or {}).get("items") or []) if isinstance(res, dict) else []:
        repo = str(it.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1]
        if declared.get(repo.lower()) == "restricted":
            restricted += 1
            continue
        budget.spend()
        rr = gh.get(f"/repos/{org}/{repo}/pulls/{it['number']}/requested_reviewers", conditional=False)
        people = [u.get("login") for u in (rr or {}).get("users") or []] if isinstance(rr, dict) else []
        teams = [f"team:{t.get('slug')}" for t in (rr or {}).get("teams") or []] if isinstance(rr, dict) else []
        try:
            days = (now_t - datetime.fromisoformat(str(it.get("updated_at")).replace("Z", "+00:00"))).days
        except ValueError:
            days = 0
        if days > STALE_DAYS:
            stale_n += 1
            continue
        for who in (people + teams) or [NOBODY]:
            out.setdefault(str(who), []).append(f"{repo}#{it['number']} ({days} days)")
    return {"by_reviewer": dict(sorted(out.items(), key=lambda kv: (kv[0] == NOBODY, -len(kv[1])))), "restricted": restricted,
            "stale": stale_n}


def build(gh, config: dict, now_t: datetime | None = None, since: str | None = None, budget: Budget | None = None,
          token_kind: str = "member", board_items: list[dict] | None = None) -> dict:
    """{"markdown", "tier", "since", "coverage", "brief", "waiting"} for the digest. Writes nothing."""
    import colab_orgview  # noqa: PLC0415
    now_t = now_t or datetime.now(timezone.utc)
    budget = budget or Budget(250)
    since = since or (now_t - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes: list[str] = []
    try:
        cov = coverage(gh, config, budget)
    except DetailBudget:
        cov, notes = {"readable": 0, "unreadable": [], "unreadable_restricted": 0}, ["budget ran out checking coverage"]
    view = colab_orgview.org_brief(gh, config, since, now_t=now_t, budget=budget, board_items=board_items,
                                   unassigned=colab_orgview.unassigned_repos(gh, config, now_t=now_t, budget=budget))
    try:
        waiting = waiting_reviews(gh, config, budget, now_t)
    except DetailBudget:
        waiting, notes = {"by_reviewer": {}, "restricted": 0, "stale": 0}, notes + ["budget ran out reading review requests"]
    if view["tier"] == "restricted":
        raise ValueError("the digest would show restricted content in detail; refusing to build it")
    lines = [MARKER, f"<!-- colab-digest-at {now_t.strftime('%Y-%m-%dT%H:%M:%SZ')} -->", "",
             f"# {TITLE}: {now_t.strftime('%Y-%m-%d')}", "",
             f"Since {since[:16].replace('T', ' ')} UTC. Built by `colab.py digest` with a {token_kind} token; "
             "it shows only what that token can read. Restricted repos appear as counts only.", "", "## Across the org", ""]
    lines += [f"{line}" if line.startswith("- ") else f"- {line}" for line in colab_orgview.brief_lines(view)[1:]]
    lines += ["", f"## Waiting on a review for over {WAIT_DAYS} days", ""]
    if waiting["by_reviewer"]:
        for who, prs in waiting["by_reviewer"].items():
            lines.append(f"- {who}: {', '.join(prs[:12])}" + (f", and {len(prs) - 12} more" if len(prs) > 12 else ""))
    else:
        lines.append("- none")
    if waiting["restricted"]:
        lines.append(f"- plus {waiting['restricted']} in restricted repos (count only)")
    if waiting.get("stale"):
        lines.append(f"- plus {waiting['stale']} untouched for over {STALE_DAYS} days: close or park them")
    lines += ["", "## Coverage", "", f"- {cov['readable']} of the lab's space repos readable by this token"]
    if cov["unreadable"]:
        lines.append(f"- not readable by this token ({len(cov['unreadable'])}): {', '.join(cov['unreadable'])}")
    if cov["unreadable_restricted"]:
        lines.append(f"- not readable by this token: {cov['unreadable_restricted']} restricted repos (count only)")
    if cov["unreadable"] or cov["unreadable_restricted"]:
        lines.append("- to cover them, an org admin sets a read-only token as the `COLAB_DIGEST_TOKEN` secret (see the workflow)")
    lines += [f"- note: {n}" for n in notes]
    lines += ["", "For what is waiting on you in person, run `colab.py unblock` with your own login."]
    return {"markdown": "\n".join(lines) + "\n", "tier": "lab" if view["tier"] != "public" else "public", "since": since,
            "coverage": cov, "brief": view, "waiting": waiting}


def last_digest(gh, config: dict, author: str | None) -> dict | None:
    """The open digest issue in the meta repo: carries the marker, and was opened by `author` when it is known."""
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    for page in range(1, 4):
        batch = gh.get(f"/repos/{org}/{meta}/issues?state=open&per_page=100&page={page}", conditional=False)
        batch = batch if isinstance(batch, list) else []
        for it in batch:
            if "pull_request" in it or MARKER not in str(it.get("body") or ""):
                continue
            if author and str((it.get("user") or {}).get("login") or "").lower() != author.lower():
                continue  # someone else's issue that merely quotes the marker: never edited
            return it
        if len(batch) < 100:
            break
    return None


def previous_at(issue: dict | None) -> str | None:
    m = SINCE_MARK.search(str((issue or {}).get("body") or ""))
    return m[1] if m else None


def post(gh, config: dict, markdown: str, author: str | None, existing: dict | None = None) -> dict:
    """Replace the digest issue's body (or open it once). The only write. Returns the issue."""
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    existing = existing if existing is not None else last_digest(gh, config, author)
    if existing:
        return gh.send("PATCH", f"/repos/{org}/{meta}/issues/{existing['number']}", {"body": markdown}) or existing
    return gh.send("POST", f"/repos/{org}/{meta}/issues", {"title": TITLE, "body": markdown})
