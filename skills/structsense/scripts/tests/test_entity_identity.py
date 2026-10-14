"""Behavioral regressions for identity, occurrence evidence and graph views."""
import copy
import sys
from pathlib import Path
import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS, XSD
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from json_to_ttl import result_to_ttl
from expand_mentions import expand
from relations import extracted_claims
from entity_view import entity_views
from group_by_entity import mention_groups
from validate_ttl import validate_file
NER = Namespace('https://brainkb.org/ner/')
PROV = Namespace('http://www.w3.org/ns/prov#')


def item(text, surface, label, occurrence=0, **fields):
    a = -1
    for _ in range(occurrence + 1): a = text.index(surface, a + 1)
    lo = text.rfind('. ', 0, a) + 2 if '. ' in text[:a] else 0
    hi = text.find('.', a)
    return dict(entity=surface, label=label, start=a, end=a+len(surface),
                sentence=text[lo:hi+1] if hi >= 0 else text[lo:], **fields)


def render(tmp_path, text, items, source='note:one', profile='compact', plan=None):
    path = tmp_path / (source.replace(':','_') + '.txt');path.write_text(text)
    ttl, report = result_to_ttl({'source_metadata':{'source_id':source}, 'entities':items},
                                source_path=path, profile=profile, kg_plan=plan)
    output=path.with_suffix('.ttl');output.write_text(ttl)
    graph=Graph().parse(data=ttl,format='turtle')
    return graph, output, report


def by_key(g,key):
    return g.value(predicate=NER.normalizedEntityKey,object=Literal(key))


@pytest.mark.parametrize('profile',['compact','full'])
def test_repetition_relations_and_views(tmp_path,profile):
    text='Alice works for Acme. Alice visited Paris. Alice works for Acme.'
    items=[item(text,'Alice','Person',i,identity_key='alice_example') for i in range(3)]
    items += [item(text,'Acme','Organization',i) for i in range(2)] + [item(text,'Paris','Location')]
    items[0]['relations']=[{'predicate':'employed_by','target':'Acme'}]
    items[1]['relations']=[{'predicate':'located_in','target':'Paris'}]
    items[2]['relations']=[{'predicate':'employed_by','target':'Acme'}]
    # Same occurrence seen in an overlapping chunk must not inflate mentions.
    items.append(copy.deepcopy(items[0]))
    g,p,report=render(tmp_path,text,items,profile=profile)
    alice=by_key(g,'alice_example')
    assert len(set(g.objects(alice,NER.hasMention)))==3
    assert len(set(g.subjects(RDF.type,NER.RelationAssertion)))==3
    assert len(set(g.objects(alice,NER.employedBy)))==1
    assert report['counts']['mentions']==6
    assert validate_file(p)['ok']
    view,index=entity_views(g)
    assert index['entity_count']==3 and index['mention_count']==6
    assert not list(view.subjects(RDF.type,NER.EntityClassification))
    assert len(next(x for x in index['entities'] if x['key']=='alice_example')['relations'])==3
    if profile=='full':
        for c in g.subjects(RDF.type,NER.EntityClassification):
            assert 'classification' in str(g.value(c,RDFS.label))


def test_global_identity_separate_source_evidence(tmp_path):
    text='Alice works for Acme.'
    items=[item(text,'Alice','Person',identity_key='alice_example',relations=[{'predicate':'employed_by','target':'Acme'}]),item(text,'Acme','Organization')]
    g1,_,_=render(tmp_path,text,items,source='note:one')
    g2,_,_=render(tmp_path,text,items,source='email:two')
    entity=by_key(g1,'alice_example')
    assert entity==by_key(g2,'alice_example')
    assert set(g1.objects(entity,NER.hasMention)).isdisjoint(g2.objects(entity,NER.hasMention))
    union=g1+g2
    assert len(set(union.objects(entity,PROV.hadPrimarySource)))==2
    assert len(set(union.subjects(RDF.type,NER.RelationAssertion)))==2
    p=tmp_path/'union.ttl';union.serialize(destination=p,format='turtle')
    assert validate_file(p)['ok']


def test_expansion_never_copies_context_and_keeps_later_relations():
    text='Alice works for Acme. Alice visited Paris. Alice rested.'
    seeds=[item(text,'Alice','Person',relations=[{'predicate':'employed_by','target':'Acme'}]),
           item(text,'Alice','Person',1,relations=[{'predicate':'located_in','target':'Paris'}])]
    out,_=expand({'entities':seeds},text)
    assert len(out)==3
    assert out[0]['relations'][0]['target']=='Acme'
    assert out[1]['relations'][0]['target']=='Paris'
    assert not out[2].get('relations')


def test_abbreviation_and_local_homonyms(tmp_path):
    text='Alpha Institute (AI) hired Jordan. Jordan left AI.'
    items=[item(text,'Alpha Institute','Organization',identity_key='alpha_institute'),item(text,'AI','Organization',0,identity_key='alpha_institute'),item(text,'AI','Organization',1,identity_key='alpha_institute'),
           item(text,'Jordan','Person',0,referent_id='jordan_a'),item(text,'Jordan','Person',1,referent_id='jordan_b')]
    assert len(mention_groups({'entities':items}))==4
    g,_,_=render(tmp_path,text,items)
    assert len(set(g.objects(by_key(g,'alpha_institute'),NER.hasMention)))==3
    people=set(g.subjects(RDF.type,NER.Person));assert len(people)==2
    assert len(set(g.subjects(RDF.type,NER.NamedEntity)))==3


def test_ambiguous_relation_target_is_not_resolved_by_frequency():
    text='Jordan met Jordan. Acme hired Jordan.'
    items=[item(text,'Jordan','Person',0,referent_id='one'),item(text,'Jordan','Person',1,referent_id='two'),
           item(text,'Acme','Organization',relations=[{'predicate':'employed_by','target':'Jordan'}])]
    report=extracted_claims({'entities':items})
    assert not report['relations'] and report['unresolved']


@pytest.mark.parametrize('qualifier',[{'negated':True},{'modality':'possible'},{'condition':'if funded'},{'time':'2020'}])
def test_qualified_assertions_do_not_become_unconditional_edges(tmp_path,qualifier):
    text='Alice works for Acme.'
    items=[item(text,'Alice','Person',relations=[{'predicate':'employed_by','target':'Acme',**qualifier}]),item(text,'Acme','Organization')]
    g,p,_=render(tmp_path,text,items)
    assert not list(g.triples((None,NER.employedBy,None)))
    assert len(set(g.subjects(RDF.type,NER.RelationAssertion)))==1
    assert validate_file(p)['ok']


def test_unstructured_source_is_not_forced_to_be_publication(tmp_path):
    text='Acme announced a launch.'
    g,p,_=render(tmp_path,text,[item(text,'Acme','Organization')])
    assert len(set(g.subjects(RDF.type,NER.SourceDocument)))==1
    assert not list(g.subjects(RDF.type,NER.Publication))
    assert validate_file(p)['ok']


def test_ungrounded_plan_relation_is_not_emitted(tmp_path):
    text='Alice visited Acme.'
    items=[item(text,'Alice','Person'),item(text,'Acme','Organization')]
    plan={'entities':{'Acme|Organization':{'normalized_key':'acme'},'Alice|Person':{'normalized_key':'alice','relations':[{'predicate':'employed_by','target_key':'acme','evidence':'Alice works for Acme.'}]}}}
    g,_,report=render(tmp_path,text,items,plan=plan)
    assert not list(g.subjects(RDF.type,NER.RelationAssertion))
    assert any('ungrounded' in x for x in report['warnings'])


def test_shacl_rejects_duplicate_entity_identity(tmp_path):
    text='Acme opened.'
    g,p,_=render(tmp_path,text,[item(text,'Acme','Organization',identity_key='acme')])
    original=by_key(g,'acme');other=Namespace('https://brainkb.org/kb/')['00000000-0000-5000-8000-000000000099']
    for pred,obj in list(g.predicate_objects(original)):g.add((other,pred,obj))
    g.serialize(destination=p,format='turtle')
    report=validate_file(p)
    assert not report['ok']
    assert any('UniqueEntityPerSourceShape' in k for k in report['violations'])


def test_explicit_global_id_disambiguates_same_name(tmp_path):
    text='Jordan greeted Jordan.'
    items=[item(text,'Jordan','Person',0,identity_key='jordan_lee'),item(text,'Jordan','Person',1,identity_key='jordan_kim')]
    g,_,_=render(tmp_path,text,items)
    assert by_key(g,'jordan_lee') != by_key(g,'jordan_kim')
    assert len(set(g.subjects(RDF.type,NER.NamedEntity)))==2


def test_mention_iri_is_stable_across_extraction_variants(tmp_path):
    text='Acme opened.';source=tmp_path/'note.txt';source.write_text(text)
    r={'source_metadata':{'source_id':'note:one'},'entities':[item(text,'Acme','Organization')]}
    graphs=[]
    for variant in ['ner:general','ner:custom']:
        ttl,_=result_to_ttl(r,source_path=source,variant=variant)
        graphs.append(Graph().parse(data=ttl,format='turtle'))
    assert set(graphs[0].subjects(RDF.type,NER.EntityMention)) == set(graphs[1].subjects(RDF.type,NER.EntityMention))
    p=tmp_path/'variants.ttl';(graphs[0]+graphs[1]).serialize(destination=p,format='turtle')
    assert validate_file(p)['ok']


def test_gene_route_does_not_fall_back_to_proteins_and_rs_not_misexpanded():
    from concept_mapping import ConceptMapper,load_config,abbreviation_table
    mapper=ConceptMapper(load_config(),sources=[])
    assert mapper.remote_route('Gene') == []
    assert 'Rs' not in abbreviation_table(['series resistance (Rs) was measured.'])


def test_bracketed_sections_are_preserved():
    text='[ABSTRACT]\nAcme opened.\n[RESULTS]\nAcme closed.'
    out,_=expand({'entities':[item(text,'Acme','Organization')]},text)
    assert [x['paper_location'] for x in out]==['Abstract','Results']


def test_unresolved_same_name_does_not_assert_cross_source_identity(tmp_path):
    text='Jordan arrived.';items=[item(text,'Jordan','Person')]
    g1,_,_=render(tmp_path,text,items,source='note:one')
    g2,_,_=render(tmp_path,text,items,source='note:two')
    assert set(g1.subjects(RDF.type,NER.NamedEntity)).isdisjoint(g2.subjects(RDF.type,NER.NamedEntity))


def test_resolved_aliases_share_iri_across_sources(tmp_path):
    g1,_,_=render(tmp_path,'Alice arrived.',[item('Alice arrived.','Alice','Person',identity_key='alice_lee')],source='note:one')
    g2,_,_=render(tmp_path,'A. Lee arrived.',[item('A. Lee arrived.','A. Lee','Person',identity_key='alice_lee')],source='note:two')
    assert by_key(g1,'alice_lee')==by_key(g2,'alice_lee')
    p=tmp_path/'aliases.ttl';(g1+g2).serialize(destination=p,format='turtle')
    assert validate_file(p)['ok']
