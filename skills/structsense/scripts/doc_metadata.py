"""Bibliographic metadata read from the document itself — deterministic, no LLM.

The TTL's ner:Publication carries title, DOI, PMID/PMCID, date, journal and authors
(prov:wasAttributedTo / dcterms:creator). A model reading chunk 1 can fill some of
that, but the document usually states it exactly, so read it first:

  jats      JATS/PMC XML front matter: article-title, article-id (doi / pmid / pmc),
            contrib-group authors (with ORCID), journal-title, pub-date
  pdf_meta  the PDF's own metadata (title / author / subject / doi keys) — used only
            when plausible (not "Microsoft Word - ...", not a file name)
  text      the first DOI printed in the text (front matter first), "PMID: n"
  filename  a leading PubMed id and a _YYYY_ year ("37731775_de_2023_Shared_...")

`harvest(path, text)` returns the fields plus `metadata_sources` ({field: source}).
`merge(model_meta, harvested)` combines them: exact identifiers (doi, pmid, pmcid)
prefer the document over the model; title and authors prefer JATS, then the model,
then PDF metadata; nothing is ever invented.
"""
from __future__ import annotations

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
        doi = m.group(1).rstrip(".")
        if not DATA_DOI.match(doi):
            out["doi"] = doi
            break
    m = PMID_RE.search(head)
    if m:
        out["pmid"] = m.group(1)
    m = PMCID_RE.search(head)
    if m:
        out["pmcid"] = m.group(1)
    return out


def from_filename(path: Path) -> dict:
    out: dict[str, Any] = {}
    stem = path.stem
    m = re.match(r"^(\d{7,9})_", stem)
    if m:
        out["pmid"] = m.group(1)
    m = YEAR_RE.search(stem)
    if m:
        out["year"] = m.group(1)
    return out


def harvest(path: Path, text: str = "") -> dict:
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
    if out.get("title"):
        out["paper_title"] = out["title"]
        src["paper_title"] = src.get("title")
    if out.get("doi"):
        out["doi"] = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", str(out["doi"]), flags=re.I).rstrip(".")
    out["metadata_sources"] = src
    return out
