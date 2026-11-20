"""Offline tests for the vendored EZproxy client: rewrite, retrieval and cookie scope.

Every request goes to an ``httpx.MockTransport``; an unrouted URL fails the test,
so nothing reaches the network. Only ``fetch_text(url=...)`` is called: ``doi=``
would make ``_resolve_doi`` build its own client. Four tests fail on the current
code; each assertion message names the defect it exposes.
"""
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

import httpx

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from ezproxy_client import SOURCE_PDF, EZProxyConfig, EZProxyFetcher  # noqa: E402

HOST = "ezproxy.uni.edu"
COOKIE = "ezproxy=SECRET"
ARTICLE = "https://www.publisher.com/article/1"
PROXIED_ARTICLE = f"https://www-publisher-com.{HOST}/article/1"
LOGIN = f"https://{HOST}/login"
PDF_BYTES = b"%PDF-1.7 stub"

Route = Callable[[httpx.Request], httpx.Response]


def pdf(content_type: str = "application/pdf") -> Route:
    return lambda req: httpx.Response(200, headers={"content-type": content_type},
                                      content=PDF_BYTES)


def html(body: str) -> Route:
    return lambda req: httpx.Response(200, headers={"content-type": "text/html"},
                                      text=body)


def redirect(location: str) -> Route:
    return lambda req: httpx.Response(302, headers={"location": location})


class StubParser:
    """Stands in for pymupdf: any PDF body parses to ``"TEXT"``."""

    available = True

    def parse_bytes(self, pdf_bytes: bytes) -> str | None:
        return "TEXT" if pdf_bytes.startswith(b"%PDF-") else None


class FetcherTestCase(unittest.TestCase):
    def make(self, routes: dict[str, Route], **config) -> EZProxyFetcher:
        """A fetcher that answers from ``routes`` and records ``(url, Cookie)`` in ``self.sent``.

        The mock client takes the jar and headers ``_get_client`` built, so the
        cookie-scope tests exercise the code's own jar, not one made here.
        """
        self.sent: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.sent.append((url, request.headers.get("cookie")))
            if url not in routes:
                raise AssertionError(f"unrouted request: {url}")
            return routes[url](request)

        cfg = EZProxyConfig(host=HOST, cookie=COOKIE, delay_seconds=0, **config)
        fetcher = EZProxyFetcher(config=cfg, pdf_parser=StubParser())
        real = fetcher._get_client()
        fetcher._client = httpx.Client(transport=httpx.MockTransport(handler),
                                       cookies=real.cookies, headers=real.headers,
                                       follow_redirects=True)
        real.close()
        self.addCleanup(fetcher.close)
        return fetcher

    def urls(self) -> list[str]:
        return [url for url, _ in self.sent]

    def cookie_for(self, url: str) -> str | None:
        return next(cookie for sent, cookie in self.sent if sent == url)


class RequestPathTests(FetcherTestCase):
    def test_hostname_suffix_fetches_rewritten_article_once(self):
        """A PDF landing page is one GET to the rewritten host and returns its text."""
        f = self.make({PROXIED_ARTICLE: pdf()})
        self.assertEqual(f.fetch_text(url=ARTICLE), ("TEXT", SOURCE_PDF))
        self.assertEqual(self.urls(), [PROXIED_ARTICLE])


class ExpiredSessionTests(FetcherTestCase):
    def test_redirect_to_login_returns_none_and_warns(self):
        """A landing that bounces to the proxy login gives None, warns, and stops."""
        f = self.make({PROXIED_ARTICLE: redirect(LOGIN), LOGIN: html("<form>Sign in</form>")})
        with self.assertLogs("ezproxy_client", "WARNING") as logs:
            self.assertIsNone(f.fetch_text(url=ARTICLE))
        self.assertIn("login page", "\n".join(logs.output))
        self.assertEqual(self.urls(), [PROXIED_ARTICLE, LOGIN])
        self.assertEqual(f.requests_made, 1)


class PdfDetectionTests(FetcherTestCase):
    def test_pdf_content_type_with_html_body_is_rejected(self):
        """A response labelled application/pdf with an HTML body is not a PDF."""
        f = self.make({PROXIED_ARTICLE: lambda req: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"<html>paywall")})
        self.assertIsNone(f.fetch_text(url=ARTICLE))

    def test_pdf_served_as_octet_stream_is_accepted(self):
        """A %PDF- body served as application/octet-stream is accepted."""
        f = self.make({PROXIED_ARTICLE: pdf("application/octet-stream")})
        self.assertEqual(
            f.fetch_text(url=ARTICLE), ("TEXT", SOURCE_PDF),
            "DEFECT: _is_pdf requires 'pdf' in the content type, so a real PDF "
            "served as application/octet-stream is rejected")


class RewriteTests(FetcherTestCase):
    def test_never_proxy_host_gets_no_session_cookie(self):
        """A direct request to a never-proxied host carries no Cookie header."""
        f = self.make({"https://arxiv.org/abs/1": pdf()})
        f.fetch_text(url="https://arxiv.org/abs/1")
        self.assertIsNone(
            self.cookie_for("https://arxiv.org/abs/1"),
            "DEFECT: _get_client builds a domain-less cookie jar, so the proxy "
            "session cookie is sent to never-proxied hosts")

    def test_already_proxied_url_is_not_rewritten_again(self):
        """A URL already on a proxy subdomain is requested as given."""
        f = self.make({PROXIED_ARTICLE: pdf()})
        f.fetch_text(url=PROXIED_ARTICLE)
        self.assertEqual(self.urls(), [PROXIED_ARTICLE])

    def test_lookalike_host_is_rewritten_through_proxy(self):
        """A host that only contains the proxy name is still sent through the proxy."""
        lookalike = f"https://{HOST}.attacker.example/a.pdf"
        proxied = f"https://ezproxy-uni-edu-attacker-example.{HOST}/a.pdf"
        f = self.make({lookalike: pdf(), proxied: pdf()})
        f.fetch_text(url=lookalike)
        self.assertEqual(
            self.urls(), [proxied],
            "DEFECT: rewrite() uses a substring check for 'already proxied', so a "
            "lookalike host is fetched directly instead of through the proxy")


class CookieScopeTests(FetcherTestCase):
    def test_proxy_hosts_receive_session_cookie(self):
        """The proxy host, a rewritten host and the login sub-host get the cookie."""
        proxy_urls = [PROXIED_ARTICLE, f"https://{HOST}/menu", f"https://login.{HOST}/x"]
        f = self.make({url: pdf() for url in proxy_urls})
        for url in proxy_urls:
            f.fetch_text(url=url)
        self.assertEqual([cookie for _, cookie in self.sent], [COOKIE] * 3)

    def test_redirect_off_proxy_gets_no_session_cookie(self):
        """A redirect from the proxy to an outside host carries no Cookie header."""
        idp = "https://idp.uni.edu/sso"
        f = self.make({PROXIED_ARTICLE: redirect(idp), idp: html("Choose your institution")})
        f.fetch_text(url=ARTICLE)
        self.assertIsNone(
            self.cookie_for(idp),
            "DEFECT: _get_client builds a domain-less cookie jar, so a redirect off "
            "the proxy carries the session cookie to the outside host")


if __name__ == "__main__":
    unittest.main()
