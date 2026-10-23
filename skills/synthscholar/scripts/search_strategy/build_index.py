"""
Build a queryable SQLite index from the MeSH RDF N-Triples dump.

One-time, streaming, pure-stdlib parse of mesh<YEAR>.nt (or .nt.gz, read
compressed). We only keep the
predicates needed to reconstruct, per topical descriptor (D...):
  - rdfs:label          -> the MeSH heading string
  - meshv:treeNumber    -> tree node(s), e.g. A01.923.047 (used for explosion)
  - meshv:concept /
    meshv:preferredConcept  -> M... concepts
  - meshv:term /
    meshv:preferredTerm     -> T... terms (per concept)
  - meshv:prefLabel /
    meshv:altLabel          -> the actual synonym / entry-term strings (per term)

The resulting SQLite has three tables:
  descriptor(dui TEXT PRIMARY KEY, label TEXT, label_norm TEXT)
  tree(dui TEXT, tree TEXT)                       -- one row per (descriptor, tree number)
  entry_term(dui TEXT, term TEXT, term_norm TEXT) -- one row per synonym string

Explosion is done at query time by dotted-prefix matching on `tree`
(A01 explodes to A01, A01.111, A01.923.047, ...), which is exactly how MeSH
"explode" works.

`ensure()` is the automatic path: if the index is missing or broken it
downloads NLM's compressed dump (~120 MB; the 2 GB .nt is never written),
builds from it, checks the result and removes the download.

Usage:
    python build_index.py [path/to/mesh2026.nt[.gz]] [path/to/mesh.sqlite]
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE  # data/ sits next to the modules in the skill layout

MESH_YEAR = 2026
MESH_URL = f"https://nlmpubs.nlm.nih.gov/projects/mesh/rdf/{MESH_YEAR}/mesh{MESH_YEAR}.nt.gz"
DEFAULT_NT = os.path.join(ROOT, f"mesh{MESH_YEAR}.nt")
# SYNTHSCHOLAR_MESH_DB points at an existing index elsewhere (same as mesh_index.py)
DEFAULT_DB = os.environ.get("SYNTHSCHOLAR_MESH_DB") or os.path.join(ROOT, "data", "mesh.sqlite")
# A full MeSH year has ~31k descriptors; far fewer means a truncated download or
# a partial dump, and an index built from it would silently miss headings.
MIN_DESCRIPTORS = 30_000

# predicate local-names we care about
P_LABEL = "label"                 # rdfs:label
P_TREE = "treeNumber"
P_CONCEPT = "concept"
P_PREF_CONCEPT = "preferredConcept"
P_TERM = "term"
P_PREF_TERM = "preferredTerm"
P_PREFLABEL = "prefLabel"
P_ALTLABEL = "altLabel"

WANTED_PREDS = frozenset(
    {P_LABEL, P_TREE, P_CONCEPT, P_PREF_CONCEPT, P_TERM, P_PREF_TERM, P_PREFLABEL, P_ALTLABEL}
)


def norm(s: str) -> str:
    """Normalize a label for exact-ish matching: lowercase, collapse whitespace."""
    return " ".join(s.lower().split())


def _localname(uri: str) -> str:
    # mesh URIs use '/', vocab predicates use '#'
    if "#" in uri:
        return uri.rsplit("#", 1)[-1]
    return uri.rsplit("/", 1)[-1]


def _parse_line(line: str):
    """Return (subject_local, pred_local, object_value, object_is_uri, lang) or None.

    N-Triples line form:  <s> <p> <o> .   or   <s> <p> "lit"@en .
    Subject and predicate are always URIs. `lang` is the literal's language tag
    ("" when absent, and always "" for URI objects) — it matters because the dump
    carries a few non-English rdfs:labels, and a non-English heading in the query
    is a tag PubMed can never match.
    """
    if not line or line[0] != "<":
        return None
    # subject
    i = line.find(">", 1)
    if i == -1:
        return None
    subj = line[1:i]
    # predicate begins at i+2 ('<')
    if line[i + 2 : i + 3] != "<":
        return None
    j = line.find(">", i + 3)
    if j == -1:
        return None
    pred = line[i + 3 : j]
    pred_l = _localname(pred)
    if pred_l not in WANTED_PREDS:
        return None
    # object begins after '> '
    o = line[j + 2 :].rstrip()
    if o.endswith(" ."):
        o = o[:-2].rstrip()
    if not o:
        return None
    if o[0] == "<":
        obj = o[1 : o.rfind(">")]
        return _localname(subj), pred_l, _localname(obj), True, ""
    if o[0] == '"':
        end = o.rfind('"')
        if end <= 0:
            return None
        val = o[1:end]
        tail = o[end + 1 :]
        lang = tail[1:].split("-", 1)[0].lower() if tail.startswith("@") else ""
        # unescape the few things N-Triples escapes
        if "\\" in val:
            val = val.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")
        return _localname(subj), pred_l, val, False, lang
    return None


def build(nt_path: str, db_path: str) -> None:
    if not os.path.exists(nt_path):
        sys.exit(f"MeSH N-Triples file not found: {nt_path}")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)   # a bare filename is cwd

    t0 = time.time()
    desc_label: dict[str, str] = {}
    desc_trees: dict[str, list[str]] = {}
    desc_concepts: dict[str, list[str]] = {}
    concept_terms: dict[str, list[str]] = {}
    term_labels: dict[str, list[str]] = {}
    skipped_labels: list[tuple[str, str, str]] = []

    n = 0
    opener = gzip.open if nt_path.endswith(".gz") else open
    with opener(nt_path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n += 1
            if n % 5_000_000 == 0:
                print(f"  ...{n:,} lines  ({time.time() - t0:.0f}s)", flush=True)
            parsed = _parse_line(line)
            if parsed is None:
                continue
            subj, pred, obj, is_uri, lang = parsed
            c0 = subj[0]
            if c0 == "D":  # topical/other descriptor
                if pred == P_LABEL and not is_uri:
                    # English (or untagged) only: the dump has a stray "@nl" label
                    # (D002493 "Ziekte, centraalzenuwstelsel-") which, being
                    # last-write-wins, replaced "Central Nervous System Diseases"
                    # and produced a MeSH tag PubMed cannot match.
                    if lang in ("", "en"):
                        desc_label[subj] = obj
                    else:
                        skipped_labels.append((subj, lang, obj))
                elif pred == P_TREE and is_uri:
                    desc_trees.setdefault(subj, []).append(obj)
                elif pred in (P_CONCEPT, P_PREF_CONCEPT) and is_uri:
                    desc_concepts.setdefault(subj, []).append(obj)
            elif c0 == "M":  # concept
                if pred in (P_TERM, P_PREF_TERM) and is_uri:
                    concept_terms.setdefault(subj, []).append(obj)
            elif c0 == "T":  # term
                if pred in (P_PREFLABEL, P_ALTLABEL) and not is_uri:
                    term_labels.setdefault(subj, []).append(obj)

    print(
        f"  parsed {n:,} lines: {len(desc_label):,} descriptors, "
        f"{len(concept_terms):,} concepts, {len(term_labels):,} terms "
        f"({time.time() - t0:.0f}s)",
        flush=True,
    )
    for dui, lang, val in skipped_labels:
        print(f"  skipped non-English label: {dui} @{lang} {val!r}", flush=True)

    # Build into a temp file and swap: an interrupted in-place rebuild once left a
    # 0-byte mesh.sqlite behind, which an existence check alone will not repair.
    tmp_path = db_path + ".building"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    desc_rows = []
    tree_rows = []
    et_rows = []
    for dui, label in desc_label.items():
        # Real descriptors always have a (preferred) concept. This filters out
        # category-D tree-number nodes (e.g. "D02.455"), which are also
        # D-prefixed and carry an rdfs:label but no concept.
        if dui not in desc_concepts:
            continue
        desc_rows.append((dui, label, norm(label)))
        for tn in desc_trees.get(dui, ()):  # tree number local names
            tree_rows.append((dui, tn))
        terms: set[str] = {label}
        for m in desc_concepts.get(dui, ()):  # concepts
            for t in concept_terms.get(m, ()):  # terms
                for lab in term_labels.get(t, ()):  # synonym strings
                    terms.add(lab)
        for term in terms:
            et_rows.append((dui, term, norm(term)))

    con = sqlite3.connect(tmp_path)
    try:
        con.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE descriptor (dui TEXT PRIMARY KEY, label TEXT, label_norm TEXT);
            CREATE TABLE tree (dui TEXT, tree TEXT);
            CREATE TABLE entry_term (dui TEXT, term TEXT, term_norm TEXT);
            """
        )
        con.executemany("INSERT OR REPLACE INTO descriptor VALUES (?,?,?)", desc_rows)
        con.executemany("INSERT INTO tree VALUES (?,?)", tree_rows)
        con.executemany("INSERT INTO entry_term VALUES (?,?,?)", et_rows)
        con.executescript(
            """
            CREATE INDEX idx_desc_norm  ON descriptor(label_norm);
            CREATE INDEX idx_tree_tree  ON tree(tree);
            CREATE INDEX idx_tree_dui   ON tree(dui);
            CREATE INDEX idx_et_dui     ON entry_term(dui);
            CREATE INDEX idx_et_norm    ON entry_term(term_norm);
            """
        )
        con.commit()
        con.execute("VACUUM")
        con.commit()
    except BaseException:
        try:
            con.close()
        finally:                         # never leave a half-built index behind
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        raise
    con.close()
    os.replace(tmp_path, db_path)
    print(
        f"  wrote {len(desc_rows):,} descriptors, {len(tree_rows):,} tree links, "
        f"{len(et_rows):,} entry terms -> {db_path} ({time.time() - t0:.0f}s total)",
        flush=True,
    )


def index_descriptors(db_path: str = DEFAULT_DB) -> int:
    """Descriptor count of a usable index at db_path, or 0 if there is none
    (absent, empty, or missing a table -- e.g. an interrupted build)."""
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return 0
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"descriptor", "tree", "entry_term"} <= tables:
                return 0
            return con.execute("SELECT count(*) FROM descriptor").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def ensure(db_path: str = DEFAULT_DB, url: str = MESH_URL) -> bool:
    """Make sure a usable index exists at db_path; download and build it if not.

    Returns True if it was built now, False if one was already there. Raises
    RuntimeError (leaving nothing half-built behind) if the download fails or
    the result is too small to be a full MeSH year.
    """
    if index_descriptors(db_path) >= MIN_DESCRIPTORS:
        return False
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    gz_path = db_path + ".download.nt.gz"
    try:
        print(f"Downloading MeSH {MESH_YEAR} from {url} (one-time, ~120 MB)", flush=True)
        t0 = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "synthscholar-search-strategy"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(gz_path, "wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
        print(f"  downloaded {os.path.getsize(gz_path) / 1e6:.0f} MB "
              f"({time.time() - t0:.0f}s); building the index", flush=True)
        build(gz_path, db_path)
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        raise RuntimeError(f"could not download or read the MeSH dump from {url}: {e}") from e
    except sqlite3.Error as e:               # e.g. a full disk while writing the index
        raise RuntimeError(f"could not write the MeSH index at {db_path}: {e}") from e
    finally:
        if os.path.exists(gz_path):
            os.remove(gz_path)
    n = index_descriptors(db_path)
    if n < MIN_DESCRIPTORS:
        os.remove(db_path)
        raise RuntimeError(f"MeSH index from {url} has only {n:,} descriptors "
                           f"(expected >= {MIN_DESCRIPTORS:,}); removed it")
    return True


if __name__ == "__main__":
    nt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_NT
    db = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB
    print(f"Building MeSH index from {nt}")
    build(nt, db)
