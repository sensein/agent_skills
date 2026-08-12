"""EZproxy client — vendored into the skill so it runs without the app installed.

This is a self-contained copy of ``synthscholar.ezproxy``. The skill vendors it
because that module ships only in the SynthScholar development checkout: the
released package on PyPI predates it, so ``import synthscholar.ezproxy`` fails
in an ordinary environment and the paywalled-retrieval path would be unusable.

``fetch_ezproxy.py`` prefers the installed ``synthscholar.ezproxy`` when it is
importable — a newer app wins — and falls back to this copy otherwise. Keep the
two in sync when changing retrieval behaviour.

Two rewrite conventions are supported — check what your library's off-campus
links look like:

* ``hostname-suffix`` (most common): the target host gains the proxy as a
  suffix and its dots become dashes —
  ``https://www-sciencedirect-com.ezproxy.uni.edu/…``
* ``login-url``: the target is passed as a query parameter —
  ``https://ezproxy.uni.edu/login?url=https://www.sciencedirect.com/…``

Authentication is **not** performed here. Institutional login is usually SSO
with MFA and cannot be scripted responsibly, so you sign in with a browser and
hand over the resulting session cookies. Nothing is stored, and configuration is
environment-first so credentials never reach a command line::

    export EZPROXY_HOST=ezproxy.myuniversity.edu
    export EZPROXY_MODE=hostname-suffix        # or login-url
    export EZPROXY_COOKIE_FILE=~/ezproxy-cookies.txt
    export EZPROXY_DELAY=3                     # seconds between requests
    export EZPROXY_MAX_REQUESTS=100            # per-run ceiling

**Use it the way your licence allows.** Articles are fetched one at a time, with
a delay, up to a per-run ceiling, using your own entitlement — the equivalent of
you opening each paper. Bulk or systematic downloading breaches most publisher
agreements and can get an institution's access suspended.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

MODE_HOSTNAME_SUFFIX = "hostname-suffix"
MODE_LOGIN_URL = "login-url"
VALID_MODES = (MODE_HOSTNAME_SUFFIX, MODE_LOGIN_URL)

# Provenance values written to Article.full_text_source.
SOURCE_PDF = "ezproxy_pdf"

# PDF links published in landing-page markup, most reliable first.
_META_PDF_RES = (
    re.compile(r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']""", re.I),
    re.compile(r"""<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""", re.I),
    re.compile(r"""<link[^>]+type=["']application/pdf["'][^>]+href=["']([^"']+)["']""", re.I),
    re.compile(r"""<link[^>]+href=["']([^"']+)["'][^>]+type=["']application/pdf["']""", re.I),
)
_ANCHOR_PDF_RE = re.compile(r"""<a[^>]+href=["']([^"']*(?:\.pdf|/pdf[^"']*))["']""", re.I)

# Hosts that must never be rewritten through a proxy — they are open access, so
# proxying them only adds a hop (and risks a spurious login bounce). ``doi.org``
# is deliberately NOT here: a DOI link that resolves outside the gateway lands
# on the publisher's paywall, and proxying the DOI is the standard fallback when
# the redirect target can't be resolved first.
_NEVER_PROXY = (
    "ncbi.nlm.nih.gov", "europepmc.org", "arxiv.org",
    "biorxiv.org", "medrxiv.org", "openalex.org", "api.crossref.org",
)


def _load_cookie_file(path: str) -> dict[str, str]:
    """Parse a Netscape/``cookies.txt`` export into a name → value dict.

    Browser extensions and ``curl``/``wget`` all write this format:
    ``domain  flag  path  secure  expiry  name  value`` (tab-separated).
    Lines that don't have 7 fields are skipped, so a file that is actually a
    raw ``Cookie`` header still works — it falls through to the header path.
    """
    cookies: dict[str, str] = {}
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("EZproxy cookie file unreadable (%s): %s", path, e)
        return cookies
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    if not cookies:
        logger.warning(
            "EZproxy cookie file %s yielded no cookies — expected Netscape "
            "cookies.txt format (tab-separated, 7 fields per line)", path,
        )
    return cookies


def _parse_cookie_header(header: str) -> dict[str, str]:
    """``'a=1; b=2'`` → ``{'a': '1', 'b': '2'}``."""
    out: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        if "=" in chunk:
            name, _, value = chunk.partition("=")
            out[name.strip()] = value.strip()
    return out


@dataclass
class EZProxyConfig:
    """Institutional-proxy settings. Inactive unless a host *and* a cookie are given."""

    host: str = ""
    mode: str = MODE_HOSTNAME_SUFFIX
    cookie: str = ""                       # raw Cookie header value
    cookie_file: str = ""                  # Netscape cookies.txt path
    delay_seconds: float = 3.0
    max_requests: int = 100
    timeout: int = 60
    user_agent: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "EZProxyConfig":
        """Build from ``EZPROXY_*`` environment variables."""
        mode = (os.environ.get("EZPROXY_MODE") or MODE_HOSTNAME_SUFFIX).strip()
        if mode not in VALID_MODES:
            logger.warning(
                "EZPROXY_MODE=%r is not one of %s — falling back to %s",
                mode, VALID_MODES, MODE_HOSTNAME_SUFFIX,
            )
            mode = MODE_HOSTNAME_SUFFIX
        def _num(name: str, default: float) -> float:
            raw = os.environ.get(name, "")
            try:
                return float(raw) if raw else default
            except ValueError:
                logger.warning("%s=%r is not a number — using %s", name, raw, default)
                return default
        return cls(
            host=(os.environ.get("EZPROXY_HOST") or "").strip(),
            mode=mode,
            cookie=os.environ.get("EZPROXY_COOKIE", ""),
            cookie_file=os.environ.get("EZPROXY_COOKIE_FILE", ""),
            delay_seconds=_num("EZPROXY_DELAY", 3.0),
            max_requests=int(_num("EZPROXY_MAX_REQUESTS", 100)),
            user_agent=os.environ.get("EZPROXY_USER_AGENT", ""),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.cookie.strip() or self.cookie_file.strip())

    @property
    def enabled(self) -> bool:
        """Active only with both a gateway host and a session credential.

        Requiring the credential is deliberate: without it every request would
        bounce to a login page, and the failure would look like a paywall
        rather than a misconfiguration.
        """
        return bool(self.host.strip()) and self.has_credentials

    def cookies(self) -> dict[str, str]:
        jar = _load_cookie_file(self.cookie_file) if self.cookie_file else {}
        jar.update(_parse_cookie_header(self.cookie))
        return jar

    def rewrite(self, url: str) -> str:
        """Rewrite *url* to go through the gateway (unchanged when not applicable)."""
        if not self.host or not url:
            return url
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http") or not parsed.netloc:
            return url
        host = parsed.netloc.lower()
        if self.host.lower() in host:
            return url                                     # already proxied
        if any(host == d or host.endswith("." + d) for d in _NEVER_PROXY):
            return url
        if self.mode == MODE_LOGIN_URL:
            return f"https://{self.host}/login?url={quote(url, safe='')}"
        # hostname-suffix: dots → dashes, existing dashes doubled (EZproxy's
        # own escaping rule), then the proxy host appended.
        netloc = parsed.netloc.split("@")[-1]
        hostname, _, port = netloc.partition(":")
        rewritten = hostname.replace("-", "--").replace(".", "-")
        proxied = f"{rewritten}.{self.host}"
        if port:
            proxied = f"{proxied}:{port}"
        return parsed._replace(netloc=proxied).geturl()

    def redacted(self) -> dict:
        """Config summary safe to store in provenance — credentials as booleans."""
        return {
            "host": self.host,
            "mode": self.mode,
            "cookie_provided": bool(self.cookie.strip()),
            "cookie_file_provided": bool(self.cookie_file.strip()),
            "delay_seconds": self.delay_seconds,
            "max_requests": self.max_requests,
        }


class EZProxyFetcher:
    """Fetch and parse paywalled PDFs through an institutional EZproxy session.

    One article at a time, ``delay_seconds`` apart, at most ``max_requests``
    per run. When the ceiling is reached the fetcher stops and says so rather
    than continuing quietly — a run that silently gave up halfway would look
    like the papers were unavailable.
    """

    def __init__(self, config: Optional[EZProxyConfig] = None, pdf_parser=None, max_chars: int = 30000):
        self.config = config or EZProxyConfig.from_env()
        self.max_chars = max_chars
        self.requests_made = 0
        self.pdfs_retrieved = 0
        self._client: Optional[httpx.Client] = None
        self._announced = False

        self.pdf_parser = pdf_parser or _LocalPdfParser(max_chars=max_chars)

    # ── availability ──

    @property
    def available(self) -> bool:
        return self.config.enabled and self.pdf_parser.available

    def status(self) -> str:
        """One-line explanation of why the fetcher is (not) usable."""
        c = self.config
        if not c.host:
            return "EZproxy not configured (set EZPROXY_HOST)"
        if not c.has_credentials:
            return ("EZproxy host set but no session cookie "
                    "(set EZPROXY_COOKIE_FILE or EZPROXY_COOKIE)")
        if not self.pdf_parser.available:
            return "EZproxy configured but pymupdf is missing (pip install pymupdf)"
        return (f"EZproxy ready via {c.host} ({c.mode}), "
                f"≤{c.max_requests} requests, {c.delay_seconds}s apart")

    # ── internals ──

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            c = self.config
            headers = {
                "User-Agent": c.user_agent or (
                    "Mozilla/5.0 (compatible; SynthScholar systematic review; "
                    "institutional access)"
                ),
                "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                **c.extra_headers,
            }
            self._client = httpx.Client(
                timeout=c.timeout, follow_redirects=True,
                headers=headers, cookies=c.cookies(),
            )
        return self._client

    def _budget_left(self) -> bool:
        if self.requests_made >= self.config.max_requests:
            logger.warning(
                "EZproxy request ceiling reached (%d) — remaining articles will "
                "not be fetched. Raise EZPROXY_MAX_REQUESTS if this run "
                "legitimately needs more.", self.config.max_requests,
            )
            return False
        return True

    def _fetch(self, url: str) -> Optional[httpx.Response]:
        """One rate-limited, budget-checked GET through the proxy."""
        if not self._budget_left():
            return None
        if not self._announced:
            logger.info("EZproxy: %s", self.status())
            self._announced = True
        if self.requests_made and self.config.delay_seconds > 0:
            time.sleep(self.config.delay_seconds)
        target = self.config.rewrite(url)
        self.requests_made += 1
        try:
            r = self._get_client().get(target)
        except httpx.HTTPError as e:
            logger.info("EZproxy request failed (%s): %s", url, e)
            return None
        if r.status_code != 200:
            logger.info("EZproxy HTTP %s for %s", r.status_code, target)
            return None
        return r

    @staticmethod
    def _looks_like_login(response: httpx.Response) -> bool:
        """Did the gateway bounce us to a sign-in page instead of the article?"""
        url = str(response.url).lower()
        if any(k in url for k in ("/login", "/signin", "wayf", "shibboleth", "idp/")):
            return True
        head = response.text[:4000].lower() if "html" in response.headers.get(
            "content-type", "").lower() else ""
        return bool(head) and (
            "ezproxy" in head and ("password" in head or "sign in" in head)
        )

    @staticmethod
    def _is_pdf(response: httpx.Response) -> bool:
        ctype = response.headers.get("content-type", "").lower()
        return "pdf" in ctype and response.content[:5] == b"%PDF-"

    def _pdf_links(self, html: str, base_url: str) -> list[str]:
        """Candidate PDF URLs from landing-page markup, best first."""
        found: list[str] = []
        for rx in _META_PDF_RES:
            found.extend(rx.findall(html))
        found.extend(_ANCHOR_PDF_RE.findall(html))
        out: list[str] = []
        for href in found:
            absolute = urljoin(base_url, href.replace("&amp;", "&"))
            if absolute not in out:
                out.append(absolute)
        return out[:3]

    def _resolve_doi(self, doi: str) -> str:
        """Follow ``doi.org`` **outside** the proxy to find the publisher URL.

        Resolving first, then proxying the publisher URL, works regardless of
        whether the library's EZproxy config has a stanza for ``doi.org``. This
        request doesn't count against the proxy budget — it never touches the
        gateway and carries no credentials. If it fails, the caller falls back
        to proxying the DOI link itself.
        """
        doi_url = f"https://doi.org/{doi.strip()}"
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as plain:
                r = plain.head(doi_url)
                resolved = str(r.url)
                if resolved and "doi.org" not in urlparse(resolved).netloc:
                    logger.debug("DOI %s resolves to %s", doi, resolved)
                    return resolved
        except httpx.HTTPError as e:
            logger.info("DOI resolution failed for %s (%s) — proxying the DOI link", doi, e)
        return doi_url

    def _pdf_response(self, doi: str = "", url: str = "") -> Optional[httpx.Response]:
        """Proxied landing page → PDF link in its markup → PDF response.

        A response that already *is* a PDF is returned as-is.
        """
        target = url or (self._resolve_doi(doi) if doi.strip() else "")
        if not target:
            return None

        response = self._fetch(target)
        if response is None:
            return None
        if self._looks_like_login(response):
            logger.warning(
                "EZproxy returned a login page for %s — the session cookie has "
                "probably expired. Re-export it from your browser.", target,
            )
            return None
        if self._is_pdf(response):
            return response

        ctype = response.headers.get("content-type", "").lower()
        if "html" not in ctype:
            logger.info("EZproxy: unexpected content-type %s for %s", ctype, target)
            return None

        for candidate in self._pdf_links(response.text, str(response.url)):
            pdf = self._fetch(candidate)
            if pdf is None:
                continue
            if self._is_pdf(pdf):
                return pdf
            if self._looks_like_login(pdf):
                logger.warning("EZproxy: PDF link bounced to login (%s)", candidate)
                return None
        logger.info("EZproxy: no usable PDF found on %s", response.url)
        return None

    # ── public API ──

    def fetch_text(self, doi: str = "", url: str = "") -> Optional[tuple[str, str]]:
        """Return ``(text, source)`` for a DOI or landing-page URL, else None."""
        if not self.available:
            return None
        response = self._pdf_response(doi=doi, url=url)
        if response is None:
            return None
        text = self.pdf_parser.parse_bytes(response.content)
        if not text:
            logger.info("EZproxy: PDF fetched but no text extracted (%s)", response.url)
            return None
        self.pdfs_retrieved += 1
        return text, SOURCE_PDF

    def fetch_pdf_bytes(self, doi: str = "", url: str = "") -> Optional[bytes]:
        """Same resolution as :meth:`fetch_text`, but return the raw PDF bytes.

        For tooling that keeps the file on disk (e.g. building a corpus of
        PDFs) rather than only its text.
        """
        if not self.available:
            return None
        response = self._pdf_response(doi=doi, url=url)
        if response is None:
            return None
        self.pdfs_retrieved += 1
        return response.content

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class _LocalPdfParser:
    """Minimal PyMuPDF text extractor, so this file needs no app import.

    Only :meth:`EZProxyFetcher.fetch_text` uses it; ``fetch_pdf_bytes`` (what
    the corpus workflow calls) works even when pymupdf is absent.
    """

    def __init__(self, max_chars: int = 0):
        self.max_chars = max_chars or 20_000_000
        try:
            try:
                import pymupdf as fitz  # type: ignore
            except ImportError:
                import fitz  # type: ignore
            self._fitz = fitz
        except ImportError:
            self._fitz = None

    @property
    def available(self) -> bool:
        return self._fitz is not None

    def parse_bytes(self, pdf_bytes: bytes):
        if not self.available or not pdf_bytes:
            return None
        try:
            doc = self._fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            logger.info("pymupdf failed to open byte stream: %s", e)
            return None
        try:
            chunks, total = [], 0
            for page in doc:
                text = page.get_text("text") or ""
                if not text:
                    continue
                chunks.append(text)
                total += len(text)
                if total >= self.max_chars:
                    break
            out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", "\n\n".join(chunks)).strip()
            return out[: self.max_chars] or None
        except Exception as e:
            logger.info("pymupdf text extraction failed: %s", e)
            return None
        finally:
            try:
                doc.close()
            except Exception:
                pass
