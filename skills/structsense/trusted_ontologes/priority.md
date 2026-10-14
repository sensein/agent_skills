# Trusted ontology priority

This table is the **single source of priority** for concept mapping against the
trusted ontology files in this directory. `scripts/concept_mapping.py` reads it on
every run, so edit it here — no code or JSON change is needed.

- **Priority**: 1 is consulted first. With the default `strategy: "priority"`
  (concept_mapping.json), the highest-priority ontology that has an accepted match
  for a term wins, even if a lower-priority one matches more strongly. Write `off`
  to disable a row without deleting it.
- **Name**: short id, used in provenance (`mapping_source: "trusted:<name>"`) and
  in the index cache.
- **File**: file name in this directory (`.owl` / `.rdf` / `.xml` RDF/XML, or
  `.ttl` / `.nt` / `.jsonld` — anything rdflib reads).
- **CURIE prefix**: prefix for this file's own non-OBO terms. `-` for an OBO file:
  OBO terms always keep their own prefix (cl.owl's UBERON terms are UBERON).
- **Namespace**: the IRI namespace(s) this file's own terms live in,
  comma-separated (e.g. `https://identifiers.org/brain-bican/vocab/`); `-` = the
  file's dominant non-OBO namespace. Only terms in it get this row's CURIE prefix.
  OBO terms and namespaces registered in `default_ontology/ttl_config.json`
  (`curie_expansions`) keep THEIR prefix whichever file they come from, and a term
  in any other namespace is left unregistered (indexed, never mapped) — so one
  prefix always means one namespace. `python -m scripts.prefixes check` lists
  conflicts; `concept_mapping index` reports unregistered namespaces per file.
- **Labels**: extractor labels this ontology may answer, comma-separated, or `*`
  for any. A label listed under `label_routing` in concept_mapping.json is also
  restricted to that route's id prefixes (plus `always_allowed_prefixes`).
- **Notes**: free text (source, version, why it is trusted).

To add an ontology: drop the file in this directory, add a row, and run
`python -m scripts.concept_mapping index` (builds the cached index once), then
`python -m scripts.concept_mapping show` to check the effective order.

| Priority | Name | File | CURIE prefix | Namespace | Labels | Notes |
|---|---|---|---|---|---|---|
| 1 | bke | bke_taxonomy.ttl | BKE | https://identifiers.org/brain-bican/vocab/ | * | BICAN knowledge-extraction taxonomy. First priority. |
| 2 | cl | cl.owl | - | - | * | Full Cell Ontology release (with imports and symbols). |
| 3 | go_basic | go-basic.owl | - | - | * | Biological process, molecular function and cellular component. The basic edition is acyclic and omits cross-aspect relationships; its versio… |
| 4 | uberon | uberon.owl | - | - | * | Cross-species anatomy including nervous-system structures. The full edition, replacing the basic edition: it carries the axioms the basic pr… |
| off | cl_basic | cl-basic.owl | - | - | * | OFF: subset of cl.owl, which is enabled. Cell types including neurons and glia. |
| 5 | pcl | pcl.owl | - | - | * | Provisional cell populations, for cell-atlas integration. No reduced edition is published. |
| 6 | mondo | mondo-simple.owl | - | - | * | Disease identifiers including neurological disease. |
| 7 | hp | hp-base.owl | - | - | * | Human phenotypic abnormalities including neurological findings. |
| 8 | chebi | chebi-lite.owl | - | - | * | Chemical grounding. The lite edition carries the class hierarchy without chemical data properties. |
| 9 | ncbitaxon | ncbitaxon-taxslim.owl | - | - | * | Species. The slim subset is taken because the full taxonomy is far larger than anything this corpus references. |
| 10 | nbo | nbo.owl | - | - | * | Behavioural processes and behavioural phenotypes. |
| 11 | mf | mf.owl | - | - | * | Mental functioning. |
| 12 | mfoem | mfoem.owl | - | - | * | Emotion. MFOEM is the Emotion Ontology; EMO is Enzyme Mechanism Ontology and is not this. |
| 13 | mfomd | mfomd.owl | - | - | * | Mental disease. |
| 14 | mp | mp.owl | - | - | * | Mammalian phenotype, including neural phenotypes in model organisms. |
| 15 | fbbt | fbbt.owl | - | - | * | Drosophila anatomy, including nervous-system structures. |
| 16 | wbbt | wbbt.owl | - | - | * | C. elegans anatomy, including its nervous system. |
| 17 | zfa | zfa.owl | - | - | * | Zebrafish anatomy, including neural structures. |
| 18 | xao | xao.owl | - | - | * | Xenopus anatomy, including neural structures. |
| 19 | pr | pr.owl | - | - | * | Protein Ontology: neural markers, receptors and channels. 1.2 GB, no reduced edition published; the download needed curl -C - to resume afte… |
| 20 | nifstd | nifstd-sub27.ttl | NIFSTD | - | * | Neuroscience Information Framework Standard Ontology. BioPortal submission 27, released 2018-02-10, pinned because an acronym alone does not… |
| 21 | npokb | npokb-sub22.ttl | NPOKB | - | * | Neuron Phenotype Ontology. BioPortal submission 22, released 2026-03-19, pinned because an acronym alone does not identify a release. Neuron… |
| 22 | aba_amb | aba-amb-sub1.owl | ABA-AMB | - | * | Allen Brain Atlas Adult Mouse Brain Ontology. BioPortal submission 1, released 2009-06-12, pinned because an acronym alone does not identify… |
| 23 | nmobr | nmobr-sub36.owl | NMOBR | - | * | NeuroMorpho.Org Brain Region Ontologies. BioPortal submission 36, released 2019-11-22, pinned because an acronym alone does not identify a r… |
| 24 | brct | brct-sub1.owl | BRCT | - | * | Brain Region and Cell Type Terminology. BioPortal submission 1, released 2015-06-17, pinned because an acronym alone does not identify a rel… |
| 25 | brain_cof | brain-cof-sub2.owl | BRAIN-COF | - | * | Computational Ontology Framework for Brain Profiles. BioPortal submission 2, released 2024-04-08, pinned because an acronym alone does not i… |
| 26 | cogat | cogat-sub8.owl | COGAT | http://www.cognitiveatlas.org/ontology/cogat.owl# | * | Cognitive Atlas Ontology. BioPortal submission 8, released 2012-04-03, pinned because an acronym alone does not identify a release. |
| 27 | cogpo | cogpo-sub1.owl | COGPO | - | * | Cognitive Paradigm Ontology. BioPortal submission 1, released 2010-11-17, pinned because an acronym alone does not identify a release. |
| 28 | cno | cno-sub8.owl | CNO | - | * | Computational Neuroscience Ontology. BioPortal submission 8, released 2015-11-16, pinned because an acronym alone does not identify a releas… |
| 29 | nemo | nemo-sub24.owl | NEMO | - | * | Neural ElectroMagnetic Ontologies. BioPortal submission 24, released 2013-10-21, pinned because an acronym alone does not identify a release… |
| 30 | nidm_results | nidm-results-sub2.ttl | NIDM-RESULTS | - | * | Neuroimaging Data Model - Results. BioPortal submission 2, released 2016-06-07, pinned because an acronym alone does not identify a release.… |
| 31 | neurobrg | neurobrg-sub2.owl | NEUROBRG | - | * | NeuroBridge Ontology. BioPortal submission 2, released 2023-06-21, pinned because an acronym alone does not identify a release. |
| 32 | onvoc | onvoc-sub9.ttl | ONVOC | - | * | OpenNeuro Vocabulary. BioPortal submission 9, released 2025-10-17, pinned because an acronym alone does not identify a release. Catalogued a… |
| 33 | bci_o | bci-o-sub6.owl | BCI-O | - | * | Brain-Computer Interaction Ontology. BioPortal submission 6, released 2018-06-06, pinned because an acronym alone does not identify a releas… |
| 34 | ado | ado-sub4.owl | ADO | - | * | Alzheimer's Disease Ontology. BioPortal submission 4, released 2023-09-09, pinned because an acronym alone does not identify a release. |
| 35 | pdon | pdon-sub1.owl | PDON | - | * | Parkinson's Disease Ontology. BioPortal submission 1, released 2015-06-17, pinned because an acronym alone does not identify a release. OWL/… |
| 36 | pmdo | pmdo-sub4.owl | PMDO | - | * | Parkinson and Movement Disorder Ontology. BioPortal submission 4, released 2023-12-12, pinned because an acronym alone does not identify a r… |
| 37 | epso | epso-sub8.owl | EPSO | - | * | Epilepsy and Seizure Ontology. BioPortal submission 8, released 2025-05-29, pinned because an acronym alone does not identify a release. |
| 38 | neo | neo-sub90.owl | NEO | - | * | Neurologic Examination Ontology. BioPortal submission 90, released 2022-07-05, pinned because an acronym alone does not identify a release. … |
| 39 | nddo | nddo-sub10.owl | NDDO | - | * | Neurodegenerative Disease Data Ontology. BioPortal submission 10, released 2019-06-03, pinned because an acronym alone does not identify a r… |
| 40 | nddrfo | nddrfo-sub3.owl | NDDRFO | - | * | Neurodegenerative Disease Risk Factor Ontology. BioPortal submission 3, released 2025-12-07, pinned because an acronym alone does not identi… |
| 41 | nihss | nihss-sub15.owl | NIHSS | - | * | National Institutes of Health Stroke Scale Ontology. BioPortal submission 15, released 2024-07-17, pinned because an acronym alone does not … |
| 42 | scio | scio-sub2.owl | SCIO | - | * | Spinal Cord Injury Ontology. BioPortal submission 2, released 2017-09-19, pinned because an acronym alone does not identify a release. |
| off | ad_cdo | ad-cdo-sub2.owl | AD-CDO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Alzheimer's Disease Common Data Element Ontology for Clinical Trials |
| off | ad_drop | ad-drop-sub1.owl | AD-DROP | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Alzheimer Disease Relevance Ontology by Process |
| off | adar | adar-sub1.owl | ADAR | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Autism DSM-ADI-R ontology |
| off | admo | admo-sub4.owl | ADMO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Alzheimer Disease Map Ontology |
| off | apaneurocluster | apaneurocluster-sub1.owl | APANEUROCLUSTER | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). APA Neuro Cluster |
| off | asdpto | asdpto-sub1.owl | ASDPTO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Autism Spectrum Disorder Phenotype Ontology |
| off | azdonto | azdonto-sub1.owl | AZDONTO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: malformed).  Alzheimer's Disease Ontology |
| off | bcio | bcio-sub1.owl | BCIO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Behaviour Change Intervention Ontology |
| off | bctt | bctt-sub6.owl | BCTT | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Behaviour Change Technique Taxonomy |
| off | bt_ontology | bt-ontology-sub3.owl | BT-ONTOLOGY | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). BrainTeaser Ontology (BTO) |
| off | bto_ontology | bto_ontology-sub1.owl | BTO_ONTOLOGY | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Brain Tumour Ontology |
| off | cabro | cabro-sub1.owl | CABRO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Computer Assisted Brain Injury Rehabilitation Ontology |
| off | cbo | cbo-sub25.owl | CBO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Cell Behavior Ontology |
| off | cogito | cogito-sub3.owl | COGITO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Cognitive Task Ontology |
| off | cogmemo | cogmemo-sub1.owl | COGMEMO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Thesaurus of the Cognitive Psychology of Human Memory |
| off | cto_ndd | cto-ndd-sub1.owl | CTO-NDD | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Clinical Trials Ontology - Neurodegenerative Diseases |
| off | ctx | ctx-sub2.owl | CTX | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). BioPortal served a ZIP; it held XCTontologyvtemp2/XCTontologyvtemp2.owl (1.2 MB RDF/XML) beside Protege project files. Extracted and loaded;… |
| off | cxso | cxso-sub2.ttl | CXSO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Cathexis Somatic Ontology |
| off | demlab | demlab-sub1.ttl | DEMLAB | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Dem@Care Lab Ontology for Dementia Assessment |
| off | doid | doid-sub675.owl | DOID | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Human Disease Ontology |
| off | dranpto | dranpto-sub1.owl | DRANPTO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Dementia-Related Agitation Non-Pharmacological Treatment Ontology |
| off | dreamdnpto | dreamdnpto-sub1.owl | DREAMDNPTO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Dementia-Related Emotional And Mood Disturbance Non-Pharmacological Treatment Ontology |
| off | drpsnpto | drpsnpto-sub1.owl | DRPSNPTO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Dementia-Related Psychotic Symptoms Non-Pharmacological Treatment Ontology |
| off | ecdo | ecdo-sub1.ttl | ECDO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Elderly Care Dementia Ontology |
| off | edem_connectonto | edem-connectonto-sub2.owl | EDEM-CONNECTONTO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). eDEM-Connect: Ontology of Dementia-related Agitation and Relationship between Informal Caregivers and Persons with Dementia |
| off | emif_ad | emif-ad-sub3.owl | EMIF-AD | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). EMIF-AD ontology |
| off | emro | emro-sub12.owl | EMRO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Emotion Response Ontology |
| off | ep | ep-sub2.owl | EP | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Cardiac Electrophysiology Ontology |
| off | epilont | epilont-sub2.owl | EPILONT | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Epilepsy Ontology |
| off | epio | epio-sub1.owl | EPIO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). EpilepsyOntology |
| off | episem | episem-sub8.owl | EPISEM | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Epilepsy Semiology |
| off | eso_c | eso_c-sub1.owl | ESO_C | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Epilepsy Semiology Ontology |
| off | esso | esso-sub5.owl | ESSO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Epilepsy Syndrome Seizure Ontology |
| off | fenics | fenics-sub9.owl | FENICS | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Functional Electrophysiology Nomenclature for Ion Channels |
| off | hns | hns-sub1.owl | HNS | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). HNS_Ontolgoy |
| off | lda | lda-sub1.owl | LDA | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Ontology of Language Disorder in Autism |
| off | mdm | mdm-sub5.owl | MDM | - | * | OFF: discovery candidate, not reviewed (BioPortal status: malformed). Mapping of Drug Names and MeSH 2022 |
| off | mepo | mepo-sub6.owl | MEPO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Mapping of Epilepsy Ontologies |
| off | mhio | mhio-sub1.owl | MHIO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Mental Health Interface Ontology |
| off | mhmo | mhmo-sub9.owl | MHMO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Mental Health Management Ontology |
| off | mso | mso-sub2.owl | MSO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Multiple sclerosis ontology |
| off | neio | neio-sub3.owl | NEIO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neural Electronic Interface Ontology |
| off | nero | nero-sub1.owl | NERO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Named Entity Recognition Ontology |
| off | neudigs | neudigs-sub1.ttl | NEUDIGS | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neuroscience Domain Insight Graph |
| off | nifcell | nifcell-sub15.owl | NIFCELL | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neuroscience Information Framework (NIF) Cell Ontology |
| off | nifdys | nifdys-sub16.ttl | NIFDYS | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neuroscience Information Framework (NIF) Dysfunction Ontlogy |
| off | nifsubcell | nifsubcell-sub1.owl | NIFSUBCELL | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neuroscience Information Framework (NIF) Subcellular Ontology |
| off | nio | nio-sub2.owl | NIO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neuropsychological Integrative Ontology |
| off | nmosp | nmosp-sub22.owl | NMOSP | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). NeuroMorpho.Org species ontology |
| off | nnd_ch | nnd_ch-sub2.owl | NND_CH | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). NND_Clinical_history |
| off | nnd_nd | nnd_nd-sub2.owl | NND_ND | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). NND_Neuropathological_Diagnosis |
| off | npdx | npdx-sub8.owl | NPDX | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Neuropathologic Diagnoses |
| off | nro | nro-sub1.owl | NRO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). NeuralReprogrammingOntology |
| off | obi_iee | obi_iee-sub1.owl | OBI_IEE | - | * | OFF: discovery candidate, not reviewed (BioPortal status: malformed). The Ontology for Biomedical Investigation based Inner Ear Electrophysiology |
| off | odnae | odnae-sub3.owl | ODNAE | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Ontology of Drug Neuropathy Adverse Events |
| off | onl_msa | onl-msa-sub1.owl | ONL-MSA | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Mental State Assessment |
| off | onl_tasks | onl-tasks-sub1.owl | ONL-TASKS | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Cognitive Reserves Assessment Tasks |
| off | ontoad | ontoad-sub2.owl | ONTOAD | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Bilingual Ontology of Alzheimer's Disease and Related Diseases |
| off | ontoparon | ontoparon-sub9.owl | ONTOPARON | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Ontology of Amyotrophic Lateral Sclerosis, all modules |
| off | ontoparon_social | ontoparon_social-sub12.owl | ONTOPARON_SOCIAL | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Ontology of amyotrophic  lateral sclerosis, social module |
| off | ontriscal | ontriscal-sub1.owl | ONTRISCAL | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Ontology of Risk Stratification in Mental Health |
| off | partumdo | partumdo-sub1.owl | PARTUMDO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). postpartum depression ontology |
| off | promot | promot-sub2.owl | PROMOT | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). PROMOT Ontology |
| off | sato | sato-sub2.owl | SATO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). SATO (IDEAS expAnded wiTh BCIO): workflow for designers of patient-centered mobile health behaviour change intervention applications |
| off | sdo | sdo-sub4.owl | SDO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Sleep Domain Ontology |
| off | sibo | sibo-sub2.owl | SIBO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Social Insect Behavior Ontology |
| off | sl_dis | sl_dis-sub2.owl | SL_DIS | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Sleep Disorders |
| off | sleep | sleep-sub1.owl | SLEEP | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). SLEEP ontology |
| off | stmso | stmso-sub1.owl | STMSO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). symptomatic treatment of multiple sclerosis ontology |
| off | sto_draft | sto-draft-sub1.owl | STO-DRAFT | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). The Stroke Ontology |
| off | veo | veo-sub1.owl | VEO | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Visualized Emotion Ontology |
| off | wweca | wweca-sub4.owl | WWECA | - | * | OFF: discovery candidate, not reviewed (BioPortal status: loaded). Women with Epilepsy of Child-bearing Age Ontology |
