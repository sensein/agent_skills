"""Comment and review threads, read under the caller's own login: new comments and reviews on the items a member is
involved in, each reported once, and whose move it is on each open thread (sensein/lab-colab#32).

Nothing here writes to GitHub. Comment text is never returned: only authors, times, counts and links, so content
written by someone else is quoted only when a person asks for it, and never acted on. A restricted item's events
carry no summary. Accounts outside the lab are marked untrusted; bots are counted apart.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from colab_sync import Budget, DetailBudget, _labels, _repo_vis, _strictest, quote_within, tier_of

INVOLVED = "involved:me"
DROP_DAYS = 90       # a thread's "reported up to" entry is dropped this long after its last activity, or when it closes
SEARCH_PAGES = 3     # pages of 100 search results per query
COMMENT_PAGES = 5    # pages of 100 new comments read per thread; beyond that the thread is marked truncated


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


BOT_LOGINS = {"codecov-commenter", "coveralls", "sonarcloud", "pre-commit-ci", "allcontributors", "netlify",
              "vercel", "readthedocs", "lgtm-com", "deepsource-autofix"}  # plain user accounts that post automatically


def is_bot(user: dict | None, extra: set[str] | frozenset = frozenset()) -> bool:
    """GitHub App bots (type Bot, login ending [bot]), well-known automation accounts, and [inbox] bots."""
    user = user or {}
    login = str(user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]") or login.endswith("-bot") or login in BOT_LOGINS or login in extra


def repo_of(item: dict) -> str:
    """owner/name from a search hit's repository_url (…/repos/owner/name)."""
    parts = str(item.get("repository_url") or "").rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def search_involved(gh, login: str, budget: Budget, since: str | None = None, open_only: bool = False,
                    extra: str = "", order: str = "desc", meta: dict | None = None) -> list[dict]:
    """Issues and PRs the caller is involved in (author, assignee, mentioned, commenter: GitHub's `involves:`) or asked
    to review (`review-requested:`), anywhere the caller can see, by last update (`order`). Deduplicated by URL.
    `meta`, when given, gets "truncated": True if a query had more than SEARCH_PAGES pages."""
    seen: dict[str, dict] = {}
    for qual in (f"involves:{login}", f"review-requested:{login}"):
        q = qual + (f" updated:>={since}" if since else "") + (" is:open" if open_only else "") + (f" {extra}" if extra else "")
        for page in range(1, SEARCH_PAGES + 1):
            budget.spend()
            res = gh.get(f"/search/issues?q={urllib.parse.quote(q)}&sort=updated&order={order}&per_page=100&page={page}",
                         conditional=False)
            items = (res or {}).get("items") if isinstance(res, dict) else None
            for it in items or []:
                seen.setdefault(it.get("html_url") or str(it.get("id")), it)
            if not items or len(items) < 100:
                break
        else:
            if meta is not None:
                meta["truncated"] = True
                last = str((items or [{}])[-1].get("updated_at") or "")
                if last:  # read up to here for this query: in ascending order nothing later was seen
                    meta.setdefault("cutoffs", []).append(last)
    return list(seen.values())


def thread_speakers(gh, ref: str, item: dict, budget: Budget, since: str,
                    bots: set[str] | frozenset = frozenset(), meta: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(new, last): comments and reviews at or after `since` (oldest first), and the item's last few speakers overall
    (the opening post, the last comment page, all reviews), each {"by", "at", "kind", "bot", "url", "state"}.
    `meta`, when given, gets "truncated": True if more than COMMENT_PAGES pages of new comments were left unread."""
    n = item["number"]
    opened = {"by": str((item.get("user") or {}).get("login") or "").lower(), "at": item.get("created_at") or "",
              "kind": "opened", "bot": is_bot(item.get("user"), bots), "url": item.get("html_url", ""), "state": ""}
    speakers = [opened]
    new: list[dict] = []
    count = int(item.get("comments") or 0)
    if count:
        batch: list = []
        for page in range(1, COMMENT_PAGES + 1):  # `since` filters on updated_at: an old comment edited later comes too
            budget.spend()
            got = gh.get(f"/repos/{ref}/issues/{n}/comments?per_page=100&since={since}&page={page}", conditional=False)
            got = got if isinstance(got, list) else []
            batch += got
            if len(got) < 100:
                break
        else:
            if meta is not None:
                meta["truncated"] = True
        for c in batch:
            sp = {"by": str((c.get("user") or {}).get("login") or "").lower(), "at": c.get("created_at") or "",
                  "kind": "comment", "bot": is_bot(c.get("user"), bots), "url": c.get("html_url", ""), "state": "",
                  "api": c.get("url", "")}
            if sp["at"] >= since:
                new.append(sp)
            speakers.append(sp)
        if not any(str(c.get("created_at") or "") >= since for c in batch):  # nothing new: read the true last comment
            budget.spend()
            last = gh.get(f"/repos/{ref}/issues/{n}/comments?per_page=1&page={count}", conditional=False)
            for c in last if isinstance(last, list) else []:
                speakers.append({"by": str((c.get("user") or {}).get("login") or "").lower(), "at": c.get("created_at") or "",
                                 "kind": "comment", "bot": is_bot(c.get("user"), bots), "url": c.get("html_url", ""), "state": "",
                  "api": c.get("url", "")})
    if "pull_request" in item:
        budget.spend()
        reviews = gh.get(f"/repos/{ref}/pulls/{n}/reviews?per_page=100", conditional=False)
        for r in reviews if isinstance(reviews, list) else []:
            if not r.get("submitted_at"):
                continue  # a pending review is not visible to others yet
            sp = {"by": str((r.get("user") or {}).get("login") or "").lower(), "at": r["submitted_at"], "kind": "review",
                  "bot": is_bot(r.get("user"), bots), "url": r.get("html_url", ""), "state": str(r.get("state") or "").lower()}
            if sp["at"] >= since:
                new.append(sp)
            speakers.append(sp)
    new.sort(key=lambda s: s["at"])
    speakers.sort(key=lambda s: s["at"])
    return new, speakers


def whose_move(item: dict, speakers: list[dict], login: str, now_t: datetime) -> dict:
    """Who spoke last on an open thread (people only), and so whose move it is: "yours" when someone else spoke last,
    "waiting" (on the requested reviewers, else the last other speaker) when it was the caller."""
    people = [s for s in speakers if not s["bot"] and s["by"]]
    if not people:
        return {"move": "unknown", "last_by": "", "last_at": "", "waiting_on": [], "age_days": None}
    last = people[-1]
    age = None
    try:
        age = round((now_t - datetime.fromisoformat(last["at"].replace("Z", "+00:00"))).total_seconds() / 86400, 1)
    except ValueError:
        pass
    if last["by"] != login:
        return {"move": "yours", "last_by": last["by"], "last_at": last["at"], "waiting_on": [login], "age_days": age}
    requested = [str(u.get("login") or "").lower() for u in item.get("requested_reviewers") or [] if isinstance(u, dict)]
    others = [s["by"] for s in reversed(people) if s["by"] != login]
    waiting = [r for r in requested if r != login] or others[:1]
    return {"move": "waiting", "last_by": login, "last_at": last["at"], "waiting_on": waiting, "age_days": age}


def thread_events(gh, config: dict, login: str, start: str, state: dict, now_t: datetime, members: set[str] | None,
                  budget: Budget, move_state: bool = True) -> dict:
    """New `commented` and `reviewed` events on the items the caller is involved in, since `start` and since what was
    already reported per item (state["threads"]), plus whose move it is on each open thread. The caller's own comments
    are not events. With move_state, state["threads"] advances to what was reported and drops closed or old entries.
    {"events": [...], "threads": [...], "notes": [...]}."""
    from colab import declared_tiers, is_external, label_space_tiers, lab_opt_in, repo_key  # noqa: PLC0415
    org = config["org"]["name"]
    login = login.lower()
    declared, opt_in = declared_tiers(config), lab_opt_in(config)
    cursors = state.setdefault("threads", {}) if move_state else dict(state.get("threads") or {})
    moves = state.setdefault("moves", {}) if move_state else dict(state.get("moves") or {})  # whose move, last computed
    bots = {str(b).lower() for b in (config.get("inbox") or {}).get("bots") or []}
    out = {"events": [], "threads": [], "notes": [], "restricted": 0, "complete": True, "processed_until": None}
    vis_cache: dict[str, str | None] = {}
    try:
        smeta: dict = {}
        hits = search_involved(gh, login, budget, since=start[:19] + "Z" if len(start) >= 19 else start, order="asc", meta=smeta)
        if smeta.get("truncated"):  # oldest first, so what was read is a prefix: the rest come next run
            out["complete"] = False
            out["notes"].append(f"more than {SEARCH_PAGES * 100} involved items changed; the oldest were read, the rest come next run")
        cutoff_at = min(smeta.get("cutoffs") or [""]) if order_is_capped(smeta) else ""
        for item in sorted(hits, key=lambda i: i.get("updated_at") or ""):
            if cutoff_at and str(item.get("updated_at") or "") > cutoff_at:
                break  # past where a capped query stopped: those items come next run, from the watermark
            full = repo_of(item)
            if not full:
                continue
            key = repo_key(full, org)
            kind = "pull_request" if "pull_request" in item else "issue"
            tkey = f"{key}#{item['number']}"
            if full not in vis_cache:
                vis_cache[full] = _repo_vis(gh, full, budget)
            vis = vis_cache[full]
            external = is_external(key)
            labels = _labels(item)
            tier = tier_of(vis, labels, _strictest(declared.get(key), *label_space_tiers(config, key, labels)), external, key in opt_in)
            restricted = tier == "restricted"
            updated = str(item.get("updated_at") or "")
            cached = moves.get(tkey)
            if cursors.get(tkey, "") >= updated and isinstance(cached, dict) and not restricted:
                # nothing changed since it was last reported: no detail reads, whose move from the last read
                if item.get("state") == "open":
                    title = quote_within(item.get("title") or "", 200) if external else (item.get("title") or "")
                    out["threads"].append({"item": tkey, "kind": kind, "url": item.get("html_url", ""), "tier": tier,
                                           "summary": title, "untrusted": external, **cached,
                                           "age_days": _age_days(cached.get("last_at", ""), now_t)})
                out["processed_until"] = updated
                continue
            since = max(start, cursors.get(tkey, ""))
            meta: dict = {}
            new, speakers = thread_speakers(gh, full, item, budget, since, bots, meta)
            if meta.get("truncated"):
                out["complete"] = False
                out["notes"].append(f"{tkey}: more than {COMMENT_PAGES * 100} new comments; the rest come next run")
            new = [s for s in new if s["by"] != login and s["at"] > cursors.get(tkey, "")]
            if restricted and any(not s["bot"] for s in new):
                out["restricted"] += 1  # counts only: no repo, link, author or time for a restricted item
            title = "" if restricted else (item.get("title") or "")
            if title and external:
                title = quote_within(title, 200)
            for transition, group in (("commented", [s for s in new if s["kind"] == "comment"]),
                                      ("reviewed", [s for s in new if s["kind"] == "review"])):
                people = [s for s in group if not s["bot"]]
                n_bots = len(group) - len(people)
                if not people or restricted:
                    continue
                authors = sorted({s["by"] for s in people})
                outsiders = [a for a in authors if members is not None and a not in members]
                out["events"].append({
                    "id": f"{key}:{kind}:{item['number']}:{transition}:{people[-1]['at']}", "kind": kind, "transition": transition,
                    "repo": key, "source": people[-1]["url"] or item.get("html_url", ""), "tier": tier, "at": people[-1]["at"],
                    "topics": [INVOLVED], "summary": title, "count": len(people), "authors": authors, "bots": n_bots,
                    "states": sorted({s["state"] for s in people if s["state"]}) if transition == "reviewed" else [],
                    "untrusted": bool(external or outsiders), "_env": None})
            if move_state:  # closed items keep their cursor too (dropped after DROP_DAYS), so nothing is reported twice
                latest = max([s["at"] for s in new] + [cursors.get(tkey, ""), updated if not meta.get("truncated") else ""])
                if latest:
                    cursors[tkey] = latest
            if item.get("state") == "open" and not restricted:
                move = whose_move(item, speakers, login, now_t)
                moves[tkey] = {k: move[k] for k in ("move", "last_by", "last_at", "waiting_on")}
                out["threads"].append({"item": tkey, "kind": kind, "url": item.get("html_url", ""), "tier": tier,
                                       "summary": title, "untrusted": external, **move})
            else:
                moves.pop(tkey, None)
            if not meta.get("truncated"):
                out["processed_until"] = updated
    except DetailBudget:
        out["complete"] = False
        out["notes"].append("the call budget ran out reading comment threads; run again for the rest")
    except (urllib.error.URLError, OSError) as err:
        out["complete"] = False
        out["notes"].append(f"comment threads stopped early: {str(err)[:200]}")
    if move_state:
        cutoff = _iso(now_t - timedelta(days=DROP_DAYS))
        for k in [k for k, v in cursors.items() if v < cutoff]:
            cursors.pop(k)
            moves.pop(k, None)
    out["threads"].sort(key=lambda t: (t["move"] != "yours", -(t["age_days"] or 0)))
    return out


def order_is_capped(meta: dict) -> bool:
    return bool(meta.get("truncated") and meta.get("cutoffs"))


def _age_days(at: str, now_t: datetime) -> float | None:
    try:
        return round((now_t - datetime.fromisoformat(str(at).replace("Z", "+00:00"))).total_seconds() / 86400, 1)
    except ValueError:
        return None


def waiting_by_person(threads: list[dict]) -> dict[str, list[dict]]:
    """Open threads grouped by who they wait on, oldest first: the "waiting on a reply" list for `brief`."""
    out: dict[str, list[dict]] = {}
    for t in threads:
        for who in t.get("waiting_on") or []:
            out.setdefault(who, []).append(t)
    return {k: sorted(v, key=lambda t: -(t.get("age_days") or 0)) for k, v in sorted(out.items())}

