"""OLS (EBI Ontology Lookup Service) concept mapping over its MCP server.

Endpoint: https://www.ebi.ac.uk/ols4/api/mcp (Streamable HTTP, JSON-RPC 2.0; no key).
This is the `ols` source in concept_mapping.json `sources_priority`, consulted after
the trusted ontology files and the local hybrid mapper, before BioPortal.

The server's `searchClasses` tool ranks loosely (for "hippocampus" in UBERON it
returns "CA1 field of hippocampus" first) and returns labels without synonyms, so a
hit is accepted ONLY when a returned class label equals the query (case, whitespace,
hyphen and simple plural insensitive). Anything weaker stays unmapped: a mapping is a
tool decision, never a ranking guess.

Usage:
    from ols_mcp_map import OlsMcpMapper
    m = OlsMcpMapper()
    m.map_batch(["Ammon's horn", "Mus musculus"], ontologies=["UBERON", "NCBITaxon"])
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional

logger = logging.getLogger("OlsMcpMapper")

OLS_MCP_URL = "https://www.ebi.ac.uk/ols4/api/mcp"
PROTOCOL = "2025-03-26"


def _norm(s: str) -> str:
    s = re.sub(r"[\s_\-]+", " ", str(s or "").lower()).strip()
    return s[:-1] if len(s) > 3 and s.endswith("s") and not s.endswith("ss") else s


class OlsMcpError(RuntimeError):
    pass


class OlsMcpMapper:
    def __init__(self, url: str = OLS_MCP_URL, timeout: float = 30.0, page_size: int = 25,
                 request_interval: float = 0.1):
        self.url = url
        self.timeout = timeout
        self.page_size = page_size
        self.request_interval = request_interval
        self._sid: Optional[str] = None
        self._next_id = 1
        self._last = 0.0
        self._cache: dict[tuple, list[dict]] = {}

    # -- transport ---------------------------------------------------------
    def _post(self, body: dict, timeout: Optional[float] = None) -> Optional[dict]:
        wait = self.request_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._sid:
            headers["mcp-session-id"] = self._sid
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            self._sid = r.headers.get("mcp-session-id") or self._sid
            raw = r.read().decode("utf-8", "replace")
        if not raw.strip():
            return None
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():  # text/event-stream: the JSON-RPC reply is a data: line
            if line.startswith("data:"):
                return json.loads(line[5:])
        return None

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        self._next_id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
        if not msg:
            raise OlsMcpError(f"{method}: empty reply")
        if msg.get("error"):
            raise OlsMcpError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def connect(self) -> None:
        self._sid = None
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "structsense", "version": "1"}})
        try:  # a notification: the server answers 202 with no body
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
        except Exception:
            pass

    def call(self, tool: str, arguments: dict) -> object:
        for attempt in range(3):
            try:
                if self._sid is None:
                    self.connect()
                res = self._rpc("tools/call", {"name": tool, "arguments": arguments})
                text = "".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
                if res.get("isError"):
                    raise OlsMcpError(f"{tool}: {text[:200]}")
                return json.loads(text) if text.strip()[:1] in "[{" else text
            except (urllib.error.URLError, TimeoutError, OlsMcpError, json.JSONDecodeError) as e:
                logger.warning("OLS MCP %s failed (%s); retrying", tool, e)
                self._sid = None  # a stale session is the usual cause; start a new one
                time.sleep(2 ** attempt)
        raise OlsMcpError(f"{tool}: OLS MCP unreachable at {self.url}")

    def health(self) -> bool:
        try:
            self.connect()
            return True
        except Exception as e:
            logger.warning("OLS MCP not reachable at %s: %s", self.url, e)
            return False

    # -- mapping -----------------------------------------------------------
    def search(self, term: str, ontology: Optional[str]) -> list[dict]:
        key = (term, ontology)
        if key not in self._cache:
            args = {"query": term, "pageSize": self.page_size}
            if ontology:
                args["ontologyId"] = ontology.lower()
            out = self.call("searchClasses", args)
            self._cache[key] = list((out or {}).get("items") or []) if isinstance(out, dict) else []
        return self._cache[key]

    def map_one(self, term: str, ontologies: Optional[Iterable[str]] = None,
                accept: Optional[Callable[[str], bool]] = None) -> dict:
        want = _norm(term)
        for onto in list(ontologies or []) or [None]:
            for it in self.search(term, onto):
                if it.get("isObsolete"):
                    continue
                labels = it.get("label") or []
                labels = labels if isinstance(labels, list) else [labels]
                iri = it.get("iri")
                if iri and any(_norm(lab) == want for lab in labels) and (accept is None or accept(iri)):
                    curie = it.get("curie") or ""
                    return {"term": term, "ontology_id": iri, "ontology_label": labels[0],
                            "ontology": (curie.split(":")[0] if ":" in curie else (it.get("ontologyId") or "").upper()) or None,
                            "concept_mapping_provenance": "tool", "ontology_match_type": "label",
                            "match_tier": "exactMatch", "score": 1.0}
        return {"term": term, "ontology_id": None, "ontology_label": None, "ontology": None,
                "concept_mapping_provenance": "unmapped"}

    def map_batch(self, terms: Iterable[str], ontologies: Optional[Iterable[str]] = None,
                  accept: Optional[Callable[[str], bool]] = None, **_) -> list[dict]:
        """Same interface as the other remote clients (max_results is accepted and
        ignored: only an exact label match is ever returned)."""
        out = []
        for t in terms:
            try:
                out.append(self.map_one(t, ontologies, accept=accept))
            except OlsMcpError as e:
                out.append({"term": t, "concept_mapping_provenance": "unmapped", "remote_error": str(e)})
        return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    m = OlsMcpMapper()
    terms = sys.argv[1:] or ["Ammon's horn", "Mus musculus"]
    for r in m.map_batch(terms):
        print(json.dumps(r))
