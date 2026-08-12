"""ABCD / HBCD variable dictionary — load a release snapshot, then resolve names.

The authoritative variable list is the NBDC data dictionary (`lst_dds`), keyed by
study (`abcd` | `hbcd`) and release (`"5.1"`, `"6.0"`, ...). It ships in the
`NBDCtoolsData` R data package; `nbdctools` (PyPI) downloads and reads it without
R. This module wraps that into three operations the ABCD extraction mode needs:

  1. `build_snapshot()` — materialise one study+release as a flat JSON snapshot on
     disk, with provenance (source, retrieval time, sha256, row count).
  2. `load_snapshot()` / `load_all_snapshots()` — read snapshots back.
  3. `Dictionary.resolve()` — decide whether a string from a paper IS a real
     variable in that release, and how we know.

Why a snapshot rather than querying live: a claim like "this paper uses
`nihtbx_flanker_uncorrected`" is only checkable against a *stated* release. The
snapshot pins it, so a re-run months later reproduces the same verdict, and the
provenance block records exactly which dictionary was consulted.

ACQUISITION IS TOOL-ONLY. There is no "the model knows ABCD variable names"
path — a variable that cannot be found in a real dictionary is reported as
unverified, never silently accepted. This mirrors the skill's concept-mapping
rule (SKILL.md hard rule 15).

    # once per release (needs: pip install nbdctools)
    python -m scripts.abcd_dictionary build --study abcd --release 6.1
    python -m scripts.abcd_dictionary build --study abcd --release latest --all-releases

    # or from a dictionary you exported yourself (R, or the NBDC portal)
    python -m scripts.abcd_dictionary build --study abcd --release 6.1 \
        --from-csv my_dd_export.csv

    python -m scripts.abcd_dictionary lookup nihtbx_flanker_uncorrected
    python -m scripts.abcd_dictionary info
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Where snapshots live. Override with STRUCTSENSE_ABCD_DIR.
DEFAULT_DIR = Path(
    os.getenv("STRUCTSENSE_ABCD_DIR")
    or (Path.home() / ".cache" / "structsense" / "abcd")
)

# r-universe endpoint nbdctools itself uses for the dictionary bundle.
LST_DDS_URL = "https://nbdc-datahub.r-universe.dev/NBDCtoolsData/data/lst_dds/rds"

# Columns we keep. The dictionary has many more; these are the ones that let us
# verify a mention and describe it back to the user.
KEEP_COLUMNS = (
    "name",
    "label",
    "description",
    "table_name",
    "nda_or_nbdc_table",
    "domain",
    "nbdc_domain",
    "sub_domain",
    "nbdc_sub_domain",
    "source",
    "metric",
    "atlas",
    "type_data",
    "type_level",
    "unit",
    "level_range",
)

# The dictionary's column naming differs between the NBDCtools bundle and a
# portal/CSV export (`table_name` vs `nda_or_nbdc_table`, `domain` vs
# `nbdc_domain`). Output always uses the right-hand canonical name, filled from
# whichever column the loaded snapshot actually has, so downstream consumers get
# one stable shape regardless of where the dictionary came from.
FIELD_ALIASES = {
    "nda_or_nbdc_table": ("nda_or_nbdc_table", "table_name"),
    "nbdc_domain": ("nbdc_domain", "domain"),
    "nbdc_sub_domain": ("nbdc_sub_domain", "sub_domain"),
}


def canonical_field(row: dict, field: str):
    """First non-empty value among `field`'s known source columns."""
    for col in FIELD_ALIASES.get(field, (field,)):
        val = row.get(col)
        if val not in (None, ""):
            return val
    return None

_WS = re.compile(r"\s+")
# ABCD/HBCD variable names are snake_case ASCII: nihtbx_flanker_uncorrected,
# smri_vol_cdk_banksstslh, ab_g_dyn__visit_type. Used to tell a plausible
# variable token from an English phrase.
VARNAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_{1,2}[a-z0-9]+)+$")


class DictionaryError(RuntimeError):
    """Raised when no real dictionary can be obtained — never fall back to guessing."""


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #

def norm_name(s: str) -> str:
    """Canonical form for variable-name comparison.

    Lowercase and strip surrounding punctuation/whitespace. Internal underscores
    are significant (`ab_g_dyn__visit_type` differs from `ab_g_dyn_visit_type`),
    so they are NOT collapsed.
    """
    return (s or "").strip().strip("`'\"“”‘’.,;:()[]{}").lower()


def norm_text(s: str) -> str:
    """Loose form for label/description comparison: lowercase, collapse spaces."""
    return _WS.sub(" ", (s or "").strip().lower())


def looks_like_variable_name(s: str) -> bool:
    """True if `s` has the shape of an ABCD/HBCD variable name."""
    return bool(VARNAME_RE.match(norm_name(s)))


# --------------------------------------------------------------------------- #
# snapshot building
# --------------------------------------------------------------------------- #

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _rows_from_nbdctools(study: str, release: str, *, rds_path: Optional[Path],
                         progress: bool) -> Tuple[List[dict], dict]:
    """Load `lst_dds` via nbdctools and return (rows, provenance) for one release."""
    try:
        from nbdctools import download_metadata, load_metadata  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on user's env
        raise DictionaryError(
            "nbdctools is not installed, so the ABCD/HBCD dictionary cannot be "
            "fetched. Either `pip install nbdctools`, or pass --from-csv with a "
            "dictionary you exported yourself (e.g. in R: "
            "write.csv(NBDCtools::get_dd('abcd', '6.1'), 'dd.csv')). "
            "This step is not optional: variable claims are verified against a "
            "real dictionary or reported as unverified."
        ) from exc

    if rds_path is not None:
        path = Path(rds_path)
        if not path.is_file():
            raise DictionaryError(f"--from-rds {path} does not exist")
        source = str(path)
    else:
        try:
            path = Path(download_metadata(type="dds", progress=progress))
        except Exception as exc:
            raise DictionaryError(
                f"Could not download the dictionary bundle ({exc}). "
                f"Fetch {LST_DDS_URL} in a browser and re-run with "
                "--from-rds <downloaded lst_dds.rds>, or use --from-csv."
            ) from exc
        source = LST_DDS_URL

    dds = load_metadata(path, progress=progress)
    if not isinstance(dds, dict) or study not in dds:
        available = ", ".join(sorted(dds)) if isinstance(dds, dict) else "?"
        raise DictionaryError(f"study {study!r} not in the bundle (have: {available})")
    by_release = dds[study]
    releases = sorted(by_release, key=_release_sort_key)
    if release in ("latest", ""):
        release = releases[-1]
    if release not in by_release:
        raise DictionaryError(
            f"release {release!r} not in the bundle for {study!r} "
            f"(have: {', '.join(releases)})"
        )

    rows = _to_rows(by_release[release])
    prov = {
        "source": source,
        "source_sha256": _sha256_file(path) if path.is_file() else None,
        "nbdctools_data_releases_available": releases,
    }
    return rows, {**prov, "resolved_release": release}


def _release_sort_key(rel: str) -> tuple:
    """Sort '5.1' < '6.0' < '6.1' numerically, tolerating odd labels."""
    parts = re.findall(r"\d+", str(rel))
    return (tuple(int(p) for p in parts) if parts else (0,), str(rel))


def _to_rows(dd: Any) -> List[dict]:
    """Coerce a polars/pandas/dict dictionary table into a list of plain dicts."""
    if hasattr(dd, "to_dicts"):          # polars
        rows = dd.to_dicts()
    elif hasattr(dd, "to_dict"):         # pandas
        rows = dd.to_dict(orient="records")
    elif isinstance(dd, dict):           # column-oriented dict
        keys = list(dd)
        n = len(dd[keys[0]]) if keys else 0
        rows = [{k: dd[k][i] for k in keys} for i in range(n)]
    elif isinstance(dd, list):
        rows = list(dd)
    else:
        raise DictionaryError(f"unrecognised dictionary object: {type(dd).__name__}")
    return [_keep(r) for r in rows if (r.get("name") or "").strip()]


def _keep(row: dict) -> dict:
    out = {}
    for k in KEEP_COLUMNS:
        v = row.get(k)
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out


def _rows_from_csv(path: Path) -> Tuple[List[dict], dict]:
    import csv

    with path.open(newline="", encoding="utf-8") as fh:
        rows = [_keep(r) for r in csv.DictReader(fh) if (r.get("name") or "").strip()]
    if not rows:
        raise DictionaryError(
            f"{path} produced no rows with a non-empty `name` column. The export "
            "must have at least `name`; `label`/`description` make label matching "
            "possible."
        )
    return rows, {"source": str(path), "source_sha256": _sha256_file(path),
                  "nbdctools_data_releases_available": None}


def build_snapshot(study: str, release: str, *, out_dir: Path = DEFAULT_DIR,
                   from_csv: Optional[Path] = None, from_rds: Optional[Path] = None,
                   progress: bool = False) -> Path:
    """Write one study+release snapshot and return its path."""
    study = study.lower().strip()
    if study not in ("abcd", "hbcd"):
        raise DictionaryError(f"study must be 'abcd' or 'hbcd', got {study!r}")

    if from_csv is not None:
        rows, prov = _rows_from_csv(Path(from_csv))
        resolved = release
    else:
        rows, prov = _rows_from_nbdctools(study, release, rds_path=from_rds,
                                          progress=progress)
        resolved = prov.pop("resolved_release")

    snapshot = {
        "study": study,
        "release": resolved,
        "variable_count": len(rows),
        "columns": sorted({k for r in rows for k in r}),
        "provenance": {
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": "csv_export" if from_csv else "nbdctools",
            "tool_version": _nbdctools_version(),
            **prov,
        },
        "variables": rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"dd-{study}-{resolved}.json"
    out.write_text(json.dumps(snapshot, indent=1, sort_keys=False))
    return out


def _nbdctools_version() -> Optional[str]:
    try:
        import nbdctools  # type: ignore

        return getattr(nbdctools, "__version__", None)
    except Exception:
        return None


def available_releases(study: str, *, progress: bool = False) -> List[str]:
    """Every release the bundle offers for `study` (needs nbdctools)."""
    _, prov = _rows_from_nbdctools(study, "latest", rds_path=None, progress=progress)
    return prov.get("nbdctools_data_releases_available") or []


# --------------------------------------------------------------------------- #
# snapshot loading + resolution
# --------------------------------------------------------------------------- #

@dataclass
class Match:
    """One dictionary hit for a string found in a paper."""

    name: str
    study: str
    release: str
    method: str                     # exact_name | normalized_name | label | description
    score: float                    # 1.0 for exact, lower for text matches
    row: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "variable": self.name,
            "study": self.study,
            "dd_release": self.release,
            "match_method": self.method,
            "match_score": round(self.score, 3),
            "label": self.row.get("label"),
            # Canonical names, resolved through FIELD_ALIASES so a bundle export
            # and a portal CSV produce the same keys.
            "nda_or_nbdc_table": canonical_field(self.row, "nda_or_nbdc_table"),
            "nbdc_domain": canonical_field(self.row, "nbdc_domain"),
            "nbdc_sub_domain": canonical_field(self.row, "nbdc_sub_domain"),
        }


class Dictionary:
    """One or more release snapshots, queried together.

    Holding several releases at once is deliberate: papers cite variables from
    whatever release they analysed, and names change between releases. Resolving
    across all loaded releases lets us say "present in 5.1 and 6.0, absent in
    6.1" instead of a bare miss.
    """

    def __init__(self, snapshots: List[dict]):
        if not snapshots:
            raise DictionaryError(
                "No dictionary snapshots loaded. Run: python -m "
                "scripts.abcd_dictionary build --study abcd --release latest"
            )
        self.snapshots = snapshots
        self._by_name: Dict[str, List[Match]] = {}
        self._by_label: Dict[str, List[Match]] = {}
        self._rows: List[Tuple[dict, str, str]] = []
        for snap in snapshots:
            study, rel = snap["study"], snap["release"]
            for row in snap["variables"]:
                name = str(row.get("name", ""))
                if not name:
                    continue
                self._rows.append((row, study, rel))
                self._by_name.setdefault(norm_name(name), []).append(
                    Match(name, study, rel, "exact_name", 1.0, row)
                )
                for col in ("label", "description"):
                    txt = norm_text(str(row.get(col) or ""))
                    if len(txt) >= 8:
                        self._by_label.setdefault(txt, []).append(
                            Match(name, study, rel, col, 0.9, row)
                        )

    # -- construction ------------------------------------------------------- #

    @classmethod
    def load(cls, *, study: Optional[str] = None, releases: Optional[Iterable[str]] = None,
             dir_: Path = DEFAULT_DIR) -> "Dictionary":
        snaps = load_all_snapshots(dir_=dir_)
        if study:
            snaps = [s for s in snaps if s["study"] == study.lower()]
        if releases:
            want = {str(r) for r in releases}
            snaps = [s for s in snaps if s["release"] in want]
        return cls(snaps)

    @property
    def provenance(self) -> List[dict]:
        """What every consulted snapshot was — goes into the run's provenance."""
        return [
            {
                "study": s["study"],
                "dd_release": s["release"],
                "variable_count": s["variable_count"],
                **{k: v for k, v in s.get("provenance", {}).items()
                   if k in ("retrieved_at", "method", "source", "source_sha256",
                            "tool_version")},
            }
            for s in self.snapshots
        ]

    # -- queries ------------------------------------------------------------ #

    def resolve(self, candidate: str, *, allow_label_match: bool = True) -> List[Match]:
        """All dictionary hits for `candidate`, best first, one per (name, release).

        Exact name match is tried first. Label/description matching is a fallback
        for papers that name a measure in prose ("NIH Toolbox Flanker score")
        rather than by variable id, and requires the FULL label to match — no
        substring or fuzzy scoring, because a partial label match is not evidence
        that a specific variable was used.
        """
        key = norm_name(candidate)
        hits = list(self._by_name.get(key, []))

        if not hits and "__" in key:
            # Some papers write single underscores where the dictionary uses
            # double (table__column). Try the double-underscore reading too.
            hits = list(self._by_name.get(key.replace("__", "_"), []))
        if not hits and "_" in key:
            for alt, matches in self._by_name.items():
                if alt.replace("__", "_") == key.replace("__", "_"):
                    hits.extend(m for m in matches)

        if hits:
            for h in hits:
                if h.method == "exact_name" and norm_name(h.name) != key:
                    h.method, h.score = "normalized_name", 0.95
            return _dedupe(hits)

        if allow_label_match:
            lab = norm_text(candidate)
            if len(lab) >= 8:
                return _dedupe(self._by_label.get(lab, []))
        return []

    def releases_for(self, name: str) -> List[str]:
        """Releases whose dictionary contains `name` — the rename detector."""
        return sorted(
            {m.release for m in self._by_name.get(norm_name(name), [])},
            key=_release_sort_key,
        )

    def search(self, needle: str, limit: int = 20) -> List[Match]:
        """Substring search over names and labels. For humans exploring, NOT for
        verification — `resolve()` is what decides whether a claim holds."""
        n = norm_text(needle)
        out: List[Match] = []
        for row, study, rel in self._rows:
            hay = " ".join(
                str(row.get(c) or "") for c in ("name", "label", "description")
            ).lower()
            if n in hay:
                out.append(Match(str(row["name"]), study, rel, "search", 0.5, row))
                if len(out) >= limit:
                    break
        return out


def _dedupe(matches: List[Match]) -> List[Match]:
    best: Dict[Tuple[str, str, str], Match] = {}
    for m in matches:
        k = (m.study, m.release, norm_name(m.name))
        if k not in best or m.score > best[k].score:
            best[k] = m
    return sorted(best.values(), key=lambda m: (-m.score, _release_sort_key(m.release)))


def load_snapshot(path: Path) -> dict:
    snap = json.loads(Path(path).read_text())
    for key in ("study", "release", "variables"):
        if key not in snap:
            raise DictionaryError(f"{path} is not a dictionary snapshot (missing {key!r})")
    return snap


def load_all_snapshots(*, dir_: Path = DEFAULT_DIR) -> List[dict]:
    if not dir_.is_dir():
        return []
    return [load_snapshot(p) for p in sorted(dir_.glob("dd-*.json"))]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="materialise a dictionary snapshot")
    b.add_argument("--study", default="abcd", choices=["abcd", "hbcd"])
    b.add_argument("--release", default="latest",
                   help="release id (e.g. 6.1) or 'latest'")
    b.add_argument("--all-releases", action="store_true",
                   help="snapshot every release the bundle offers")
    b.add_argument("--from-csv", type=Path, help="build from your own CSV export")
    b.add_argument("--from-rds", type=Path, help="use an already-downloaded lst_dds.rds")
    b.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    b.add_argument("--progress", action="store_true")

    i = sub.add_parser("info", help="list local snapshots")
    i.add_argument("--dir", type=Path, default=DEFAULT_DIR)

    l = sub.add_parser("lookup", help="resolve a variable name / label")
    l.add_argument("candidate")
    l.add_argument("--study")
    l.add_argument("--dir", type=Path, default=DEFAULT_DIR)

    s = sub.add_parser("search", help="substring search (exploration only)")
    s.add_argument("needle")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--dir", type=Path, default=DEFAULT_DIR)

    a = ap.parse_args(argv)

    try:
        if a.cmd == "build":
            targets = [a.release]
            if a.all_releases:
                targets = available_releases(a.study, progress=a.progress)
                print(f"releases: {', '.join(targets)}", file=sys.stderr)
            for rel in targets:
                out = build_snapshot(a.study, rel, out_dir=a.dir, from_csv=a.from_csv,
                                     from_rds=a.from_rds, progress=a.progress)
                snap = load_snapshot(out)
                print(f"wrote {out}  ({snap['variable_count']} variables)")
            return 0

        if a.cmd == "info":
            snaps = load_all_snapshots(dir_=a.dir)
            if not snaps:
                print(f"no snapshots in {a.dir}", file=sys.stderr)
                return 1
            for s_ in snaps:
                p = s_.get("provenance", {})
                print(f"{s_['study']:5} {s_['release']:6} {s_['variable_count']:>7} vars"
                      f"  via {p.get('method')}  {p.get('retrieved_at')}")
            return 0

        d = Dictionary.load(study=getattr(a, "study", None), dir_=a.dir)
        if a.cmd == "lookup":
            hits = d.resolve(a.candidate)
            if not hits:
                print(f"UNVERIFIED: {a.candidate!r} is not in any loaded dictionary")
                return 2
            for m in hits:
                print(json.dumps(m.to_dict()))
            rel = d.releases_for(hits[0].name)
            if rel:
                print(f"present in releases: {', '.join(rel)}", file=sys.stderr)
            return 0

        if a.cmd == "search":
            for m in d.search(a.needle, limit=a.limit):
                print(f"{m.name:44} {str(m.row.get('label'))[:60]}")
            return 0
    except DictionaryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
