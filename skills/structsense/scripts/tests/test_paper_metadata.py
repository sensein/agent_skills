"""The resource graph names its source paper by its real identity (DOI, or a
preprint id), with full bibliographic metadata, and records the processed text as
a version of the paper — never the extracted text file as if it were the paper."""
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

import doc_metadata as dm
from resource_kg import build, validate_graph
from test_resource_kg import record, res

NER = Namespace("https://brainkb.org/ner/")
DCT = Namespace("http://purl.org/dc/terms/")
PROV = Namespace("http://www.w3.org/ns/prov#")

TEXT = ("Striatal cell atlas across species\n"
        "Ada Lovelace, Grace Hopper\n"
        "The copyright holder for this preprint is the author/funder. It is made available "
        "under aCC-BY-NC-ND 4.0 International license.\n"
        "We introduce CellMapper, a Python package for mapping single-cell profiles onto a "
        "reference atlas. CellMapper version 2.1.0 is available at https://github.com/example/cellmapper "
        "under the MIT license. CellMapper requires raw integer counts as input. "
        "We benchmarked CellMapper on mouse cortex, where it reached an accuracy of 0.93. "
        "On human tissue it was not evaluated. When the reference lacks a cell type, CellMapper "
        "silently assigns the nearest type. We used Scanpy for preprocessing.")


def preprint(tmp_path):
    txt = tmp_path / "2026.02.03.703645v1_text.txt"
    txt.write_text(TEXT, encoding="utf-8")
    (tmp_path / "2026.02.03.703645v1.pdf").write_bytes(b"%PDF-1.4 fake")
    result = res(record(), meta={})
    result["paper"] = {"title": "Striatal cell atlas across species",
                       "authors": [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}, {"name": "Invented Person"}],
                       "doi": "10.9999/not.in.text"}
    return txt, result


def publication(g):
    return next(g.subjects(RDF.type, NER.Publication))


def test_unregistered_preprint_is_named_by_its_id_not_its_text_file(tmp_path):
    txt, result = preprint(tmp_path)
    ttl, rep = build(result, source_path=txt)
    g = Graph().parse(data=ttl, format="turtle")
    pub = publication(g)
    assert str(g.value(pub, NER.sourceIdentifier)) == "preprint:2026.02.03.703645"
    assert str(g.value(pub, NER.title)) == "Striatal cell atlas across species"
    assert g.value(pub, NER.doi) is None                                   # a DOI not printed is not invented
    assert (pub, DCT.license, URIRef("https://creativecommons.org/licenses/by-nc-nd/4.0/")) in g
    authors = {str(g.value(a, RDFS.label)) for a in g.objects(pub, DCT.creator)}
    assert authors == {"Ada Lovelace", "Grace Hopper"}                    # "Invented Person" is not in the text
    assert any("dropped" in w for w in rep["warnings"])
    assert validate_graph(g)["ok"]


def test_processed_text_is_a_version_derived_from_the_pdf(tmp_path):
    txt, result = preprint(tmp_path)
    g = Graph().parse(data=build(result, source_path=txt)[0], format="turtle")
    pub = publication(g)
    versions = {str(g.value(v, RDFS.label)): v for v in g.objects(pub, NER.hasDocumentVersion)}
    assert set(versions) == {"source document", "processed text"}
    text, pdf = versions["processed text"], versions["source document"]
    assert (text, PROV.wasDerivedFrom, pdf) in g
    assert str(g.value(text, NER.sourcePath)) == txt.name and str(g.value(text, NER.mediaType)) == "text/plain"
    assert str(g.value(pdf, NER.mediaType)) == "application/pdf"
    assert len(str(g.value(text, NER.checksum))) == 64
    assert g.value(pub, Namespace("https://brainkb.org/resource/").mediaType) is None   # a file's type, not the paper's


def test_text_that_exists_only_in_memory_is_preserved(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "paper.source.txt"
    ttl, rep = build(res(record(), meta={}), source_path=pdf, source_text=TEXT, text_out=out)
    assert out.read_text(encoding="utf-8") == TEXT
    g = Graph().parse(data=ttl, format="turtle")
    names = {str(g.value(v, NER.sourcePath)) for v in g.objects(publication(g) if list(g.subjects(RDF.type, NER.Publication))
                                                                 else next(g.subjects(RDF.type, NER.SourceDocument)), NER.hasDocumentVersion)}
    assert names == {"paper.pdf", "paper.source.txt"}


def test_doi_printed_in_the_text_is_the_identity():
    h = dm.harvest(Path("paper_text.txt"), "bioRxiv preprint doi: https://doi.org/10.1101/2026.02.03.703645; this version")
    assert h["doi"] == "10.1101/2026.02.03.703645" and h["metadata_sources"]["doi"] == "text"


def test_filename_preprint_id_becomes_a_doi_only_when_confirmed():
    assert "doi" not in dm.harvest(Path("2026.02.03.703645v1_text.txt"), "")     # registries stubbed: unconfirmed
    assert dm.harvest(Path("2026.02.03.703645v1_text.txt"), "")["preprint_id"] == "2026.02.03.703645"


def test_crossref_and_biorxiv_records_are_parsed():
    crossref = {"message": {"title": ["A cell atlas"], "type": "posted-content",
                            "author": [{"given": "Ada", "family": "Lovelace", "ORCID": "https://orcid.org/0000-0001-2345-6789"}],
                            "posted": {"date-parts": [[2026, 2, 3]]}, "institution": [{"name": "bioRxiv"}],
                            "license": [{"URL": "http://creativecommons.org/licenses/by/4.0/", "content-version": "vor"}]}}
    out = dm._real_from_crossref("10.1101/x", fetch=lambda _u: crossref)
    assert out == {"title": "A cell atlas", "authors": [{"name": "Ada Lovelace", "orcid": "0000-0001-2345-6789"}],
                   "year": "2026", "publication_date": "2026-02-03", "journal": "bioRxiv", "source_type": "preprint",
                   "license": "http://creativecommons.org/licenses/by/4.0/"}
    biorxiv = {"collection": [{"title": "A cell atlas", "authors": "Lovelace, A.; Hopper, G.", "date": "2026-02-03",
                               "license": "cc_by_nc_nd"}]}
    out = dm._real_from_biorxiv("10.1101/x", fetch=lambda _u: biorxiv)
    assert [a["name"] for a in out["authors"]] == ["A. Lovelace", "G. Hopper"]
    assert out["license"] == "https://creativecommons.org/licenses/by-nc-nd/4.0/" and out["journal"] == "bioRxiv"
