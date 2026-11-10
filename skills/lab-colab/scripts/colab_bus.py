#!/usr/bin/env python3
"""colab-bus: poll GitHub, build envelopes, tier-check, match subscriptions, deliver.

Runs as a light self-requeuing Slurm job (see assets/templates/colab-bus.sbatch).
It polls instead of receiving webhooks because compute nodes accept no inbound
connections. Stdlib only, Python 3.11+.

Per destination space, from .colab/rollout.json:
  dry-run  log what would be delivered to <state>/would-deliver.jsonl; write nothing
  live     open a delivery issue in the subscriber repo (throttled)
The heartbeat (repo variable COLAB_BUS_HEARTBEAT on the meta repo) is written in both modes.

Safety rules enforced here:
  - destination tier = max(actual repo visibility, declared override); unknown repo = public
  - events only move to an equal or stricter tier; anything else is dropped and audited
  - restricted events carry no summary, and move only for spaces with the `restricted` switch
  - the bus never routes its own deliveries (body marker), so it cannot loop
  - it adds no labels anywhere: tier and space membership come from the config
  - it validates each issue's fenced links block once per body version
  - one envelope per item transition (opened, closed, published), not per edit

State lives in --state-dir (private group storage, not scratch).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
RANK = {"public": 0, "lab": 1, "restricted": 2}
DELIVERY_MARKER = "<!-- colab-delivery -->"
SEEN_CAP = 20000


# --------------------------------------------------------------------------- pure logic


def _strictest(*tiers: str | None) -> str:
    return max([t for t in tiers if t in RANK] or ["public"], key=RANK.__getitem__)


def tier_of(repo_visibility: str, labels: list[str], declared: str | None = None) -> str:
    """Stricter of the repo's visibility, its declared space tier, and an item tier:* label."""
    base = "public" if repo_visibility == "public" else "lab"
    tiers = [lab.split(":", 1)[1] for lab in labels if lab.startswith("tier:")]
    return _strictest(base, declared, *tiers)


def dest_tier(visibility: str | None, override: str | None = None, declared: str | None = None) -> str:
    """Where a delivery lands. Unknown visibility fails closed to public; overrides only tighten."""
    actual = "lab" if visibility in ("private", "internal") else "public"
    return _strictest(actual, override, declared)


def repo_spaces(config: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, spec in (config.get("spaces") or {}).items():
        for repo in spec.get("repos", []):
            out.setdefault(repo, []).append(name)
    return out


def repo_tiers(config: dict) -> dict[str, str]:
    """repo -> strictest `tier` declared by any space it belongs to."""
    out: dict[str, str] = {}
    for spec in (config.get("spaces") or {}).values():
        if spec.get("tier") in RANK:
            for repo in spec.get("repos", []):
                out[repo] = _strictest(out.get(repo), spec["tier"])
    return out


def may_deliver(event_tier: str, to_tier: str) -> bool:
    """Events move only toward stricter (or equal) tiers."""
    return RANK[to_tier] >= RANK[event_tier]


def is_delivery(item: dict) -> bool:
    return DELIVERY_MARKER in (item.get("body") or "")


def envelopes_for(kind: str, repo: str, visibility: str, item: dict,
                  declared: str | None = None, spaces: list[str] | None = None) -> list[dict]:
    """One envelope per transition the item has reached; dedupe makes each fire once."""
    labels = [lab["name"] if isinstance(lab, dict) else lab for lab in item.get("labels", [])]
    tier = tier_of(visibility, labels, declared)
    member = sorted(set(spaces or []) | {lab.split(":", 1)[1] for lab in labels if lab.startswith("space:")})
    summary = "" if tier == "restricted" else (item.get("title") or item.get("name") or item.get("tag_name") or "")
    key = item.get("number", item.get("id"))
    if kind == "release":
        transitions = [("published", item.get("published_at") or "")] if not item.get("draft") else []
    else:
        transitions = [("opened", item.get("created_at") or "")]
        if item.get("state") == "closed":
            transitions.append(("closed", item.get("closed_at") or ""))
    return [{
        "id": f"{repo}:{kind}:{key}:{transition}",
        "kind": kind, "transition": transition, "repo": repo, "tier": tier,
        "source": item.get("html_url", ""), "summary": summary, "labels": labels, "spaces": member,
        "sender": (item.get("user") or item.get("author") or {}).get("login", ""),
        "at": at,
    } for transition, at in transitions]


def matches(sub: dict, env: dict) -> bool:
    """A subscription matches on topic and, optionally, event kinds."""
    kinds = sub.get("events")
    if kinds and env["kind"] not in kinds:
        return False
    scheme, _, value = sub["topic"].partition(":")
    if scheme == "repo":
        return env["repo"] == value
    if scheme == "label":
        return value in env["labels"]
    if scheme == "space":
        return value in env.get("spaces", [])
    return False


def route(env: dict, subscriptions: list[dict], to_tiers: dict[str, str], bot_login: str,
          restricted_ok: bool = False) -> tuple[list[dict], list[dict]]:
    """Return (deliveries, drops) for one envelope."""
    if env["sender"] == bot_login:
        return [], []
    deliveries, drops = [], []
    for sub in subscriptions:
        if not matches(sub, env) or sub["subscriber"] == env["repo"]:
            continue
        to = to_tiers.get(sub["subscriber"], "public")  # unknown repo: fail closed
        if env["tier"] == "restricted" and not restricted_ok:
            drops.append({"to": sub["subscriber"], "reason": "restricted switch is off", "envelope": env["id"]})
        elif may_deliver(env["tier"], to):
            deliveries.append({"to": sub["subscriber"], "via": sub.get("deliver", "issue"), "envelope": env["id"]})
        else:
            drops.append({"to": sub["subscriber"], "reason": f"{env['tier']} -> {to}", "envelope": env["id"]})
    return deliveries, drops


try:  # shared grammar; colab.py sits next to this file in the skill and in the meta repo
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from colab import parse_links, validate_links  # noqa: E402
except ImportError:  # pragma: no cover
    parse_links = validate_links = None


def remember(state: dict, env_id: str) -> bool:
    """Record an envelope id; False if already seen. Trims oldest first."""
    seen: list = state.setdefault("seen", [])
    index = state.setdefault("_seen_index", set(seen))
    if env_id in index:
        return False
    seen.append(env_id)
    index.add(env_id)
    if len(seen) > SEEN_CAP:
        for old in seen[: len(seen) - SEEN_CAP]:
            index.discard(old)
        del seen[: len(seen) - SEEN_CAP]
    return True


# --------------------------------------------------------------------------- GitHub I/O


class GitHub:
    def __init__(self, token: str, state: dict):
        self.token = token
        self.etags: dict = state.setdefault("etags", {})
        self.cache: dict = state.setdefault("cache", {})

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def get(self, path: str, cache: bool = False):
        """GET with ETag. A 304 (unchanged) returns None, or the cached body when cache=True."""
        headers = self._headers()
        if path in self.etags:
            headers["If-None-Match"] = self.etags[path]
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as resp:
                body = json.loads(resp.read() or b"null")
                if resp.headers.get("ETag"):
                    self.etags[path] = resp.headers["ETag"]
                if cache:
                    self.cache[path] = body
                return body
        except urllib.error.HTTPError as err:
            if err.code == 304:
                return self.cache.get(path) if cache else None
            if err.code == 404:
                return None
            raise

    def send(self, method: str, path: str, body: dict) -> dict:
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode(),
                                     headers={**self._headers(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")


def read_token(token_file: Path | None) -> str:
    """Prefer a 0600 token file (fine-grained PAT or App token), then GITHUB_TOKEN, then gh."""
    if token_file:
        if token_file.stat().st_mode & 0o077:
            raise SystemExit(f"{token_file} must not be readable by group or others (chmod 600)")
        return token_file.read_text().strip()
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


# --------------------------------------------------------------------------- loop


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(path: Path, record: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def poll_once(gh, config: dict, rollout: dict, subs: list[dict], state: dict, state_dir: Path,
              bot: str, save=lambda: None) -> dict:
    org = config["org"]["name"]
    meta_repo = config["org"].get("meta_repo", "lab-colab")
    spaces = config.get("spaces") or {}
    member_of = repo_spaces(config)
    declared = repo_tiers(config)
    space_state = rollout.get("spaces", {})
    interval = config.get("bus", {}).get("write_interval_seconds", 1.0)
    threshold = config.get("bus", {}).get("fanout_digest_threshold", 20)
    stats = {"envelopes": 0, "deliveries": 0, "drops": 0, "dry_run": 0, "errors": 0}

    # Visibility of every repo we read from or deliver to, fetched fresh each poll (ETag-cheap).
    repos = sorted(set(member_of) | {s["subscriber"] for s in subs} | {meta_repo})
    visibility: dict[str, str | None] = {}
    for repo in repos:
        try:
            meta = gh.get(f"/repos/{org}/{repo}", cache=True)
            visibility[repo] = meta.get("visibility") if isinstance(meta, dict) else None
        except urllib.error.URLError:
            visibility[repo] = None
            stats["errors"] += 1
    overrides = {s["subscriber"]: s.get("tier") for s in subs if s.get("tier")}
    to_tiers = {r: dest_tier(visibility.get(r), overrides.get(r), declared.get(r)) for r in repos}

    def live(repo: str) -> bool:
        # a repo in several spaces is live only when all of them are
        names = member_of.get(repo, [])
        return bool(names) and all(space_state.get(n, {}).get("bus") == "live" for n in names)

    def restricted_ok(repo: str) -> bool:
        names = member_of.get(repo, [])
        return bool(names) and all(space_state.get(n, {}).get("switches", {}).get("restricted") for n in names)

    def write(path: str, body: dict) -> bool:
        try:
            gh.send("POST", path, body)
            time.sleep(interval)
            return True
        except urllib.error.URLError as err:
            stats["errors"] += 1
            _append(state_dir / "audit.jsonl", {"write_failed": path, "error": str(err), "at": now()})
            return False

    def check_links(item: dict, repo: str) -> None:
        """Validate the links block once per body version; comment only when the repo is live."""
        body = item.get("body") or ""
        if "```links" not in body or "pull_request" in item or parse_links is None:
            return
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        key = f"{repo}#{item.get('number')}"
        if checked.get(key) == digest:
            return
        checked[key] = digest
        edges, errors = parse_links(body)
        errors += validate_links(edges, config, (item.get("type") or {}).get("name") if isinstance(item.get("type"), dict) else None)
        if not errors:
            return
        _append(state_dir / "audit.jsonl", {"links_errors": key, "errors": errors, "at": now()})
        if live(repo):
            gh_comment = "\n".join(["The links block has problems:", "", *[f"- {e}" for e in errors], "",
                                     "Format: `<edge>: <owner>/<repo>#<n>` or `<edge>: kg:<id>`, one per line.", DELIVERY_MARKER])
            write(f"/repos/{org}/{repo}/issues/{item.get('number')}/comments", {"body": gh_comment})

    cursors = state.setdefault("cursors", {})
    checked = state.setdefault("links_checked", {})
    for repo in sorted(member_of):
        vis = visibility.get(repo)
        if vis is None:
            continue
        try:
            since = cursors.get(repo, "1970-01-01T00:00:00Z")
            items = gh.get(f"/repos/{org}/{repo}/issues?state=all&sort=updated&direction=asc&per_page=100&since={since}") or []
            releases = gh.get(f"/repos/{org}/{repo}/releases?per_page=10") or []
        except urllib.error.URLError as err:
            stats["errors"] += 1
            _append(state_dir / "audit.jsonl", {"read_failed": repo, "error": str(err), "at": now()})
            continue
        events = []
        for item in items:
            if is_delivery(item):
                continue
            events += envelopes_for("pull_request" if "pull_request" in item else "issue", repo, vis, item,
                                    declared.get(repo), member_of.get(repo))
            check_links(item, repo)
        for rel in releases:
            events += envelopes_for("release", repo, vis, rel, declared.get(repo), member_of.get(repo))
        for env in sorted(events, key=lambda e: e["at"]):
            if not remember(state, env["id"]):
                continue
            stats["envelopes"] += 1
            _append(state_dir / "events.jsonl", env)
            deliveries, drops = route(env, subs, to_tiers, bot, restricted_ok(repo))
            for d in drops:
                stats["drops"] += 1
                _append(state_dir / "audit.jsonl", {"dropped": d, "at": now()})
            if len(deliveries) > threshold:
                if not may_deliver(env["tier"], to_tiers[meta_repo]):
                    stats["drops"] += 1
                    _append(state_dir / "audit.jsonl", {"dropped": {"digest": env["id"], "reason": "meta tier"}, "at": now()})
                elif live(meta_repo) or any(live(d["to"]) for d in deliveries):
                    if write(f"/repos/{org}/{meta_repo}/issues", {
                        "title": f"[colab digest] {env['kind']} {env['transition']} in {env['repo']}: {env['summary'] or 'restricted item'}"[:250],
                        "body": f"{DELIVERY_MARKER}\nSource: {env['source']}\nTier: {env['tier']}\n\nSubscribers:\n"
                                + "\n".join(f"- {d['to']}" for d in deliveries),
                    }):
                        stats["deliveries"] += 1
                else:
                    stats["dry_run"] += 1
                    _append(state_dir / "would-deliver.jsonl", {"digest": [d["to"] for d in deliveries], "envelope": env["id"], "at": now()})
                deliveries = []
            for d in deliveries:
                if live(d["to"]) and d["via"] == "issue":
                    if write(f"/repos/{org}/{d['to']}/issues", {
                        "title": f"[colab] {env['kind']} {env['transition']} in {env['repo']}: {env['summary'] or 'restricted item'}"[:250],
                        "body": f"{DELIVERY_MARKER}\nDelivered by colab-bus.\n\nSource: {env['source']}\nTier: {env['tier']}\nEnvelope: `{env['id']}`",
                    }):
                        stats["deliveries"] += 1
                else:
                    stats["dry_run"] += 1
                    _append(state_dir / "would-deliver.jsonl", {**d, "at": now()})
            save()  # persist seen ids after every envelope, so a crash never re-delivers
        if items:
            cursors[repo] = max(i["updated_at"] for i in items)
            save()
    return stats


def heartbeat(gh, org: str, meta: str, stats: dict) -> None:
    value = json.dumps({"at": now(), **stats})
    path = f"/repos/{org}/{meta}/actions/variables/COLAB_BUS_HEARTBEAT"
    try:
        gh.send("PATCH", path, {"name": "COLAB_BUS_HEARTBEAT", "value": value})
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
        gh.send("POST", f"/repos/{org}/{meta}/actions/variables", {"name": "COLAB_BUS_HEARTBEAT", "value": value})


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta", type=Path, required=True, help="local clone of the meta repo (reads .colab/*)")
    ap.add_argument("--state-dir", type=Path, required=True, help="private group storage for bus state")
    ap.add_argument("--token-file", type=Path, help="0600 file holding a fine-grained token (preferred)")
    ap.add_argument("--bot", help="login whose events are ignored (default: the token's own login)")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGUSR1):
        signal.signal(sig, lambda *_: stop.update(flag=True))
    gh_token = read_token(args.token_file)
    backoff, failed = 60, False
    while True:
        try:
            # Pick up config, subscription, and rollout changes (including reverts) every poll.
            subprocess.run(["git", "-C", str(args.meta), "pull", "--ff-only", "-q"], capture_output=True)
            with open(args.meta / ".colab" / "config.toml", "rb") as fh:
                config = tomllib.load(fh)
            rollout = load_json(args.meta / ".colab" / "rollout.json", {"spaces": {}})
            subs_path = args.meta / ".colab" / "subscriptions.toml"
            subs = tomllib.loads(subs_path.read_text()).get("subscribe", []) if subs_path.exists() else []
            state_path = args.state_dir / "state.json"
            state = load_json(state_path, {})
            gh = GitHub(gh_token, state)
            bot = args.bot or (gh.get("/user") or {}).get("login", "")

            def save() -> None:
                clean = {k: v for k, v in state.items() if not k.startswith("_")}
                tmp = state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(clean))
                tmp.replace(state_path)

            stats = poll_once(gh, config, rollout, subs, state, args.state_dir, bot, save)
            save()
            heartbeat(gh, config["org"]["name"], config["org"]["meta_repo"], stats)
            print(json.dumps({"at": now(), **stats}), flush=True)
            backoff, failed = 60, False
            wait = int(config.get("bus", {}).get("poll_seconds", 300))
        except Exception as err:  # keep the job alive; the watchdog sees a stale heartbeat if this persists
            print(json.dumps({"at": now(), "error": repr(err)[:500]}), flush=True)
            wait, backoff, failed = backoff, min(backoff * 2, 3600), True
        if args.once:
            return 1 if failed else 0
        if stop["flag"]:
            return 0
        for _ in range(wait):
            if stop["flag"]:
                return 0
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
