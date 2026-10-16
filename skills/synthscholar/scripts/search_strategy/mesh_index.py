"""
Query layer over the MeSH SQLite index built by build_index.py.

Everything here is deterministic: given the same index and inputs it always
returns the same descriptors / entry terms in a stable, sorted order.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE  # data/ sits next to the modules in the skill layout
# SYNTHSCHOLAR_MESH_DB points at an existing index elsewhere (same as build_index.py)
DEFAULT_DB = os.environ.get("SYNTHSCHOLAR_MESH_DB") or os.path.join(ROOT, "data", "mesh.sqlite")


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


@dataclass
class Descriptor:
    dui: str
    label: str
    trees: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"dui": self.dui, "label": self.label, "trees": self.trees}


class MeshIndex:
    def __init__(self, db_path: str = DEFAULT_DB):
        if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
            raise FileNotFoundError(
                f"MeSH index missing or empty at {db_path} "
                f"(size={os.path.getsize(db_path) if os.path.exists(db_path) else 'absent'}). "
                f"Rebuild it: python scripts/generate_search_strategy.py --ensure-index"
            )
        self.db_path = db_path
        # sanity: the file must actually contain the schema (a truncated/partial
        # build would otherwise fail later with a cryptic 'no such table').
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            got = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        missing = {"descriptor", "tree", "entry_term"} - got
        if missing:
            raise FileNotFoundError(
                f"MeSH index at {db_path} is incomplete (missing tables: "
                f"{', '.join(sorted(missing))}). Rebuild it: python scripts/generate_search_strategy.py --ensure-index"
            )
        # A single sqlite3 connection is NOT safe for concurrent use from
        # multiple threads (corrupts cursor state -> wrong rows or
        # InterfaceError). FastAPI and the determinism test both hit this index
        # concurrently, so give every thread its own read-only connection.
        self._local = threading.local()

    @property
    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            # READ-ONLY: the runtime must never create or truncate the index. A
            # plain connect() would silently create an empty DB if the file were
            # missing (→ confusing "no such table"); mode=ro raises instead.
            con = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
            )
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    # ---- basic lookups -------------------------------------------------

    def _trees_for(self, dui: str) -> list[str]:
        rows = self._con.execute("SELECT tree FROM tree WHERE dui = ? ORDER BY tree", (dui,))
        return [r["tree"] for r in rows]

    def trees(self, dui: str) -> list[str]:
        """Public tree numbers for a descriptor (sorted). Used for subsumption."""
        return self._trees_for(dui)

    def parents(self, dui: str) -> list[str]:
        """Immediate broader descriptors: one MeSH tree level up. Sorted."""
        out: set[str] = set()
        for tn in self.trees(dui):
            if "." not in tn:
                continue
            rows = self._con.execute(
                "SELECT DISTINCT dui FROM tree WHERE tree = ?", (tn.rsplit(".", 1)[0],)
            ).fetchall()
            out.update(r["dui"] for r in rows)
        out.discard(dui)
        return sorted(out)

    def get(self, dui: str) -> Descriptor | None:
        row = self._con.execute(
            "SELECT dui, label FROM descriptor WHERE dui = ?", (dui,)
        ).fetchone()
        if not row:
            return None
        return Descriptor(row["dui"], row["label"], self._trees_for(dui))

    def exact(self, label: str) -> list[Descriptor]:
        """Descriptors whose heading exactly matches `label` (normalized)."""
        rows = self._con.execute(
            "SELECT dui, label FROM descriptor WHERE label_norm = ? ORDER BY dui",
            (_norm(label),),
        ).fetchall()
        return [Descriptor(r["dui"], r["label"], self._trees_for(r["dui"])) for r in rows]

    def by_entry_term(self, term: str) -> list[Descriptor]:
        """Descriptors that carry `term` as a heading OR entry term (normalized)."""
        rows = self._con.execute(
            "SELECT DISTINCT dui FROM entry_term WHERE term_norm = ? ORDER BY dui",
            (_norm(term),),
        ).fetchall()
        return [d for d in (self.get(r["dui"]) for r in rows) if d]

    def like_terms(self, sql: str, params) -> list[tuple[str, str]]:
        """
        Run a caller-built LIKE query over entry_term, returning (dui, term).

        Used by candidates.phrase_candidates, which needs an AND of several LIKE
        patterns (one per word) — not expressible through the fixed lookups above.
        Read-only connection, parameterized values.
        """
        return [(r[0], r[1]) for r in self._con.execute(sql, params).fetchall()]

    def search(self, text: str, limit: int = 25) -> list[Descriptor]:
        """
        Resolve a free-text label (e.g. an LLM suggestion) to real descriptors.
        Tries, in order: exact heading -> exact entry term -> token-substring
        match on heading. Deterministic ordering.
        """
        seen: dict[str, Descriptor] = {}

        def add(ds: list[Descriptor]):
            for d in ds:
                seen.setdefault(d.dui, d)

        add(self.exact(text))
        add(self.by_entry_term(text))
        if len(seen) < limit:
            like = f"%{_norm(text)}%"
            rows = self._con.execute(
                "SELECT dui, label FROM descriptor WHERE label_norm LIKE ? "
                "ORDER BY length(label), dui LIMIT ?",
                (like, limit),
            ).fetchall()
            add([Descriptor(r["dui"], r["label"], self._trees_for(r["dui"])) for r in rows])
        return list(seen.values())[:limit]

    # ---- explosion & expansion ----------------------------------------

    def explode(self, dui: str) -> list[str]:
        """
        All descendant descriptor DUIs (including `dui` itself) reachable by
        MeSH tree explosion: any descriptor whose tree number is at or below
        one of this descriptor's tree numbers.
        """
        trees = self._trees_for(dui)
        if not trees:
            return [dui]
        found: set[str] = {dui}
        for tn in trees:
            rows = self._con.execute(
                "SELECT DISTINCT dui FROM tree WHERE tree = ? OR tree LIKE ?",
                (tn, tn + ".%"),
            ).fetchall()
            found.update(r["dui"] for r in rows)
        return sorted(found)

    def entry_terms(self, dui: str) -> list[str]:
        """All heading + synonym strings for a descriptor, sorted & de-duped."""
        rows = self._con.execute(
            "SELECT DISTINCT term FROM entry_term WHERE dui = ?", (dui,)
        ).fetchall()
        return sorted({r["term"] for r in rows}, key=lambda s: (s.lower(), s))

    def strict_terms(self, duis, explode: bool = True, max_total: int | None = None) -> list[str]:
        """
        Deterministic, COMPACT free-text term set for a concept's selected
        descriptor(s).

        Intelligent descendant handling: the query's ``"Heading"[MeSH Terms]`` tag
        already auto-explodes the whole descendant hierarchy for *indexed* articles
        (server-side, zero query length). So here we do NOT enumerate every
        descendant's synonyms — that produces thousands of terms and an unusable
        URL. Instead we take each heading's OWN lexical variants (to catch
        not-yet-indexed records), then, only if there is room under the budget, add
        descendant synonyms in stable order for *specific* headings. The union is
        capped at ``max_total`` (default env STRICT_MAX_TERMS or 60) so a broad
        heading like "Nervous System" stays compact while a narrow one still gains
        its descendants' variants. Fully deterministic (sorted, stable truncation).
        """
        if max_total is None:
            max_total = int(os.environ.get("STRICT_MAX_TERMS", "60"))
        terms: set[str] = set()
        for dui in duis:                      # 1) always: each heading's own synonyms
            terms.update(self.entry_terms(dui))
        if explode:                            # 2) fill remaining budget with descendants
            for dui in duis:
                if len(terms) >= max_total:
                    break
                for d in self.explode(dui):    # sorted DUIs → stable
                    if d == dui:
                        continue
                    if len(terms) >= max_total:
                        break
                    terms.update(self.entry_terms(d))
        return sorted(terms, key=lambda s: (s.lower(), s))[:max_total]

    def expand(self, dui: str, explode: bool = True) -> dict:
        """
        Return the full deterministic expansion of a descriptor:
          { seed, descriptors:[{dui,label}], entry_terms:[...] }
        With explode=True, includes all narrower descriptors and their synonyms.
        """
        duis = self.explode(dui) if explode else [dui]
        descriptors = []
        terms: set[str] = set()
        for d in duis:
            desc = self.get(d)
            if not desc:
                continue
            descriptors.append({"dui": desc.dui, "label": desc.label})
            terms.update(self.entry_terms(d))
        descriptors.sort(key=lambda x: x["dui"])
        return {
            "seed": dui,
            "explode": explode,
            "descriptors": descriptors,
            "entry_terms": sorted(terms, key=lambda s: (s.lower(), s)),
        }


_singleton: MeshIndex | None = None


def get_index(db_path: str = DEFAULT_DB) -> MeshIndex:
    global _singleton
    if _singleton is None:
        _singleton = MeshIndex(db_path)
    return _singleton
