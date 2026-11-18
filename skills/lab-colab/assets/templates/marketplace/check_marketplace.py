#!/usr/bin/env python3
"""Check marketplace.json against the rules claude.ai org sync enforces (stdlib only).

claude plugin validate passes some things claude.ai then rejects. Rules from
https://claude.com/docs/plugins/org-sync.md plus errors claude.ai has returned:
  - required fields and plugin-id characters
  - only source types org sync accepts: github, url, git-subdir, or a ./ path (no bare names)
  - git sources (url, git-subdir) use https:// URLs, not owner/repo shorthand
  - a github.com source under another owner must be a public repository
  - the marketplace repository itself must be private (checked with --fetch when GITHUB_REPOSITORY is set)
  - with --fetch, at the pinned commit:
      under 200 MB uncompressed
      no top-level bin/ directory
      no .claude-plugin/plugin.json requires "strict": false (the entry is the manifest)
Exit 1 on any error.
"""
import argparse, json, os, re, subprocess, sys, tempfile

LIMIT = 200 * 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ORG_SOURCES = {"github", "url", "git-subdir"}  # plus relative paths; npm/archive/command unsupported


def tree_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total


def github_owner_repo(src):
    if src.get("source") == "github":
        return src["repo"]
    m = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", str(src.get("url", "")))
    return m.group(1) if m else None


def is_public(repo):
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen(f"https://api.github.com/repos/{repo}", timeout=20)  # unauthenticated: 404 if private
        return True
    except urllib.error.HTTPError:
        return False


def check_private(repo, token=None):
    """Errors if the marketplace repo is public. Unknown (no token, API failure) is a warning, not an error."""
    import urllib.request, urllib.error
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            info = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as err:
        print(f"WARN could not confirm that {repo} is private: {err}")
        return []
    return [] if info.get("private") else [f"{repo} is public; the marketplace repo must be private"]


def fetch_plugin(src):
    """Sparse, blobless fetch of just the plugin's files at its pinned commit.
    Returns (size_bytes, has_plugin_json, has_top_level_bin)."""
    url = src.get("url") or f"https://github.com/{src['repo']}.git"
    ref = src.get("sha") or src.get("ref") or "HEAD"
    with tempfile.TemporaryDirectory() as d:
        run = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True)
        run("init", "-q"); run("remote", "add", "origin", url)
        if src.get("path"):
            run("sparse-checkout", "set", "--no-cone", src["path"])
        run("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref)
        run("checkout", "-q", "FETCH_HEAD")
        target = os.path.join(d, src["path"]) if src.get("path") else d
        if not os.path.isdir(target):
            raise FileNotFoundError(f"{src.get('path')} not found at {ref}")
        return (tree_size(target), os.path.isfile(os.path.join(target, ".claude-plugin", "plugin.json")),
                os.path.isdir(os.path.join(target, "bin")))


def check(m, fetch, owner):
    errors = []
    for key in ("name", "owner", "plugins"):
        if key not in m:
            errors.append(f"missing top-level '{key}'")
    if not NAME.match(m.get("name", "")):
        errors.append(f"bad marketplace name {m.get('name')!r}")
    seen = set()
    for i, p in enumerate(m.get("plugins", [])):
        where = f"plugins[{i}] {p.get('name', '?')}"
        if not NAME.match(p.get("name", "")):
            errors.append(f"{where}: bad plugin name")
        if p.get("name") in seen:
            errors.append(f"{where}: duplicate name")
        seen.add(p.get("name"))
        src = p.get("source")
        if isinstance(src, str):
            if not src.startswith("./"):
                errors.append(f"{where}: relative source must start with ./")
            continue
        if not isinstance(src, dict) or src.get("source") not in ORG_SOURCES:
            errors.append(f"{where}: source type {src and src.get('source')!r} not supported by claude.ai org marketplaces")
            continue
        if src["source"] in ("url", "git-subdir") and not str(src.get("url", "")).startswith("https://"):
            errors.append(f"{where}: git source URL must use https:// (got {src.get('url')!r})")
        if src.get("sha") and not re.fullmatch(r"[0-9a-f]{40}", src["sha"]):
            errors.append(f"{where}: sha must be a full 40-character lowercase commit")
        repo = github_owner_repo(src)
        if repo and repo.split("/")[0].lower() != owner.lower() and fetch and not is_public(repo):
            errors.append(f"{where}: {repo} is private and not under {owner}; org sync cannot fetch it")
        if fetch and not errors:
            try:
                size, has_manifest, has_bin = fetch_plugin(src)
                print(f"{p['name']:<20} {size / 1e6:8.1f} MB  plugin.json={'yes' if has_manifest else 'no'}")
                if size > LIMIT:
                    errors.append(f"{where}: {size / 1e6:.0f} MB uncompressed, over the 200 MB limit")
                if has_bin:
                    errors.append(f"{where}: plugin contains a top-level bin/ directory, which org sync rejects")
                if not has_manifest and p.get("strict", True) is not False:
                    errors.append(f'{where}: no .claude-plugin/plugin.json, so the entry must set "strict": false')
            except Exception as err:  # noqa: BLE001
                errors.append(f"{where}: could not fetch source: {err}")
    return errors


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=".claude-plugin/marketplace.json")
    ap.add_argument("--fetch", action="store_true", help="fetch each source and check size, bin/, and manifest")
    ap.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "__ORG__"),
                    help="owner of the marketplace repository")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="owner/name of the marketplace repository (default: $GITHUB_REPOSITORY)")
    a = ap.parse_args()
    errs = check(json.load(open(a.file)), a.fetch, a.owner)
    if a.fetch and a.repo:
        errs += check_private(a.repo, os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    for e in errs:
        print("ERROR", e)
    print("OK" if not errs else f"{len(errs)} error(s)")
    sys.exit(1 if errs else 0)
