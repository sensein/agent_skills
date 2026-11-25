"""`unblock`: what is waiting on the caller, ranked by what it holds up, with a suggested way to unblock each
(sensein/lab-colab#37). Read-only, under the caller's own login; nothing is posted.

Asks of the caller, read now:
1. pull requests with a review requested from them;
2. open threads where someone else spoke last (whose move: colab_threads), including comments that mention them;
3. items they lead or are assigned that are Blocked on the board, or that another item `depends-on`;
4. explicit decision lists aimed at them (a recent comment that mentions them and numbers several choices);
5. Proposed items, in spaces they steward, waiting for acceptance.

Comment text is read only to recognise a decision list; it is never returned. Restricted items are counted, never
listed. Content from other orgs or outside accounts is marked untrusted.
"""
from __future__ import annotations

import re
import urllib.error
from datetime import datetime, timedelta, timezone

from colab_sync import Budget, DetailBudget, _repo_vis, _strictest, quote_within, tier_of
from colab_threads import repo_of, search_involved, thread_speakers, whose_move

DECISION_WORDS = re.compile(r"(?i)\b(decide|decision|decisions|choose|choice|pick|approve|which (?:one|option))\b")
NUMBERED = re.compile(r"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?(?:\d{1,2}|[A-Z]\d{1,2})[.):](?:\*\*)?\s+\S"   # 1. / A1) / **A1:**
                      r"|^\s*\|\s*(?:\*\*)?(?:\d{1,2}|[A-Z]\d{1,2})(?:\*\*)?\s*\|")                   # | A1 | table rows
FAR_FUTURE = "2999-01-01T00:00:00Z"
THREAD_LIMIT = 60      # open involved threads read in detail, most recently updated first
LOOKBACK_DAYS = 120    # threads not updated for this long are left out


def _age(at: str, now_t: datetime) -> float | None:
    try:
        return round((now_t - datetime.fromisoformat(str(at).replace("Z", "+00:00"))).total_seconds() / 86400, 1)
    except ValueError:
        return None


def review_requested_urls(gh, login: str, budget: Budget) -> set[str]:
    """Open PRs with a review requested from the caller (search items carry no requested_reviewers)."""
    import urllib.parse  # noqa: PLC0415
    budget.spend()
    res = gh.get(f"/search/issues?q={urllib.parse.quote(f'review-requested:{login} is:pr is:open')}&per_page=100",
                 conditional=False)
    return {i.get("html_url") for i in ((res or {}).get("items") or []) if isinstance(res, dict)}


def decision_count(text: str, login: str, addressed: bool = False) -> int:
    """How many numbered choices (a numbered list or a table keyed 1, 2 or A1, A2) a comment puts to `login`. It must
    use decision words, and mention them unless it is `addressed` to them already (the last word on a thread where
    the move is theirs)."""
    if not addressed and f"@{login}".lower() not in (text or "").lower():
        return 0
    if not DECISION_WORDS.search(text or ""):
        return 0
    n = len(NUMBERED.findall(text or ""))
    return n if n >= 3 else 0


KIND_RANK = {"decisions": 0, "stack": 1, "review_requested": 2, "depended_on": 3, "blocked": 4, "your_move": 5, "proposed": 6}


def group_stacks(asks: list[dict]) -> list[dict]:
    """Open PRs in one repo, by the same people, waiting on the caller, become one `stack` ask (they hold up each other);
    a decision list in that repo from the same people is what the stack waits on, so it holds up the stack too."""
    groups: dict[tuple, list[dict]] = {}
    for a in asks:
        if a["kind"] in ("your_move", "review_requested") and a.get("is_pr") and a["who"]:
            groups.setdefault((a["item"].split("#")[0], tuple(a["who"])), []).append(a)
    out = [a for a in asks if not any(a in g for g in groups.values() if len(g) > 1)]
    for (repo, who), group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda a: int(a["item"].split("#")[1]))
        oldest = max((a["age_days"] or 0) for a in group)
        out.append({**group[0], "kind": "stack", "item": f"{repo}#{group[0]['item'].split('#')[1]}..#{group[-1]['item'].split('#')[1]}",
                    "items": [a["item"] for a in group], "urls": [a["url"] for a in group], "holds_up": len(group),
                    "age_days": oldest, "summary": f"{len(group)} open PRs", "reviewed_by_me": all(a.get("reviewed_by_me") for a in group)})
        for a in out:
            if a["kind"] == "decisions" and a["item"].split("#")[0] == repo and set(a["who"]) & set(who):
                a["holds_up"] = max(a["holds_up"], len(group) + 1)
                a["holds_up_stack"] = out[-1]["item"]
    return out


def suggest(ask: dict) -> str:
    """One suggested way to unblock an ask."""
    age = ask.get("age_days") or 0
    kind = ask["kind"]
    if kind == "review_requested":
        if age > 14:
            return "decline with a reason, or delegate to a named reviewer (or an agent check of the fix commits against the review findings)"
        return "review it: approve, or request changes with a reason"
    if kind == "stack":
        first = ("review them together (in base order if they are stacked) and submit formal reviews" if not ask.get("reviewed_by_me")
                 else "answer the open threads together, in base order if they are stacked")
        return f"{first}, or delegate the stack to a named reviewer (or an agent check of the fix commits against the review findings)"
    if kind == "decisions":
        if ask.get("holds_up_stack"):
            return (f"answer these first: {ask['holds_up_stack']} waits on them. Split the {ask.get('decisions', 0)} numbered "
                    "items: decide the ones you can now in one comment, and ask the one short question you still need")
        return f"split the {ask.get('decisions', 0)} decisions: answer the ones you can now in one comment, and ask the one short question you still need"
    if kind == "depended_on":
        return f"finish or hand off: {ask.get('holds_up', 0)} items depend on it"
    if kind == "blocked":
        return "say what it waits on (a person, a decision, a link), or hand it off"
    if kind == "proposed":
        return "accept or decline it in the Proposals inbox, with a reason when declining"
    if kind == "your_move":
        if age > 30:
            return "close or park it if it's stale, saying why"
        if ask.get("is_pr") and not ask.get("reviewed_by_me"):
            return "submit a formal review so the queue shows it, rather than a comment"
        return "reply: a draft answer, or the short question you need answered first"
    return "look at it"


def gather(gh, config: dict, login: str, now_t: datetime | None = None, board_items: list[dict] | None = None,
           members: set[str] | None = None, budget: Budget | None = None) -> dict:
    """{"asks": [...ranked], "restricted": n, "notes": [...]}. Each ask: kind, item, url, tier, summary, who (the people
    waiting), since (their last move), age_days, holds_up, suggestion, untrusted."""
    from colab import declared_tiers, field_value, is_external, item_stewards, label_space_tiers, lab_opt_in, parse_links, repo_key  # noqa: PLC0415
    now_t = now_t or datetime.now(timezone.utc)
    budget = budget or Budget(200)
    login = login.lower()
    org = config["org"]["name"]
    declared, opt_in = declared_tiers(config), lab_opt_in(config)
    asks: dict[str, dict] = {}
    restricted: set[str] = set()
    notes: list[str] = []
    vis_cache: dict[str, str | None] = {}

    def tier_for(full: str, labels: list[str]) -> str:
        key = repo_key(full, org)
        if full not in vis_cache:
            try:
                vis_cache[full] = _repo_vis(gh, full, budget)
            except (DetailBudget, urllib.error.URLError, OSError):
                vis_cache[full] = None  # unknown visibility fails closed: the item is counted as restricted
                if "some repos' tiers could not be checked; their items are counted as restricted" not in notes:
                    notes.append("some repos' tiers could not be checked; their items are counted as restricted")
        return tier_of(vis_cache[full], labels, _strictest(declared.get(key), *label_space_tiers(config, key, labels)),
                       is_external(key), key in opt_in)

    def add(kind: str, full: str, number: int, url: str, title: str, labels: list[str], **extra) -> None:
        key = f"{repo_key(full, org)}#{number}"
        tier = tier_for(full, labels)
        if tier == "restricted":
            restricted.add(key)
            return
        external = is_external(repo_key(full, org))
        summary = quote_within(title, 200) if (title and external) else title
        prev = asks.get(key)
        ask = {"kind": kind, "item": key, "url": url, "tier": tier, "summary": summary, "who": [], "since": "",
               "age_days": None, "holds_up": 0, "untrusted": external, **extra}
        if prev:  # one entry per item: keep the stronger kind, add up what it holds up
            order = ["decisions", "review_requested", "depended_on", "blocked", "your_move", "proposed"]
            keep, other = (prev, ask) if order.index(prev["kind"]) <= order.index(ask["kind"]) else (ask, prev)
            keep["holds_up"] = max(keep["holds_up"], other["holds_up"])
            keep["who"] = sorted(set(keep["who"]) | set(other["who"]))
            asks[key] = keep
        else:
            asks[key] = ask

    try:
        # 1-2, 4: open threads I am involved in or asked to review, read in detail
        since = (now_t - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        review_urls = review_requested_urls(gh, login, budget)
        hits = sorted(search_involved(gh, login, budget, since=since, open_only=True),
                      key=lambda i: i.get("updated_at") or "", reverse=True)
        if len(hits) > THREAD_LIMIT:
            notes.append(f"{len(hits)} open threads involve you; the {THREAD_LIMIT} most recently updated were read")
        for item in hits[:THREAD_LIMIT]:
            full = repo_of(item)
            if not full:
                continue
            labels = [lab["name"] if isinstance(lab, dict) else str(lab) for lab in item.get("labels", [])]
            is_pr = "pull_request" in item
            requested = item.get("html_url") in review_urls
            if tier_for(full, labels) == "restricted":
                restricted.add(f"{repo_key(full, org)}#{item['number']}")
                continue
            _, speakers = thread_speakers(gh, full, item, budget, since=FAR_FUTURE)  # whose move only; no "new" window
            move = whose_move(item, speakers, login, now_t)
            author = str((item.get("user") or {}).get("login") or "").lower()
            reviewed_by_me = any(s["kind"] == "review" and s["by"] == login for s in speakers)
            decisions = 0
            if move["move"] == "yours" and move["last_by"]:
                last = [s for s in speakers if s["kind"] == "comment" and s["by"] == move["last_by"]]
                if last:  # read the last comment from them to recognise a decision list; its text is not kept
                    budget.spend()
                    api = str(last[-1].get("api") or "").replace("https://api.github.com", "", 1)
                    try:
                        c = gh.get(api, conditional=False) if api.startswith("/repos/") else None
                    except (urllib.error.URLError, OSError):
                        c = None
                    body = ""
                    if isinstance(c, dict):
                        body = str(c.get("body") or "")
                    decisions = decision_count(body, login, addressed=True)
            waiting = sorted({move["last_by"], author} - {login, ""}) if move["move"] == "yours" else []
            if not decisions and move["move"] == "yours" and move["last_by"] == author and not [s for s in speakers if s["kind"] != "opened"]:
                decisions = decision_count(str(item.get("body") or ""), login)  # the opening post is the last word
            if decisions:
                add("decisions", full, item["number"], item.get("html_url", ""), item.get("title", ""), labels,
                    who=waiting, since=move["last_at"], age_days=move["age_days"], holds_up=len(waiting), decisions=decisions,
                    is_pr=is_pr, reviewed_by_me=reviewed_by_me)
            elif is_pr and author != login and requested:
                add("review_requested", full, item["number"], item.get("html_url", ""), item.get("title", ""), labels,
                    who=[author], since=item.get("updated_at", ""), age_days=_age(item.get("updated_at", ""), now_t),
                    holds_up=1, is_pr=True, reviewed_by_me=reviewed_by_me)
            elif move["move"] == "yours":
                add("your_move", full, item["number"], item.get("html_url", ""), item.get("title", ""), labels,
                    who=waiting, since=move["last_at"], age_days=move["age_days"], holds_up=len(waiting), is_pr=is_pr,
                    reviewed_by_me=reviewed_by_me)
    except DetailBudget:
        notes.append("the call budget ran out reading threads; the list is partial")
    except (urllib.error.URLError, OSError) as err:
        notes.append(f"threads stopped early: {str(err)[:200]}")

    if board_items is None:
        notes.append("the board was not read: Blocked, depends-on and Proposed asks are missing")
    else:
        stewarded = {name for name in (config.get("spaces") or {}) if login in item_stewards(config, [name])}
        mine: dict[str, dict] = {}
        for it in board_items:
            c = it.get("content") or {}
            if not c.get("repository") or not c.get("number"):
                continue
            key = f"{repo_key(c['repository'], org)}#{c['number']}"
            status = str(field_value(it, "Status") or "").strip().lower()
            lead = str(field_value(it, "Lead") or "").strip().lower()
            spaces = field_value(it, "Spaces")
            spaces = spaces if isinstance(spaces, list) else [x.strip() for x in str(spaces or "").split(",") if x.strip()]
            labels = [str(x) for x in (it.get("labels") or [])]
            if lead == login and status not in ("done", "closed"):
                mine[key] = it
                if status == "blocked":
                    add("blocked", c["repository"], c["number"], c.get("url", ""), c.get("title", ""), labels)
            if status == "proposed" and stewarded & set(spaces):
                add("proposed", c["repository"], c["number"], c.get("url", ""), c.get("title", ""), labels,
                    who=[str(field_value(it, "Contributions") or "").split(":")[0].strip().lower()] if field_value(it, "Contributions") else [],
                    age_days=None)
        dependents: dict[str, list[str]] = {}
        for it in board_items:  # depends-on edges pointing at items I lead
            c = it.get("content") or {}
            edges, _ = parse_links(str(c.get("body") or ""))
            src = f"{repo_key(c.get('repository') or '', org)}#{c.get('number')}"
            for e in edges:
                if e["edge"] != "depends-on":
                    continue
                owner_repo, _, num = e["target"].partition("#")
                tgt = f"{repo_key(owner_repo, org)}#{num}"
                if tgt in mine and str(field_value(it, "Status") or "").strip().lower() not in ("done", "closed"):
                    dependents.setdefault(tgt, []).append(src)
        for tgt, srcs in dependents.items():
            c = mine[tgt].get("content") or {}
            add("depended_on", c["repository"], c["number"], c.get("url", ""), c.get("title", ""),
                [str(x) for x in (mine[tgt].get("labels") or [])], holds_up=len(srcs), depended_on_by=sorted(srcs))
    ranked = sorted(group_stacks(list(asks.values())), key=lambda a: (-(a["holds_up"] or 0), KIND_RANK.get(a["kind"], 9),
                                                                    -(a["age_days"] or 0)))
    from colab_threads import is_bot  # noqa: PLC0415
    for a in ranked:
        a["who"] = [w for w in a["who"] if w and not is_bot({"login": w})]
        a["suggestion"] = suggest(a)
        if members is not None and any(w and w not in members for w in a["who"]):
            a["untrusted"] = True
    return {"asks": ranked, "restricted": len(restricted), "notes": notes, "calls_used": budget.used}


def unblock_lines(view: dict, limit: int = 25) -> list[str]:
    """A short ranked list for a person: who waits, since when, what they need, the suggestion, the link."""
    labels = {"stack": "PRs from the same people waiting on you", "review_requested": "review requested", "decisions": "decisions for you", "depended_on": "others depend on it",
              "blocked": "Blocked, you lead it", "proposed": "Proposed, waiting for a steward", "your_move": "your move"}
    lines = []
    for i, a in enumerate(view["asks"][:limit], 1):
        who = f" — {', '.join(a['who'])} waiting" if a["who"] else ""
        age = f", {a['age_days']:.0f} days" if a.get("age_days") is not None else ""
        lines.append(f"{i}. {a['item']}: {labels.get(a['kind'], a['kind'])}{who}{age}. Suggest: {a['suggestion']}. {a['url']}")
    if len(view["asks"]) > limit:
        lines.append(f"... and {len(view['asks']) - limit} more")
    if view["restricted"]:
        lines.append(f"{view['restricted']} restricted items also wait on you (counts only: open them where restricted work runs)")
    lines += [f"Note: {n}" for n in view["notes"]]
    return lines or ["Nothing is waiting on you."]
