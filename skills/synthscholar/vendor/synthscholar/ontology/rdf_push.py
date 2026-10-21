"""Push RDF triples to an external SPARQL-compatible graph database.

Two protocols are supported:

- ``gsp``   — SPARQL 1.1 Graph Store HTTP Protocol. Works with native
              Oxigraph (``/store``), Apache Jena Fuseki (``/<dataset>/data``),
              GraphDB (``/repositories/<repo>/rdf-graphs/service``).
- ``update`` — SPARQL 1.1 Update via ``INSERT DATA``. Works with anything
               that exposes a SPARQL Update endpoint (Stardog, Virtuoso,
               Blazegraph, Fuseki ``/update``, Oxigraph ``/update``).

Authentication options (combine as needed):

- ``auth="user:pass"``  — HTTP Basic
- ``auth="Bearer <token>"`` — preformatted Authorization header
- ``headers={"X-API-Key": "..."}`` — arbitrary custom headers (API keys,
  CSRF tokens, repository tokens, etc.)

Programmatic API (for embedding from code, not just CLI)
--------------------------------------------------------

The simplest path is :class:`GraphDBConfig`, which loads its values from
environment variables and exposes a one-shot ``push(ttl)``::

    from synthscholar.ontology.rdf_push import GraphDBConfig

    cfg = GraphDBConfig.from_env()      # reads PRISMA_GRAPH_DB_* env vars
    cfg.push(ttl_string)                 # POST/PUT to the configured graph DB

For ad-hoc construction without env vars::

    cfg = GraphDBConfig(
        host="http://localhost:7878",
        user="admin", password="...",
        named_graph="https://your.org/reviews/run-001",
    )
    cfg.push(ttl_string)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Mapping
from urllib.parse import quote

import httpx


Protocol = Literal["gsp", "update"]

# Default env-var prefix; matches the existing CLI behaviour.
ENV_PREFIX = "PRISMA_GRAPH_DB_"


def _auth_kwargs(auth: str | None) -> dict:
    """Translate a CLI auth string into httpx kwargs."""
    if not auth:
        return {}
    if auth.lower().startswith(("bearer ", "token ")):
        return {"headers": {"Authorization": auth}}
    if ":" in auth:
        user, _, password = auth.partition(":")
        return {"auth": (user, password)}
    return {"headers": {"Authorization": auth}}


def _raise_with_body(resp: httpx.Response) -> None:
    """Raise ``HTTPStatusError`` with the response body included in the message.

    Auth failures (401/403) and quota errors often include a helpful message
    in the body that ``raise_for_status()`` would otherwise drop.
    """
    if resp.is_success:
        return
    body = resp.text.strip()
    snippet = (body[:500] + "…") if len(body) > 500 else body
    raise httpx.HTTPStatusError(
        f"{resp.status_code} {resp.reason_phrase} from {resp.request.url}\n"
        f"Response body: {snippet or '<empty>'}",
        request=resp.request,
        response=resp,
    )


def push_via_graph_store_protocol(
    ttl: str,
    endpoint: str,
    named_graph: str | None = None,
    auth: str | None = None,
    *,
    replace: bool = False,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> int:
    """POST (or PUT, when ``replace=True``) Turtle to a GSP endpoint.

    ``endpoint`` is the GSP base, e.g. ``http://localhost:7878/store``.
    Returns the HTTP status code on success; raises ``httpx.HTTPStatusError``
    with the response body on 4xx/5xx so auth failures are actionable.
    """
    if named_graph:
        url = f"{endpoint}?graph={quote(named_graph, safe='')}"
    else:
        url = f"{endpoint}?default"

    method = "PUT" if replace else "POST"
    kwargs = _auth_kwargs(auth)
    merged_headers = kwargs.setdefault("headers", {})
    merged_headers["Content-Type"] = "text/turtle; charset=utf-8"
    if headers:
        merged_headers.update(headers)

    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, content=ttl.encode("utf-8"), **kwargs)
        _raise_with_body(resp)
        return resp.status_code


def push_via_sparql_update(
    ttl: str,
    endpoint: str,
    named_graph: str | None = None,
    auth: str | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> int:
    """Send ``INSERT DATA { [GRAPH <uri>] { triples } }`` to a SPARQL Update endpoint.

    Triples are converted from Turtle to N-Triples (via rdflib) so they can
    be inlined into the update string without prefix/escape ambiguity.
    """
    from rdflib import Graph

    g = Graph()
    g.parse(data=ttl, format="turtle")
    nt = g.serialize(format="nt").strip()

    if named_graph:
        update = f"INSERT DATA {{ GRAPH <{named_graph}> {{\n{nt}\n}} }}"
    else:
        update = f"INSERT DATA {{\n{nt}\n}}"

    kwargs = _auth_kwargs(auth)
    merged_headers = kwargs.setdefault("headers", {})
    merged_headers["Content-Type"] = "application/sparql-update; charset=utf-8"
    if headers:
        merged_headers.update(headers)

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(endpoint, content=update.encode("utf-8"), **kwargs)
        _raise_with_body(resp)
        return resp.status_code


def push_ttl(
    ttl: str,
    endpoint: str,
    *,
    protocol: Protocol = "gsp",
    named_graph: str | None = None,
    auth: str | None = None,
    replace: bool = False,
    headers: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> int:
    """Dispatch to the requested protocol. Returns the HTTP status code."""
    if protocol == "gsp":
        return push_via_graph_store_protocol(
            ttl, endpoint, named_graph, auth,
            replace=replace, headers=headers, timeout=timeout,
        )
    if protocol == "update":
        return push_via_sparql_update(
            ttl, endpoint, named_graph, auth,
            headers=headers, timeout=timeout,
        )
    raise ValueError(f"Unknown protocol {protocol!r}; expected 'gsp' or 'update'")


# ── Programmatic configuration (env-var aware) ────────────────────────────────


def _env(name: str, prefix: str = ENV_PREFIX) -> str | None:
    """Read ``{prefix}{name}`` from the environment, returning None if unset/blank."""
    val = os.environ.get(prefix + name, "").strip()
    return val or None


def _parse_headers_blob(blob: str | None) -> dict[str, str]:
    """Parse a semicolon-separated ``KEY:VALUE;KEY2:VALUE2`` blob into a dict."""
    out: dict[str, str] = {}
    if not blob:
        return out
    for spec in blob.split(";"):
        spec = spec.strip()
        if not spec or ":" not in spec:
            continue
        key, _, value = spec.partition(":")
        out[key.strip()] = value.strip()
    return out


def _parse_bool(val: str | None) -> bool:
    """Parse a string env-var value as a boolean."""
    if not val:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class GraphDBConfig:
    """Programmatic configuration for pushing RDF to an external graph DB.

    Use :meth:`from_env` to load settings from environment variables, or
    construct directly. ``push(ttl)`` runs the actual HTTP push and returns
    the response status code.

    Resolution rules (in priority order):

    - ``endpoint`` (full URL) > ``host`` + ``path``
    - ``auth`` (full string) > ``user`` + ``password`` (assembled as Basic)

    Field-level env vars (read by :meth:`from_env`):

    ============================  ===========================================
    Env var                       Maps to
    ============================  ===========================================
    ``PRISMA_GRAPH_DB_HOST``      ``host`` — base URL, e.g. ``http://localhost:7878``
    ``PRISMA_GRAPH_DB_PATH``     ``path`` — appended to host (default ``/store``)
    ``PRISMA_GRAPH_DB_ENDPOINT`` ``endpoint`` — full URL, overrides host+path
    ``PRISMA_GRAPH_DB_USER``     ``user`` — Basic-auth username
    ``PRISMA_GRAPH_DB_PASSWORD`` ``password`` — Basic-auth password
    ``PRISMA_GRAPH_DB_AUTH``     ``auth`` — full ``user:pass`` or ``Bearer …`` (overrides user/password)
    ``PRISMA_GRAPH_DB_NAMED_GRAPH`` ``named_graph``
    ``PRISMA_GRAPH_DB_PROTOCOL`` ``protocol`` — ``gsp`` or ``update``
    ``PRISMA_GRAPH_DB_REPLACE``  ``replace`` — ``true``/``false``
    ``PRISMA_GRAPH_DB_HEADERS``  ``headers`` — ``KEY:VAL;KEY2:VAL2``
    ``PRISMA_GRAPH_DB_TIMEOUT``  ``timeout`` — seconds (float)
    ============================  ===========================================
    """

    host: str | None = None
    path: str = "/store"
    endpoint: str | None = None
    user: str | None = None
    password: str | None = None
    auth: str | None = None
    named_graph: str | None = None
    protocol: Protocol = "gsp"
    replace: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0

    @classmethod
    def from_env(cls, prefix: str = ENV_PREFIX) -> "GraphDBConfig":
        """Build a config from environment variables (see class docstring)."""
        timeout_env = _env("TIMEOUT", prefix)
        protocol_env = (_env("PROTOCOL", prefix) or "gsp").lower()
        if protocol_env not in ("gsp", "update"):
            raise ValueError(
                f"{prefix}PROTOCOL must be 'gsp' or 'update', got {protocol_env!r}"
            )

        return cls(
            host=_env("HOST", prefix),
            path=_env("PATH", prefix) or "/store",
            endpoint=_env("ENDPOINT", prefix),
            user=_env("USER", prefix),
            password=_env("PASSWORD", prefix),
            auth=_env("AUTH", prefix),
            named_graph=_env("NAMED_GRAPH", prefix),
            protocol=protocol_env,  # type: ignore[arg-type]
            replace=_parse_bool(_env("REPLACE", prefix)),
            headers=_parse_headers_blob(_env("HEADERS", prefix)),
            timeout=float(timeout_env) if timeout_env else 60.0,
        )

    @property
    def resolved_endpoint(self) -> str:
        """Concrete endpoint URL, combining ``host``+``path`` if needed."""
        if self.endpoint:
            return self.endpoint
        if not self.host:
            raise ValueError(
                "GraphDBConfig has no endpoint or host set. "
                "Set 'endpoint' (full URL) or 'host' (+ optional 'path')."
            )
        host = self.host.rstrip("/")
        path = self.path if self.path.startswith("/") else "/" + self.path
        return host + path

    @property
    def resolved_auth(self) -> str | None:
        """Concrete auth string, combining ``user``+``password`` if needed."""
        if self.auth:
            return self.auth
        if self.user and self.password is not None:
            return f"{self.user}:{self.password}"
        return None

    def is_configured(self) -> bool:
        """True if enough fields are set to attempt a push."""
        return bool(self.endpoint or self.host)

    def push(self, ttl: str) -> int:
        """Push ``ttl`` to the configured graph DB. Returns the HTTP status code.

        Raises ``ValueError`` if no endpoint/host is configured, or
        ``httpx.HTTPStatusError`` (with the response body in the message) on
        4xx/5xx responses.
        """
        if not self.is_configured():
            raise ValueError(
                "GraphDBConfig is not configured: set host/endpoint either "
                "directly or via PRISMA_GRAPH_DB_HOST / PRISMA_GRAPH_DB_ENDPOINT."
            )
        return push_ttl(
            ttl,
            self.resolved_endpoint,
            protocol=self.protocol,
            named_graph=self.named_graph,
            auth=self.resolved_auth,
            replace=self.replace,
            headers=self.headers or None,
            timeout=self.timeout,
        )
