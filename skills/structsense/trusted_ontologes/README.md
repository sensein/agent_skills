# Trusted ontologies

These are the ontologies, which we call the "trusted ontologies", that are loaded
in our graph database for connecting assertion-evidence and materialization, and
that structsense uses **first** for concept mapping.

The directory holds **115 ontology files** in two formats: `.owl` (RDF/XML) and
`.ttl` (Turtle). `sources.json` is the acquisition log (source, submission,
release, outcome) for each of them.

## How structsense uses them

- [priority.md](priority.md) is the **only** place that says which files are used
  and in what order. Edit it to reorder, enable (`off` → a number) or add an
  ontology; no code or JSON change is needed.
- `python -m scripts.concept_mapping index` streams each enabled file once into
  `lexicon/<name>.tsv.gz` (key, IRI, prefix, match type, label) and one indexed
  `lexicon/lexicon.sqlite`. Only changed files are re-indexed; priority is applied
  at query time. `lexicon/` is regenerable and not committed.
- A term keeps the prefix of its namespace: OBO terms their OBO prefix, known
  namespaces the prefix registered in `default_ontology/ttl_config.json`, and a
  file's own terms the CURIE prefix of its row. Anything else is left
  unregistered and never mapped (`python -m scripts.prefixes check`).
- After changing priority.md or re-indexing, regenerate the list below with
  `python -m scripts.concept_mapping readme`.

## Ontologies

<!-- BEGIN generated: python -m scripts.concept_mapping readme -->

**42 enabled** for concept mapping, **73 disabled** (listed in priority.md as `off`). Priority, CURIE prefix and namespace come from [priority.md](priority.md); class counts from the last `index`.

| Priority | Name | File | Size | Prefix / namespace | Classes indexed |
|---:|---|---|---:|---|---:|
| 1 | `bke` | bke_taxonomy.ttl | 0.3 MB | BKE: `https://identifiers.org/brain-bican/vocab/` | 98 |
| 2 | `cl` | cl.owl | 66.0 MB | OBO prefixes | 18690 |
| 3 | `go_basic` | go-basic.owl | 118.1 MB | OBO prefixes | 38092 |
| 4 | `uberon` | uberon.owl | 95.9 MB | OBO prefixes | 25188 |
| 5 | `pcl` | pcl.owl | 167.6 MB | OBO prefixes | 36396 |
| 6 | `mondo` | mondo-simple.owl | 226.6 MB | OBO prefixes | 32109 |
| 7 | `hp` | hp-base.owl | 48.3 MB | OBO prefixes | 19944 |
| 8 | `chebi` | chebi-lite.owl | 183.4 MB | OBO prefixes | 218533 |
| 9 | `ncbitaxon` | ncbitaxon-taxslim.owl | 40.6 MB | OBO prefixes | 31798 |
| 10 | `nbo` | nbo.owl | 5.6 MB | OBO prefixes | 4542 |
| 11 | `mf` | mf.owl | 0.4 MB | OBO prefixes | 399 |
| 12 | `mfoem` | mfoem.owl | 0.6 MB | OBO prefixes | 622 |
| 13 | `mfomd` | mfomd.owl | 0.0 MB | OBO prefixes | 0 |
| 14 | `mp` | mp.owl | 101.2 MB | OBO prefixes | 34397 |
| 15 | `fbbt` | fbbt.owl | 120.3 MB | OBO prefixes | 28122 |
| 16 | `wbbt` | wbbt.owl | 9.4 MB | OBO prefixes | 6780 |
| 17 | `zfa` | zfa.owl | 7.4 MB | OBO prefixes | 3169 |
| 18 | `xao` | xao.owl | 3.9 MB | OBO prefixes | 1781 |
| 19 | `pr` | pr.owl | 1279.4 MB | OBO prefixes | 327402 |
| 20 | `nifstd` | nifstd-sub27.ttl | 0.0 MB | — | 0 |
| 21 | `npokb` | npokb-sub22.ttl | 0.9 MB | NPOKB: `http://uri.interlex.org/base/` | 2757 |
| 22 | `aba_amb` | aba-amb-sub1.owl | 0.7 MB | ABA-AMB: `http://mouse.brain-map.org/atlas/index.html#` | 913 |
| 23 | `nmobr` | nmobr-sub36.owl | 0.3 MB | NMOBR: `http://neuromorpho.org/ontologies/vertebrateH.owl#` | 272 |
| 24 | `brct` | brct-sub1.owl | 1.2 MB | BRCT: `http://www.semanticweb.org/ontologies/2009/9/Ontology1255357986125.owl#` | 1654 |
| 25 | `brain_cof` | brain-cof-sub2.owl | 0.1 MB | BRAIN-COF: `https://explaination.net/onto/tree-structure-brain-anatomy-FastSurfer#` | 125 |
| 26 | `cogat` | cogat-sub8.owl | 2.7 MB | COGAT: `http://www.cognitiveatlas.org/ontology/cogat.owl#` | 3786 |
| 27 | `cogpo` | cogpo-sub1.owl | 0.2 MB | COGPO: `http://www.cogpo.org/ontologies/CogPOver1.owl#` | 191 |
| 28 | `cno` | cno-sub8.owl | 0.2 MB | CNO: `http://purl.org/incf/ontology/Computational_Neurosciences/cno_alpha.owl#` | 219 |
| 29 | `nemo` | nemo-sub24.owl | 4.4 MB | NEMO: `http://purl.bioontology.org/NEMO/ontology/NEMO.owl#` | 1808 |
| 30 | `nidm_results` | nidm-results-sub2.ttl | 0.3 MB | NIDM-RESULTS: `http://purl.org/nidash/nidm#` | 150 |
| 31 | `neurobrg` | neurobrg-sub2.owl | 0.4 MB | NEUROBRG: `http://maven.renci.org/NeuroBridge/neurobridge#` | 667 |
| 32 | `onvoc` | onvoc-sub9.ttl | 0.4 MB | ONVOC: `https://w3id.org/onvoc/` | 754 |
| 33 | `bci_o` | bci-o-sub6.owl | 0.5 MB | BCI-O: `https://w3id.org/BCI-ontology#` | 71 |
| 34 | `ado` | ado-sub4.owl | 5.5 MB | ADO: `http://scai.fraunhofer.de/AlzheimerOntology#` | 1961 |
| 35 | `pdon` | pdon-sub1.owl | 0.8 MB | PDON: `http://www.semanticweb.org/ontologies/2011/1/Ontology1296772722296.owl#` | 631 |
| 36 | `pmdo` | pmdo-sub4.owl | 0.2 MB | PMDO: `http://www.case.edu/PMDO#` | 628 |
| 37 | `epso` | epso-sub8.owl | 1.4 MB | EPSO: `http://www.case.edu/EpilepsyOntology.owl#` | 2195 |
| 38 | `neo` | neo-sub90.owl | 1.3 MB | NEO: `http://www.semanticweb.org/danielhier/ontologies/2019/3/untitled-ontology-57/` | 1609 |
| 39 | `nddo` | nddo-sub10.owl | 0.2 MB | NDDO: `http://www.purl.org/NDDO/` | 184 |
| 40 | `nddrfo` | nddrfo-sub3.owl | 1.1 MB | NDDRFO: `http://www.semanticweb.org/NDDRFO#` | 855 |
| 41 | `nihss` | nihss-sub15.owl | 0.1 MB | NIHSS: `https://mre.zcu.cz/ontology/nihss.owl#` | 18 |
| 42 | `scio` | scio-sub2.owl | 0.3 MB | SCIO: `http://psink.de/scio/` | 298 |

<details><summary>Disabled rows</summary>

| Name | File | Why |
|---|---|---|
| `cl_basic` | cl-basic.owl | subset of cl.owl, which is enabled |
| `ad_cdo` | ad-cdo-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ad_drop` | ad-drop-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `adar` | adar-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `admo` | admo-sub4.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `apaneurocluster` | apaneurocluster-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `asdpto` | asdpto-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `azdonto` | azdonto-sub1.owl | discovery candidate, not reviewed (BioPortal status: malformed) |
| `bcio` | bcio-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `bctt` | bctt-sub6.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `bt_ontology` | bt-ontology-sub3.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `bto_ontology` | bto_ontology-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `cabro` | cabro-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `cbo` | cbo-sub25.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `cogito` | cogito-sub3.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `cogmemo` | cogmemo-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `cto_ndd` | cto-ndd-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ctx` | ctx-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `cxso` | cxso-sub2.ttl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `demlab` | demlab-sub1.ttl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `doid` | doid-sub675.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `dranpto` | dranpto-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `dreamdnpto` | dreamdnpto-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `drpsnpto` | drpsnpto-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ecdo` | ecdo-sub1.ttl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `edem_connectonto` | edem-connectonto-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `emif_ad` | emif-ad-sub3.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `emro` | emro-sub12.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ep` | ep-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `epilont` | epilont-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `epio` | epio-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `episem` | episem-sub8.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `eso_c` | eso_c-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `esso` | esso-sub5.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `fenics` | fenics-sub9.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `hns` | hns-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `lda` | lda-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `mdm` | mdm-sub5.owl | discovery candidate, not reviewed (BioPortal status: malformed) |
| `mepo` | mepo-sub6.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `mhio` | mhio-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `mhmo` | mhmo-sub9.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `mso` | mso-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `neio` | neio-sub3.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nero` | nero-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `neudigs` | neudigs-sub1.ttl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nifcell` | nifcell-sub15.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nifdys` | nifdys-sub16.ttl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nifsubcell` | nifsubcell-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nio` | nio-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nmosp` | nmosp-sub22.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nnd_ch` | nnd_ch-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nnd_nd` | nnd_nd-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `npdx` | npdx-sub8.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `nro` | nro-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `obi_iee` | obi_iee-sub1.owl | discovery candidate, not reviewed (BioPortal status: malformed) |
| `odnae` | odnae-sub3.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `onl_msa` | onl-msa-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `onl_tasks` | onl-tasks-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ontoad` | ontoad-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ontoparon` | ontoparon-sub9.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ontoparon_social` | ontoparon_social-sub12.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `ontriscal` | ontriscal-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `partumdo` | partumdo-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `promot` | promot-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `sato` | sato-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `sdo` | sdo-sub4.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `sibo` | sibo-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `sl_dis` | sl_dis-sub2.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `sleep` | sleep-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `stmso` | stmso-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `sto_draft` | sto-draft-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `veo` | veo-sub1.owl | discovery candidate, not reviewed (BioPortal status: loaded) |
| `wweca` | wweca-sub4.owl | discovery candidate, not reviewed (BioPortal status: loaded) |

</details>

<!-- END generated -->
