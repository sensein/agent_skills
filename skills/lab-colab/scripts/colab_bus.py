#!/usr/bin/env python3
"""colab-bus: poll GitHub, build envelopes, tier-check, match subscriptions, deliver.

By default it runs as a scheduled GitHub Actions workflow in the meta repo (assets/templates/colab-bus.yml), one poll
per run (`--once --app`), signed in as the org's GitHub App: it mints the App's installation token itself from the
COLAB_APP_ID and COLAB_APP_PRIVATE_KEY secrets (openssl, or the `cryptography` package, signs the JWT), keeps the
token in memory only, and keeps its own state (cursors, ETags, seen ids, scan digests, security bookkeeping) on
colab-state as bus/<bus id>/state.json, in the run's single commit (no item text; restricted entries only under
sha256 keys), and appends its audit lines to audit/<YYYY-MM>.jsonl there. A standby (or an org without an App) runs the same code as a light
self-requeuing Slurm job under an operator's login (assets/templates/colab-bus.sbatch). Either way it polls instead
of receiving webhooks. Stdlib only, Python 3.11+.

Per destination space, from .colab/rollout.json:
  dry-run  log what would be delivered to <state>/would-deliver.jsonl; write nothing
  live     open a delivery issue in the subscriber repo (throttled)
A bus runs under the GitHub login of its operator ([[buses]] in the config; operators are the org team
colab-bus-operators). One bus is active per org: it holds lease.json on the meta repo's protected `colab-state`
branch, renewed every poll; standbys take over in order only when it is stale, and a returning primary stands by
until `colab.py bus handback <id>`. Each poll writes at most one commit there (lease, heartbeat/<bus id>.json,
security/state.json) with compare-and-set on the branch head. Without that branch (a meta repo not migrated yet)
the bus writes the deprecated Actions variables COLAB_BUS_HEARTBEAT and COLAB_SECURITY_STATE, for one release.
The heartbeat is written in both modes.

Safety rules enforced here:
  - destination tier = max(actual repo visibility, declared override); unknown repo = public
  - events only move to an equal or stricter tier; anything else is dropped and audited
  - restricted events carry no summary, and move only for spaces with the `restricted` switch
  - the bus never routes its own deliveries (body marker naming the bus and operator), so it cannot loop; each
    delivery carries an idempotency key (the envelope id), checked in the destination before creating an issue
  - it adds no labels anywhere: tier and space membership come from the config
  - tiers are per repo: [tiers] entry, else visibility; tier:* labels tighten, and a space:* label only tightens (its space's legacy tier);
    a digest's tier is the combined (strictest) tier of what it includes
  - space repos may live in other orgs (`owner/name` in [spaces]): they are tracked read-only, by default
    only items an org member contributed to ([bus] external_track; contributions by kind are recorded, with
    details read for at most [bus] external_detail_per_poll items per poll); a non-public one is restricted
    unless [tiers] lab lists it, an unreadable one is skipped (stats["unreadable"]); their labels never
    route; their summaries are delivered quoted, marked [external] and untrusted; nothing is ever written
    there, and subscriptions deliver only to repos in [org] name (others, and malformed entries, are dropped)
  - new or edited bodies, comments, and reviews are injection-scanned once per version (capped, time-budgeted); hits
    are audited (reason, truncated spans) in private state only. The bus publishes its security state (flags,
    verified clears and ignores, repo cursors) as security/state.json on colab-state: the trust anchor the CLI reads.
    The Project's Security field is display only and is overwritten where it disagrees
  - repo keys are lower-case; state saved under an older mixed-case key is migrated on the next poll
  - it validates each issue's fenced links block once per body version

Drift check (`[bus] drift_check_hours`, 0 = off): at most that often the bus runs colab.py observe + build_plan read-only
with the config it loaded, drops the actions expected to recur, and keeps one `Drift report` issue in the meta repo
in sync (edited in place, closed when clean). It never applies anything.

Project sync (`[bus] project_sync`, with `[org] project_number` set) is board bookkeeping on the
private org Project, not a delivery, so it runs in dry-run spaces too: newly opened items from
space repos are added to the Project (only the kinds in `[bus] project_add`, default ["issue"], so PRs
do not clutter the board) and their Spaces field is set in the same pass; every `project_sync_every`
polls the multi-select Spaces field is recomputed (repo membership plus space:<name> labels) for
items that differ.
  - one envelope per item transition (opened, closed, published), not per edit

State lives in --state-dir (private group storage, not scratch); with --app, in a private temporary folder for the
run, plus bus/<bus id>/state.json on colab-state. An Actions run's audit lines are appended to audit/<YYYY-MM>.jsonl on
colab-state (kind, item ref hashed when restricted, actor, comment id, time; no spans or reasons), and the run's log
prints how many lines of each kind it had.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# colab.py and colab_sync.py sit next to this file in the skill and in the meta repo. The pure logic (tiers, Spaces,
# envelopes, contributions), the GitHub client, and the Project queries are shared with `colab.py sync` and `inbox`,
# which do the same work under each member's own login: the relay is optional, and nothing depends on it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from colab_sync import (API, GRAPHQL_URL, RANK, DELIVERY_MARKER, MARKED, M_ADD, M_CLEAR, M_SELECT, M_SET, M_TEXT,  # noqa: E402,F401
                        Q_AUDIT, Q_ITEMS, Q_PROJECT, Q_PROJECT_FIELDS, TITLE_MAX, UNTRUSTED, BUG, CLOSING, DetailBudget,
                        GitHub, GraphQLError, _number, _pages, _strictest, closes, contributions_from, delivery_text,
                        delivery_title, desired_spaces, dest_tier, envelopes_for, fetch_contributions, involves, is_bug,
                        is_delivery, matches, may_deliver, merge_contributions, normalise_subscriptions, org_members,
                        project_field_ids, ProjectLookupError, running_jobs, SUB_KINDS, SUB_SCHEMES, DELIVERY_KEY,
                        already_delivered,
                        quote_within, tier_of)
SEEN_CAP = 20000


# --------------------------------------------------------------------------- pure logic (the rest is in colab_sync.py)


def repo_tiers(config: dict) -> dict[str, str]:
    """repo key (bare for the lab's org, owner/name otherwise) -> its declared tier: `[tiers]`, tightened by a
    legacy space-level `tier` (see colab.declared_tiers)."""
    return declared_tiers(config)


SUPPRESSED_PER_ITEM = 10  # outsider hits kept per item for promotion
HOOK_PENDING_CAP = 200  # texts carried over for the injection hook
HOOK_TEXT_CAP = 20000
COMMENT_PAGES = 30  # pages of 100 comments read per repo per poll before the cursor is held back
CONTRIB_STATE_CAP = 5000


def route(env: dict, subscriptions: list[dict], to_tiers: dict[str, str], bot_login: str,
          restricted_ok: bool = False) -> tuple[list[dict], list[dict]]:
    """Return (deliveries, drops) for one envelope."""
    if env["sender"] == bot_login:
        return [], []
    deliveries, drops = [], []
    for sub in subscriptions:
        if not matches(sub, env) or sub["subscriber"] == env["repo"]:
            continue
        if is_external(sub["subscriber"]):  # deliveries go only to repos in [org] name
            drops.append({"to": sub["subscriber"], "reason": "subscriber is outside the lab's org", "envelope": env["id"]})
            continue
        to = to_tiers.get(sub["subscriber"], "public")  # unknown repo: fail closed
        if env["tier"] == "restricted" and not restricted_ok:
            drops.append({"to": sub["subscriber"], "reason": "restricted switch is off", "envelope": env["id"]})
        elif may_deliver(env["tier"], to):
            deliveries.append({"to": sub["subscriber"], "via": sub.get("deliver", "issue"), "envelope": env["id"]})
        else:
            drops.append({"to": sub["subscriber"], "reason": f"{env['tier']} -> {to}", "envelope": env["id"]})
    return deliveries, drops


from colab import (app_login, bus_state_path, state_branch_readme, CLEAR_MARKER, LEASE_FILE, LEASE_SKEW_SECONDS, _parse_time, OPERATORS_TEAM, SECURITY_FILE, STATE_BRANCH, BusConfigError,  # noqa: E402
                   buses, compose_bus_lease, delivery_marker, heartbeat_path, lease_decision, lease_ttl_minutes,
                   CONTRIB_KINDS, HOOK_SECONDS_PER_POLL, IGNORE_MARKER, OUTSIDER_FLAGS_PER_DAY,  # noqa: E402
                   OUTSIDER_OPEN_FLAGS_PER_REPO, SECURITY_SIG_NAMESPACE, hashed_ref,
                   all_stewards, cap_text, claimants_from, combined_tier, involved_by, item_stewards, declared_tiers, format_contributions,  # noqa: E402
                   injection_scan, is_external, lab_opt_in, label_space_tiers, repo_key, repo_ref, repo_spaces)

try:  # shared grammar
    from colab import (build_plan, is_recurring, observe, parse_links, parse_sessions,  # noqa: E402
                       session_state, validate_links)
except ImportError:  # pragma: no cover
    parse_links = validate_links = parse_sessions = session_state = None
    build_plan = is_recurring = observe = None


def remember(state: dict, env_id: str) -> bool:
    """Record an envelope id; False if already seen. Trims oldest first."""
    seen: list = state.setdefault("seen", [])
    index = state.setdefault("_seen_index", set(seen))
    if env_id in index or hashed_key(env_id) in index:  # an Actions bus keeps restricted ids hashed
        return False
    seen.append(env_id)
    index.add(env_id)
    if len(seen) > SEEN_CAP:
        for old in seen[: len(seen) - SEEN_CAP]:
            index.discard(old)
        del seen[: len(seen) - SEEN_CAP]
    return True


# --------------------------------------------------------------------------- GitHub App authentication
#
# An Actions bus signs in as the org's GitHub App: a JWT (RS256, signed with the App's private key) proves the App, and
# is exchanged for a short-lived installation token for the org (GET /orgs/{org}/installation, then
# POST /app/installations/{id}/access_tokens). The key comes from an Actions secret through the environment; the token
# lives only in this process's memory and is minted again before it expires (GitHub gives it one hour). Signing uses
# `openssl dgst -sha256 -sign` (the key in a 0600 file in a private temporary folder, deleted at once), else the
# `cryptography` package when it is importable; nothing else is needed.

APP_TOKEN_REFRESH_SECONDS = 50 * 60  # mint a new installation token after this long (GitHub's last one hour)
APP_JWT_SECONDS = 9 * 60             # GitHub accepts at most 10 minutes


class AppAuthError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def rs256_sign(data: bytes, key_pem: str, runner=None) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256 over `data`: openssl first, then the `cryptography` package. Raises AppAuthError."""
    import shutil
    import tempfile
    runner = runner or subprocess.run
    errors = []
    if shutil.which("openssl"):
        folder = tempfile.mkdtemp(prefix="colab-app-")  # 0700
        key_path, proc = Path(folder) / "key.pem", None
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key_pem)
            proc = runner(["openssl", "dgst", "-sha256", "-sign", str(key_path)], input=data, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as err:
            errors.append(f"openssl: {err.__class__.__name__}")
        finally:
            try:
                key_path.unlink()
            except OSError:
                pass
            try:
                os.rmdir(folder)
            except OSError:
                pass
        if proc is not None and proc.returncode == 0 and proc.stdout:
            return proc.stdout
        if proc is not None:
            errors.append("openssl could not sign (is COLAB_APP_PRIVATE_KEY the App's .pem private key?)")
    else:
        errors.append("openssl is not on PATH")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        errors.append("the cryptography package is not installed")
    else:
        try:
            key = serialization.load_pem_private_key(key_pem.encode(), password=None)
            return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, TypeError) as err:
            errors.append(f"cryptography: {err.__class__.__name__}")
    raise AppAuthError("could not sign the App's JWT: " + "; ".join(errors))


def app_jwt(app_id: str | int, key_pem: str, now_ts: float | None = None, signer=None) -> str:
    """A GitHub App JWT: iat 60 s in the past (clock drift), exp 9 minutes ahead, iss the App id."""
    now_ts = int(now_ts if now_ts is not None else time.time())
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iat": now_ts - 60, "exp": now_ts + APP_JWT_SECONDS, "iss": str(app_id)},
                              separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    return f"{head}.{body}.{_b64url((signer or rs256_sign)(signing_input, key_pem))}"


def _app_http(method: str, path: str, jwt: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def mask(value: str, out=None) -> None:
    """In GitHub Actions, ask the runner to hide `value` in the job log (the command itself is not shown)."""
    if value and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}", file=out or sys.stdout, flush=True)


class AppAuth:
    """Installation tokens for one org, minted from the App id and private key and kept only in memory. `token()` is
    what GitHub calls for each request: it mints a new one once the current one is APP_TOKEN_REFRESH_SECONDS old or
    within ten minutes of its expiry. `login` is the App's bot login, `<slug>[bot]`."""

    def __init__(self, app_id: str, key_pem: str, org: str, http=None, clock=None, signer=None):
        if not str(app_id).strip().isdigit():
            raise AppAuthError("COLAB_APP_ID must be the App's number (an Actions secret of the meta repo)")
        if "PRIVATE KEY" not in (key_pem or ""):
            raise AppAuthError("COLAB_APP_PRIVATE_KEY must hold the App's .pem private key (an Actions secret of the meta repo)")
        self.app_id, self._key, self.org = str(app_id).strip(), key_pem, org
        self.http, self.clock, self.signer = http or _app_http, clock or time.time, signer
        self._token, self._minted, self._expires = "", 0.0, 0.0
        self.slug, self.installation = "", None

    @classmethod
    def from_env(cls, org: str, env=None, **kw) -> "AppAuth":
        env = os.environ if env is None else env
        return cls(env.get("COLAB_APP_ID", ""), env.get("COLAB_APP_PRIVATE_KEY", ""), org, **kw)

    @property
    def login(self) -> str:
        return app_login(self.slug) if self.slug else ""

    def mint(self) -> str:
        jwt = app_jwt(self.app_id, self._key, self.clock(), signer=self.signer)
        try:
            if not self.slug:
                self.slug = str((self.http("GET", "/app", jwt) or {}).get("slug") or "").lower()
            if self.installation is None:
                inst = self.http("GET", f"/orgs/{self.org}/installation", jwt) or {}
                self.installation = inst.get("id")
            if not self.installation:
                raise AppAuthError(f"the App is not installed on {self.org}")
            out = self.http("POST", f"/app/installations/{self.installation}/access_tokens", jwt, {}) or {}
        except urllib.error.HTTPError as err:
            hint = {401: "the App id or private key is wrong", 404: f"the App is not installed on {self.org}"}.get(err.code, "")
            raise AppAuthError(f"GitHub refused the App sign-in (HTTP {err.code}{': ' + hint if hint else ''})") from None
        token = str(out.get("token") or "")
        if not token:
            raise AppAuthError("GitHub gave no installation token")
        mask(token)
        self._token, self._minted = token, self.clock()
        try:
            self._expires = datetime.fromisoformat(str(out.get("expires_at")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            self._expires = self._minted + 3600
        return token

    def token(self) -> str:
        t = self.clock()
        if not self._token or t - self._minted >= APP_TOKEN_REFRESH_SECONDS or self._expires - t < 600:
            return self.mint()
        return self._token


def read_token(token_file: Path | None) -> str:
    """Prefer a 0600 token file (fine-grained PAT or App token), then GITHUB_TOKEN, then gh."""
    if token_file:
        if token_file.stat().st_mode & 0o077:
            raise SystemExit(f"{token_file} must not be readable by group or others (chmod 600)")
        return token_file.read_text().strip()
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


# --------------------------------------------------------------------------- Project sync


class ProjectSync:
    """Keeps the org Project's items and multi-select Spaces field current. Never raises."""

    def __init__(self, gh, config: dict, state: dict, state_dir: Path, stats: dict, member_of: dict[str, list[str]]):
        bus = config.get("bus", {})
        self.gh, self.state, self.state_dir, self.stats, self.member_of = gh, state, state_dir, stats, member_of
        self.org = config["org"]["name"]
        # orgs whose items the sweep manages: the lab's own, plus the owners of external space repos
        self.owners = {self.org.lower()} | {k.split("/")[0].lower() for k in member_of if is_external(k)}
        self.number = int(config["org"].get("project_number") or 0)
        self.enabled = bool(bus.get("project_sync", False)) and self.number > 0
        self.every = max(1, int(bus.get("project_sync_every", 6)))
        # envelope kinds that are added to the Project; PRs are off by default
        wanted = bus.get("project_add", ["issue"])
        wanted = [wanted] if isinstance(wanted, str) else list(wanted or [])
        self.add_kinds = {k for k in wanted if k in ("issue", "pull_request")}
        for k in sorted(set(wanted) - self.add_kinds):
            print(f"colab-bus: ignoring unknown [bus] project_add kind {k!r} (use \"issue\" or \"pull_request\")", file=sys.stderr)
            _append(state_dir / "audit.jsonl", {"project_add_unknown_kind": str(k), "at": now()})
        self.interval = bus.get("write_interval_seconds", 1.0)
        # Non-public external items stay off the shared Project unless [sync] external_private = "board",
        # the same rule members' own sync follows (they see them live in next --me / inbox).
        self.external_private_board = str((config.get("sync") or {}).get("external_private", "personal")) == "board"
        self._ids: dict | None = None
        self._noted: set[str] = set()
        # Set by poll_once: the Security value the bus's state says to show, the hot-item note, and the call budget.
        self.display_value = self.note_hot = None
        self.write_budget: dict | None = None
        self.display_checked: dict = {}
        stats.setdefault("project_writes", 0)

    def _fail(self, what: str, err: Exception) -> None:
        self.stats["errors"] += 1
        _append(self.state_dir / "audit.jsonl", {"project_write_failed": what, "error": str(err)[:500], "at": now()})

    def _write(self, what: str, query: str, variables: dict):
        """True on success; for an add, the new item's id (or True when the reply carries none)."""
        try:
            reply = self.gh.graphql(query, variables)
        except (urllib.error.URLError, OSError, GraphQLError) as err:
            self._fail(what, err)
            return False
        self.stats["project_writes"] += 1
        time.sleep(self.interval)
        item = ((reply or {}).get("addProjectV2ItemById") or {}).get("item") or {}
        return item.get("id") or True

    def ids(self) -> dict | None:
        """Project node id, Spaces field id, and option name -> id; cached per sync, refreshed each sweep."""
        if self._ids is None:
            try:
                ids, missing = project_field_ids(self.gh, self.org, self.number)
            except ProjectLookupError as err:
                self._fail(err.what, err.cause)
                return None
            for name in ("Contributions", "Security"):
                if name in missing:
                    self._missing(name)
                else:
                    self.state.get("project_fields_missing", {}).pop(name, None)
            self._ids = ids
        return self._ids

    def add(self, env: dict) -> None:
        """Add a newly opened item to the Project (idempotent on GitHub's side), when its kind is in
        `project_add`, and set its Spaces field in the same pass instead of waiting for the sweep."""
        if not self.enabled or not env.get("node_id") or env["kind"] not in self.add_kinds:
            return
        ids = self.ids()
        if not ids:
            return
        item_id = self._write(f"add {env['id']}", M_ADD, {"project": ids["project"], "content": env["node_id"]})
        if not isinstance(item_id, str) or not ids["field"]:
            return  # the sweep will set Spaces later
        want = sorted(env.get("spaces") or [])
        known = [n for n in want if n in ids["options"]]
        for n in set(want) - set(known):
            _append(self.state_dir / "audit.jsonl", {"project_unknown_space": n, "item": item_id, "at": now()})
        if known:
            self._write(f"set Spaces {item_id}", M_SET, {"project": ids["project"], "item": item_id, "field": ids["field"],
                                                         "options": [ids["options"][n] for n in known]})

    def _missing(self, field: str) -> None:
        """Audit a Project field the config wants but the board lacks (run plan/apply): once per field per day, not
        every poll. Writes to that field are skipped; the fields that exist keep syncing."""
        if field in self._noted:
            return
        self._noted.add(field)
        noted = self.state.setdefault("project_fields_missing", {})
        if noted.get(field, "")[:10] != now()[:10]:
            noted[field] = now()
            _append(self.state_dir / "audit.jsonl", {"project_field_missing": field, "at": now()})

    def track_external(self, env: dict, by: dict, entry: dict, involved: bool, opened_after_baseline: bool) -> None:
        """Add an external item to the Project once it involves the group (or, with external_track = "all",
        when it opened after the baseline), set its Spaces, and keep its Contributions text current.
        `entry` is the item's record in bus state; it remembers the Project item id and the last text written."""
        if not self.enabled or not env.get("node_id") or env["kind"] not in self.add_kinds:
            return
        if env.get("tier") == "restricted" and not self.external_private_board:
            return  # a non-public external repo: off the shared board ([sync] external_private = "personal")
        ids = self.ids()
        if not ids:
            return
        if not entry.get("project_item"):
            if not (involved or opened_after_baseline):
                return
            item_id = self._write(f"add {env['id']}", M_ADD, {"project": ids["project"], "content": env["node_id"]})
            if not isinstance(item_id, str):
                return
            entry["project_item"] = item_id
            known = [n for n in sorted(env.get("spaces") or []) if n in ids["options"]]
            if known and ids["field"]:
                self._write(f"set Spaces {item_id}", M_SET, {"project": ids["project"], "item": item_id, "field": ids["field"],
                                                             "options": [ids["options"][n] for n in known]})
        text = format_contributions(by)
        if text == entry.get("contributions_text", ""):
            return
        if not ids.get("contributions"):
            self._missing("Contributions")
            return
        if self._write(f"set Contributions {entry['project_item']}", M_TEXT,
                       {"project": ids["project"], "item": entry["project_item"], "field": ids["contributions"], "text": text}):
            entry["contributions_text"] = text

    def set_security(self, node_id: str, project_item: str | None, value: str) -> str | None:
        """Set Security = value on an item (adding it to the Project first when needed; adding is idempotent).
        Returns the Project item id on success, else None."""
        if not self.enabled or not (node_id or project_item):
            return None
        ids = self.ids()
        if not ids:
            return None
        option = ids.get("security_options", {}).get(value)
        if not ids.get("security") or not option:
            self._missing("Security")
            return None
        item_id = project_item or self._write(f"add {node_id}", M_ADD, {"project": ids["project"], "content": node_id})
        if not isinstance(item_id, str):
            return None
        ok = self._write(f"set Security {item_id}", M_SELECT,
                         {"project": ids["project"], "item": item_id, "field": ids["security"], "option": option})
        return item_id if ok else None

    def flag(self, node_id: str, project_item: str | None = None) -> str | None:
        return self.set_security(node_id, project_item, "flagged")

    def sweep_due(self) -> bool:
        """True on the first poll and then every project_sync_every polls."""
        n = self.state.get("project_polls", 0)
        self.state["project_polls"] = n + 1
        return self.enabled and n % self.every == 0

    def sweep(self) -> None:
        """Page through every Project item and fix the Spaces value where it differs from the desired one, and the
        (display-only) Security value where it disagrees with the bus's state."""
        self._ids = None
        ids = self.ids()
        if not ids or not ids["field"]:
            if ids:
                self._fail("sweep", RuntimeError("Spaces field not found on the Project"))
            return
        after = None
        mismatched: list[tuple[str, str]] = []
        for _ in range(500):
            try:
                page = self.gh.graphql(Q_ITEMS, {"project": ids["project"], "after": after})["node"]["items"]
            except (urllib.error.URLError, OSError, GraphQLError, KeyError, TypeError) as err:
                self._fail("sweep read", err)
                break
            for item in page["nodes"]:
                content = item.get("content") or {}
                repo = content.get("repository") or {}
                owner = (repo.get("owner") or {}).get("login") or ""
                if content.get("__typename") not in ("Issue", "PullRequest") or owner.lower() not in self.owners:
                    continue
                labels = [x["name"] for x in (content.get("labels") or {}).get("nodes", [])]
                value = str(((item.get("security") or {}).get("name")) or "").lower()
                if self.note_hot:
                    self.note_hot(content, str((item.get("status") or {}).get("name") or ""),
                                  str((item.get("ready") or {}).get("name") or ""))
                want_display = self.display_value(content, value) if self.display_value else None
                if want_display and want_display != value:
                    mismatched.append((item["id"], want_display))
                want = desired_spaces(repo_key(f"{owner}/{repo['name']}", self.org), labels, self.member_of)
                have = sorted(o["name"] for o in ((item.get("fieldValueByName") or {}).get("options") or []))
                if want == have:
                    continue
                known = [n for n in want if n in ids["options"]]
                for n in set(want) - set(known):
                    _append(self.state_dir / "audit.jsonl", {"project_unknown_space": n, "item": item["id"], "at": now()})
                base = {"project": ids["project"], "item": item["id"], "field": ids["field"]}
                if known:
                    self._write(f"set Spaces {item['id']}", M_SET, {**base, "options": [ids["options"][n] for n in known]})
                elif have:
                    self._write(f"clear Spaces {item['id']}", M_CLEAR, base)
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        self.fix_display(mismatched)

    def fix_display(self, mismatched: list[tuple[str, str]]) -> None:
        """Overwrite Security values that disagree with the bus's state, on every item the sweep saw, oldest-checked
        first, within the poll's call budget; the rest wait for the next sweep."""
        mismatched.sort(key=lambda m: self.display_checked.get(m[0], ""))
        for item_id, value in mismatched:
            if self.write_budget is not None:
                if self.write_budget["left"] <= 0:
                    self.stats["display_carried"] = self.stats.get("display_carried", 0) + 1
                    continue
                self.write_budget["left"] -= 1
            if self.set_security("", item_id, value):
                self.display_checked.pop(item_id, None)
                self.display_checked[item_id] = now()
                _append(self.state_dir / "audit.jsonl", {"security_display_fixed": item_id, "value": value, "at": now()})
        while len(self.display_checked) > SEEN_CAP:
            del self.display_checked[next(iter(self.display_checked))]


# --------------------------------------------------------------------------- loop


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(path: Path, record: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def session_stats(gh, config: dict, state: dict, state_dir: Path, jobs=running_jobs) -> dict:
    """Count active, waiting, and stale agent sessions from the Session activity issue."""
    sess = config.get("sessions", {})
    if not sess.get("issue") or parse_sessions is None:
        return {}
    org, meta_repo = config["org"]["name"], config["org"].get("meta_repo", "lab-colab")
    comments = []
    for page in range(1, 11):
        batch = gh.get(f"/repos/{org}/{meta_repo}/issues/{sess['issue']}/comments?per_page=100&page={page}", cache=True) or []
        comments += batch
        if len(batch) < 100:
            break
    sessions = parse_sessions(comments)
    running = jobs({x["job"] for x in sessions if x.get("job")})
    now_t = datetime.now(timezone.utc)
    counts = {"sessions_active": 0, "sessions_waiting": 0, "sessions_stale": 0}
    reported = state.setdefault("stale_reported", {})
    for x in sessions:
        st = session_state(x, now_t, int(sess.get("stale_minutes", 60)), running)
        counts[f"sessions_{st}"] += 1
        if st == "stale" and x.get("item") and reported.get(x["session"]) != x["beat"].isoformat():
            reported[x["session"]] = x["beat"].isoformat()
            _append(state_dir / "audit.jsonl", {"stale_session": x["session"], "user": x["user"], "item": x["item"],
                                                "last_beat": x["beat"].isoformat(), "at": now()})
    return counts


DRIFT_TITLE = "Drift report"
DRIFT_MARKER = "<!-- colab-drift -->"
DRIFT_RETRY_SECONDS = 3600


def drift_due(config: dict, state: dict, now_t: datetime) -> bool:
    """True when `[bus] drift_check_hours` have passed since the last good check (and an hour since a failed one)."""
    hours = float(config.get("bus", {}).get("drift_check_hours", 0) or 0)
    if hours <= 0:
        return False
    def age(key: str) -> float | None:
        raw = state.get(key)
        return (now_t - datetime.fromisoformat(raw)).total_seconds() if raw else None
    since_ok, since_fail = age("drift_checked_at"), age("drift_failed_at")
    if since_ok is not None and since_ok < hours * 3600:
        return False
    return since_fail is None or since_fail >= DRIFT_RETRY_SECONDS


def drift_body(actions: list[dict]) -> str:
    cfg = "<meta clone>/.colab/config.toml"
    lines = [DELIVERY_MARKER, DRIFT_MARKER,
             "The bus compared the org with `.colab/config.toml` (read-only) and these actions are proposed. "
             "Nothing was applied. This issue is edited in place at each check and closed when the plan is clean.", ""]
    lines += [f"- `{a['kind']}`: {a['desc']}" for a in actions]
    lines += ["", "Review, then apply the `gh` actions yourself (`local` and `manual` actions are printed for you to do):", "",
              "```bash", f"python3 colab.py --config {cfg} plan", f"python3 colab.py --config {cfg} apply --yes", "```", "",
              "If a change was intentional, update the config by PR instead. Expected recurring steps "
              "(gates, starting the bus, own-template notes) are left out of this list."]
    return "\n".join(lines)


def find_drift_issue(gh, org: str, meta: str, state: dict) -> dict | None:
    """The existing Drift report issue (open or closed): the remembered number, else a scan of recent issues."""
    if state.get("drift_issue"):
        item = gh.get(f"/repos/{org}/{meta}/issues/{state['drift_issue']}", cache=True)
        if isinstance(item, dict) and item.get("title") == DRIFT_TITLE:
            return item
    for page in range(1, 6):
        batch = gh.get(f"/repos/{org}/{meta}/issues?state=all&per_page=100&page={page}", cache=True) or []
        for item in batch:
            if item.get("title") == DRIFT_TITLE and "pull_request" not in item and DRIFT_MARKER in (item.get("body") or ""):
                state["drift_issue"] = item["number"]
                return item
        if len(batch) < 100:
            break
    return None


def drift_check(gh, config: dict, state: dict, state_dir: Path, stats: dict, now_t: datetime | None = None,
                observe_fn=None, plan_fn=None) -> None:
    """Read-only drift check: observe and plan with the loaded config, then report in one issue.

    Never applies anything and never raises. Sets stats["drift_actions"] when a check ran.
    """
    now_t = now_t or datetime.now(timezone.utc)
    observe_fn, plan_fn = observe_fn or observe, plan_fn or build_plan
    if observe_fn is None or plan_fn is None or not drift_due(config, state, now_t):
        return
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    try:
        snapshot = observe_fn(config)
        if not snapshot.get("repos") or not snapshot.get("meta_repo_exists"):
            raise RuntimeError("observe saw no repos or no meta repo (gh unavailable or unauthenticated?); not reporting drift")
        remaining = [a for a in plan_fn(config, snapshot) if not is_recurring(a)]
        issue = find_drift_issue(gh, org, meta, state)
        if remaining:
            body = drift_body(remaining)
            if issue is None:
                made = gh.send("POST", f"/repos/{org}/{meta}/issues", {"title": DRIFT_TITLE, "body": body})
                state["drift_issue"] = made.get("number")
            elif issue.get("body") != body or issue.get("state") != "open":
                gh.send("PATCH", f"/repos/{org}/{meta}/issues/{issue['number']}", {"title": DRIFT_TITLE, "body": body, "state": "open"})
        elif issue is not None and issue.get("state") == "open":
            gh.send("POST", f"/repos/{org}/{meta}/issues/{issue['number']}/comments",
                    {"body": f"The org matches the config again as of {now_t.isoformat(timespec='minutes')}. Closing.\n\n{DELIVERY_MARKER}"})
            gh.send("PATCH", f"/repos/{org}/{meta}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
    except Exception as err:  # a drift check must never take the bus down
        stats["errors"] = stats.get("errors", 0) + 1
        state["drift_failed_at"] = now_t.isoformat()
        _append(state_dir / "audit.jsonl", {"drift_check_failed": repr(err)[:500], "at": now()})
        return
    state["drift_checked_at"] = now_t.isoformat()
    state.pop("drift_failed_at", None)
    state["drift_actions"] = stats["drift_actions"] = len(remaining)




def valid_subscriptions(subs, org: str, state_dir: Path, stats: dict) -> list[dict]:
    """Normalise each [[subscribe]] entry (colab_sync.normalise_subscriptions: repo names like [spaces] entries; bare =
    [org] name). A malformed entry is dropped and audited (`subscription_invalid`), so one bad entry never stops a
    poll. A subscriber in another org stays qualified, and route() drops every delivery to it."""
    out, bad = normalise_subscriptions(subs, org)
    for i, entry, why in bad:
        stats["errors"] += 1
        _append(state_dir / "audit.jsonl", {"subscription_invalid": i, "entry": entry, "error": why, "at": now()})
    return out


def migrate_case(state: dict, keys: set[str]) -> None:
    """Repo keys are lower-case now. Move state saved under an older mixed-case key (e.g. `BrainKB`) to
    the lower-case key, including seen envelope ids and links checks, so nothing re-delivers. Idempotent."""
    renames: dict[str, str] = {}
    for table in ("cursors", "baseline_at"):
        saved = state.get(table) or {}
        for old in [k for k in saved if k != k.lower() and k.lower() in keys]:
            renames[old] = old.lower()
            saved.setdefault(old.lower(), saved[old])
            del saved[old]
    if not renames:
        return
    def fix(text: str) -> str:
        for old, new in renames.items():
            for sep in (":", "#"):
                if text.startswith(old + sep):
                    return new + text[len(old):]
        return text
    if state.get("seen"):
        state["seen"] = [fix(x) for x in state["seen"]]
        state.pop("_seen_index", None)
    if state.get("links_checked"):
        state["links_checked"] = {fix(k): v for k, v in state["links_checked"].items()}


def poll_once(gh, config: dict, rollout: dict, subs: list[dict], state: dict, state_dir: Path,
              bot: str, save=lambda: None, store=None, bus_id: str = "", operator: str = "", writers=None) -> dict:
    org = config["org"]["name"]
    meta_repo = config["org"].get("meta_repo", "lab-colab")
    spaces = config.get("spaces") or {}
    member_of = repo_spaces(config)
    declared = repo_tiers(config)
    opt_in = lab_opt_in(config)
    space_state = rollout.get("spaces", {})
    interval = config.get("bus", {}).get("write_interval_seconds", 1.0)
    threshold = config.get("bus", {}).get("fanout_digest_threshold", 20)
    stats = {"envelopes": 0, "deliveries": 0, "drops": 0, "dry_run": 0, "errors": 0}
    writers = set(writers) if writers is not None else {operator or bot}  # whose delivery issues count for a key
    # External repos: by default only items that involve org members ([bus] external_track = "involved");
    # "all" takes every item. Any other value counts as "involved". The member list is read once per poll.
    track_all = config.get("bus", {}).get("external_track", "involved") == "all"
    members: dict = {}

    project = ProjectSync(gh, config, state, state_dir, stats, member_of)

    # Repo names in subscriptions are normalised like [spaces] entries: bare = [org] name. A subscriber in
    # another org stays qualified, and route() drops (and the loop below audits) every delivery to it.
    subs = valid_subscriptions(subs, org, state_dir, stats)
    meta_key = repo_key(meta_repo, org)
    migrate_case(state, set(member_of) | {s["subscriber"] for s in subs})

    def path(repo: str) -> str:
        return "/repos/" + repo_ref(repo, org)

    # Visibility of every repo we read from or deliver to, fetched fresh each poll (ETag-cheap).
    # Repos in other orgs are read with the bus token, which may have no access there: a 404/403 is
    # skipped and listed in stats["unreadable"], never counted as public.
    repos = sorted(set(member_of) | {s["subscriber"] for s in subs if not is_external(s["subscriber"])} | {meta_key})
    visibility: dict[str, str | None] = {}
    for repo in repos:
        try:
            meta = gh.get(path(repo), cache=True)
            visibility[repo] = meta.get("visibility") if isinstance(meta, dict) else None
        except urllib.error.HTTPError as err:
            visibility[repo] = None
            if not (is_external(repo) and err.code in (403, 404)):
                stats["errors"] += 1
        except urllib.error.URLError:
            visibility[repo] = None
            stats["errors"] += 1
        if visibility[repo] is None and is_external(repo):
            stats.setdefault("unreadable", []).append(repo)
    state["_visibility"] = dict(visibility)  # not saved: which external repos are public, for an Actions bus's own state
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
        if checked.get(key) == digest or checked.get(hashed_key(key)) == digest:
            return
        checked[key] = digest
        edges, errors = parse_links(body)
        errors += validate_links(edges, config, (item.get("type") or {}).get("name") if isinstance(item.get("type"), dict) else None)
        if not errors:
            return
        _append(state_dir / "audit.jsonl", {"links_errors": key, "errors": errors, "at": now()})
        if live(repo) and not is_external(repo):  # never write into another org's repos
            gh_comment = "\n".join(["The links block has problems:", "", *[f"- {e}" for e in errors], "",
                                     "Format: `<edge>: <owner>/<repo>#<n>` or `<edge>: kg:<id>`, one per line.", DELIVERY_MARKER])
            write(f"{path(repo)}/issues/{item.get('number')}/comments", {"body": gh_comment})

    detail = {"left": max(0, int(config.get("bus", {}).get("external_detail_per_poll", 50)))}
    calls = {"left": max(0, int(config.get("bus", {}).get("external_calls_per_poll", 300)))}
    hook_budget = {"left": float((config.get("scan") or {}).get("injection_seconds_per_poll", HOOK_SECONDS_PER_POLL))}
    hook_pending = state.setdefault("hook_pending", [])
    contrib = state.setdefault("contributions", {})
    scanned = state.setdefault("scanned", {})
    flagged = state.setdefault("security", {})
    scan_on = bool(config.get("bus", {}).get("injection_scan", True))

    def member_set():
        if "set" not in members:
            members["set"] = org_members(gh, org)
            if members["set"] is None:
                stats["errors"] += 1
                _append(state_dir / "audit.jsonl", {"org_members_unreadable": org, "skipped": "external items", "at": now()})
        return members["set"]

    def select_external(repo: str, items: list[dict]) -> tuple[list[dict], str | None, bool]:
        """(kept items, cursor to save when details were carried over, carried). An item is kept when a member
        contributed to it (or always with external_track = "all"). Details (comments, reviews, timeline) are read
        only for items that changed since they were last read, at most external_detail_per_poll per poll; the
        rest wait for the next poll, and the cursor stops at the last item handled."""
        mem = member_set()
        if mem is None and not track_all:
            return [], None, False
        kept, last = [], None
        for item in sorted(items, key=lambda i: i.get("updated_at") or ""):
            key = f"{repo}#{item.get('number')}"
            entry = contrib.get(key)
            if entry and entry.get("updated_at") == item.get("updated_at"):
                by = entry.get("by") or {}
            elif mem is None:  # "all" without a member list: keep it, record nothing, read details later
                by = (entry or {}).get("by") or {}
            else:
                if detail["left"] <= 0:
                    stats["detail_carried"] = stats.get("detail_carried", 0) + 1
                    return kept, last, True
                detail["left"] -= 1
                try:
                    found = fetch_contributions(gh, path(repo), item, mem, calls)
                except DetailBudget:
                    stats["detail_carried"] = stats.get("detail_carried", 0) + 1
                    _append(state_dir / "audit.jsonl", {"external_budget": "calls", "deferred": key, "at": now()})
                    return kept, last, True
                except urllib.error.URLError as err:
                    stats["errors"] += 1
                    _append(state_dir / "audit.jsonl", {"read_failed": f"{key} details", "error": str(err)[:300], "at": now()})
                    return kept, last, True
                stats["detail_reads"] = stats.get("detail_reads", 0) + 1
                by = merge_contributions((entry or {}).get("by"), found)
                entry = contrib.pop(key, None) or {}
                contrib[key] = {**entry, "updated_at": item.get("updated_at"), "by": by}
                while len(contrib) > CONTRIB_STATE_CAP:
                    del contrib[next(iter(contrib))]
            last = item.get("updated_at")
            item["_contributions"] = by
            if involved_by(by) or track_all:
                kept.append(item)
        return kept, None, False

    sec_cfg = config.get("security") or {}
    outsider_limit = int(sec_cfg.get("outsider_flags_per_day", OUTSIDER_FLAGS_PER_DAY))
    outsider_repo_cap = int(sec_cfg.get("outsider_open_flags_per_repo", OUTSIDER_OPEN_FLAGS_PER_REPO))
    closed_flags = state.setdefault("security_closed", {})  # flags on closed items: out of the published state

    suppressed = state.setdefault("security_suppressed", {})  # outsider hits kept audit-only, per item

    def keep_suppressed(key: str, where: str, found: dict, node_id: str, item: dict | None, repo: str) -> None:
        rows = suppressed.pop(key, [])[-(SUPPRESSED_PER_ITEM - 1):]
        rows.append({"where": where, "cat": _categories(found["reason"]), "reason": found["reason"][:200],
                     "at": now().replace("+00:00", "Z"), "node_id": node_id,
                     "restricted": tier_for(item, repo) == "restricted"})
        suppressed[key] = rows
        while len(suppressed) > SEEN_CAP:
            del suppressed[next(iter(suppressed))]

    def promote(key: str, why: str) -> None:
        """Flag an item whose outsider hits were kept audit-only, now that someone is working on it."""
        rows = suppressed.pop(key, [])
        if not rows or key in flagged:
            return
        last = rows[-1]
        item_id = project.flag(last.get("node_id", ""), (contrib.get(key) or {}).get("project_item"))
        flagged[key] = {"flagged_at": now().replace("+00:00", "Z"), "where": last["where"], "reason": last["reason"],
                        "ref": ref_of(key), "node_id": last.get("node_id", ""), "project_item": item_id,
                        "outsider": False, "restricted": any(r.get("restricted") for r in rows)}
        stats["flagged"] = stats.get("flagged", 0) + 1
        _append(state_dir / "audit.jsonl", {"security_flag_promoted": key, "why": why, "hits": len(rows), "at": now()})

    def tier_for(item: dict | None, repo: str) -> str:
        labels = [lab["name"] if isinstance(lab, dict) else lab for lab in (item or {}).get("labels", [])]
        return tier_of(visibility.get(repo), labels, _strictest(declared.get(repo), *label_space_tiers(config, repo, labels)),
                       is_external(repo), repo in opt_in)

    def track_open_state(repo: str, item: dict) -> None:
        """A closed item's flag leaves the published state (so throwaway issues age out); reopening restores it."""
        key = f"{repo}#{item.get('number')}"
        if item.get("state") == "closed" and key in flagged:
            closed_flags[key] = {**flagged.pop(key), "closed_at": now().replace("+00:00", "Z")}
            _append(state_dir / "audit.jsonl", {"security_flag_closed": key, "at": now()})
        elif item.get("state") != "closed" and key in closed_flags:
            flagged[key] = {k: v for k, v in closed_flags.pop(key).items() if k != "closed_at"}
            _append(state_dir / "audit.jsonl", {"security_flag_reopened": key, "at": now()})
        while len(closed_flags) > SEEN_CAP:
            del closed_flags[next(iter(closed_flags))]
    outsider_counts = state.setdefault("outsider_flags", {})
    marked = state.setdefault("security_unreviewed", {})
    verified = state.setdefault("security_verified", {"clears": {}, "ignored": {}})  # audit records the bus accepted
    hot = state.setdefault("security_hot", {})  # refs that are Ready/Agent-ready, in progress, or assigned (from the sweep)

    def ref_of(key: str) -> str:
        """State key (repo key#n) -> `owner/repo#n`, lower-case: the item ref used in COLAB_SECURITY_STATE."""
        repo, _, number = key.rpartition("#")
        return f"{repo_ref(repo, org)}#{number}".lower()

    def flagged_by_ref(ref: str) -> tuple[str, dict] | tuple[None, None]:
        for key, rec in flagged.items():
            if ref_of(key) == ref:
                return key, rec
        # A restricted flag kept only by hash (taken over, or an Actions bus's saved state): pick it up again by ref,
        # so it can be cleared and shown like any other.
        carried = state.get("security_inherited") or {}
        name = hashed_ref(ref)
        f = (carried.get("flags") or {}).get(name)
        if isinstance(f, dict) and f.get("at") and not f.get("closed"):
            repo_full, _, number = ref.rpartition("#")
            try:
                key = f"{repo_key(repo_full, org)}#{number}"
            except Exception:  # noqa: BLE001 - a malformed ref never stops the poll
                return None, None
            rec = {"flagged_at": f["at"], "where": "inherited", "reason": f"rules: {f.get('cat') or 'inherited'}",
                   "ref": ref, "restricted": True}
            flagged[key] = rec
            carried["flags"].pop(name, None)
            clear = (carried.get("clears") or {}).pop(name, None)
            if isinstance(clear, dict) and clear.get("at"):
                verified.setdefault("clears", {}).setdefault(ref, clear)
            return key, rec
        return None, None

    def item_people(ref: str) -> tuple[list[str], set[str], set[str]] | None:
        """(stewards of the item's spaces, assignees, claimants) read now through the API; None if unreadable."""
        repo_full, _, number = ref.rpartition("#")
        try:
            issue = gh.get(f"/repos/{repo_full}/issues/{number}", conditional=False)
            comments = _pages(gh, f"/repos/{repo_full}/issues/{number}/comments", pages=10)
        except urllib.error.URLError as err:
            stats["errors"] += 1
            _append(state_dir / "audit.jsonl", {"read_failed": f"{ref} people", "error": str(err)[:300], "at": now()})
            return None
        if not isinstance(issue, dict):
            return None
        key = repo_key(repo_full, org)
        # Stewards come from the config's repo lists only: a space: label could put an item in a steward's own space.
        stewards = item_stewards(config, member_of.get(key, []))
        assignees = {str(a.get("login") or "").lower() for a in issue.get("assignees") or []}
        return stewards, assignees, claimants_from(comments)

    def consider_record(c: dict) -> None:
        """Accept one audit-issue comment as a clear or ignore record, or audit why not. Records are read through
        GraphQL: an edited comment (lastEditedAt, or an editor who is not the author) never counts, a comment holds
        exactly one marker, and the author and time come from the API."""
        body = c.get("body") or ""
        clears, ignores = list(CLEAR_MARKER.finditer(body)), list(IGNORE_MARKER.finditer(body))
        if not clears and not ignores:
            return
        author = str((c.get("author") or {}).get("login") or "").lower()
        editor = str((c.get("editor") or {}).get("login") or "").lower()
        at, cid = str(c.get("createdAt") or ""), c.get("databaseId")

        def reject(why: str) -> None:
            stats["records_rejected"] = stats.get("records_rejected", 0) + 1
            _append(state_dir / "audit.jsonl", {"security_record_rejected": cid, "by": author, "why": why, "at": now()})
        if c.get("lastEditedAt") or (editor and editor != author):
            return reject("edited after it was written")
        if len(clears) + len(ignores) != 1:
            return reject("more than one marker in one comment")
        if clears:
            ref = clears[0]["item"].lower()
            key, rec = flagged_by_ref(ref)
            if rec is None:
                return reject(f"{ref} is not flagged")
            if at <= str(rec.get("flagged_at") or ""):
                return reject(f"written before the flag at {rec.get('flagged_at')}")
            people = item_people(ref)
            if people is None:
                return reject(f"could not read {ref}")
            stewards, assignees, claimants = people
            if author not in stewards:
                return reject(f"{author} is not a steward of {ref}'s spaces")
            if author in assignees | claimants:
                return reject(f"{author} is {ref}'s assignee or claimant")
            verified["clears"][ref] = {"by": author, "id": cid, "at": at}
            _append(state_dir / "audit.jsonl", {"security_clear_verified": ref, "by": author, "id": cid, "at": now()})
            return
        login = ignores[0]["login"].lower()
        mem = member_set()
        if author not in set(all_stewards(config)) or author == login:
            return reject(f"{author} may not ignore {login}")
        if mem is None or login in mem:
            return reject(f"{login} is a member (or membership is unreadable): members' content always flags")
        verified["ignored"][login] = {"by": author, "id": cid, "at": at}
        _append(state_dir / "audit.jsonl", {"security_ignore_verified": login, "by": author, "id": cid, "at": now()})

    def read_audit_records() -> None:
        """New comments on `[security] audit_issue` since the last one read (GraphQL, paged)."""
        issue = int(sec_cfg.get("audit_issue") or 0)
        if not issue or not hasattr(gh, "graphql"):
            return
        after = state.get("audit_cursor")
        for _ in range(20):
            try:
                conn = gh.graphql(Q_AUDIT, {"o": org, "r": meta_repo, "n": issue, "after": after})["repository"]["issue"]["comments"]
            except (urllib.error.URLError, OSError, GraphQLError, KeyError, TypeError) as err:
                stats["errors"] += 1
                _append(state_dir / "audit.jsonl", {"read_failed": "security audit issue", "error": str(err)[:300], "at": now()})
                return
            for c in conn.get("nodes") or []:
                consider_record(c)
            if (conn.get("pageInfo") or {}).get("endCursor"):
                after = state["audit_cursor"] = conn["pageInfo"]["endCursor"]
            if not (conn.get("pageInfo") or {}).get("hasNextPage"):
                return

    def blocked(ref: str) -> bool:
        key, rec = flagged_by_ref(ref)
        clear = verified["clears"].get(ref)
        return rec is not None and not (clear and clear["at"] > str(rec.get("flagged_at") or ""))

    def display_value(content: dict, value: str) -> str | None:
        """The Security value the Project should show for an item, from the bus's state (display only)."""
        repo = content.get("repository") or {}
        owner = (repo.get("owner") or {}).get("login") or ""
        if not owner or content.get("number") is None:
            return None
        ref = f"{owner}/{repo['name']}#{content['number']}".lower()
        key, rec = flagged_by_ref(ref)
        if rec is not None:
            return "flagged" if blocked(ref) else "cleared"
        return "unreviewed" if value in ("flagged", "cleared") else None

    def note_hot(content: dict, status: str, ready: str) -> None:
        repo = content.get("repository") or {}
        owner = (repo.get("owner") or {}).get("login") or ""
        if not owner or content.get("number") is None:
            return
        ref = f"{owner}/{repo['name']}#{content['number']}".lower()
        assigned = bool((content.get("assignees") or {}).get("nodes"))
        if (status == "Ready" and ready.lower() == "yes") or status == "In progress" or assigned:
            hot[ref] = now()
        else:
            hot.pop(ref, None)

    project.display_value, project.note_hot, project.write_budget = display_value, note_hot, calls
    project.display_checked = state.setdefault("display_checked", {})

    def on_hit(repo: str, number, where: str, found: dict, node_id: str, eligible: bool, authors=(), item=None) -> None:
        key = f"{repo}#{number}"
        ref = ref_of(key)
        authors = {str(a).lower() for a in authors if a}
        mem = member_set()
        outsiders = authors - mem if mem is not None else set()  # members unreadable: everyone counts as a member
        claimed = ref in hot or bool((item or {}).get("assignees"))
        outsider_only = bool(authors) and outsiders == authors
        if outsider_only and not claimed and sum(1 for k, v in flagged.items()
                                                 if v.get("outsider") and k.rpartition("#")[0] == repo) >= outsider_repo_cap:
            # Outsiders cannot fill COLAB_SECURITY_STATE with throwaway issues: past the cap the hit is audit-only,
            # kept so it is flagged as soon as someone starts working on the item.
            keep_suppressed(key, where, found, node_id, item, repo)
            stats["flags_suppressed"] = stats.get("flags_suppressed", 0) + 1
            _append(state_dir / "audit.jsonl", {"security_flag_suppressed": key, "where": where, "authors": sorted(authors),
                                                "why": "outsider open-flag cap for this repo", "reason": found["reason"],
                                                "spans": found["spans"][:5], "at": now()})
            return
        if outsider_only and not claimed:
            # Only non-members wrote it, on an item nobody is working on: an ignored author never flags, and each
            # outsider flags each item at most outsider_flags_per_day times a day; past that the hit is audit-only.
            # Members' content, and any hit on a Ready/Agent-ready, in-progress, or assigned item, always flags.
            day = now()[:10]
            live = [a for a in sorted(authors) if a not in verified["ignored"]]
            under = [a for a in live if (outsider_counts.get(f"{a}|{ref}") or {}).get("day") != day
                     or outsider_counts[f"{a}|{ref}"].get("n", 0) < outsider_limit]
            if not under:
                if live:  # over the quota (an ignored author's content is not kept)
                    keep_suppressed(key, where, found, node_id, item, repo)
                stats["flags_suppressed"] = stats.get("flags_suppressed", 0) + 1
                _append(state_dir / "audit.jsonl", {"security_flag_suppressed": key, "where": where, "authors": sorted(authors),
                                                    "why": "ignored author" if not live else "outsider daily limit for this item",
                                                    "reason": found["reason"], "spans": found["spans"][:5], "at": now()})
                return
            for a in under:
                c = outsider_counts.get(f"{a}|{ref}") or {}
                outsider_counts[f"{a}|{ref}"] = {"day": day, "n": (c.get("n", 0) if c.get("day") == day else 0) + 1}
            while len(outsider_counts) > SEEN_CAP:
                del outsider_counts[next(iter(outsider_counts))]
        stats["flagged"] = stats.get("flagged", 0) + 1
        item_id = None
        if eligible and node_id:
            item_id = project.flag(node_id, (contrib.get(key) or {}).get("project_item") or (flagged.get(key) or {}).get("project_item"))
        flagged.pop(key, None)
        flagged[key] = {"flagged_at": now().replace("+00:00", "Z"), "where": where, "reason": found["reason"], "ref": ref,
                        "node_id": node_id, "project_item": item_id, "outsider": outsider_only and not claimed,
                        "restricted": tier_for(item, repo) == "restricted"}
        if (item or {}).get("state") == "closed":  # a hit on a closed item is kept as a closed flag
            closed_flags[key] = {**flagged.pop(key), "closed_at": now().replace("+00:00", "Z")}
        _append(state_dir / "audit.jsonl", {"security_flagged": key, "where": where, "reason": found["reason"],
                                            "authors": sorted(authors), "spans": found["spans"][:5], "truncated": found.get("truncated", False),
                                            "project": bool(item_id), "at": now()})

    def run_scan(text: str) -> dict | None:
        """Rules plus hook on a capped text, charging the rules' time to the poll's scan budget too. None when the
        budget is spent before the rules run (the caller carries the text over)."""
        if hook_budget["left"] <= 0:
            return None
        capped, truncated = cap_text(text)
        before, started = hook_budget["left"], time.monotonic()
        found = injection_scan(capped, config, budget=hook_budget)
        hook_used = before - hook_budget["left"]
        hook_budget["left"] -= max(0.0, time.monotonic() - started - hook_used)
        found["truncated"] = truncated
        if truncated:
            stats["texts_truncated"] = stats.get("texts_truncated", 0) + 1
        return found

    def defer(skey: str, repo: str, number, where: str, text: str, node_id: str, eligible: bool, authors) -> None:
        hook_pending[:] = [x for x in hook_pending if x["key"] != skey][-(HOOK_PENDING_CAP - 1):]
        hook_pending.append({"key": skey, "repo": repo, "number": number, "where": where, "text": cap_text(text)[0],
                             "node_id": node_id, "eligible": eligible, "authors": sorted(a for a in authors if a)})
        stats["hook_deferred"] = stats.get("hook_deferred", 0) + 1
        _append(state_dir / "audit.jsonl", {"injection_hook_deferred": skey, "at": now()})

    def scan_text_for(repo: str, number, where: str, text: str, item: dict | None, eligible: bool, authors=()) -> None:
        """Injection-scan one text once per version (capped at 64 KB: head and tail); on a hit flag it. Rule and
        hook time share `[scan] injection_seconds_per_poll`; past it the text waits for a later poll. A clean issue
        body marks an item the bus has never flagged as Security = unreviewed (display only)."""
        if not scan_on or not text:
            return
        skey = f"{repo}#{number}:{where}"
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        if scanned.get(skey) == digest or scanned.get(hashed_key(skey)) == digest:
            return
        scanned.pop(skey, None)
        scanned[skey] = digest
        while len(scanned) > SEEN_CAP:
            del scanned[next(iter(scanned))]
        node_id = (item or {}).get("node_id", "")
        found = run_scan(text)
        if found is None or (found["deferred"] and not found["flag"]):
            defer(skey, repo, number, where, text, node_id, eligible, authors)
            if found is None:
                return
        if found["flag"]:
            on_hit(repo, number, where, found, node_id, eligible, authors, item)
            return
        key = f"{repo}#{number}"
        if where == "body" and eligible and node_id and key not in flagged and key not in marked and not found["deferred"]:
            item_id = project.set_security(node_id, (contrib.get(key) or {}).get("project_item"), "unreviewed")
            if item_id:
                marked[key] = now()
                while len(marked) > SEEN_CAP:
                    del marked[next(iter(marked))]

    def read_since(endpoint: str, since: str) -> tuple[list, str | None]:
        """Every comment updated since the cursor, page by page; (comments, hold). When more remain than
        COMMENT_PAGES pages, hold is the last one's updated_at, and the repo cursor stops there."""
        out: list = []
        for page in range(1, COMMENT_PAGES + 1):
            batch = gh.get(f"{endpoint}?sort=updated&direction=asc&per_page=100&since={since}&page={page}", conditional=False)
            batch = batch if isinstance(batch, list) else []
            out += batch
            if len(batch) < 100:
                return out, None
        return out, (out[-1].get("updated_at") if out else None)

    def eligible(item: dict | None) -> bool:
        """May the item carry a Security value on the board (its kind is added, and the restricted rule)."""
        if item is None:
            return False
        kind = "pull_request" if "pull_request" in item else "issue"
        labels = [lab["name"] if isinstance(lab, dict) else lab for lab in item.get("labels", [])]
        tier = tier_of(visibility.get(current["repo"]), labels,
                       _strictest(declared.get(current["repo"]), *label_space_tiers(config, current["repo"], labels)),
                       is_external(current["repo"]), current["repo"] in opt_in)
        return kind in project.add_kinds and (tier != "restricted" or restricted_ok(current["repo"]))

    read_audit_records()  # new clear and ignore records, verified against the bus's own flag times

    # Texts the hook could not see on an earlier poll (out of time): they go first, within this poll's budget.
    for job in list(hook_pending):
        found = run_scan(job["text"])
        if found is None or found["deferred"]:
            break
        hook_pending.remove(job)
        if found["flag"]:
            on_hit(job["repo"], job["number"], job["where"], found, job.get("node_id", ""), bool(job.get("eligible")),
                   job.get("authors") or ())

    current: dict = {}
    cursors = state.setdefault("cursors", {})
    baseline_at = state.setdefault("baseline_at", {})
    checked = state.setdefault("links_checked", {})
    for repo in sorted(member_of):
        vis = visibility.get(repo)
        current["repo"] = repo
        if vis is None:
            continue
        try:
            if repo not in cursors:
                # Baseline: the bus starts from now. Record the newest update and the
                # current releases as seen, and route nothing, so history never floods.
                newest = gh.get(f"{path(repo)}/issues?state=all&sort=updated&direction=desc&per_page=1") or []
                for rel in gh.get(f"{path(repo)}/releases?per_page=10") or []:
                    for env in envelopes_for("release", repo, vis, rel):
                        remember(state, env["id"])
                cursors[repo] = newest[0]["updated_at"] if newest else now().replace("+00:00", "Z")
                baseline_at[repo] = now().replace("+00:00", "Z")
                stats["baselined"] = stats.get("baselined", 0) + 1
                save()
                continue
            items = gh.get(f"{path(repo)}/issues?state=all&sort=updated&direction=asc&per_page=100&since={cursors[repo]}") or []
            releases = gh.get(f"{path(repo)}/releases?per_page=10") or []
            comments, review_comments, comment_hold = [], [], None
            if scan_on and items:
                comments, hold_a = read_since(f"{path(repo)}/issues/comments", cursors[repo])
                review_comments, hold_b = read_since(f"{path(repo)}/pulls/comments", cursors[repo])
                comment_hold = min([h for h in (hold_a, hold_b) if h] or [None], key=lambda h: h or "")
        except urllib.error.URLError as err:
            stats["errors"] += 1
            _append(state_dir / "audit.jsonl", {"read_failed": repo, "error": str(err), "at": now()})
            continue
        events = []
        fetched = items
        carried, carry_cursor = False, None
        external = is_external(repo)
        base = baseline_at.get(repo, "")
        if external:
            items, carry_cursor, carried = select_external(repo, items)
            releases = [r for r in releases if track_all or (member_set() is not None and involves(r, member_set()))]
        by_number = {i.get("number"): i for i in items}
        for item in items:
            if is_delivery(item):
                continue
            kind = "pull_request" if "pull_request" in item else "issue"
            item_declared = _strictest(declared.get(repo), *label_space_tiers(config, repo, item.get("labels")))
            envs = envelopes_for(kind, repo, vis, item, item_declared, member_of.get(repo), repo in opt_in,
                                 item.get("_contributions") if external else None)
            events += envs
            check_links(item, repo)
            on_board = bool(envs) and kind in project.add_kinds and (envs[0]["tier"] != "restricted" or restricted_ok(repo))
            if external and on_board:
                # Board bookkeeping for tracked external items: add once involved, keep Contributions current.
                entry = contrib.setdefault(f"{repo}#{item.get('number')}", {"by": item.get("_contributions") or {}})
                project.track_external(envs[0], item.get("_contributions") or {}, entry, involved_by(item.get("_contributions")),
                                       bool(base) and (item.get("created_at") or "") >= base)
            scan_text_for(repo, item.get("number"), "body", f"{item.get('title') or ''}\n{item.get('body') or ''}", item, on_board,
                          {(item.get("user") or {}).get("login")})
            track_open_state(repo, item)
            if item.get("state") != "closed" and item.get("assignees") and f"{repo}#{item.get('number')}" in suppressed:
                promote(f"{repo}#{item.get('number')}", "assigned")
        # Comments, PR review bodies, and inline review comments: each one, and per item their concatenation
        # (so text split across several comments is scanned whole too).
        texts: list[tuple[int | None, str, str]] = []
        for c in comments:
            texts.append((_number(c.get("issue_url")), f"comment:{c.get('id')}", c.get("body") or "", (c.get("user") or {}).get("login")))
        for c in review_comments:
            texts.append((_number(c.get("pull_request_url")), f"review_comment:{c.get('id')}", c.get("body") or "",
                          (c.get("user") or {}).get("login")))
        review_hold = None
        if scan_on:
            for item in items:
                if "pull_request" in item:
                    try:
                        reviews = _pages(gh, f"{path(repo)}/pulls/{item.get('number')}/reviews", calls=calls)
                    except DetailBudget:  # out of API calls: read this PR's reviews on a later poll
                        review_hold = min(review_hold or item.get("updated_at"), item.get("updated_at") or "")
                        stats["reviews_carried"] = stats.get("reviews_carried", 0) + 1
                        continue
                    except urllib.error.URLError as err:
                        stats["errors"] += 1
                        _append(state_dir / "audit.jsonl", {"read_failed": f"{repo}#{item.get('number')} reviews",
                                                            "error": str(err)[:300], "at": now()})
                        reviews = []
                    texts += [(item.get("number"), f"review:{r.get('id')}", r.get("body") or "", (r.get("user") or {}).get("login"))
                              for r in reviews if r.get("body")]
        joined: dict = {}
        for number, where, body, author in texts:
            item = by_number.get(number)
            if (external and item is None) or MARKED.search(body):
                continue  # untracked external items are not scanned; the bus's own comments are skipped
            scan_text_for(repo, number, where, body, item, eligible(item), {author})
            joined.setdefault(number, []).append((body, author))
        for number, parts in joined.items():
            if len(parts) > 1:
                scan_text_for(repo, number, "joined", "\n".join(b for b, _ in parts), by_number.get(number),
                              eligible(by_number.get(number)), {a for _, a in parts})
        for rel in releases:
            events += envelopes_for("release", repo, vis, rel, declared.get(repo), member_of.get(repo), repo in opt_in)
        for env in sorted(events, key=lambda e: e["at"]):
            if not remember(state, env["id"]):
                continue
            if base and env["at"] and env["at"] < base:
                continue  # happened before the bus started watching this repo
            stats["envelopes"] += 1
            _append(state_dir / "events.jsonl", env)
            # Board bookkeeping, not a delivery: runs in dry-run spaces too. Restricted items stay off
            # the board unless the space has the restricted switch, as for routing. External items are
            # added above, once they involve the group.
            if env["transition"] == "opened" and not env["external"] and (env["tier"] != "restricted" or restricted_ok(repo)):
                project.add(env)
            deliveries, drops = route(env, subs, to_tiers, bot, restricted_ok(repo))
            for d in drops:
                stats["drops"] += 1
                _append(state_dir / "audit.jsonl", {"dropped": d, "at": now()})
            marker = delivery_marker(bus_id, operator or bot, env["id"])
            if len(deliveries) > threshold:
                # A digest combines items; its tier is the strictest of what it includes (one envelope here).
                if not may_deliver(combined_tier([env]), to_tiers[meta_key]):
                    stats["drops"] += 1
                    _append(state_dir / "audit.jsonl", {"dropped": {"digest": env["id"], "reason": "meta tier"}, "at": now()})
                elif live(meta_key) or any(live(d["to"]) for d in deliveries):
                    _, untrusted = delivery_text(env)
                    if already_delivered(gh, f"/repos/{org}/{meta_repo}", env["id"], env["at"] or now(), writers):
                        stats["deliveries_skipped"] = stats.get("deliveries_skipped", 0) + 1
                        _append(state_dir / "audit.jsonl", {"delivery_exists": env["id"], "to": meta_repo, "at": now()})
                    elif write(f"/repos/{org}/{meta_repo}/issues", {
                        "title": delivery_title("[colab digest]", env),
                        "body": f"{DELIVERY_MARKER}\n{marker}\n{untrusted}Source: {env['source']}\nTier: {env['tier']}\n\nSubscribers:\n"
                                + "\n".join(f"- {d['to']}" for d in deliveries),
                    }):
                        stats["deliveries"] += 1
                else:
                    stats["dry_run"] += 1
                    _append(state_dir / "would-deliver.jsonl", {"digest": [d["to"] for d in deliveries], "envelope": env["id"], "at": now()})
                deliveries = []
            for d in deliveries:
                if live(d["to"]) and d["via"] == "issue" and not is_external(d["to"]):
                    _, untrusted = delivery_text(env)
                    if already_delivered(gh, path(d["to"]), env["id"], env["at"] or now(), writers):
                        stats["deliveries_skipped"] = stats.get("deliveries_skipped", 0) + 1
                        _append(state_dir / "audit.jsonl", {"delivery_exists": env["id"], "to": d["to"], "at": now()})
                    elif write(f"{path(d['to'])}/issues", {
                        "title": delivery_title("[colab]", env),
                        "body": f"{DELIVERY_MARKER}\n{marker}\nDelivered by colab-bus {bus_id or 'default'} "
                                f"(operator {operator or bot}).\n\n{untrusted}Source: {env['source']}\nTier: {env['tier']}\nEnvelope: `{env['id']}`",
                    }):
                        stats["deliveries"] += 1
                else:
                    stats["dry_run"] += 1
                    _append(state_dir / "would-deliver.jsonl", {**d, "at": now()})
            save()  # persist seen ids after every envelope, so a crash never re-delivers
        new_cursor = None
        if carried:
            new_cursor = carry_cursor  # details carried over: resume from the last item handled
        elif fetched and not (external and not track_all and members.get("set") is None):
            new_cursor = max(i["updated_at"] for i in fetched)  # unreadable members: retry these next poll
        if new_cursor and review_hold and review_hold <= new_cursor:
            new_cursor = review_hold  # reviews not read yet: resume from that PR
        if new_cursor and comment_hold and comment_hold < new_cursor:
            new_cursor = comment_hold  # more comments than one poll reads: resume from the last one scanned
            stats["comments_held"] = stats.get("comments_held", 0) + 1
        if new_cursor:
            cursors[repo] = new_cursor
            save()
    try:
        if project.sweep_due():
            project.sweep()
    except Exception as err:  # bookkeeping must never break the poll
        project._fail("sweep", err)
    for key in [k for k in suppressed if ref_of(k) in hot and k not in closed_flags]:
        promote(key, "Ready/Agent-ready, in progress, or assigned")
    publish_security_state(gh, config, state, state_dir, stats, store)  # the trust anchor the CLI reads
    save()
    return stats


SECURITY_VAR = "COLAB_SECURITY_STATE"
SECURITY_VAR_LIMIT = 45000  # bytes; GitHub caps a variable at 48 KB
SIGNATURE_ROOM = 1500  # bytes kept free for the ssh signature and the wrapper


def _categories(reason: str) -> str:
    m = re.match(r"rules: ([^;]+)", reason or "")
    return m.group(1).replace(" ", "") if m else ("model" if "model:" in (reason or "") else "")


def security_state_value(config: dict, state: dict, now_t: datetime | None = None, sign=None) -> tuple[str, int, bool]:
    """(the COLAB_SECURITY_STATE value, cleared entries dropped to fit, overflowed).

    flags: every open item the bus flagged (ref -> flag time, rule categories; restricted items by hashed_ref);
    clears and ignored: the audit records the bus verified (steward login, comment id, time); seen: each repo's
    cursor. Cleared pairs older than `[security] state_keep_days` (default 30) are pruned; past the limit the oldest
    clears go first, and if open flags alone do not fit, the value is {"v": 1, "overflow": true, "at": ...}, which
    every gate treats as untrustworthy. With `sign` (signing on), the value is {"v": 1, "state": <json>, "sig": ...}."""
    org = config["org"]["name"]
    now_t = now_t or datetime.now(timezone.utc)
    keep_days = float((config.get("security") or {}).get("state_keep_days", 30))
    cutoff = (now_t.timestamp() - keep_days * 86400)
    verified = state.get("security_verified") or {"clears": {}, "ignored": {}}
    limit = SECURITY_VAR_LIMIT - (SIGNATURE_ROOM if sign else 0)

    def ref_of(key: str) -> str:
        repo, _, number = key.rpartition("#")
        return f"{repo_ref(repo, org)}#{number}".lower()
    flags, names = {}, {}
    for k, v in (state.get("security") or {}).items():
        if not v.get("flagged_at"):
            continue
        ref = ref_of(k)
        name = hashed_ref(ref) if v.get("restricted") else ref
        names[ref] = name
        flags[name] = {"at": v["flagged_at"], "cat": _categories(v.get("reason", ""))}
    def age_of(at) -> float:
        try:
            return datetime.fromisoformat(str(at or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    closed_days = float((config.get("security") or {}).get("closed_flag_days", 30))
    closed_cutoff = now_t.timestamp() - closed_days * 86400
    for k, v in (state.get("security_closed") or {}).items():
        try:
            closed_at = datetime.fromisoformat(str(v.get("closed_at") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            closed_at = 0.0
        if not v.get("flagged_at") or closed_at < closed_cutoff:
            continue
        ref = ref_of(k)
        name = hashed_ref(ref) if v.get("restricted") else ref
        names[ref] = name
        flags[name] = {"at": v["flagged_at"], "cat": _categories(v.get("reason", "")), "closed": 1}
    clears = {names[r]: c for r, c in (verified.get("clears") or {}).items()
              if r in names and c["at"] > flags[names[r]]["at"]}
    # Restricted flags taken over from a previous lease holder are listed by hash only, so they are carried verbatim
    # (open ones always; closed ones for closed_flag_days) until this bus learns the item itself.
    carried = state.get("security_inherited") or {}
    for name, f in (carried.get("flags") or {}).items():
        if name in flags or not isinstance(f, dict):
            continue
        if f.get("closed") and age_of(f.get("at")) < closed_cutoff:
            continue
        flags[name] = f
        c = (carried.get("clears") or {}).get(name)
        if isinstance(c, dict) and str(c.get("at") or "") > str(f.get("at") or ""):
            clears[name] = c

    def age(at: str) -> float:
        try:
            return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    for r in [r for r, c in clears.items() if age(c["at"]) < cutoff]:
        clears.pop(r)
        flags.pop(r)
    body = {"v": 1, "issued_at": now_t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "seen": dict(sorted((state.get("cursors") or {}).items())), "flags": flags, "clears": clears,
            "ignored": verified.get("ignored") or {}}
    dropped = 0
    while len(json.dumps(body, separators=(",", ":")).encode()) > limit and clears:
        oldest = min(clears, key=lambda r: clears[r]["at"])
        clears.pop(oldest)
        flags.pop(oldest)
        dropped += 1
    closed = sorted((r for r, f in flags.items() if f.get("closed")), key=lambda r: flags[r]["at"])
    while len(json.dumps(body, separators=(",", ":")).encode()) > limit and closed:
        flags.pop(closed.pop(0))  # closed items' flags go next; open flags are always kept
        dropped += 1
    value = json.dumps(body, separators=(",", ":"), sort_keys=True)
    if len(value.encode()) > limit:
        return json.dumps({"v": 1, "overflow": True, "at": now_t.isoformat(timespec="seconds")}, separators=(",", ":")), dropped, True
    if sign:
        return json.dumps({"v": 1, "state": value, "sig": sign(value)}, separators=(",", ":")), dropped, False
    return value, dropped, False


def ssh_sign(key_path: str, data: str) -> str:
    """`ssh-keygen -Y sign -n colab-security` over the state, with the bus host's key. Raises on failure."""
    proc = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", SECURITY_SIG_NAMESPACE], input=data,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or "BEGIN SSH SIGNATURE" not in proc.stdout:
        raise RuntimeError(f"ssh-keygen -Y sign failed: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def publish_security_state(gh, config: dict, state: dict, state_dir: Path, stats: dict, store=None) -> None:
    """Keep the published security state equal to the bus's state: the trust anchor the CLI reads. With a loaded
    `colab-state` branch (`store`), it is staged as `security/state.json` and committed with the poll's single
    commit (only the operators team may push there); otherwise (a meta repo that has not moved yet) it is the
    deprecated Actions variable COLAB_SECURITY_STATE. Each poll it reads the published value back and re-asserts
    its own when anything else changed it (`security_state_foreign_edit`, counted in the heartbeat). The Project's
    Security field is display only. Never raises."""
    if not (config.get("bus") or {}).get("injection_scan", True):
        return
    sec = config.get("security") or {}
    sign = None
    if str(sec.get("signing") or "").lower() == "ssh":
        key = os.environ.get("COLAB_SIGNING_KEY", "")
        if not key or not Path(key).is_file():
            stats["errors"] = stats.get("errors", 0) + 1
            _append(state_dir / "audit.jsonl", {"security_state_unsigned": "COLAB_SIGNING_KEY is not set or missing", "at": now()})
            return
        sign = lambda data: ssh_sign(key, data)  # noqa: E731
    try:
        value, dropped, overflow = security_state_value(config, state, sign=sign)
    except (OSError, RuntimeError, subprocess.SubprocessError) as err:
        stats["errors"] = stats.get("errors", 0) + 1
        _append(state_dir / "audit.jsonl", {"security_state_unsigned": str(err)[:300], "at": now()})
        return
    if dropped:
        _append(state_dir / "audit.jsonl", {"security_state_pruned": dropped, "at": now()})
    if overflow:
        stats["security_state_overflow"] = True
        stats["errors"] = stats.get("errors", 0) + 1
        _append(state_dir / "audit.jsonl", {"security_state_overflow": True, "open_flags": len(state.get("security") or {}),
                                            "at": now()})
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    published = state.get("security_published")
    if getattr(store, "hold", False):
        return  # colab-state could not be read this poll: publish nothing rather than fall back to the variable
    if store is not None and store.available:
        try:
            live = store.read(SECURITY_FILE)
        except (urllib.error.URLError, OSError) as err:
            live = None
            _append(state_dir / "audit.jsonl", {"security_state_read_failed": str(err)[:300], "at": now()})
        if published is not None and live is not None and live != published:
            stats["security_foreign_edits"] = stats.get("security_foreign_edits", 0) + 1
            _append(state_dir / "audit.jsonl", {"security_state_foreign_edit": True, "where": f"{STATE_BRANCH}:{SECURITY_FILE}",
                                                "at": now()})
        if live == value and published == value:
            return
        if overflow and published is not None and json.loads(published).get("overflow") and live == published:
            return
        store.stage(SECURITY_FILE, value)

        def done() -> None:
            state["security_published"] = value
            stats["security_state_bytes"] = len(value.encode())
        store.on_commit(done)
        return
    path = f"/repos/{org}/{meta}/actions/variables/{SECURITY_VAR}"
    try:
        current = gh.get(path, cache=True)
    except (urllib.error.URLError, OSError) as err:
        current = None
        _append(state_dir / "audit.jsonl", {"security_state_read_failed": str(err)[:300], "at": now()})
    live = current.get("value") if isinstance(current, dict) else None
    if published is not None and live is not None and live != published:
        stats["security_foreign_edits"] = stats.get("security_foreign_edits", 0) + 1
        _append(state_dir / "audit.jsonl", {"security_state_foreign_edit": True, "at": now()})
    if live == value and published == value:
        return
    if overflow and published is not None and json.loads(published).get("overflow") and live == published:
        return  # still overflowing: keep the first marker rather than rewriting its time every poll
    try:
        try:
            gh.send("PATCH", path, {"name": SECURITY_VAR, "value": value})
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
            gh.send("POST", f"/repos/{org}/{meta}/actions/variables", {"name": SECURITY_VAR, "value": value})
    except (urllib.error.URLError, OSError) as err:
        stats["errors"] = stats.get("errors", 0) + 1
        _append(state_dir / "audit.jsonl", {"security_state_write_failed": str(err)[:300], "at": now()})
        return
    state["security_published"] = value
    stats["security_state_bytes"] = len(value.encode())


def heartbeat(gh, org: str, meta: str, stats: dict) -> None:
    """The deprecated heartbeat variable, written only while the meta repo has no `colab-state` branch."""
    value = json.dumps({"at": now(), **stats})
    path = f"/repos/{org}/{meta}/actions/variables/COLAB_BUS_HEARTBEAT"
    try:
        gh.send("PATCH", path, {"name": "COLAB_BUS_HEARTBEAT", "value": value})
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
        gh.send("POST", f"/repos/{org}/{meta}/actions/variables", {"name": "COLAB_BUS_HEARTBEAT", "value": value})


# --------------------------------------------------------------------------- shared state on colab-state


class StateConflict(RuntimeError):
    """The colab-state ref moved since it was read (another bus committed): re-read and decide again."""


class StateReadError(RuntimeError):
    """colab-state exists but could not be read just now (a transient failure, not a missing branch)."""


class StateBranch:
    """The protected `colab-state` branch of the meta repo, read at one commit and written with at most one commit
    per poll: the staged files go into one tree (on top of the head's tree), one commit whose only parent is the
    head that was read, and a ref update with force=false. A ref that moved meanwhile refuses the update (it is no
    longer a fast-forward), so two buses can never both write on the same head: compare-and-set. The bus only ever
    reads the head commit, so the branch's growing history never matters (shallow-safe)."""

    def __init__(self, gh, org: str, meta: str):
        self.gh, self.base = gh, f"/repos/{org}/{meta}"
        self.available, self.head, self.tree = False, None, None
        self.staged: dict[str, str] = {}
        self._read: dict[str, str | None] = {}
        self._callbacks: list[tuple[str, object]] = []

    def load(self) -> bool:
        """Read the head commit and its tree. Staged files are kept (a retry re-applies them on the new head)."""
        self.available, self._read = False, {}
        ref = self.gh.get(f"{self.base}/git/ref/heads/{STATE_BRANCH}", conditional=False)
        sha = ((ref.get("object") or {}).get("sha")) if isinstance(ref, dict) else None
        if not sha:
            return False
        commit = self.gh.get(f"{self.base}/git/commits/{sha}", conditional=False)
        tree = ((commit.get("tree") or {}).get("sha")) if isinstance(commit, dict) else None
        if not tree:
            raise StateReadError(f"{STATE_BRANCH} head {sha[:12]} could not be read")
        self.head, self.tree, self.available = sha, tree, True
        return True

    def read(self, path: str) -> str | None:
        """The file as committed at the head that was loaded (never a staged value); None when absent."""
        if path not in self._read:
            blob = self.gh.get(f"{self.base}/contents/{path}?ref={self.head}", conditional=False)
            text = None
            if isinstance(blob, dict) and blob.get("content") is not None:
                try:
                    text = base64.b64decode(blob["content"]).decode()
                except (ValueError, UnicodeDecodeError):
                    text = None
            self._read[path] = text
        return self._read[path]

    def read_strict(self, path: str) -> str | None:
        """The file at the loaded head; None only when GitHub says it does not exist (404). Any other failure, or a
        reply without its full content (the Contents API leaves `content` empty above 1 MB), raises: never mistake a
        failed read for an empty file."""
        blob = self.gh.get(f"{self.base}/contents/{path}?ref={self.head}", conditional=False)
        if blob is None:
            return None
        try:
            text = base64.b64decode(blob["content"]).decode()
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise StateReadError(f"{path} on {STATE_BRANCH} could not be read whole") from None
        if isinstance(blob.get("size"), int) and len(text.encode()) != blob["size"]:
            raise StateReadError(f"{path} on {STATE_BRANCH} came back incomplete")
        return text

    def read_json(self, path: str):
        text = self.read(path)
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    def file_date(self, path: str) -> str | None:
        """The committer date, as GitHub returns it, of the newest commit that changed `path` (None when none).
        The bus commits without setting dates, so GitHub stamps its own time: lease freshness never depends on a
        writer's clock."""
        commits = self.gh.get(f"{self.base}/commits?sha={self.head}&path={path}&per_page=1", conditional=False)
        if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
            return None
        return ((commits[0].get("commit") or {}).get("committer") or {}).get("date")

    def file_commit(self, path: str) -> str | None:
        """The sha of the newest commit, up to the loaded head, that changed `path` (None when none)."""
        commits = self.gh.get(f"{self.base}/commits?sha={self.head}&path={path}&per_page=1", conditional=False)
        if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
            return None
        return commits[0].get("sha")

    def read_lease(self) -> dict | None:
        """lease.json, with `renewed_at` replaced by the server-side date of the commit that last wrote it."""
        lease = self.read_json(LEASE_FILE)
        if isinstance(lease, dict):
            served = self.file_date(LEASE_FILE)
            if served:
                lease = {**lease, "written_renewed_at": lease.get("renewed_at"), "renewed_at": served}
        return lease if isinstance(lease, dict) else None

    def head_author(self) -> str:
        """The GitHub login that made the head commit ('' when unknown)."""
        c = self.gh.get(f"{self.base}/commits/{self.head}", conditional=False)
        if not isinstance(c, dict):
            return ""
        return str((c.get("author") or {}).get("login") or (c.get("committer") or {}).get("login") or "").lower()

    def stage(self, path: str, text: str) -> None:
        self.staged[path] = text

    def unstage(self, path: str) -> None:
        self.staged.pop(path, None)
        self._callbacks = [(p, fn) for p, fn in self._callbacks if p != path]

    def on_commit(self, fn, path: str = SECURITY_FILE) -> None:
        self._callbacks.append((path, fn))

    def commit(self, message: str) -> str | None:
        """One tree, one commit, one ref update (force=false). None when nothing is staged; StateConflict when the
        ref moved since load()."""
        if not self.staged:
            return None
        tree = self.gh.send("POST", f"{self.base}/git/trees", {
            "base_tree": self.tree,
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": t} for p, t in sorted(self.staged.items())]})
        commit = self.gh.send("POST", f"{self.base}/git/commits",
                              {"message": message, "tree": tree["sha"], "parents": [self.head]})
        try:
            self.gh.send("PATCH", f"{self.base}/git/refs/heads/{STATE_BRANCH}", {"sha": commit["sha"], "force": False})
        except urllib.error.HTTPError as err:
            if err.code in (409, 422):
                raise StateConflict(str(err)) from None
            raise
        self.head, self.tree = commit["sha"], tree["sha"]
        for _, fn in self._callbacks:
            fn()
        self.staged, self._callbacks, self._read = {}, [], {}
        return self.head


class HoldStore:
    """A store that is not available and must not fall back to the Actions variables: publishing is held for this
    poll (used when colab-state could not be read)."""
    available = False
    hold = True


# --------------------------------------------------------------------------- an Actions bus's own state, kept on colab-state
#
# A workflow runner is thrown away after each run, so an Actions bus keeps its own state (cursors, ETags, seen envelope
# ids, scan digests, links checks, its security bookkeeping, contribution records) on colab-state as
# `bus/<bus id>/state.json`, written in the run's single commit. Members can read colab-state, so the file keeps no item
# text: response caches and texts waiting for the injection hook are dropped (they are rebuilt or rescanned), flag
# reasons are reduced to their rule categories, and the published security state is kept only as its hash. Entries
# about restricted items (a restricted repo, or a flag on an item the bus found restricted) are kept only under
# sha256 keys with no text: flags and their verified clears as the published security state lists them
# (`security_inherited`, picked up again by ref when the item is seen or cleared), seen ids, scan digests, and links
# checks under hashed keys; everything else about them is dropped and rebuilt.

BUS_STATE_CAP = 512 * 1024  # bytes
BUS_STATE_DROP = ("cache", "hook_pending", "security_published")
BUS_STATE_CAPS = {"seen": 5000, "scanned": 5000, "links_checked": 5000, "display_checked": 2000, "contributions": 2000,
                  "outsider_flags": 2000, "security_unreviewed": 5000, "security_suppressed": 1000, "etags": 1000,
                  "stale_reported": 500}
ITEM_SECTIONS = ("scanned", "links_checked", "contributions", "security", "security_closed", "security_suppressed",
                 "security_unreviewed")  # keyed by "<repo key>#<n>[:where]"
HASHED_SECTIONS = ("scanned", "links_checked")  # restricted keys kept as sha256 (value: a digest, no text)
AUDIT_FILE_CAP = 900 * 1024  # bytes per audit file on colab-state (the Contents API returns files up to 1 MB whole)
AUDIT_MAX_PARTS = 500
AUDIT_ACTOR_KEYS = ("by", "author", "authors", "user", "login", "from_operator", "operator")
LOGIN_OR_BOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?\Z")
_ITEM_KEY = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)#(?P<n>\d+)(?::.*)?\Z")
_ENV_ID = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?):(?:issue|pull_request|release):(?P<n>[^:\s]+):\S+\Z")


def hashed_key(key: str) -> str:
    """How a restricted entry's key is kept in an Actions bus's state: never the plain ref."""
    return "sha256:" + hashlib.sha256(str(key).strip().lower().encode()).hexdigest()


def safe_reason(reason: str) -> str:
    """A flag's reason reduced to its rule categories (and whether the model hook flagged): never the hook's free text."""
    cats = _categories(reason or "")
    parts = [f"rules: {cats.replace(',', ', ')}"] if cats and cats != "model" else []
    if "model:" in (reason or ""):
        parts.append("model: flagged")
    return "; ".join(parts) or "rules: inherited"


class BusStateStore:
    """Loads and writes an Actions bus's own state as `bus/<bus id>/state.json` on colab-state (see above), and appends
    its audit lines to `audit/<YYYY-MM>.jsonl` there in the same commit."""

    def __init__(self, config: dict, bus_id: str, run: str | None = None):
        self.config, self.bus_id = config, bus_id
        self.path = bus_state_path(bus_id)
        if not run and os.environ.get("GITHUB_RUN_ID"):
            run = f"{os.environ['GITHUB_RUN_ID']}.{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        self.run = run or base64.b32encode(os.urandom(5)).decode().lower()  # tells two overlapping runs apart
        self.adopted_run: str | None = None

    # which entries are about restricted items
    def _restricted_repos(self, state: dict) -> set[str]:
        out = {k for k, t in declared_tiers(self.config).items() if t == "restricted"}
        vis, opt = state.get("_visibility") or {}, lab_opt_in(self.config)
        for key in repo_spaces(self.config):
            if is_external(key) and key not in opt and vis.get(key) != "public":
                out.add(key)
        return out

    def _ref_repo(self, ref: str) -> str | None:
        try:
            return repo_key(str(ref).split("#", 1)[0], self.config["org"]["name"])
        except Exception:  # noqa: BLE001 - a malformed ref is treated as restricted
            return None

    def _ref_of(self, key: str) -> str:
        repo, _, number = key.partition("#")
        try:
            return f"{repo_ref(repo, self.config['org']['name'])}#{number.split(':', 1)[0]}".lower()
        except Exception:  # noqa: BLE001 - a malformed key never stops a save
            return key.lower()

    def _hidden_refs(self, state: dict, restricted: set[str]) -> set[str]:
        out = set()
        for section in ("security", "security_closed"):
            for k, v in (state.get(section) or {}).items():
                if isinstance(v, dict) and (v.get("restricted") or k.split("#", 1)[0] in restricted):
                    out.add(str(v.get("ref") or self._ref_of(k)).lower())
                    out.add(self._ref_of(k))
        return out

    def split(self, state: dict) -> tuple[dict, dict]:
        """(plain, restricted) copies of the state to keep, with caps applied; nothing in BUS_STATE_DROP or `_*`."""
        data = json.loads(json.dumps({k: v for k, v in state.items() if not k.startswith("_") and k not in BUS_STATE_DROP},
                                     default=sorted))
        cache = state.get("cache") or {}
        data["etags"] = {p: e for p, e in (data.get("etags") or {}).items() if p not in cache}
        restricted = self._restricted_repos(state)
        hidden_refs = self._hidden_refs(data, restricted)
        hidden: dict = {}

        def hide_key(section: str, key: str, value) -> bool:
            if key.startswith("sha256:"):
                return False  # already hashed
            if key.split("#", 1)[0] in restricted:
                return True
            if section in ("security", "security_closed") and isinstance(value, dict) and value.get("restricted"):
                return True
            if section == "security_suppressed" and isinstance(value, list) and any(isinstance(r, dict) and r.get("restricted") for r in value):
                return True
            return self._ref_of(key) in hidden_refs  # e.g. the scan digests of an item with a tier:restricted label

        def hide_ref(ref: str) -> bool:
            return str(ref).lower() in hidden_refs or self._ref_repo(ref) in restricted | {None}

        for section in ITEM_SECTIONS:
            d = data.get(section)
            if isinstance(d, dict):
                for k in [k for k, v in d.items() if hide_key(section, k, v)]:
                    hidden.setdefault(section, {})[k] = d.pop(k)
        for section, pick in (("security_hot", lambda k: k), ("outsider_flags", lambda k: k.split("|", 1)[-1])):
            d = data.get(section)
            if isinstance(d, dict):
                for k in [k for k in d if hide_ref(pick(k))]:
                    hidden.setdefault(section, {})[k] = d.pop(k)
        clears = (data.get("security_verified") or {}).get("clears")
        if isinstance(clears, dict):
            for k in [k for k in clears if hide_ref(k)]:
                hidden.setdefault("security_verified", {"clears": {}})["clears"][k] = clears.pop(k)
        if isinstance(data.get("seen"), list):
            keep = []
            for env_id in data["seen"]:
                (hidden.setdefault("seen", []) if str(env_id).split(":", 1)[0] in restricted else keep).append(env_id)
            data["seen"] = keep
        etags = data.get("etags") or {}
        for path in [p for p in etags if p.startswith("/repos/") and self._ref_repo("/".join(p.split("/")[2:4])) in restricted]:
            hidden.setdefault("etags", {})[path] = etags.pop(path)
        # Plain flags keep only their rule categories, never the hook's free-text reason.
        for section in ("security", "security_closed"):
            for v in (data.get(section) or {}).values():
                if isinstance(v, dict) and "reason" in v:
                    v["reason"] = safe_reason(v["reason"])
        for rows in (data.get("security_suppressed") or {}).values():
            for r in rows if isinstance(rows, list) else []:
                if isinstance(r, dict) and "reason" in r:
                    r["reason"] = safe_reason(r["reason"])
        for part in (data, hidden):
            for section, cap in BUS_STATE_CAPS.items():
                part_val = part.get(section)
                if isinstance(part_val, list) and len(part_val) > cap:
                    part[section] = part_val[-cap:]
                elif isinstance(part_val, dict) and len(part_val) > cap:
                    part[section] = dict(list(part_val.items())[-cap:])
        return data, hidden

    def _carry_hashed(self, data: dict, hidden: dict) -> None:
        """Keep the restricted flags (and their verified clears) only as hashes, as the published security state lists
        them, so the gate never loses one; the bus picks one up again by ref when it sees or clears the item."""
        carried = data.setdefault("security_inherited", {"flags": {}, "clears": {}})
        carried.setdefault("flags", {})
        carried.setdefault("clears", {})
        clears = (hidden.get("security_verified") or {}).get("clears") or {}
        for section in ("security", "security_closed"):
            for k, v in (hidden.get(section) or {}).items():
                if not isinstance(v, dict) or not v.get("flagged_at"):
                    continue
                ref = self._ref_of(k)
                name = hashed_ref(ref)
                flag = {"at": v["flagged_at"], "cat": _categories(v.get("reason", ""))}
                if section == "security_closed":
                    flag["closed"] = 1
                carried["flags"][name] = flag
                c = clears.get(ref)
                if isinstance(c, dict) and str(c.get("at") or "") > flag["at"]:
                    carried["clears"][name] = {k2: c[k2] for k2 in ("by", "id", "at") if k2 in c}

    def dump(self, state: dict, published: str | None = None, overrides: dict | None = None) -> str:
        data, hidden = self.split({**state, **(overrides or {})})
        self._carry_hashed(data, hidden)
        for section in HASHED_SECTIONS:
            for k, v in (hidden.get(section) or {}).items():
                data.setdefault(section, {})[hashed_key(k)] = v
        if hidden.get("seen"):
            data["seen"] = [hashed_key(x) for x in hidden["seen"]] + list(data.get("seen") or [])
        data.update({"v": 1, "bus": self.bus_id, "run": self.run, "saved_at": now()})
        if published:
            data["security_published_sha256"] = hashlib.sha256(published.encode()).hexdigest()
        text = json.dumps(data, separators=(",", ":"), sort_keys=True)
        trimmable = [k for k in BUS_STATE_CAPS if k != "stale_reported"]
        while len(text.encode()) > BUS_STATE_CAP:
            sizes = {k: len(data[k]) for k in trimmable if isinstance(data.get(k), (list, dict)) and len(data[k]) > 1}
            if not sizes:
                break
            k = max(sizes, key=sizes.get)
            data[k] = data[k][len(data[k]) // 2:] if isinstance(data[k], list) else dict(list(data[k].items())[len(data[k]) // 2:])
            text = json.dumps(data, separators=(",", ":"), sort_keys=True)
        return text + "\n"

    def adopt(self, state: dict, store, audit=None) -> str:
        """Load this bus's state from the store's head into `state` (in place: the GitHub client holds its ETags).
        Returns what happened: none, loaded, or unreadable."""
        text = store.read(self.path)
        if not text:
            return "none"
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("v") != 1:
            if audit:
                audit({"bus_state_unreadable": self.path})
            return "unreadable"
        published_sha = data.pop("security_published_sha256", None)
        self.adopted_run = data.pop("run", None)
        for k in ("v", "bus", "saved_at", "sealed"):  # "sealed": an earlier draft of this format; its entries are rebuilt
            data.pop(k, None)
        etags = data.pop("etags", None) or {}
        state.setdefault("etags", {}).update(etags)
        state.pop("_seen_index", None)
        state.update(data)
        live = store.read(SECURITY_FILE)
        if published_sha:
            ok = live is not None and hashlib.sha256(live.encode()).hexdigest() == published_sha
            state["security_published"] = live if ok else ""
        last = store.file_commit(self.path)
        if last:
            state["state_branch_last"] = last
        return "loaded"

    # the audit log, append-only on colab-state
    def audit_record(self, record: dict, state: dict, restricted: set[str], hidden_refs: set[str]) -> dict | None:
        """One audit line as kept on colab-state: its kind, the item ref (hashed when restricted), the actor(s), the
        comment id, the bus, and the time. No spans, reasons, errors, or other text."""
        if not isinstance(record, dict) or not record:
            return None
        kind = next(iter(record))
        if not re.match(r"^[a-z_]+\Z", str(kind)):
            return None
        out: dict = {"kind": kind, "bus": self.bus_id}
        value = record[kind]
        ref = None
        if isinstance(value, str):
            m = _ITEM_KEY.match(value) or _ENV_ID.match(value)
            if m:
                ref = self._ref_of(f"{m['repo']}#{m['n']}")
        elif isinstance(value, (bool, int)) and not isinstance(value, float):
            out["value"] = value
        if kind == "security_record_rejected" and isinstance(value, int):
            out["comment"] = value
        if ref:
            out["ref"] = hashed_ref(ref) if (ref in hidden_refs or self._ref_repo(ref) in restricted | {None}) else ref
        actors = []
        for k in AUDIT_ACTOR_KEYS:
            v = record.get(k)
            for a in (v if isinstance(v, list) else [v]):
                if isinstance(a, str) and LOGIN_OR_BOT.match(a):
                    actors.append(a.lower())
        if kind in ("security_ignore_verified",) and isinstance(value, str) and LOGIN_OR_BOT.match(value):
            out["login"] = value.lower()
        if actors:
            out["actor"] = sorted(set(actors))
        if isinstance(record.get("id"), int):
            out["comment"] = record["id"]
        at = record.get("at")
        out["at"] = str(at)[:40] if at else now()
        return out

    def audit_part(self, now_t: datetime, n: int) -> str:
        month = f"{now_t.astimezone(timezone.utc):%Y-%m}"
        return f"audit/{month}.jsonl" if n == 1 else f"audit/{month}-{n}.jsonl"

    def audit_files(self, store, state: dict, records: list[dict], now_t: datetime) -> dict[str, str]:
        """{path: text} to stage: this run's lines appended to the month's newest file, rolling over to
        audit/<YYYY-MM>-<n>.jsonl when it would pass AUDIT_FILE_CAP. Nothing is ever dropped or rewritten: each new text
        starts with the file exactly as read at the head. A failed read raises (StateReadError, URLError); the caller
        then writes no audit this run rather than start a file that would hide the history."""
        records = [r for r in records if isinstance(r, dict)]
        restricted = self._restricted_repos(state)
        hidden_refs = self._hidden_refs(state, restricted)
        lines = []
        for r in records:
            rec = self.audit_record(r, state, restricted, hidden_refs)
            if rec is not None:
                lines.append(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
        if not lines:
            return {}
        n, base = 1, store.read_strict(self.audit_part(now_t, 1))
        while base is not None:  # find the newest part: the first one that does not exist ends the search
            nxt = store.read_strict(self.audit_part(now_t, n + 1))
            if nxt is None:
                break
            n, base = n + 1, nxt
            if n >= AUDIT_MAX_PARTS:
                raise StateReadError("too many audit parts this month")
        original = base or ""
        current = original + ("\n" if original and not original.endswith("\n") else "")
        out: dict[str, str] = {}
        size = len(current.encode())
        for line in lines:
            if size and size + len(line.encode()) > AUDIT_FILE_CAP:
                out[self.audit_part(now_t, n)] = current  # full: the rest goes into the next part
                n, current, size = n + 1, "", 0
            current += line
            size += len(line.encode())
        out[self.audit_part(now_t, n)] = current
        return {p: t for p, t in out.items() if t and t.rstrip("\n") != original.rstrip("\n")}  # unchanged parts are not restaged

    def changed_since_adopt(self, store) -> bool:
        """Did another run of this bus write its state since this run loaded it (two runs overlapping)?"""
        try:
            data = json.loads(store.read(self.path) or "null")
        except ValueError:
            return True
        return (data or {}).get("run") != self.adopted_run if isinstance(data, dict) else self.adopted_run is not None


def _read_audit(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def create_state_branch(gh, config: dict) -> str | None:
    """An Actions bus on a meta repo without colab-state: create the orphan branch (a README only), but only when GitHub
    reports active creation, update, and deletion rules on that name (so it is never writable by every member). Why
    not, or None when created."""
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    base = f"/repos/{org}/{meta}"
    try:
        rules = gh.get(f"{base}/rules/branches/{STATE_BRANCH}", conditional=False)
    except (urllib.error.URLError, OSError) as err:
        return f"could not read the rules for {STATE_BRANCH} ({str(err)[:100]})"
    types = {r.get("type") for r in rules if isinstance(r, dict)} if isinstance(rules, list) else set()
    missing = [t for t in ("creation", "update", "deletion") if t not in types]
    if missing:
        return f"not creating {STATE_BRANCH}: the ruleset is not active on it yet ({', '.join(missing)} missing); apply `colab.py plan` first"
    try:
        tree = gh.send("POST", f"{base}/git/trees", {"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
                                                                "content": state_branch_readme(config)}]})
        commit = gh.send("POST", f"{base}/git/commits", {"message": f"Start {STATE_BRANCH}: shared state of the Lab Colab bus",
                                                         "tree": tree["sha"], "parents": []})
        gh.send("POST", f"{base}/git/refs", {"ref": f"refs/heads/{STATE_BRANCH}", "sha": commit["sha"]})
    except (urllib.error.URLError, OSError, KeyError, TypeError) as err:
        return f"could not create {STATE_BRANCH} ({str(err)[:100]})"
    return None


def operator_refusal(gh, org: str, entry: dict, login: str) -> str | None:
    """Why this login may not run bus `entry` (it then stays passive and writes nothing), or None: the token's login
    must be the entry's operator and an active member of the operators team."""
    login = (login or "").lower()
    if not login:
        return "could not read the token's login (GET /user)"
    if entry.get("kind") == "actions":
        # An App is in no team: what lets it write colab-state is the ruleset's Integration bypass (by app_id).
        if login != entry["operator"]:
            return (f"bus {entry['id']} runs as the App {entry.get('app')} ({entry['operator']}), but this token belongs to "
                    f"{login}; run it from the colab-bus workflow with --app")
        return None
    if login != entry["operator"]:
        return f"bus {entry['id']} is declared for operator {entry['operator']}, but this token belongs to {login}"
    try:
        m = gh.get(f"/orgs/{org}/teams/{OPERATORS_TEAM}/memberships/{login}", conditional=False)
    except (urllib.error.URLError, OSError) as err:
        return f"could not check {login}'s membership in {org}/{OPERATORS_TEAM} ({str(err)[:100]})"
    if not (isinstance(m, dict) and m.get("state") == "active"):
        return f"{login} is not an active member of the team {org}/{OPERATORS_TEAM} (ask an org owner; colab.py plan proposes it)"
    return None


def pick_entry(entries: list[dict], bus_id: str | None, login: str) -> dict | None:
    if bus_id:
        return next((b for b in entries if b["id"] == bus_id.lower()), None)
    mine = [b for b in entries if b["operator"] == (login or "").lower()]
    return mine[0] if len(mine) == 1 else None


def inherit_state(state: dict, published: str | None, config: dict, lookback_minutes: float, now_t: datetime) -> int:
    """On taking over the lease, start from the previous holder's published security state: its flags (plain refs
    become local flags; hashed restricted refs are carried verbatim), verified clears and ignores, and its repo
    cursors less a look-back window (with the baseline moved there too, so nothing older is routed; delivery keys
    make the overlap harmless). Local knowledge wins. Returns the number of flags taken over."""
    try:
        value = json.loads(published or "")
        if isinstance(value.get("state"), str):
            value = json.loads(value["state"])
    except (ValueError, AttributeError, TypeError):
        return 0
    if not isinstance(value, dict) or value.get("v") != 1 or value.get("overflow"):
        return 0
    org = config["org"]["name"]
    local = state.setdefault("security", {})
    closed = state.setdefault("security_closed", {})
    carried = state.setdefault("security_inherited", {"flags": {}, "clears": {}})
    verified = state.setdefault("security_verified", {"clears": {}, "ignored": {}})
    taken = 0
    for name, flag in (value.get("flags") or {}).items():
        if not isinstance(flag, dict) or not flag.get("at"):
            continue
        if name.startswith("sha256:"):
            carried["flags"].setdefault(name, flag)
            if name in (value.get("clears") or {}):
                carried["clears"].setdefault(name, value["clears"][name])
            taken += 1
            continue
        repo, _, number = name.rpartition("#")
        try:
            key = f"{repo_key(repo, org)}#{number}"
        except Exception:  # noqa: BLE001 - a malformed ref is skipped, never fatal
            continue
        if key in local or key in closed:
            continue
        entry = {"flagged_at": flag["at"], "where": "inherited", "reason": f"rules: {flag.get('cat') or 'inherited'}", "ref": name}
        if flag.get("closed"):
            closed[key] = {**entry, "closed_at": now()}
        else:
            local[key] = entry
        taken += 1
        clear = (value.get("clears") or {}).get(name)
        if isinstance(clear, dict):
            verified.setdefault("clears", {}).setdefault(name, clear)
    for login, rec in (value.get("ignored") or {}).items():
        verified.setdefault("ignored", {}).setdefault(login, rec)
    cursors, baseline = state.setdefault("cursors", {}), state.setdefault("baseline_at", {})
    for repo, cur in (value.get("seen") or {}).items():
        t = _iso_minus(cur, lookback_minutes)
        if t and (repo not in cursors or cursors[repo] < t):
            cursors[repo] = t
            baseline[repo] = max(baseline.get(repo, ""), t)
    return taken


def _iso_minus(at: str, minutes: float) -> str | None:
    try:
        t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(t.timestamp() - minutes * 60, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def cycle(gh, config: dict, rollout: dict, subs: list[dict], state: dict, state_dir: Path, login: str, save=lambda: None,
          bus_id: str | None = None, bot: str | None = None, now_t: datetime | None = None, poll=None, remote=None) -> dict:
    """One bus cycle: who am I ([[buses]]), may I run (operator and team, or the App of an Actions bus), who holds the
    lease, then either a full poll (active) or nothing but an occasional heartbeat (passive), and one commit to
    colab-state.

    `remote` (a BusStateStore, for an Actions bus) loads the bus's own state from colab-state first and writes it back in
    the same commit; such a bus needs colab-state (it creates it when the ruleset is already active) and never falls back
    to Actions variables. Two overlapping runs of one bus cannot both commit: the second sees the first's state file
    change (compare-and-set on the head), writes nothing, and its deliveries were already deduplicated by their keys.

    Without a colab-state branch (a meta repo that has not migrated) it polls as before and writes the deprecated
    variables, for one release. Without [[buses]] it is a single implicit bus run by the token's login."""
    poll = poll or poll_once
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    now_t = now_t or datetime.now(timezone.utc)
    login = (login or "").lower()
    audit = state_dir / "audit.jsonl"
    try:
        entries = buses(config)
    except BusConfigError as err:
        _append(audit, {"buses_invalid": str(err), "at": now()})
        return {"passive": True, "reason": f"[[buses]] is invalid: {err}", "errors": 1}
    if entries:
        entry = pick_entry(entries, bus_id, login)
        if entry is None:
            why = (f"no [[buses]] entry with id {bus_id}" if bus_id else
                   f"no single [[buses]] entry for operator {login or '?'}; pass --bus-id")
            _append(audit, {"bus_passive": why, "at": now()})
            return {"passive": True, "reason": why}
        refusal = operator_refusal(gh, org, entry, login)
        if refusal:
            _append(audit, {"bus_passive": refusal, "bus": entry["id"], "at": now()})
            return {"passive": True, "bus": entry["id"], "reason": refusal}
        operators = {b["operator"] for b in entries}  # people, and the bot login of each Actions bus's App
    else:
        entry = {"id": (bus_id or "default").lower(), "operator": login, "host": "", "role": "primary", "order": 0, "kind": "operator"}
        operators = {login}
    if remote is not None and entries and entry.get("kind") != "actions":
        why = f"bus {entry['id']} is run by an operator, not by the App: --app runs only an Actions bus (kind = \"actions\")"
        _append(audit, {"bus_passive": why, "bus": entry["id"], "at": now()})
        return {"passive": True, "bus": entry["id"], "reason": why}
    if remote is not None and remote.bus_id != entry["id"]:
        remote = type(remote)(remote.config, entry["id"], remote.run)
    route_bot = bot if bot is not None else ("" if entries else login)  # operator-run: own deliveries carry the marker
    ident = {"bus": entry["id"], "operator": entry["operator"], "host": entry.get("host", ""), "role": entry["role"]}
    writers = operators
    store = StateBranch(gh, org, meta)

    ttl = lease_ttl_minutes(config)

    def keep_role(err) -> dict:
        """A transient read failure never counts as "no lease": the bus keeps the role it had for this poll. An
        active bus keeps delivering (delivery keys stop duplicates) but writes nothing to colab-state; a standby
        stays standing by. Reads failing for longer than one lease period: an active bus can no longer know it
        still holds the lease (a standby may have taken over), so it stops delivering until a read succeeds."""
        _append(audit, {"state_branch_read_failed": str(err)[:300], "kept_role": state.get("bus_mode") or "passive", "at": now()})
        since = state.setdefault("state_read_failing_since", now_t.timestamp())
        if state.get("bus_mode") == "active" and now_t.timestamp() - float(since) > ttl * 60:
            state["bus_mode"] = "passive"
            _append(audit, {"lease_read_failing": f"colab-state unreadable for over {ttl:.0f} min; standing by", "at": now()})
            save()
            return {**ident, "passive": True, "lease_read_failing": True, "errors": 1,
                    "reason": "colab-state has been unreadable for longer than the lease; standing by until a read succeeds"}
        save()
        if state.get("bus_mode") == "active":
            found = poll(gh, config, rollout, subs, state, state_dir, route_bot, save, store=HoldStore(),
                         bus_id=entry["id"], operator=entry["operator"], writers=writers)
            return {**ident, **found, "active": True, "state_read_failed": True, "errors": found.get("errors", 0) + 1}
        return {**ident, "passive": True, "reason": "could not read the colab-state branch this poll", "errors": 1}
    try:
        store.load()
        if remote is not None and not store.available:
            why = create_state_branch(gh, config)
            if why:
                _append(audit, {"bus_passive": why, "bus": entry["id"], "at": now()})
                return {**ident, "passive": True, "errors": 1, "reason": f"an Actions bus needs the {STATE_BRANCH} branch: {why}"}
            _append(audit, {"state_branch_created": True, "at": now()})
            store.load()
        if remote is not None and store.available:
            loaded = remote.adopt(state, store, audit=lambda r: _append(audit, {**r, "at": now()}))
            if loaded != "loaded":
                _append(audit, {"bus_state": loaded, "at": now()})
    except (urllib.error.URLError, OSError, StateReadError) as err:
        return keep_role(err)
    state.pop("state_read_failing_since", None)
    if not store.available and remote is not None:
        return {**ident, "passive": True, "errors": 1, "reason": f"the {STATE_BRANCH} branch is missing"}
    if not store.available:
        # Migration: one release on the Actions variables, single bus, no lease (variables cannot compare-and-set).
        stats = poll(gh, config, rollout, subs, state, state_dir, route_bot, save, bus_id=entry["id"], operator=entry["operator"],
                     writers=writers)
        stats["state_branch"] = f"missing: run colab.py plan, which proposes creating {STATE_BRANCH}"
        if state.get("state_branch_warned", "")[:10] != now()[:10]:
            _append(audit, {"state_branch_missing": True, "fallback": "Actions variables (deprecated)", "at": now()})
            state["state_branch_warned"] = now()
        heartbeat(gh, org, meta, {**ident, **stats})
        return {**ident, **stats}

    stats: dict = {}
    last = state.get("state_branch_last")
    if last and store.head != last:
        author = store.head_author()
        if author not in operators | {login}:
            stats["security_foreign_edits"] = stats.get("security_foreign_edits", 0) + 1
            stats["errors"] = stats.get("errors", 0) + 1
            stats["state_branch_foreign"] = author or "unknown"
            _append(audit, {"state_branch_foreign_edit": store.head, "author": author or "unknown", "at": now()})
            state["security_published"] = None  # re-assert our own state on this commit
    lookback = float((config.get("bus") or {}).get("takeover_lookback_minutes", 60))
    passive_every = float((config.get("bus") or {}).get("passive_heartbeat_minutes", 30))
    was_active = False
    for attempt in range(3):
        try:
            lease = store.read_lease()
        except (urllib.error.URLError, OSError) as err:
            if attempt == 0:
                return keep_role(err)
            stats["errors"] = stats.get("errors", 0) + 1
            _append(audit, {"state_branch_read_failed": str(err)[:300], "at": now()})
            break
        if attempt == 0:
            state.pop("state_read_failing_since", None)
        served = _parse_time((lease or {}).get("renewed_at"))
        if served is not None and (served - now_t).total_seconds() > LEASE_SKEW_SECONDS:
            # A commit dated in the future cannot prove a fresh lease: it counts as stale.
            _append(audit, {"lease_future_date": (lease or {}).get("renewed_at"), "holder": (lease or {}).get("holder"), "at": now()})
            stats["lease_future_date"] = (lease or {}).get("renewed_at")
        mode, why = lease_decision(lease, entry, entries or [entry], now_t, ttl)
        if attempt == 0:
            if mode == "active":
                was_active = True
                previous = (lease or {}).get("handed_from") if (lease or {}).get("holder") == entry["id"] else (lease or {}).get("holder")
                if state.get("bus_mode") != "active":  # becoming active (takeover, handback, or a fresh state dir)
                    taken = inherit_state(state, store.read(SECURITY_FILE), config, lookback, now_t)
                    _append(audit, {"lease_taken": previous or "(none)", "from_operator": (lease or {}).get("operator"),
                                    "flags_inherited": taken, "at": now()})
                    if previous and previous != entry["id"]:
                        stats["took_over_from"] = previous
                found = poll(gh, config, rollout, subs, state, state_dir, route_bot, save, store=store,
                             bus_id=entry["id"], operator=entry["operator"], writers=writers)
                for k, v in found.items():
                    stats[k] = stats.get(k, 0) + v if isinstance(v, int) and not isinstance(v, bool) and k in stats else v
            else:
                stats.update({"passive": True, "holder": (lease or {}).get("holder"), "reason": why})
        elif was_active and mode != "active":
            store.unstage(SECURITY_FILE)
            store.unstage(LEASE_FILE)
            stats.update({"lease_lost": True, "holder": (lease or {}).get("holder"), "reason": why})
            _append(audit, {"lease_lost": (lease or {}).get("holder"), "at": now()})
        active = mode == "active"
        if active:
            since = lease.get("since") if lease and lease.get("holder") == entry["id"] else None
            store.stage(LEASE_FILE, json.dumps(compose_bus_lease(entry, now_t, ttl, since), indent=2, sort_keys=True) + "\n")
        beat_due = active or state.get("bus_mode") != mode or \
            (now_t.timestamp() - float(state.get("passive_beat_ts") or 0)) >= passive_every * 60
        if beat_due:
            store.stage(heartbeat_path(entry["id"]), json.dumps({"at": now(), **ident, "active": active, "lease": why,
                                                                 **{k: v for k, v in stats.items() if k not in ident}},
                                                                indent=2, sort_keys=True) + "\n")
        if remote is not None and store.staged:
            # The bus's own state rides in the same commit (what it will be once this commit lands).
            published = store.staged.get(SECURITY_FILE) or state.get("security_published")
            overrides = {"bus_mode": mode}
            if beat_due and not active:
                overrides["passive_beat_ts"] = now_t.timestamp()
            store.stage(remote.path, remote.dump({k: v for k, v in state.items()}, published, overrides))
            # This run's audit lines, appended to the month's file in the same commit (kinds, refs, actors, times only).
            for path in [p for p in store.staged if p.startswith("audit/")]:
                store.unstage(path)  # a retry recomputes them from the head it just read
            try:
                for path, text in remote.audit_files(store, state, _read_audit(audit), now_t).items():
                    store.stage(path, text)
            except (urllib.error.URLError, OSError, StateReadError) as err:
                stats["audit_skipped"] = True  # never a fresh file over unread history; the lines stay in the run's log counts
                _append(audit, {"audit_write_skipped": str(err)[:200], "at": now()})
        try:
            sha = store.commit(f"bus {entry['id']} ({entry['operator']}): {'poll' if active else 'standby'} at {now()}")
        except StateConflict:
            try:
                if not store.load():
                    break
            except (urllib.error.URLError, OSError, StateReadError) as err:
                stats["errors"] = stats.get("errors", 0) + 1
                _append(audit, {"state_branch_read_failed": str(err)[:300], "at": now()})
                break
            if remote is not None and remote.changed_since_adopt(store):
                # Another run of this same bus committed meanwhile: its commit stands, this run writes nothing.
                store.staged.clear()
                store._callbacks = []
                stats.update({"overlap": True, "errors": stats.get("errors", 0) + 1})
                _append(audit, {"bus_run_overlap": "another run of this bus committed first; this run wrote nothing", "at": now()})
                break
            continue
        except (urllib.error.URLError, OSError) as err:
            stats["errors"] = stats.get("errors", 0) + 1
            _append(audit, {"state_branch_write_failed": str(err)[:300], "at": now()})
            break
        if sha:
            state["state_branch_last"] = sha
        if beat_due and not active:
            state["passive_beat_ts"] = now_t.timestamp()
        state["bus_mode"] = mode
        stats["active"] = active
        break
    else:
        stats["errors"] = stats.get("errors", 0) + 1
        _append(audit, {"state_branch_conflict": "gave up after 3 attempts", "at": now()})
    save()
    return {**ident, **stats}


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _default_meta() -> Path | None:
    """The meta repo checkout this script sits in (`<meta>/bus/colab_bus.py`), when it has the config."""
    here = Path(__file__).resolve().parent.parent
    return here if (here / ".colab" / "config.toml").is_file() else None


def _app_signing_key(state_dir: Path) -> None:
    """An Actions bus with [security] signing = "ssh": the key comes from the optional secret COLAB_SIGNING_KEY (its
    content, in COLAB_SIGNING_KEY_CONTENT) into a 0600 file in the private run folder; COLAB_SIGNING_KEY then names it."""
    content = os.environ.pop("COLAB_SIGNING_KEY_CONTENT", "")
    if not content.strip():
        return
    path = state_dir / "signing-key"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content.strip() + "\n")
    os.environ["COLAB_SIGNING_KEY"] = str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta", type=Path, default=None,
                    help="local clone of the meta repo (reads .colab/*); default: the checkout this script is in")
    ap.add_argument("--state-dir", type=Path, help="private group storage for bus state (an operator-run bus: required; "
                                                   "--app: a private temporary folder, since its state lives on colab-state)")
    ap.add_argument("--token-file", type=Path, help="0600 file holding a fine-grained token (preferred)")
    ap.add_argument("--app", action="store_true",
                    help="run as the org's GitHub App (an Actions bus): COLAB_APP_ID and COLAB_APP_PRIVATE_KEY from the "
                         "environment (Actions secrets); the bus's own state is kept on colab-state")
    ap.add_argument("--bot", help="login whose events are ignored (default: the token's own login without [[buses]]; "
                                  "nobody with [[buses]], where the bus runs as a person and its writes carry a marker)")
    ap.add_argument("--bus-id", default=os.environ.get("COLAB_BUS_ID") or None,
                    help="this bus's [[buses]] id (default: $COLAB_BUS_ID, else the one entry run by the token's login)")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)
    args.meta = args.meta or _default_meta()
    if args.meta is None:
        ap.error("--meta is required (no .colab/config.toml next to this script's folder)")
    if args.state_dir is None:
        if not args.app:
            ap.error("--state-dir is required (except with --app)")
        import tempfile
        args.state_dir = Path(tempfile.mkdtemp(prefix="colab-bus-"))  # 0700; the runner is thrown away after the run
    args.state_dir.mkdir(parents=True, exist_ok=True)
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGUSR1):
        signal.signal(sig, lambda *_: stop.update(flag=True))
    auth, remote = None, None
    if args.app:
        try:
            with open(args.meta / ".colab" / "config.toml", "rb") as fh:
                org = tomllib.load(fh)["org"]["name"]
            auth = AppAuth.from_env(org)
            os.environ.pop("COLAB_APP_PRIVATE_KEY", None)  # nothing started from here needs the key
            auth.token()
        except (AppAuthError, OSError, KeyError, tomllib.TOMLDecodeError, urllib.error.URLError) as err:
            print(json.dumps({"at": now(), "error": f"App sign-in failed: {err}"[:500]}), flush=True)
            return 1
        _app_signing_key(args.state_dir)
        gh_token = auth.token
    else:
        gh_token = read_token(args.token_file)
    backoff, failed = 60, False
    while True:
        try:
            # Pick up config, subscription, and rollout changes (including reverts) every poll. An Actions run has a
            # fresh checkout, and its token stays out of git.
            if not args.app:
                subprocess.run(["git", "-C", str(args.meta), "pull", "--ff-only", "-q"], capture_output=True)
            with open(args.meta / ".colab" / "config.toml", "rb") as fh:
                config = tomllib.load(fh)
            rollout = load_json(args.meta / ".colab" / "rollout.json", {"spaces": {}})
            subs_path = args.meta / ".colab" / "subscriptions.toml"
            subs = tomllib.loads(subs_path.read_text()).get("subscribe", []) if subs_path.exists() else []
            state_path = args.state_dir / "state.json"
            state = load_json(state_path, {})
            gh = GitHub(gh_token, state)
            if auth is not None:
                login = auth.login
                remote = BusStateStore(config, (args.bus_id or "").lower() or next(
                    (b["id"] for b in buses(config) if b["operator"] == login), "default"))
            else:
                login = (gh.get("/user", conditional=False) or {}).get("login", "")

            def save() -> None:
                clean = {k: v for k, v in state.items() if not k.startswith("_")}
                tmp = state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(clean))
                tmp.replace(state_path)

            def poll(*a, **kw):
                found = poll_once(*a, **kw)
                found.update(session_stats(gh, config, state, args.state_dir))
                if auth is None:
                    drift_check(gh, config, state, args.state_dir, found)
                else:
                    # observe needs owner reads (issue types, rulesets' bypass lists, App installations) that the App
                    # does not have; a partial observation would report false drift. Owners run `colab.py plan`.
                    found["drift_check"] = "off for an Actions bus"
                return found

            stats = cycle(gh, config, rollout, subs, state, args.state_dir, login, save, bus_id=args.bus_id,
                          bot=args.bot, poll=poll, remote=remote)
            if auth is not None:  # the private audit log goes with the runner: say what kinds of lines it had
                stats["audit"] = audit_summary(args.state_dir / "audit.jsonl")
            print(json.dumps({"at": now(), **stats}), flush=True)
            backoff, failed = 60, bool(auth is not None and stats.get("errors") and not stats.get("active"))
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


def audit_summary(path: Path) -> dict:
    """Counts of the audit log's record kinds (the first key of each line): no reasons, spans, or item refs."""
    out: dict = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            first = next(iter(json.loads(line)), "")
        except (ValueError, StopIteration, TypeError):
            continue
        out[first] = out.get(first, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
