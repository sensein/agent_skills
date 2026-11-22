"""EZproxy client: URL rewrite, PDF detection, login bounce and session-cookie scope.

    python -m pytest skills/synthscholar/tests/test_ezproxy_client.py -q

Every request goes to an ``httpx.MockTransport``; an unrouted URL fails the test,
so these run offline. Only ``fetch_text(url=...)`` is called: ``doi=`` would make
``_resolve_doi`` build its own client. The mock client reuses the cookie jar the
fetcher built, so the cookie tests check which hosts the real jar sends it to.
"""
from __future__ import annotations

import logging
import sys
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
    """A route that serves a PDF body under ``content_type``."""
    return lambda req: httpx.Response(200, headers={"content-type": content_type},
                                      content=PDF_BYTES)


def html(body: str) -> Route:
    """A route that serves ``body`` as an HTML page."""
    return lambda req: httpx.Response(200, headers={"content-type": "text/html"},
                                      text=body)


def redirect(location: str) -> Route:
    """A route that answers 302 to ``location``."""
    return lambda req: httpx.Response(302, headers={"location": location})


class StubParser:
    """Stands in for pymupdf: any PDF body parses to ``"TEXT"``."""

    available = True

    def parse_bytes(self, pdf_bytes: bytes) -> str | None:
        """``"TEXT"`` for a PDF body, else None."""
        return "TEXT" if pdf_bytes.startswith(b"%PDF-") else None


class Recorder:
    """Answers requests from ``routes`` and records ``(url, Cookie header)`` for each."""

    def __init__(self, routes: dict[str, Route]):
        self.routes = routes
        self.sent: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.sent.append((url, request.headers.get("cookie")))
        assert url in self.routes, f"unrouted request: {url}"
        return self.routes[url](request)

    def urls(self) -> list[str]:
        """The URLs requested, in order."""
        return [url for url, _ in self.sent]

    def cookie_for(self, url: str) -> str | None:
        """The Cookie header sent with the first request to ``url``."""
        return next(cookie for sent, cookie in self.sent if sent == url)


def make(routes: dict[str, Route], **config: str | int) -> tuple[EZProxyFetcher, Recorder]:
    """A fetcher answered by ``routes``, keeping the jar and headers ``_get_client`` built."""
    recorder = Recorder(routes)
    cfg = EZProxyConfig(**{"host": HOST, "cookie": COOKIE, "delay_seconds": 0, **config})
    fetcher = EZProxyFetcher(config=cfg, pdf_parser=StubParser())
    real = fetcher._get_client()
    fetcher._client = httpx.Client(transport=httpx.MockTransport(recorder),
                                   cookies=real.cookies, headers=real.headers,
                                   follow_redirects=True)
    real.close()
    return fetcher, recorder


def test_hostname_suffix_fetches_rewritten_article_once() -> None:
    """A PDF landing page is one GET to the rewritten host and returns its text."""
    f, rec = make({PROXIED_ARTICLE: pdf()})
    assert f.fetch_text(url=ARTICLE) == ("TEXT", SOURCE_PDF)
    assert rec.urls() == [PROXIED_ARTICLE]


def test_redirect_to_login_returns_none_and_warns(caplog) -> None:
    """A landing that bounces to the proxy login gives None, warns, and stops."""
    f, rec = make({PROXIED_ARTICLE: redirect(LOGIN), LOGIN: html("<form>Sign in</form>")})
    with caplog.at_level(logging.WARNING, logger="ezproxy_client"):
        assert f.fetch_text(url=ARTICLE) is None
    assert "login page" in caplog.text
    assert rec.urls() == [PROXIED_ARTICLE, LOGIN]
    assert f.requests_made == 1


def test_pdf_content_type_with_html_body_is_rejected() -> None:
    """A response labelled application/pdf with an HTML body is not a PDF."""
    f, _ = make({PROXIED_ARTICLE: lambda req: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"<html>paywall")})
    assert f.fetch_text(url=ARTICLE) is None


def test_pdf_served_as_octet_stream_is_accepted() -> None:
    """A %PDF- body served as application/octet-stream is accepted."""
    f, _ = make({PROXIED_ARTICLE: pdf("application/octet-stream")})
    assert f.fetch_text(url=ARTICLE) == ("TEXT", SOURCE_PDF), (
        "a PDF is recognised by its %PDF- bytes, whatever its content type")


def test_never_proxy_host_gets_no_session_cookie() -> None:
    """A direct request to a never-proxied host carries no Cookie header."""
    f, rec = make({"https://arxiv.org/abs/1": pdf()})
    f.fetch_text(url="https://arxiv.org/abs/1")
    assert rec.cookie_for("https://arxiv.org/abs/1") is None, (
        "the session cookie is scoped to the proxy domain, not sent to never-proxied hosts")


def test_already_proxied_url_is_not_rewritten_again() -> None:
    """A URL already on a proxy subdomain is requested as given."""
    f, rec = make({PROXIED_ARTICLE: pdf()})
    f.fetch_text(url=PROXIED_ARTICLE)
    assert rec.urls() == [PROXIED_ARTICLE]


def test_lookalike_host_is_rewritten_through_proxy() -> None:
    """A host that only contains the proxy name is still sent through the proxy."""
    lookalike = f"https://{HOST}.attacker.example/a.pdf"
    proxied = f"https://ezproxy-uni-edu-attacker-example.{HOST}/a.pdf"
    f, rec = make({lookalike: pdf(), proxied: pdf()})
    f.fetch_text(url=lookalike)
    assert rec.urls() == [proxied], (
        "only the proxy host and its subdomains count as already proxied")


def test_proxy_host_with_port_keeps_cookie_and_single_rewrite() -> None:
    """A proxy configured as host:port gets the cookie and is not added twice."""
    proxied = f"https://www-publisher-com.{HOST}:2048/article/1"
    f, rec = make({proxied: pdf()}, host=f"{HOST}:2048")
    assert f.fetch_text(url=proxied) == ("TEXT", SOURCE_PDF)
    assert rec.urls() == [proxied], "a proxied URL on host:port must not be rewritten again"
    assert rec.cookie_for(proxied) == COOKIE, "the cookie domain must not include the port"


def test_proxy_hosts_receive_session_cookie() -> None:
    """The proxy host, a rewritten host and the login sub-host get the cookie."""
    proxy_urls = [PROXIED_ARTICLE, f"https://{HOST}/menu", f"https://login.{HOST}/x"]
    f, rec = make({url: pdf() for url in proxy_urls})
    for url in proxy_urls:
        f.fetch_text(url=url)
    assert [cookie for _, cookie in rec.sent] == [COOKIE] * 3


def test_redirect_off_proxy_gets_no_session_cookie() -> None:
    """A redirect from the proxy to an outside host carries no Cookie header."""
    idp = "https://idp.uni.edu/sso"
    f, rec = make({PROXIED_ARTICLE: redirect(idp), idp: html("Choose your institution")})
    f.fetch_text(url=ARTICLE)
    assert rec.cookie_for(idp) is None, (
        "a redirect off the proxy must not carry the proxy session cookie")
