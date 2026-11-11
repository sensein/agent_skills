"""Download-on-demand cache for the trusted ontologies (stdlib only).

The ~400 MB of ontology files used to live in `trusted_ontologes/`. They are now
listed in `ontologies.json` (file name, asset URL, sha256, size, optional upstream
IRI) and fetched lazily into a local cache that is shared across runs and projects:

    ${STRUCTSENSE_ONTOLOGY_CACHE:-${XDG_CACHE_HOME:-~/.cache}/structsense/ontologies}

Downloads go to a temp file in the cache directory, are verified against the
manifest sha256, and then moved into place with an atomic `os.replace`. A per-file
lock makes concurrent runs safe (one downloads, the others wait and reuse).

    python -m scripts.ontology_cache path cl.owl          # resolve (downloads if needed)
    python -m scripts.ontology_cache prefetch --all       # or: prefetch cl.owl hp-base.owl
    python -m scripts.ontology_cache import-local DIR     # seed from a trusted_ontologes/ checkout
    python -m scripts.ontology_cache verify [names]       # re-hash cached files
    python -m scripts.ontology_cache list                 # manifest + cache state
    python -m scripts.ontology_cache build-manifest DIR   # maintainer: regenerate ontologies.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

try:  # POSIX; on Windows the lock degrades to a no-op (atomic replace still holds)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

SKILL_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = SKILL_DIR / "ontologies.json"
RELEASE_TAG = "structsense-ontologies-v1"
ASSET_BASE = f"https://github.com/sensein/agent_skills/releases/download/{RELEASE_TAG}"
ENV_CACHE = "STRUCTSENSE_ONTOLOGY_CACHE"
ONTOLOGY_EXTS = (".owl", ".rdf", ".xml", ".ttl", ".nt", ".jsonld")
_CHUNK = 1 << 20


class OntologyError(RuntimeError):
    """Base class for every error raised here."""


class OntologyUnavailable(OntologyError):
    """The file is not cached and could not be downloaded (offline, HTTP error)."""

    def __init__(self, msg: str, network: bool = False):
        super().__init__(msg)
        self.network = network


class ChecksumMismatch(OntologyError):
    pass


def cache_dir() -> Path:
    env = os.environ.get(ENV_CACHE)
    if env:
        return Path(os.path.expanduser(env))
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".cache"
    return base / "structsense" / "ontologies"


def load_manifest(path: Optional[Path] = None) -> dict[str, dict]:
    """name -> entry. Missing manifest => empty (callers then fall back to local files)."""
    p = Path(path) if path else MANIFEST_PATH
    if not p.is_file():
        return {}
    data = json.loads(p.read_text())
    return {e["file"]: e for e in data.get("ontologies", [])}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(name: str, manifest: Optional[dict]) -> dict:
    manifest = load_manifest() if manifest is None else manifest
    if name not in manifest:
        raise OntologyError(f"{name!r} is not listed in the ontology manifest ({MANIFEST_PATH.name})")
    return manifest[name]


def cached_path(name: str, manifest: Optional[dict] = None, cache: Optional[Path] = None) -> Optional[Path]:
    """The cached file if present with the manifest size; never downloads, never hashes."""
    manifest = load_manifest() if manifest is None else manifest
    e = manifest.get(name)
    p = (cache or cache_dir()) / name
    if e is None or not p.is_file():
        return None
    return p if p.stat().st_size == e["size"] else None


@contextmanager
def _lock(cache: Path, name: str) -> Iterator[None]:
    cache.mkdir(parents=True, exist_ok=True)
    f = open(cache / f".{name}.lock", "a+")
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _open_url(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url, headers={"User-Agent": "structsense-ontology-cache"})
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch_to(url: str, tmp_fh, opener: Callable = _open_url) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with opener(url) as r:
        for chunk in iter(lambda: r.read(_CHUNK), b""):
            h.update(chunk)
            tmp_fh.write(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _install(src_tmp: str, dest: Path) -> None:
    os.chmod(src_tmp, 0o644)
    os.replace(src_tmp, dest)


def resolve(name: str, *, manifest: Optional[dict] = None, cache: Optional[Path] = None,
            opener: Callable = _open_url, log: Callable[[str], None] = lambda m: None) -> Path:
    """Local path of ontology `name`, downloading and verifying it on first use."""
    manifest = load_manifest() if manifest is None else manifest
    e = _entry(name, manifest)
    cache = cache or cache_dir()
    hit = cached_path(name, manifest, cache)
    if hit:
        return hit
    dest = cache / name
    with _lock(cache, name):
        hit = cached_path(name, manifest, cache)  # another process may have finished meanwhile
        if hit:
            return hit
        urls = [e["url"]] + ([e["upstream"]] if e.get("upstream", "").startswith(("http://", "https://")) else [])
        problems: list[str] = []
        network = False
        for url in urls:
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=str(cache))
            try:
                log(f"downloading {name} from {url}")
                with os.fdopen(fd, "wb") as fh:
                    digest, n = _fetch_to(url, fh, opener)
                if digest != e["sha256"]:
                    problems.append(f"{url}: sha256 {digest[:12]}... != expected {e['sha256'][:12]}... ({n} bytes)")
                    continue
                _install(tmp, dest)
                tmp = ""
                return dest
            except (urllib.error.URLError, OSError, TimeoutError) as ex:
                network = True
                problems.append(f"{url}: {getattr(ex, 'reason', ex)}")
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
    mismatch = any("sha256" in p for p in problems)
    msg = (f"cannot obtain ontology {name} (cache: {cache}):\n  " + "\n  ".join(problems) +
           "\nIf offline, fill the cache from a machine or checkout that has the files: "
           "`python -m scripts.ontology_cache import-local <dir with the .owl files>`, or copy "
           f"verified files into the cache directory (override with ${ENV_CACHE}).")
    if mismatch and not network:
        raise ChecksumMismatch(msg)
    raise OntologyUnavailable(msg, network=network)


def import_local(src: Path, names: Optional[list[str]] = None, *, manifest: Optional[dict] = None,
                 cache: Optional[Path] = None, log: Callable[[str], None] = lambda m: None) -> dict:
    """Seed the cache from `src` (e.g. an old checkout's trusted_ontologes/). Only files whose
    sha256 matches the manifest are imported; others are reported, never copied."""
    manifest = load_manifest() if manifest is None else manifest
    cache = cache or cache_dir()
    src = Path(src)
    if not src.is_dir():
        raise OntologyError(f"{src} is not a directory")
    out = {"imported": [], "already_cached": [], "mismatch": [], "absent": []}
    for name in (names or sorted(manifest)):
        e = _entry(name, manifest)
        f = src / name
        if not f.is_file():
            out["absent"].append(name)
            continue
        if cached_path(name, manifest, cache):
            out["already_cached"].append(name)
            continue
        if f.stat().st_size != e["size"] or sha256_file(f) != e["sha256"]:
            out["mismatch"].append(name)
            continue
        with _lock(cache, name):
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=str(cache))
            try:
                with os.fdopen(fd, "wb") as fh, open(f, "rb") as sf:
                    shutil.copyfileobj(sf, fh, _CHUNK)
                _install(tmp, cache / name)
                tmp = ""
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
        out["imported"].append(name)
        log(f"imported {name}")
    return out


def prefetch(names: Optional[list[str]] = None, **kw) -> dict:
    manifest = kw.pop("manifest", None)
    manifest = load_manifest() if manifest is None else manifest
    out = {"ok": [], "failed": {}}
    for name in (names or sorted(manifest)):
        try:
            resolve(name, manifest=manifest, **kw)
            out["ok"].append(name)
        except OntologyError as ex:
            out["failed"][name] = str(ex)
    return out


def verify(names: Optional[list[str]] = None, *, manifest: Optional[dict] = None,
           cache: Optional[Path] = None) -> dict:
    manifest = load_manifest() if manifest is None else manifest
    cache = cache or cache_dir()
    out = {"ok": [], "bad": [], "not_cached": []}
    for name in (names or sorted(manifest)):
        p = cache / name
        if not p.is_file():
            out["not_cached"].append(name)
        elif sha256_file(p) == _entry(name, manifest)["sha256"]:
            out["ok"].append(name)
        else:
            out["bad"].append(name)
    return out


def build_manifest(src: Path, sources_json: Optional[Path] = None) -> dict:
    """Maintainer helper: manifest for every ontology file in `src`."""
    upstream: dict[str, str] = {}
    sj = Path(sources_json) if sources_json else Path(src) / "sources.json"
    if sj.is_file():
        import re
        data = json.loads(re.sub(r",(\s*[}\]])", r"\1", sj.read_text()))
        for e in data.get("declared") or []:
            s = e.get("source") or ""
            if e.get("file") and s.startswith(("http://", "https://")):
                upstream[e["file"]] = s
    entries = []
    for p in sorted(Path(src).iterdir()):
        if p.suffix.lower() in ONTOLOGY_EXTS and p.is_file():
            ent = {"file": p.name, "url": f"{ASSET_BASE}/{p.name}", "sha256": sha256_file(p),
                   "size": p.stat().st_size}
            if p.name in upstream:
                ent["upstream"] = upstream[p.name]
            entries.append(ent)
    return {"_comment": "Trusted ontology files, hosted as assets of the GitHub release "
                        f"{RELEASE_TAG} on sensein/agent_skills. `upstream` is optional provenance and a "
                        "fallback download (used only if its bytes match sha256). Regenerate with "
                        "`python -m scripts.ontology_cache build-manifest <dir>`.",
            "release_tag": RELEASE_TAG, "ontologies": entries}


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ontology_cache", description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, help="alternative ontologies.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("path", help="print the local path of one ontology (downloads if needed)")
    p.add_argument("name")
    p = sub.add_parser("prefetch", help="download ontologies into the cache")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("import-local", help="seed the cache from a directory of ontology files")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("verify", help="re-hash cached files against the manifest")
    p.add_argument("names", nargs="*")
    sub.add_parser("list", help="manifest entries and whether each is cached")
    sub.add_parser("dir", help="print the cache directory")
    p = sub.add_parser("build-manifest", help="regenerate ontologies.json from a directory")
    p.add_argument("dir", type=Path)
    p.add_argument("-o", "--output", type=Path)
    a = ap.parse_args(argv)
    say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731

    if a.cmd == "dir":
        print(cache_dir())
        return 0
    if a.cmd == "build-manifest":
        m = build_manifest(a.dir)
        out = a.output or MANIFEST_PATH
        out.write_text(json.dumps(m, indent=2) + "\n")
        say(f"wrote {out}: {len(m['ontologies'])} files")
        return 0
    manifest = load_manifest(a.manifest)
    try:
        if a.cmd == "path":
            print(resolve(a.name, manifest=manifest, log=say))
        elif a.cmd == "prefetch":
            if not a.names and not a.all:
                ap.error("give ontology file names or --all")
            r = prefetch(None if a.all else a.names, manifest=manifest, log=say)
            say(f"cached {len(r['ok'])}; failed {len(r['failed'])}; cache {cache_dir()}")
            for n, why in r["failed"].items():
                say(f"FAILED {n}: {why}")
            return 1 if r["failed"] else 0
        elif a.cmd == "import-local":
            r = import_local(a.dir, manifest=manifest, log=say)
            say(f"imported {len(r['imported'])}, already cached {len(r['already_cached'])}, "
                f"checksum mismatch {len(r['mismatch'])}, absent in dir {len(r['absent'])}; cache {cache_dir()}")
            for n in r["mismatch"]:
                say(f"MISMATCH (not imported) {n}")
            return 1 if r["mismatch"] else 0
        elif a.cmd == "verify":
            r = verify(a.names or None, manifest=manifest)
            say(f"ok {len(r['ok'])}, bad {len(r['bad'])}, not cached {len(r['not_cached'])}")
            for n in r["bad"]:
                say(f"BAD {n}")
            return 1 if r["bad"] else 0
        elif a.cmd == "list":
            for name, e in sorted(manifest.items()):
                state = "cached" if cached_path(name, manifest) else "not cached"
                print(f"{name:<32} {e['size'] / 1e6:>8.1f} MB  {state}")
    except OntologyError as ex:
        say(f"error: {ex}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
