"""Semantic regressions for global identity, source provenance and compact coverage."""
from pathlib import Path
import tempfile
import unittest
import rdflib
from run_cqs import load_cqs, parameterize, discover

PREFIX='''@prefix ner: <https://brainkb.org/ner/> .
@prefix ex: <https://example.org/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix obo: <http://purl.obolibrary.org/obo/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
'''
DATA='''
ex:cell a ner:NamedEntity, ner:CellType ; ner:normalizedEntityKey "cell" ; ner:normalizedEntityLabel "Cell" ;
 prov:hadPrimarySource ex:a, ex:b ; ner:hasMention ex:m1, ex:m2, ex:m3 ; ner:resolvedToConcept ex:fallback ;
 obo:RO_0001025 ex:region ; obo:RO_0002292 ex:marker .
ex:a a ner:SourceDocument ; ner:publicationDate "2020-01-01"^^xsd:date .
ex:b a ner:SourceDocument ; dct:issued "2021"^^xsd:gYear .
ex:va ner:versionOfDocument ex:a . ex:vb ner:versionOfDocument ex:b .
ex:m1 ner:partOfDocumentVersion ex:va ; ner:surfaceForm "cell" .
ex:m2 ner:partOfDocumentVersion ex:va ; ner:surfaceForm "cells" .
ex:m3 ner:partOfDocumentVersion ex:vb ; ner:surfaceForm "cell" .
ex:fallback ner:conceptInOntologyVersion ex:ov . ex:ov ner:versionOfOntology ex:brainkb .
ex:brainkb ner:ontologyAcronym "BRAINKB" .
ex:region ner:normalizedEntityKey "region" . ex:marker ner:normalizedEntityKey "marker" .
ex:ra a ner:RelationAssertion ; ner:assertionSubject ex:cell ; ner:assertionPredicate obo:RO_0001025 ;
 ner:assertionObject ex:region ; prov:hadPrimarySource ex:a ; ner:assertionNegated true .
ex:rm a ner:RelationAssertion ; ner:assertionSubject ex:cell ; ner:assertionPredicate obo:RO_0002292 ;
 ner:assertionObject ex:marker ; prov:hadPrimarySource ex:b ; ner:evidenceText "cells express marker" .
ex:phen a ner:NamedEntity, ner:Phenotype, ner:CellularPhenotype ; ner:normalizedEntityKey "phen" ;
 skos:narrowMatch obo:HP_1 ; skos:exactMatch obo:CL_1 .
ex:other a ner:NamedEntity ; ner:normalizedEntityKey "other" ; ner:resolvedToConcept ex:ec .
ex:ec ner:conceptInOntologyVersion ex:ev . ex:ev ner:versionOfOntology ex:eo . ex:eo ner:ontologyAcronym "EFO" .
'''
class Queries(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.q = {cq: body for cq, _, body in load_cqs(Path(__file__).with_name('named_entity_ontology_CQs.md'))}
 def setUp(self): self.g = rdflib.Graph(bind_namespaces='none').parse(data=PREFIX+DATA,format='turtle')
 def runq(self, cq, **params): return [r.asdict() for r in self.g.query(parameterize(self.q[cq],params))]
 def test_all_queries_parse_and_execute(self):
  for cq in self.q:
   with self.subTest(cq=cq): self.runq(cq)
 def test_mentions_are_source_scoped(self):
  rows=self.runq('CQ24');counts={str(r['pub']):int(r['mentions']) for r in rows}
  self.assertEqual(counts,{'https://example.org/a':2,'https://example.org/b':1})
  self.assertTrue(all(int(r['entities'])==1 for r in rows))
 def test_no_doi_sources_and_parameters(self):
  self.assertEqual(len(self.runq('CQ4')),2)
  self.assertEqual(len(self.runq('CQ2',requestedKey='cell')),3)
  self.assertEqual(self.runq('CQ2',requestedKey='absent'),[])
  self.assertEqual(self.runq('CQ2',requestedKey='x" } UNION { ?s ?p ?o } #'),[])
 def test_relation_source_not_entity_source(self):
  rows=self.runq('CQ25');self.assertEqual(len(rows),1)
  self.assertEqual(str(rows[0]['pub']),'https://example.org/a')
  self.assertEqual(rows[0]['negated'].toPython(),True)
  rows=self.runq('CQ55');self.assertEqual(len(rows),1)
  self.assertEqual(str(rows[0]['pub']),'https://example.org/b')
 def test_provisional_is_external_coverage_gap(self):
  keys={str(r['key']) for r in self.runq('CQ10')}
  self.assertIn('cell',keys);self.assertNotIn('other',keys)
  self.assertEqual(int(self.runq('CQ38')[0]['mentions']),3)
 def test_normalized_year(self):
  row=next(r for r in self.runq('CQ17') if str(r['key'])=='cell')
  self.assertEqual(int(row['first']),2020);self.assertEqual(int(row['papers']),2)
 def test_phenotype_tiers_no_multitype_inflation(self):
  rows=self.runq('CQ52');self.assertEqual(len(rows),1)
  self.assertEqual(str(rows[0]['vocab']),'HP');self.assertEqual(str(rows[0]['tier']),'narrow');self.assertEqual(int(rows[0]['n']),1)
 def test_mapping_metadata_and_ontology_parameter(self):
  self.g.parse(data=PREFIX + '\nex:other skos:narrowMatch <http://www.ebi.ac.uk/efo/EFO_1> . ex:ec ner:conceptIdentifier "EFO:1" ; ner:conceptIRI "http://www.ebi.ac.uk/efo/EFO_1"^^xsd:anyURI .',format='turtle')
  rows=self.runq('CQ47',requestedOntology='EFO');self.assertEqual(len(rows),1)
  self.assertEqual(str(rows[0]['tier']),'narrow')
  self.assertEqual(self.runq('CQ47',requestedOntology='CL'),[])
 def test_compact_mention_does_not_inherit_document_run(self):
  self.g.parse(data=PREFIX + '\nex:run prov:used ex:va ; ner:runIdentifier "run" ; prov:wasAssociatedWith ex:agent . ex:agent <http://www.w3.org/2000/01/rdf-schema#label> "model" .',format='turtle')
  self.assertTrue(all('runId' not in row for row in self.runq('CQ22')))
 def test_full_profile_accepted_mapping_and_report(self):
  self.g.parse(data=PREFIX + '\nex:d1 a ner:ConceptMappingDecision ; ner:mappingStatus <https://brainkb.org/ner/mapping-status/accepted> ; prov:wasGeneratedBy ex:act . ex:d2 a ner:ConceptMappingDecision ; ner:mappingStatus <https://brainkb.org/ner/mapping-status/rejected> ; prov:wasGeneratedBy ex:act . ex:act <http://www.w3.org/2000/01/rdf-schema#label> "mapper" . ex:snapshot a ner:ExtractionSnapshot ; ner:hasValidationReport ex:report . ex:report <http://www.w3.org/2000/01/rdf-schema#comment> "2 fixed" .',format='turtle')
  self.assertEqual(int(self.runq('CQ45')[0]['decisions']),1)
  self.assertEqual(str(self.runq('CQ46')[0]['summary']),'2 fixed')
 def test_discovery_excludes_projection_invalid_hidden(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   for name in ['a.ttl','a.entities.ttl','a.invalid.ttl']: (root/name).write_text('')
   (root/'.hidden').mkdir();(root/'.hidden'/'b.ttl').write_text('')
   self.assertEqual(discover([root,root/'a.ttl']),[(root/'a.ttl').resolve()])
 def test_term_parameter_rejects_injection(self):
  with self.assertRaises(ValueError): parameterize(self.q['CQ18'],{'requestedTerm':'http://x> ?s ?p ?o'})
if __name__=='__main__': unittest.main()
