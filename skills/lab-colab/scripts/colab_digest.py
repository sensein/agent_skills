"""The nightly digest: one issue in the meta repo, rebuilt each night from the org-wide brief, what is waiting on whom
(review requests left for over a week), and the repos in no space. The same code runs anywhere: a scheduled GitHub
Action on the default GITHUB_TOKEN, the same Action with a supplied token, or a member running `colab.py digest`
with their own login.

Token fallback: whatever `gh` holds decides what is covered. A repo the token cannot read is listed as "not readable
by this token" (a count, for restricted repos), never treated as quiet, and never an error; API errors become notes.

Safety: the digest's tier is the strictest tier of everything it shows (the brief, repos named in no space or under
coverage, PRs waiting on review). It is refused when that tier is looser-than-allowed for the meta repo, and when
anything restricted would be shown in detail: restricted repos and items appear as counts only. The text is
leak-scanned before it is printed or posted, and the only write is the digest issue itself (opened once, then its
body replaced). Its state block (colab_orgview: repo -> first_seen) is carried from one night to the next.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from colab_orgview import MARKER, next_state, parse_state, render_state, unassigned_lines, unassigned_repos
from colab_sync import RANK, Budget, DetailBudget, _labels, _repo_vis, _strictest, tier_of

SINCE_MARK = re.compile(r"<!-- colab-digest-at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) -->")
TITLE = "Nightly digest"
WAIT_DAYS = 7
STALE_DAYS = 180  # older review waits are counted, not listed: they need parking, not a nudge
NOBODY = "review required, nobody requested"
BOT = "github-actions[bot]"


def coverage(gh, config: dict, budget: Budget) -> dict:
    """Which of the lab's own space repos this token can read: {"readable": n, "unreadable": [names],
    "unreadable_restricted": n, "tiers": [tier of each named repo]}. Repos declared restricted are counted, never named."""
    from colab import declared_tiers, is_external, repo_spaces  # noqa: PLC0415
    org = config["org"]["name"]
    declared = declared_tiers(config)
    out = {"readable": 0, "unreadable": [], "unreadable_restricted": 0, "tiers": []}
    for key in sorted(k for k in repo_spaces(config) if not is_external(k)):
        try:
            vis = _repo_vis(gh, f"{org}/{key}", budget)
        except (urllib.error.URLError, OSError):
            vis = None
        if vis:
            out["readable"] += 1
        elif declared.get(key) == "restricted":
            out["unreadable_restricted"] += 1
        else:
            out["unreadable"].append(key)
            out["tiers"].append(declared.get(key) or "lab")  # unknown visibility: at least lab, it is in the org
    return out


def _search(gh, q: str, budget: Budget, pages: int = 1) -> tuple[list[dict], int]:
    items, total = [], 0
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


def waiting_reviews(gh, config: dict, budget: Budget, now_t: datetime) -> dict:
    """Open PRs in the org waiting on a review for WAIT_DAYS to STALE_DAYS, grouped by the requested reviewer (people
    and teams), plus PRs whose branch rule requires a review that nobody was asked for. Older ones are a count
    (`stale`); restricted repos or items (a tier:restricted label) are a count (`restricted`). No titles.
    Errors become `notes`."""
    from colab import declared_tiers, label_space_tiers, repo_key  # noqa: PLC0415
    org = config["org"]["name"]
    declared = declared_tiers(config)
    wait = (now_t - timedelta(days=WAIT_DAYS)).strftime("%Y-%m-%d")
    cap = (now_t - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
    out = {"by_reviewer": {}, "restricted": 0, "stale": 0, "notes": [], "tiers": []}
    seen: dict[str, dict] = {}
    try:
        for required, q in ((False, f"org:{org} is:pr is:open review:none updated:{cap}..{wait}"),     # nobody reviewed yet
                            (True, f"org:{org} is:pr is:open review:required updated:{cap}..{wait}")):  # a rule requires one
            for it in _search(gh, q, budget, pages=2)[0]:
                entry = seen.setdefault(it.get("html_url") or str(it.get("id")), {**it, "_required": False})
                entry["_required"] = entry["_required"] or required
        out["stale"] = _search(gh, f"org:{org} is:pr is:open review:none updated:<{cap}", budget)[1]
    except DetailBudget:
        out["notes"].append("budget ran out reading review requests")
    except (urllib.error.URLError, OSError) as err:
        out["notes"].append(f"review requests not read: {str(err)[:160]}")
    vis_cache: dict[str, str | None] = {}
    by: dict[str, list[str]] = {}
    for it in sorted(seen.values(), key=lambda i: str(i.get("updated_at") or "")):
        repo = str(it.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1]
        key = repo_key(repo, org)
        try:
            if key not in vis_cache:
                vis_cache[key] = _repo_vis(gh, f"{org}/{repo}", budget)
            labels = _labels(it)
            tier = tier_of(vis_cache[key], labels, _strictest(declared.get(key), *label_space_tiers(config, key, labels)))
            if tier == "restricted":
                out["restricted"] += 1
                continue
            budget.spend()
            rr = gh.get(f"/repos/{org}/{repo}/pulls/{it['number']}/requested_reviewers", conditional=False)
        except DetailBudget:
            out["notes"].append("budget ran out reading requested reviewers")
            break
        except (urllib.error.URLError, OSError):
            rr = None  # e.g. 403 for this token: still listed, with the reviewer unknown
        people = [str(u.get("login")) for u in (rr or {}).get("users") or []] if isinstance(rr, dict) else []
        teams = [f"team:{t.get('slug')}" for t in (rr or {}).get("teams") or []] if isinstance(rr, dict) else []
        names = people + teams
        if not names and rr is not None and not it["_required"]:
            continue  # no reviews, nobody asked, no rule requiring one: not a review wait
        try:
            days = (now_t - datetime.fromisoformat(str(it.get("updated_at")).replace("Z", "+00:00"))).days
        except ValueError:
            days = 0
        out["tiers"].append(tier)
        for who in names or [NOBODY if rr is not None else "reviewer not readable by this token"]:
            by.setdefault(who, []).append(f"{repo}#{it['number']} ({days} days)")
    out["by_reviewer"] = dict(sorted(by.items(), key=lambda kv: (kv[0] in (NOBODY, "reviewer not readable by this token"),
                                                                -len(kv[1]))))
    return out


def build(gh, config: dict, now_t: datetime | None = None, since: str | None = None, budget: Budget | None = None,
          token_kind: str = "member", board_items: list[dict] | None = None, previous: dict | None = None,
          dest_tier: str = "lab") -> dict:
    """{"markdown", "tier", "since", "coverage", "brief", "waiting", "state"} for the digest. Writes nothing.
    `previous` is the last digest's state block (first-seen dates); `dest_tier` the meta repo's tier: a digest whose
    tier is stricter than its destination raises ValueError, as does anything restricted shown in detail."""
    import colab_orgview  # noqa: PLC0415
    now_t = now_t or datetime.now(timezone.utc)
    budget = budget or Budget(250)
    since = since or (now_t - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes: list[str] = []
    previous = previous if previous is not None else {"repos": {}}
    try:
        cov = coverage(gh, config, budget)
    except DetailBudget:
        cov = {"readable": 0, "unreadable": [], "unreadable_restricted": 0, "tiers": []}
        notes.append("budget ran out checking coverage")
    try:
        unassigned = unassigned_repos(gh, config, now_t=now_t, budget=budget, state=previous)
    except (DetailBudget, urllib.error.URLError, OSError) as err:
        unassigned = None
        notes.append(f"repos in no space not read: {str(err)[:160] or 'budget'}")
    try:
        view = colab_orgview.org_brief(gh, config, since, now_t=now_t, budget=budget, board_items=board_items,
                                       unassigned=unassigned)
    except (urllib.error.URLError, OSError) as err:
        raise ValueError(f"the org brief could not be read: {str(err)[:160]}") from None
    waiting = waiting_reviews(gh, config, budget, now_t)
    notes += waiting["notes"]
    shown = [view["tier"], *cov["tiers"], *waiting["tiers"],
             *(["lab" if e["visibility"] != "public" else "public" for e in (unassigned or {}).get("unassigned", [])])]
    tier = _strictest(*shown)
    if tier == "restricted":
        raise ValueError("the digest would show restricted content in detail; refusing to build it")
    if RANK[tier] > RANK[dest_tier]:
        raise ValueError(f"the digest is {tier} tier but the meta repo is {dest_tier}; refusing to build it")
    state = next_state(unassigned, previous, now_t) if unassigned else previous
    head = token_kind if "token" in token_kind.lower() else f"{token_kind} token"
    lines = [MARKER, f"<!-- colab-digest-at {now_t.strftime('%Y-%m-%dT%H:%M:%SZ')} -->", render_state(state), "",
             f"# {TITLE}: {now_t.strftime('%Y-%m-%d')}", "",
             f"Since {since[:16].replace('T', ' ')} UTC. Built by `colab.py digest` with the {head}; "
             "it shows only what that token can read. Restricted repos appear as counts only.", "", "## Across the org", ""]
    lines += [line if line.startswith("- ") else f"- {line}" for line in colab_orgview.brief_lines({**view, "unassigned": None})[1:]]
    lines += ["", "## Repos in no space", ""]
    lines += [f"- {x}" for x in (unassigned_lines(unassigned) if unassigned else [])] or ["- none"]
    lines += ["", f"## Waiting on a review for {WAIT_DAYS} to {STALE_DAYS} days", ""]
    if waiting["by_reviewer"]:
        for who, prs in waiting["by_reviewer"].items():
            lines.append(f"- {who}: {', '.join(prs[:12])}" + (f", and {len(prs) - 12} more" if len(prs) > 12 else ""))
    else:
        lines.append("- none")
    if waiting["restricted"]:
        lines.append(f"- plus {waiting['restricted']} in restricted repos or items (count only)")
    if waiting.get("stale"):
        lines.append(f"- plus {waiting['stale']} open PRs with no review, untouched for over {STALE_DAYS} days: close or park them")
    lines += ["", "## Coverage", "", f"- {cov['readable']} of the lab's space repos readable by this token"]
    if cov["unreadable"]:
        lines.append(f"- not readable by this token ({len(cov['unreadable'])}): {', '.join(cov['unreadable'])}")
    if cov["unreadable_restricted"]:
        lines.append(f"- not readable by this token: {cov['unreadable_restricted']} restricted repos (count only)")
    if cov["unreadable"] or cov["unreadable_restricted"]:
        lines.append("- to cover them, an org admin sets a read-only token as the `COLAB_DIGEST_TOKEN` secret (see the workflow)")
    lines += [f"- note: {n}" for n in notes]
    lines += ["", "For what is waiting on you in person, run `colab.py unblock` with your own login."]
    return {"markdown": "\n".join(lines) + "\n", "tier": tier, "since": since, "coverage": cov, "brief": view,
            "waiting": waiting, "state": state}


def last_digest(gh, config: dict, author: str | None) -> dict | None:
    """The open digest issue in the meta repo: it carries the marker and was opened by `author` or by the scheduled
    Action (github-actions[bot], which is nobody's personal issue). Anyone else's issue is never picked."""
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    allowed = {BOT, *([author.lower()] if author else [])}
    picks = []
    for creator in sorted(allowed):
        for page in range(1, 4):
            batch = gh.get(f"/repos/{org}/{meta}/issues?state=open&creator={urllib.parse.quote(creator)}"
                           f"&sort=updated&direction=desc&per_page=100&page={page}", conditional=False)
            batch = batch if isinstance(batch, list) else []
            picks += [it for it in batch if "pull_request" not in it and MARKER in str(it.get("body") or "")
                      and str((it.get("user") or {}).get("login") or "").lower() in allowed]
            if len(batch) < 100:
                break
    if not picks:
        return None
    return sorted(picks, key=lambda it: str(it.get("updated_at") or ""), reverse=True)[0]


def previous_at(issue: dict | None) -> str | None:
    """The previous digest's time stamp (validated: an exact UTC timestamp, else None)."""
    m = SINCE_MARK.search(str((issue or {}).get("body") or ""))
    return m[1] if m else None


def previous_state(issue: dict | None) -> dict:
    return parse_state((issue or {}).get("body"))


def post(gh, config: dict, markdown: str, author: str | None, existing: dict | None = None) -> dict:
    """Replace the digest issue's body (or open it once). The only write. Returns the issue."""
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    existing = existing if existing is not None else last_digest(gh, config, author)
    if existing:
        return gh.send("PATCH", f"/repos/{org}/{meta}/issues/{existing['number']}", {"body": markdown}) or existing
    return gh.send("POST", f"/repos/{org}/{meta}/issues", {"title": TITLE, "body": markdown})
