"""Bibliographic metadata read from the document itself — deterministic, no LLM.

The TTL's ner:Publication carries title, DOI, PMID/PMCID, date, journal and authors
(prov:wasAttributedTo / dcterms:creator). A model reading chunk 1 can fill some of
that, but the document usually states it exactly, so read it first:

  jats      JATS/PMC XML front matter: article-title, article-id (doi / pmid / pmc),
            contrib-group authors (with ORCID), journal-title, pub-date
  pdf_meta  the PDF's own metadata (title / author / subject / doi keys) — used only
            when plausible (not "Microsoft Word - ...", not a file name)
  text      the first DOI printed in the text (front matter first), "PMID: n"
  filename  a leading PubMed id and a _YYYY_ year ("37731775_de_2023_Shared_..."), and
            a bioRxiv/medRxiv id ("2026.02.03.703645v1_text.txt"): its DOI
            10.1101/<id> is used only once a registry confirms it (a file name is a
            hint, not a statement); unconfirmed, it is kept as `preprint_id`
  crossref  given a DOI: title, authors (with ORCID), date and venue from Crossref,
            the registration agency for most journals; bioRxiv's own API for 10.1101
            preprints Crossref doesn't have yet (network; skipped when
            STRUCTSENSE_NO_NETWORK is set or resolve_doi=False)

`harvest(path, text)` returns the fields plus `metadata_sources` ({field: source}).
`merge(model_meta, harvested)` combines them: exact identifiers (doi, pmid, pmcid)
prefer the document over the model; title and authors prefer JATS, then Crossref,
then the model, then PDF metadata; nothing is ever invented.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>\]\[(){},;]+[^\s\"'<>\]\[(){},;.])", re.I)
PMID_RE = re.compile(r"\bPMID:?\s*(\d{6,9})\b")
PMCID_RE = re.compile(r"\b(PMC\d{5,9})\b")
YEAR_RE = re.compile(r"(?:^|_)((?:19|20)\d{2})(?:_|$)")


def _clean(s: Any) -> Optional[str]:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


def from_jats(xml: bytes) -> dict:
    try:
        from lxml import etree  # type: ignore
        root = etree.fromstring(xml)
        find = lambda el, path: el.xpath(path)  # noqa: E731
    except Exception:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return {}
        find = None
    out: dict[str, Any] = {}
    if find is None:  # stdlib fallback: no namespaces in PMC JATS body tags
        def text_of(el):
            return _clean("".join(el.itertext())) if el is not None else None
        meta = root.find(".//article-meta")
        if meta is None:
            return {}
        out["title"] = text_of(meta.find(".//title-group/article-title"))
        for aid in meta.findall("article-id"):
            kind = (aid.get("pub-id-type") or "").lower()
            if kind in ("doi", "pmid", "pmc", "pmcid"):
                out["pmcid" if kind.startswith("pmc") else kind] = _clean(aid.text)
        authors = []
        for c in meta.findall(".//contrib"):
            if c.get("contrib-type") not in (None, "author"):
                continue
            sur, giv = text_of(c.find(".//surname")), text_of(c.find(".//given-names"))
            name = " ".join(x for x in (giv, sur) if x) or text_of(c.find(".//collab"))
            orcid = next((_clean(i.text) for i in c.findall("contrib-id")
                          if (i.get("contrib-id-type") or "").lower() == "orcid"), None)
            if name:
                authors.append({"name": name, **({"orcid": orcid} if orcid else {})})
        if authors:
            out["authors"] = authors
        out["journal"] = text_of(root.find(".//journal-meta//journal-title"))
        pd = meta.find(".//pub-date")
        if pd is not None:
            y, m, d = (text_of(pd.find(x)) for x in ("year", "month", "day"))
            if y:
                out["year"] = y
                if m and d and m.isdigit() and d.isdigit():
                    out["publication_date"] = f"{y}-{int(m):02d}-{int(d):02d}"
        return {k: v for k, v in out.items() if v}
    return from_jats_lxml(root)


def from_jats_lxml(root) -> dict:
    def t(el) -> Optional[str]:
        return _clean("".join(el.itertext())) if el is not None else None

    def first(path):
        r = root.xpath(path)
        return r[0] if r else None
    out: dict[str, Any] = {"title": t(first("//article-meta//title-group/article-title"))}
    for aid in root.xpath("//article-meta/article-id"):
        kind = (aid.get("pub-id-type") or "").lower()
        if kind in ("doi", "pmid", "pmc", "pmcid"):
            out["pmcid" if kind.startswith("pmc") else kind] = _clean(aid.text)
    authors = []
    for c in root.xpath("//article-meta//contrib[not(@contrib-type) or @contrib-type='author']"):
        sur, giv = t(first_in(c, ".//surname")), t(first_in(c, ".//given-names"))
        name = " ".join(x for x in (giv, sur) if x) or t(first_in(c, ".//collab"))
        orcid = next((_clean(i.text) for i in c.xpath("contrib-id[@contrib-id-type='orcid']")), None)
        if name:
            authors.append({"name": name, **({"orcid": orcid} if orcid else {})})
    if authors:
        out["authors"] = authors
    out["journal"] = t(first("//journal-meta//journal-title"))
    pd = first("//article-meta/pub-date")
    if pd is not None:
        y, m, d = (t(first_in(pd, x)) for x in ("year", "month", "day"))
        if y:
            out["year"] = y
            if m and d and m.isdigit() and d.isdigit():
                out["publication_date"] = f"{y}-{int(m):02d}-{int(d):02d}"
    return {k: v for k, v in out.items() if v}


def first_in(el, path):
    r = el.xpath(path)
    return r[0] if r else None


def from_pdf(path: Path) -> dict:
    try:
        import fitz  # type: ignore  (PyMuPDF)
    except ImportError:
        return {}
    try:
        with fitz.open(str(path)) as doc:
            meta, pages = doc.metadata or {}, doc.page_count
    except Exception:
        return {}
    out: dict[str, Any] = {"page_count": pages}
    title = _clean(meta.get("title"))
    if title and len(title.split()) >= 4 and not re.search(
            r"^(microsoft word|untitled)|\.(docx?|pdf|tex)$|\d{3,}_|_\w+ \d|\.\.", title, re.I):
        out["title"] = title
    author = _clean(meta.get("author"))
    if author and len(author) > 3 and not re.search(r"^(admin|user|author)$", author, re.I):
        names = [a.strip() for a in re.split(r";|,\s*(?=[A-Z][a-z]+\s)|\band\b", author) if a.strip()]
        if names:  # PDF metadata often lists only the first author: a fallback, never final
            out["authors"] = [{"name": n} for n in names]
            out["authors_partial"] = len(names) == 1
    for key in ("subject", "keywords", "doi"):
        m = DOI_RE.search(meta.get(key) or "")
        if m:
            out["doi"] = m.group(1)
            break
    return out


# DOI registrants of data repositories: a paper cites its dataset's DOI, which is not
# the paper's own (Dataverse, Zenodo, figshare, Dryad, Mendeley Data, OSF, PANGAEA)
DATA_DOI = re.compile(r"^10\.(34894|5281|6084|5061|17632|17605|1594|7910|25378|18112|7488|48550)/", re.I)


def from_text(text: str) -> dict:
    head = text[:8000]
    out: dict[str, Any] = {}
    for m in list(DOI_RE.finditer(head)) + list(DOI_RE.finditer(text[8000:])):
        doi = clean_doi(m.group(1))
        if not DATA_DOI.match(doi):
            out["doi"] = doi
            break
    m = PMID_RE.search(head)
    if m:
        out["pmid"] = m.group(1)
    m = PMCID_RE.search(head)
    if m:
        out["pmcid"] = m.group(1)
    m = LICENSE_TEXT_RE.search(head)
    if m:
        out["license"] = _cc_url(m.group(1), m.group(2))
    return out


def clean_doi(doi: str) -> str:
    """A DOI as captured from extracted text, minus what extraction fused onto its end
    (bioRxiv headers become "…/2026.02.03.703645doi: bioRxiv preprint")."""
    doi = str(doi or "").strip().rstrip(".:;,")
    while True:
        trimmed = re.sub(r"(?i)(doi:?|biorxiv|medrxiv|preprint)$", "", doi).rstrip(".:;,")
        if trimmed == doi or not re.search(r"/.", trimmed):
            return doi
        doi = trimmed


# DOI prefixes bioRxiv / medRxiv register preprints under: 10.64898 for new ones,
# 10.1101 historically. A preprint id from a file name is tried with each.
PREPRINT_PREFIXES = ("10.64898", "10.1101")
# bioRxiv / medRxiv ids (2026.02.03.703645, optionally v1)
PREPRINT_ID_RE = re.compile(r"(?<![\d.])((?:19|20)\d{2}\.\d{2}\.\d{2}\.\d{6})(?:v\d+)?(?![\d])")


def from_filename(path: Path) -> dict:
    out: dict[str, Any] = {}
    stem = path.stem
    m = PREPRINT_ID_RE.search(stem)
    if m:
        out["preprint_id"] = m.group(1)     # a DOI only once a registry confirms it (harvest)
        out["year"] = m.group(1)[:4]
    m = re.match(r"^(\d{7,9})_", stem)
    if m:
        out["pmid"] = m.group(1)
    m = YEAR_RE.search(stem)
    if m:
        out["year"] = m.group(1)
    return out


def from_crossref(doi: str, fetch=None) -> dict:
    """Title, authors, date and venue for a DOI from Crossref's REST API. {} when
    offline, disabled, unknown to Crossref, or on any error — never a guess."""
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(doi or ""), flags=re.I).strip().rstrip(".")
    if not doi or os.environ.get("STRUCTSENSE_NO_NETWORK"):
        return {}
    url = "https://api.crossref.org/works/" + doi
    try:
        if fetch is None:
            from urllib.parse import quote
            from urllib.request import Request, urlopen
            url = "https://api.crossref.org/works/" + quote(doi, safe="/")
            req = Request(url, headers={"User-Agent": "structsense-skill/1.0 (mailto:structsense@brainkb.org)"})
            with urlopen(req, timeout=15) as r:
                msg = json.loads(r.read().decode("utf-8")).get("message") or {}
        else:
            msg = (fetch(url) or {}).get("message") or {}
    except Exception:
        return {}
    out: dict[str, Any] = {}
    title = _clean((msg.get("title") or [None])[0])
    if title:
        out["title"] = title
    authors = []
    for a in msg.get("author") or []:
        name = _clean(" ".join(x for x in (a.get("given"), a.get("family")) if x)) or _clean(a.get("name"))
        if name:
            orcid = re.sub(r"^https?://orcid\.org/", "", str(a.get("ORCID") or "")) or None
            authors.append({"name": name, **({"orcid": orcid} if orcid else {})})
    if authors:
        out["authors"] = authors
    parts = ((msg.get("published") or msg.get("posted") or msg.get("issued") or {}).get("date-parts") or [[]])[0]
    if parts and parts[0]:
        out["year"] = str(parts[0])
        if len(parts) == 3:
            out["publication_date"] = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    venue = _clean((msg.get("container-title") or [None])[0]) or _clean(
        ((msg.get("institution") or [{}])[0] or {}).get("name"))
    if venue:
        out["journal"] = venue
    if msg.get("type") == "posted-content":
        out["source_type"] = "preprint"
    lic = _crossref_license(msg.get("license") or [])
    if lic:
        out["license"] = lic
    return out


def _crossref_license(items: list) -> Optional[str]:
    """The licence of the published version (Crossref lists one per content version)."""
    by_version = {str(x.get("content-version") or ""): x.get("URL") for x in items if x.get("URL")}
    return by_version.get("vor") or by_version.get("am") or next(iter(by_version.values()), None)


# bioRxiv API licence codes -> canonical licence URLs
_BIORXIV_LICENSE = {
    "cc_by": "https://creativecommons.org/licenses/by/4.0/",
    "cc_by_nc": "https://creativecommons.org/licenses/by-nc/4.0/",
    "cc_by_nd": "https://creativecommons.org/licenses/by-nd/4.0/",
    "cc_by_nc_nd": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
    "cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "cc_no": "all rights reserved (no reuse without permission)",
}
# a licence as preprints and OA papers print it ("…under a CC-BY 4.0 International license")
LICENSE_TEXT_RE = re.compile(
    # CC must be capitals but may follow a letter: bioRxiv prints "aCC-BY-NC-ND 4.0", and
    # text extraction often fuses the article onto it
    r"(?<![0-9-])((?-i:CC)[- ]?(?:BY(?:[- ](?:NC|ND|SA))*|0)(?:[- ](?:NC|ND|SA))*)\s*(\d\.\d)?\s*(?:International)?\s+licen[cs]e",
    re.I)


def _cc_url(code: str, version: Optional[str]) -> str:
    code = re.sub(r"[\s_]+", "-", code.upper()).replace("CC-", "CC-").replace("CC0", "CC-0")
    if code in ("CC-0", "CC0"):
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    parts = [p.lower() for p in code.split("-")[1:] if p]
    return f"https://creativecommons.org/licenses/{'-'.join(parts)}/{version or '4.0'}/"


def from_biorxiv(doi: str, fetch=None) -> dict:
    """bioRxiv / medRxiv's own API for a 10.1101 DOI (newest version). {} if unknown."""
    doi = str(doi or "").strip()
    if not doi.lower().startswith(tuple(p + "/" for p in PREPRINT_PREFIXES)) or os.environ.get("STRUCTSENSE_NO_NETWORK"):
        return {}
    for server in ("biorxiv", "medrxiv"):
        url = f"https://api.biorxiv.org/details/{server}/{doi}"
        try:
            if fetch is None:
                from urllib.request import Request, urlopen
                with urlopen(Request(url, headers={"User-Agent": "structsense-skill/1.0"}), timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
            else:
                data = fetch(url) or {}
        except Exception:
            continue
        posts = data.get("collection") or []
        if not posts:
            continue
        x = posts[-1]
        out: dict[str, Any] = {"journal": "medRxiv" if server == "medrxiv" else "bioRxiv", "source_type": "preprint"}
        if _clean(x.get("title")):
            out["title"] = _clean(x["title"])
        names = [n.strip() for n in str(x.get("authors") or "").split(";") if n.strip()]
        if names:
            out["authors"] = [{"name": " ".join(reversed([p.strip() for p in n.split(",", 1)])) if "," in n else n}
                              for n in names]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x.get("date") or "")):
            out["publication_date"], out["year"] = x["date"], x["date"][:4]
        lic = _BIORXIV_LICENSE.get(str(x.get("license") or "").lower())
        if lic:
            out["license"] = lic
        return out
    return {}


def doi_resolves(doi: str, fetch_status=None) -> bool:
    """Whether doi.org knows this DOI (a registered DOI redirects; unknown is 404)."""
    if os.environ.get("STRUCTSENSE_NO_NETWORK"):
        return False
    try:
        if fetch_status is not None:
            return fetch_status(doi) in (301, 302, 303, 307, 308, 200)
        from urllib.error import HTTPError
        from urllib.request import HTTPRedirectHandler, Request, build_opener

        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            build_opener(_NoRedirect).open(Request(f"https://doi.org/{doi}", method="HEAD"), timeout=15)
            return True
        except HTTPError as e:
            return e.code in (301, 302, 303, 307, 308)
    except Exception:
        return False


BIBLIO = ("title", "authors", "year", "publication_date", "journal", "source_type", "license")


def harvest(path: Path, text: str = "", *, resolve_doi: bool = True, fetch=None) -> dict:
    path = Path(path)
    layers: list[tuple[str, dict]] = []
    if path.suffix.lower() == ".xml" and path.is_file():
        layers.append(("jats", from_jats(path.read_bytes())))
    if path.suffix.lower() == ".pdf" and path.is_file():
        layers.append(("pdf_meta", from_pdf(path)))
    if text:
        layers.append(("text", from_text(text)))
    layers.append(("filename", from_filename(path)))
    out: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, layer in layers:
        for k, v in layer.items():
            if v and k not in out:
                out[k] = v
                sources[k] = name
    # A DOI resolves to the registry's own record: better than a PDF's metadata or a
    # file name for title/authors/date/venue, never over JATS front matter.
    record, via = {}, None
    if resolve_doi:
        candidates = [out["doi"]] if out.get("doi") else (
            [f"{p}/{out['preprint_id']}" for p in PREPRINT_PREFIXES] if out.get("preprint_id") else [])
        for candidate in candidates:
            record, via = from_crossref(candidate, fetch=fetch), "crossref"
            if not record:
                record, via = from_biorxiv(candidate, fetch=fetch), "biorxiv"
            if not out.get("doi") and (record or doi_resolves(candidate)):
                out["doi"], sources["doi"] = candidate, f"filename ({via if record else 'doi.org'} confirmed)"
            if record or out.get("doi") == candidate:
                break
    for k, v in record.items():
        if v and (k not in out or sources.get(k) in ("pdf_meta", "filename")):
            out[k] = v
            sources[k] = via
    out["metadata_sources"] = sources
    return out


EXACT = ("doi", "pmid", "pmcid")


def merge(model_meta: Optional[dict], harvested: dict) -> dict:
    """model_meta: what the extractor wrote in source_metadata. Returns one dict with
    `metadata_sources` saying where every field came from."""
    model_meta = {k: v for k, v in (model_meta or {}).items() if v}
    src = dict(harvested.get("metadata_sources") or {})
    out = {k: v for k, v in harvested.items() if k != "metadata_sources"}
    for k, v in model_meta.items():
        if k in ("metadata_sources",):
            continue
        if k == "paper_title":
            k = "title"
        have = src.get(k)
        if k not in out:
            out[k], src[k] = v, "model"
        elif k in EXACT:
            continue  # the document's own identifier wins
        elif have in ("pdf_meta", "filename") and k in ("title", "authors", "year"):
            out[k], src[k] = v, "model"  # a reader beats PDF metadata / a file name
        # JATS and Crossref (the publisher's / registry's record) beat the model
    if out.get("title"):
        out["paper_title"] = out["title"]
        src["paper_title"] = src.get("title")
    if out.get("doi"):
        out["doi"] = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(out["doi"]), flags=re.I).rstrip(".")
    out["metadata_sources"] = src
    return out
