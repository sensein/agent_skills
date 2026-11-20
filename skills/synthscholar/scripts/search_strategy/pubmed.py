"""
PubMed retrieval via NCBI E-utilities.

Deterministic: the set of PMIDs matching a fixed query is stable (barring NCBI
index updates). We use the history server (esearch usehistory=y) then page
through efetch to pull *every* matching record — no silent cap.

Rate limits: 10 req/s with an API key, 3 req/s without. We self-throttle.
"""
from __future__ import annotations

import csv
import io
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# NCBI hard limit, quoted from the efetch error: "'retstart' cannot be larger than
# 9998. For PubMed, ESearch can only retrieve the first 9,999 records matching the
# query." So neither the history server nor esearch paging can walk a large result
# set, and the naive loop either 400s (efetch) or silently stops at 9,999
# (esearch). Anything bigger has to be split into sub-queries — which is what
# NCBI's own EDirect does. We split on publication date; see date_partitions.
HISTORY_MAX = 9999

CSV_FIELDS = [
    "pmid", "doi", "title", "authors", "journal", "year",
    "pub_types", "mesh_terms", "abstract", "url",
]


@dataclass
class Article:
    pmid: str = ""
    doi: str = ""
    title: str = ""
    abstract: str = ""
    journal: str = ""
    year: str = ""
    authors: list[str] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    pub_types: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    def to_row(self) -> dict:
        return {
            "pmid": self.pmid,
            "doi": self.doi,
            "title": self.title,
            "authors": "; ".join(self.authors),
            "journal": self.journal,
            "year": self.year,
            "pub_types": "; ".join(self.pub_types),
            "mesh_terms": "; ".join(self.mesh_terms),
            "abstract": self.abstract,
            "url": self.url,
        }


class PubMed:
    def __init__(self, api_key: str = "", email: str = "", tool: str = "deterministic-lit-search"):
        self.api_key = api_key or ""
        self.email = email or ""
        self.tool = tool
        self._min_interval = 1.0 / (10 if self.api_key else 3) + 0.02
        self._last = 0.0
        self._session = requests.Session()

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last
        if dt < self._min_interval:
            time.sleep(self._min_interval - dt)
        self._last = time.monotonic()

    def _params(self, **kw) -> dict:
        p = {"tool": self.tool}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        p.update(kw)
        return p

    def _request(self, path: str, params: dict, method: str = "GET") -> requests.Response:
        for attempt in range(5):
            self._throttle()
            try:
                if method == "POST":
                    r = self._session.post(f"{EUTILS}/{path}", data=params, timeout=60)
                else:
                    r = self._session.get(f"{EUTILS}/{path}", params=params, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    # raise_for_status() drops the body, and NCBI puts the actual
                    # reason there ("'retstart' cannot be larger than 9998").
                    raise RuntimeError(
                        f"NCBI {path} returned {r.status_code}: {r.text[:400].strip()}")
                return r
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("E-utilities request failed after retries")

    # ---- search -------------------------------------------------------

    @staticmethod
    def _esearch(r) -> dict:
        """The `esearchresult` of an esearch reply. NCBI occasionally answers 200
        with an error page or a body without it; that is a RuntimeError like any
        other failed request, not a KeyError."""
        try:
            data = r.json()["esearchresult"]
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError(f"unexpected NCBI esearch response: {r.text[:200]!r}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected NCBI esearch response: {r.text[:200]!r}")
        return data

    def search(self, query: str) -> dict:
        """esearch with history server. Returns count, webenv, query_key, translation."""
        params = self._params(db="pubmed", term=query, usehistory="y", retmode="json", retmax=0)
        r = self._request("esearch.fcgi", params, method="POST")
        data = self._esearch(r)
        return {
            "count": int(data.get("count", 0)),
            "webenv": data.get("webenv", ""),
            "query_key": data.get("querykey", ""),
            "translation": data.get("querytranslation", ""),
            "warnings": data.get("warninglist", {}),
            "errors": data.get("errorlist", {}),
        }

    # ---- working around the 9,999-record ceiling ----------------------

    def count(self, query: str) -> int:
        params = self._params(db="pubmed", term=query, retmode="json", retmax=0)
        r = self._request("esearch.fcgi", params, method="POST")
        return int(self._esearch(r).get("count", 0))

    @staticmethod
    def _scope(query: str, lo: date, hi: date) -> str:
        return (f'({query}) AND ("{lo:%Y/%m/%d}"[Date - Publication] : '
                f'"{hi:%Y/%m/%d}"[Date - Publication])')

    def date_partitions(self, query: str, limit: int = HISTORY_MAX,
                        lo: date | None = None, hi: date | None = None,
                        progress=None) -> list[dict]:
        """
        Split a query into publication-date slices that each fit under `limit`.

        Recursive bisection on the date range: a slice with more than `limit` hits
        is halved and each half re-counted, so every returned slice is retrievable
        in full. Deterministic (the same query always yields the same slices) and
        complete (slices are contiguous and disjoint, so their counts sum to the
        total — `fetch_query` checks that and reports any shortfall).

        Returns [{"from", "to", "count", "query", "truncated"} ...].
        """
        lo = lo or date(1500, 1, 1)
        hi = hi or date.today().replace(month=12, day=31) + timedelta(days=730)

        def rec(a: date, b: date, known: int | None = None) -> list[dict]:
            scoped = self._scope(query, a, b)
            n = known if known is not None else self.count(scoped)
            if progress:
                progress(a, b, n)
            if n == 0:
                return []
            if n <= limit:
                return [{"from": f"{a:%Y/%m/%d}", "to": f"{b:%Y/%m/%d}", "count": n,
                         "query": scoped, "truncated": False}]
            if a >= b:      # a single day over the limit: nothing left to split
                return [{"from": f"{a:%Y/%m/%d}", "to": f"{b:%Y/%m/%d}", "count": n,
                         "query": scoped, "truncated": True}]
            mid = a + timedelta(days=(b - a).days // 2)
            return rec(a, mid) + rec(mid + timedelta(days=1), b)

        return rec(lo, hi)

    def pmids(self, query: str, retmax: int | None = None) -> list[str]:
        """
        Every PMID matching `query`, sorted ascending.

        Pages within a slice and splits by date across slices, so it is not capped
        at 9,999 — the previous version silently returned the first 9,999 of a
        35,089-record query, which quietly falsified any set-overlap computed from
        it. `retmax` caps the result deliberately (and is reported as truncation by
        the caller if it bites).
        """
        slices = ([{"query": query}] if self.count(query) <= HISTORY_MAX
                  else self.date_partitions(query))
        out: list[str] = []
        for sl in slices:
            start = 0
            while True:
                n = min(HISTORY_MAX - start, 10000)
                if n <= 0:
                    break
                params = self._params(db="pubmed", term=sl["query"], retmode="json",
                                      retstart=start, retmax=n)
                r = self._request("esearch.fcgi", params, method="POST")
                ids = self._esearch(r).get("idlist", [])
                out.extend(ids)
                if len(ids) < n:
                    break
                start += n
            if retmax is not None and len(out) >= retmax:
                break
        uniq = sorted(set(out), key=lambda p: int(p) if p.isdigit() else 0)
        return uniq[:retmax] if retmax is not None else uniq

    # ---- fetch --------------------------------------------------------

    def fetch_query(self, query: str, max_records: int | None = None,
                    batch: int = 500, progress=None) -> dict:
        """
        Retrieve every record matching `query`, however many there are.

        Under 9,999 hits this is one history-server walk. Above it, the query is
        split by publication date (date_partitions) and each slice walked
        separately — the history server refuses `retstart` > 9998, which is why
        fetching a 35k-record search used to die with `400 Bad Request` from
        efetch partway through.

        Returns {"articles", "count", "fetched", "slices", "capped", "missing"}.
        `missing` is count - unique records fetched, which should be 0 on an
        uncapped run: a non-zero value means records fell outside the date window
        or carry no publication date, and it is surfaced rather than hidden. Slice
        counts may *overlap* (a record can carry both a print and an electronic
        publication date), so they are not a completeness check — the deduplicated
        PMID count is.
        """
        total = self.count(query)
        if total <= HISTORY_MAX:
            slices = [{"from": "", "to": "", "count": total, "query": query,
                       "truncated": False}]
        else:
            # `progress` is fetch_all's (done, target) callback; date_partitions
            # calls its own with (lo, hi, n), so it is not passed on
            slices = self.date_partitions(query)

        articles: list[Article] = []
        seen: set[str] = set()
        for sl in slices:
            if max_records is not None and len(articles) >= max_records:
                break
            res = self.search(sl["query"])
            if not res["count"]:
                continue
            room = None if max_records is None else max_records - len(articles)
            for art in self.fetch_all(res["webenv"], res["query_key"], res["count"],
                                      batch=batch, max_records=room, progress=progress):
                if art.pmid not in seen:
                    seen.add(art.pmid)
                    articles.append(art)
        articles.sort(key=lambda a: int(a.pmid) if a.pmid.isdigit() else 0)
        return {
            "articles": articles,
            "count": total,
            "fetched": len(articles),
            "slices": slices,
            "capped": max_records is not None and total > max_records,
            "missing": 0 if (max_records is not None and total > max_records)
                       else max(0, total - len(articles)),
        }

    def fetch_all(self, webenv: str, query_key: str, count: int,
                  batch: int = 200, max_records: int | None = None,
                  progress=None) -> list[Article]:
        """One history-server walk. Only safe for count <= HISTORY_MAX; callers
        with a bigger result set must go through fetch_query."""
        target = count if max_records is None else min(count, max_records)
        target = min(target, HISTORY_MAX)
        out: list[Article] = []
        start = 0
        while start < target:
            n = min(batch, target - start)
            params = self._params(
                db="pubmed", WebEnv=webenv, query_key=query_key,
                retstart=start, retmax=n, retmode="xml",
            )
            r = self._request("efetch.fcgi", params, method="POST")
            out.extend(self._parse(r.content))
            start += n
            if progress:
                progress(min(start, target), target)
        # stable order
        out.sort(key=lambda a: int(a.pmid) if a.pmid.isdigit() else 0)
        return out

    # ---- XML parsing --------------------------------------------------

    @staticmethod
    def _text(el) -> str:
        return "".join(el.itertext()).strip() if el is not None else ""

    @classmethod
    def _parse(cls, xml_bytes: bytes) -> list[Article]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:            # an HTML error page instead of efetch XML
            raise RuntimeError(f"unexpected NCBI efetch response: {xml_bytes[:200]!r}") from e
        articles: list[Article] = []
        for pa in root.findall(".//PubmedArticle"):
            art = Article()
            medline = pa.find("MedlineCitation")
            if medline is None:
                continue
            art.pmid = cls._text(medline.find("PMID"))
            article = medline.find("Article")
            if article is not None:
                art.title = cls._text(article.find("ArticleTitle"))
                # abstract (may have multiple labeled sections)
                abstracts = article.findall(".//Abstract/AbstractText")
                parts = []
                for a in abstracts:
                    label = a.get("Label")
                    txt = cls._text(a)
                    parts.append(f"{label}: {txt}" if label else txt)
                art.abstract = " ".join(p for p in parts if p)
                journal = article.find("Journal")
                if journal is not None:
                    art.journal = cls._text(journal.find("Title"))
                    y = journal.find(".//JournalIssue/PubDate/Year")
                    if y is not None:
                        art.year = cls._text(y)
                    else:
                        md = journal.find(".//JournalIssue/PubDate/MedlineDate")
                        art.year = cls._text(md)[:4]
                for author in article.findall(".//AuthorList/Author"):
                    last = cls._text(author.find("LastName"))
                    init = cls._text(author.find("Initials"))
                    coll = cls._text(author.find("CollectiveName"))
                    if last:
                        art.authors.append(f"{last} {init}".strip())
                    elif coll:
                        art.authors.append(coll)
                for pt in article.findall(".//PublicationTypeList/PublicationType"):
                    t = cls._text(pt)
                    if t:
                        art.pub_types.append(t)
            for mh in medline.findall(".//MeshHeadingList/MeshHeading/DescriptorName"):
                t = cls._text(mh)
                if t:
                    art.mesh_terms.append(t)
            # DOI / ids
            for aid in pa.findall(".//PubmedData/ArticleIdList/ArticleId"):
                if aid.get("IdType") == "doi":
                    art.doi = cls._text(aid)
            if not art.doi:
                for eloc in pa.findall(".//Article/ELocationID"):
                    if eloc.get("EIdType") == "doi":
                        art.doi = cls._text(eloc)
            articles.append(art)
        return articles


def to_csv(articles: list[Article]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for a in articles:
        w.writerow(a.to_row())
    return buf.getvalue()


def to_jsonl(articles: list[Article]) -> str:
    import json
    return "\n".join(json.dumps(a.to_row(), ensure_ascii=False) for a in articles)
