"""Open-access DOI → PDF resolver, vendored into the skill.

Mirrors the DOI chain the application uses in ``synthscholar.clients``'s
``FullTextResolver`` — **Unpaywall → OpenAlex → Semantic Scholar** — so a
bring-your-own-corpus review retrieves freely-available papers the same way the
app does, and labels them with the same provenance values.

Why this exists as a separate step, ahead of institutional access:

* **It costs the user nothing.** An open-access PDF needs no library session,
  so a corpus that is largely OA can be assembled with no EZproxy setup at all.
* **It preserves entitlement.** Every article resolved here is one that doesn't
  consume a slot in ``EZPROXY_MAX_REQUESTS`` or a ``EZPROXY_DELAY`` interval,
  and doesn't spend the user's institutional access on something that was free.
* **It makes the provenance answerable.** Retrieval routes are recorded
  distinctly (``unpaywall_pdf``, ``openalex_pdf``, ``semanticscholar_pdf``,
  ``ezproxy_pdf``), so "how much of this review was open access?" has an answer
  in the export instead of everything collapsing to one route.

Configuration is environment-only, matching the app::

    export SYNTHSCHOLAR_EMAIL=you@example.com     # Unpaywall requires it (ToS)
    export SEMANTIC_SCHOLAR_API_KEY=...           # optional; lifts the 1 req/s cap

Without an email Unpaywall is skipped — that is their terms of service, not a
technical limit — and the chain falls through to OpenAlex and Semantic Scholar,
which are still useful on their own.

Nothing here is authenticated and nothing touches a proxy, so these requests
carry no credentials and are safe to make for any DOI.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Provenance values, matching synthscholar/export.py's route labels.
SOURCE_UNPAYWALL = "unpaywall_pdf"
SOURCE_OPENALEX = "openalex_pdf"
SOURCE_SEMANTIC_SCHOLAR = "semanticscholar_pdf"

OA_SOURCES = (SOURCE_UNPAYWALL, SOURCE_OPENALEX, SOURCE_SEMANTIC_SCHOLAR)

# At most this many candidate URLs are tried per provider. OA metadata often
# lists several copies of the same paper (publisher, repository, preprint); a
# handful covers the useful ones without turning one DOI into a crawl.
MAX_CANDIDATES = 3


def _clean_doi(doi: str) -> str:
    """Strip the URL forms of a DOI down to the bare identifier."""
    d = (doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:"):
        if d.lower().startswith(prefix):
            d = d[len(prefix):]
            break
    return d.strip()


@dataclass
class OAConfig:
    """Open-access resolver settings, all optional."""

    email: str = ""                 # Unpaywall's required contact
    semantic_scholar_key: str = ""  # optional, lifts the public rate limit
    timeout: int = 30
    user_agent: str = ""

    @classmethod
    def from_env(cls) -> "OAConfig":
        return cls(
            email=(os.environ.get("SYNTHSCHOLAR_EMAIL")
                   or os.environ.get("NCBI_EMAIL") or "").strip(),
            semantic_scholar_key=(os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip(),
            user_agent=(os.environ.get("SYNTHSCHOLAR_USER_AGENT") or "").strip(),
        )

    def redacted(self) -> dict:
        """Config summary safe to store in provenance.

        The email is a deliberate exception to the credentials-are-booleans
        rule elsewhere: it is not a secret, it is the polite-pool contact the
        providers' terms require, and recording it makes the requests this run
        made attributable.
        """
        return {
            "email_provided": bool(self.email),
            "unpaywall_enabled": bool(self.email),
            "semantic_scholar_key_provided": bool(self.semantic_scholar_key),
        }


class OAResolver:
    """Resolve a DOI to an open-access PDF, trying each provider in turn.

    Stateless per DOI and safe to reuse across a corpus. Failures are never
    raised — a provider that is down, rate-limited or simply has no record is a
    miss, and the next provider (and ultimately institutional access) gets its
    turn.
    """

    UNPAYWALL = "https://api.unpaywall.org/v2"
    OPENALEX = "https://api.openalex.org/works"
    SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"

    def __init__(self, config: Optional[OAConfig] = None):
        self.config = config or OAConfig.from_env()
        self.requests_made = 0
        self.pdfs_retrieved = 0
        self.by_source: dict[str, int] = {}
        self._client: Optional[httpx.Client] = None
        self._last_call: dict[str, float] = {}

    # ── availability ──

    @property
    def available(self) -> bool:
        """OpenAlex and Semantic Scholar need no configuration at all."""
        return True

    def status(self) -> str:
        """One-line summary of which OA providers this run can use."""
        providers = []
        if self.config.email:
            providers.append("Unpaywall")
        providers += ["OpenAlex", "Semantic Scholar"]
        note = "" if self.config.email else (
            " — Unpaywall skipped, set SYNTHSCHOLAR_EMAIL to include it")
        return f"Open access: {' → '.join(providers)}{note}"

    # ── internals ──

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.config.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self.config.user_agent or (
                        "SynthScholar systematic review"
                        + (f" (mailto:{self.config.email})" if self.config.email else "")
                    ),
                    "Accept": "application/json,application/pdf;q=0.9,*/*;q=0.8",
                },
            )
        return self._client

    def _throttle(self, provider: str, min_interval: float) -> None:
        """Keep within each provider's published rate limit."""
        last = self._last_call.get(provider)
        if last is not None:
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[provider] = time.monotonic()

    def _get_json(self, url: str, provider: str, min_interval: float,
                  params: Optional[dict] = None,
                  headers: Optional[dict] = None) -> Optional[dict]:
        self._throttle(provider, min_interval)
        self.requests_made += 1
        try:
            r = self._get_client().get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            logger.info("[%s] lookup failed: %s", provider, e)
            return None
        if r.status_code == 404:
            return None                       # no record for this DOI — normal
        if r.status_code != 200:
            logger.info("[%s] HTTP %s for %s", provider, r.status_code, url)
            return None
        try:
            return r.json()
        except ValueError:
            logger.info("[%s] non-JSON response for %s", provider, url)
            return None

    def _download_pdf(self, url: str) -> Optional[bytes]:
        """Fetch a candidate URL, accepting it only if it really is a PDF.

        The magic bytes are authoritative, not the content-type: repositories
        routinely serve PDFs as ``application/octet-stream``, and paywalls
        routinely serve HTML with a PDF-ish URL.
        """
        if not url:
            return None
        self.requests_made += 1
        try:
            r = self._get_client().get(url, headers={"Accept": "application/pdf,*/*"})
        except httpx.HTTPError as e:
            logger.info("OA PDF download failed (%s): %s", url, e)
            return None
        if r.status_code != 200:
            logger.info("OA PDF HTTP %s for %s", r.status_code, url)
            return None
        if r.content[:5] != b"%PDF-":
            logger.info("OA candidate is not a PDF (%s)", url)
            return None
        return r.content

    @staticmethod
    def _dedupe(urls: list[str]) -> list[str]:
        out: list[str] = []
        for u in urls:
            u = (u or "").strip()
            if u and u not in out:
                out.append(u)
        return out[:MAX_CANDIDATES]

    # ── per-provider candidate discovery ──

    def _unpaywall_candidates(self, doi: str) -> list[str]:
        """Unpaywall OA locations. Requires an email — their ToS, not ours."""
        if not self.config.email:
            return []
        data = self._get_json(f"{self.UNPAYWALL}/{quote(doi)}", "unpaywall", 0.1,
                              params={"email": self.config.email})
        if not data:
            return []
        locations = [data.get("best_oa_location") or {}]
        locations += [loc for loc in (data.get("oa_locations") or []) if loc]
        urls: list[str] = []
        for loc in locations:
            urls.append(loc.get("url_for_pdf") or "")
            urls.append(loc.get("url") or "")
        return self._dedupe(urls)

    def _openalex_candidates(self, doi: str) -> list[str]:
        data = self._get_json(f"{self.OPENALEX}/doi:{quote(doi)}", "openalex", 0.1,
                              params={"mailto": self.config.email} if self.config.email else None)
        if not data:
            return []
        urls = [
            (data.get("best_oa_location") or {}).get("pdf_url") or "",
            (data.get("primary_location") or {}).get("pdf_url") or "",
            (data.get("open_access") or {}).get("oa_url") or "",
        ]
        for loc in (data.get("locations") or []):
            if isinstance(loc, dict) and loc.get("pdf_url"):
                urls.append(loc["pdf_url"])
        return self._dedupe(urls)

    def _semantic_scholar_candidates(self, doi: str) -> list[str]:
        # 1 req/s on the public tier; a key lifts that considerably.
        interval = 0.1 if self.config.semantic_scholar_key else 1.0
        headers = ({"x-api-key": self.config.semantic_scholar_key}
                   if self.config.semantic_scholar_key else None)
        data = self._get_json(f"{self.SEMANTIC_SCHOLAR}/DOI:{quote(doi)}",
                              "semantic_scholar", interval,
                              params={"fields": "openAccessPdf,isOpenAccess,title"},
                              headers=headers)
        if not data:
            return []
        return self._dedupe([(data.get("openAccessPdf") or {}).get("url") or ""])

    # ── public API ──

    def fetch_pdf_bytes(self, doi: str) -> Optional[tuple[bytes, str]]:
        """Return ``(pdf_bytes, source)`` for an open-access DOI, else None.

        ``source`` is the provider that found the copy, as the same provenance
        value the application records — so a corpus assembled here and a corpus
        assembled by the app are indistinguishable downstream.
        """
        doi = _clean_doi(doi)
        if not doi:
            return None

        chain = (
            (SOURCE_UNPAYWALL, self._unpaywall_candidates),
            (SOURCE_OPENALEX, self._openalex_candidates),
            (SOURCE_SEMANTIC_SCHOLAR, self._semantic_scholar_candidates),
        )
        for source, discover in chain:
            try:
                candidates = discover(doi)
            except Exception as e:      # a provider bug must not end the run
                logger.info("[%s] candidate discovery failed for %s: %s", source, doi, e)
                continue
            for url in candidates:
                data = self._download_pdf(url)
                if data:
                    self.pdfs_retrieved += 1
                    self.by_source[source] = self.by_source.get(source, 0) + 1
                    logger.debug("OA hit for %s via %s (%s)", doi, source, url)
                    return data, source
        return None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
