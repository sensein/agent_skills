# Modeling patterns (Turtle)

Prefixes: `prov:` W3C PROV, `nidm:` NIDM, `ndar:` NDA data dictionary, `nkg:` this skill
(`urn:neuroimaging-kg:`), `crypto:` hash vocabulary, `dcat:` datasets, `dct:` Dublin Core.

**Participant (as PyNIDM writes it)**
```turtle
niiri:p1 a prov:Agent, prov:Person ; ndar:src_subject_id "51456" .
```

**A value with its variable definition** — the per-file element IRI points to a shared concept.
```turtle
niiri:FIQ_x1 a nidm:PersonalDataElement ; nidm:sourceVariable "FIQ" ;
    nidm:isAbout <http://uri.interlex.org/base/ilx_0739365> .     # full-scale IQ
niiri:ao1 a nidm:AcquisitionObject ; prov:wasGeneratedBy niiri:acq1 ; niiri:FIQ_x1 "112.0"^^xsd:float .
niiri:acq1 prov:qualifiedAssociation [ prov:agent niiri:p1 ; prov:hadRole sio:Subject ] .
```

**An analysis step (scripts/record_activity.py writes this)**
```turtle
:activity/motion-cohort a prov:Activity ; rdfs:label "Keep participants with mean FD < 0.2 mm" ;
    prov:used <urn:sha256:c4c8…> ; prov:wasAssociatedWith :software/build/0.1 , :agent/Analyst-A ;
    nkg:parameter [ rdfs:label "mean_fd_max_mm" ; nkg:value "0.2" ] .
:cohort/x prov:wasGeneratedBy :activity/motion-cohort ; prov:wasDerivedFrom <urn:sha256:c4c8…> .
```

**Cohort**
```turtle
:cohort/x a nkg:Cohort, prov:Collection ; nkg:rule "func_mean_fd < 0.2" ; nkg:n 38 ;
    prov:hadMember niiri:p1 , niiri:p2 .
```

**Identity link (scripts/join_identity.py writes this)** — never owl:sameAs.
```turtle
:identity/51456 a nkg:IdentityLink ; nkg:localId "51456" ; nkg:externalSubject niiri:ea07af80… ;
    nkg:reviewStatus "proposed" ; prov:wasGeneratedBy :activity/identity-join .
```

**Claim with scope and evidence**
```turtle
:claim/c1 a nkg:Claim ; nkg:claimType "predictive" ;
    nkg:population "ABIDE I, 871 of 1,112 participants after quality control" ;
    nkg:measure "classification accuracy" ; nkg:evaluation "leave-site-out" ;
    nkg:usesDataset :dataset/abide_i ; dct:source <https://doi.org/10.1016/j.neuroimage.2016.10.045> ;
    nkg:evidence [ a nkg:EvidenceSpan ; nkg:quote "…verbatim…" ; nkg:source <https://arxiv.org/abs/1611.06066v1> ] .
```

**Brain volumes in ReproNim NIDM** (values in the site file; meaning only in the tool's CDE file)
```turtle
# site nidm.ttl
niiri:act a prov:Activity ;
    prov:qualifiedAssociation [ prov:agent niiri:p1 ; prov:hadRole sio:Subject ] ,        # niiri:p1 ndar:src_subject_id "51456"
                              [ prov:agent niiri:fs ; prov:hadRole nidm:NIDM_0000164 ] . # niiri:fs nidm:NIDM_0000164 fs:
niiri:ent prov:wasGeneratedBy niiri:act ; fs:fs_003343 4513.9 .
# freesurfer_cde.ttl
fs:fs_003343 rdfs:label "Left-Hippocampus Volume_mm3 (mm^3)" ; nidm:isAbout obo:UBERON_0001954 ;
    nidm:hasLaterality "Left" ; nidm:hasUnit "mm^3" .
```
SPARQL (load site file and CDE files; patterns outside `GRAPH {}` need the union default graph):
```sparql
SELECT ?id ?tool ?side ?v WHERE {
  ?s ndar:src_subject_id ?id . ?act prov:qualifiedAssociation ?a1 , ?a2 .
  ?a1 prov:agent ?sw . ?sw nidm:NIDM_0000164 ?tool . ?a2 prov:agent ?s .
  ?e prov:wasGeneratedBy ?act ; ?m ?v .
  ?m nidm:isAbout <http://purl.obolibrary.org/obo/UBERON_0001954> ; nidm:hasUnit "mm^3" ; nidm:hasLaterality ?side .
}
```

**Dataset with consent scope**
```turtle
:dataset/abide_i a dcat:Dataset ; nkg:knowledgeScope "lake" ;
    nkg:sensitivity [ a nkg:SensitivityProfile ; nkg:consentScope "broad-sharing" ] .
```

**A pond record and an attempt to promote it to the lake** (validate_graph.py must fail this)
```turtle
@prefix nkg: <urn:neuroimaging-kg:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:neuroimaging-kg:lab:record/clinician_notes> rdfs:label "Clinician notes table" ;
    nkg:knowledgeScope "pond" ;
    nkg:sensitivity <urn:neuroimaging-kg:lab:record/clinician_notes/sensitivity> ;
    nkg:promotionStatus "promoted" .
<urn:neuroimaging-kg:lab:record/clinician_notes/sensitivity> a nkg:SensitivityProfile ;
    nkg:consentScope "study-only" .
```
Check that hand-written Turtle parses before validating:
`uv run --with rdflib python -c "import rdflib,sys; rdflib.Graph().parse(sys.argv[1])" file.ttl`.
