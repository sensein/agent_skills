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

**Dataset with consent scope**
```turtle
:dataset/abide_i a dcat:Dataset ; nkg:knowledgeScope "lake" ;
    nkg:sensitivity [ a nkg:SensitivityProfile ; nkg:consentScope "broad-sharing" ] .
```
