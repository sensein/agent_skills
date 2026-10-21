"""
rdf_store.py — pyoxigraph-backed RDF store for SLR review results.

Wraps pyoxigraph.Store to provide load-from-result, SPARQL query,
save-to-file, and load-from-file operations.
"""
from __future__ import annotations

import io
from typing import TYPE_CHECKING, Union

try:
    import pyoxigraph
except ImportError as exc:
    raise ImportError(
        "pyoxigraph is required for SLRStore. "
        "Install with: pip install pyoxigraph>=0.3"
    ) from exc

if TYPE_CHECKING:
    from synthscholar.models import PRISMAReviewResult


class SLRStore:
    """Pyoxigraph-backed triple store for a PRISMA systematic review result.

    Usage::

        store = SLRStore()
        store.load(result)
        rows = store.query("SELECT ?s WHERE { ?s a <https://w3id.org/slr-ontology/SystematicReview> }")
        store.save("review.ttl")

    Parameters
    ----------
    path:
        If given, opens (or creates) a persistent Oxigraph database at that
        path. If None (default), the store is in-memory only.
    """

    def __init__(self, path: str | None = None) -> None:
        self.store: pyoxigraph.Store = (
            pyoxigraph.Store(path) if path else pyoxigraph.Store()
        )

    # ── T023: load from PRISMAReviewResult ────────────────────────────────

    def load(self, result: "PRISMAReviewResult") -> None:
        """Build an RDF graph from *result* and load all triples into the store.

        The round-trip goes: rdflib graph → Turtle bytes → pyoxigraph.Store.
        Raises RuntimeError if the Turtle cannot be parsed.
        """
        from .rdf_export import to_turtle

        try:
            turtle_bytes = to_turtle(result).encode("utf-8")
            self.store.load(io.BytesIO(turtle_bytes), format=pyoxigraph.RdfFormat.TURTLE)
        except Exception as exc:
            raise RuntimeError(f"Failed to load RDF into pyoxigraph store: {exc}") from exc

    # ── T024: SPARQL query ────────────────────────────────────────────────

    def query(self, sparql: str) -> Union[list[dict], bool]:
        """Run *sparql* against the store and return results as plain Python.

        SELECT queries return a list of dicts mapping variable name → value string.
        ASK queries return a bool.
        CONSTRUCT/DESCRIBE queries return a list of subject/predicate/object dicts.

        Raises ValueError with the SPARQL error message on invalid queries.
        """
        try:
            results = self.store.query(sparql)
        except Exception as exc:
            raise ValueError(f"Invalid SPARQL query: {exc}") from exc

        # ASK → bool
        if isinstance(results, bool):
            return results

        out: list[dict] = []

        # SELECT → QuerySolutions (has .variables attribute)
        if hasattr(results, "variables"):
            var_names = [v.value for v in results.variables]
            for solution in results:
                row: dict[str, str | None] = {}
                for name in var_names:
                    term = solution[name]
                    row[name] = term.value if term is not None else None
                out.append(row)
            return out

        # CONSTRUCT / DESCRIBE → QueryTriples
        for triple in results:
            out.append({
                "subject":   triple.subject.value,
                "predicate": triple.predicate.value,
                "object":    triple.object.value,
            })
        return out

    # ── T025: persistence ────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Serialize the store to a Turtle file at *path*.

        Creates or overwrites the file. Does nothing if *path* is falsy.
        """
        if not path:
            return
        with open(path, "wb") as f:
            self.store.dump(
                f,
                format=pyoxigraph.RdfFormat.TURTLE,
                from_graph=pyoxigraph.DefaultGraph(),
            )

    def load_from_file(self, path: str) -> None:
        """Load RDF triples from the Turtle file at *path* into the store.

        Additive — existing triples are not removed first.
        Does nothing if *path* is falsy.
        """
        if not path:
            return
        with open(path, "rb") as f:
            self.store.load(f, format=pyoxigraph.RdfFormat.TURTLE)

    # ── push to external SPARQL-compatible graph database ────────────────

    def to_turtle(self) -> str:
        """Serialize the default graph of this store back to Turtle text."""
        buf = io.BytesIO()
        self.store.dump(
            buf,
            format=pyoxigraph.RdfFormat.TURTLE,
            from_graph=pyoxigraph.DefaultGraph(),
        )
        return buf.getvalue().decode("utf-8")

    def push(
        self,
        endpoint: str,
        *,
        protocol: str = "gsp",
        named_graph: str | None = None,
        auth: str | None = None,
        replace: bool = False,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> int:
        """Send all triples in this store to an external graph DB.

        ``protocol`` is ``"gsp"`` (SPARQL 1.1 Graph Store Protocol) or
        ``"update"`` (SPARQL 1.1 Update INSERT DATA). ``headers`` lets you
        attach API keys or other custom auth headers. See ``rdf_push`` for
        endpoint conventions and auth formats.
        """
        from .rdf_push import push_ttl

        return push_ttl(
            self.to_turtle(),
            endpoint,
            protocol=protocol,  # type: ignore[arg-type]
            named_graph=named_graph,
            auth=auth,
            replace=replace,
            headers=headers,
            timeout=timeout,
        )

    def push_with_config(self, config) -> int:
        """Push all triples using a :class:`~rdf_push.GraphDBConfig` instance.

        Convenience wrapper for the env-var-driven programmatic API::

            from synthscholar.ontology.rdf_push import GraphDBConfig
            store.push_with_config(GraphDBConfig.from_env())
        """
        return config.push(self.to_turtle())
