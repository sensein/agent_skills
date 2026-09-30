# Cell NER against human annotation — the evidence behind rules S1–S6

From the structsense context layer (v2, examples/gold_validation). Scores and error modes
that the specificity (S1–S5), span-boundary (S6) and salience rules in
prompts/extractor-ner-cns-cells.md encode.


Scored the repo's `extractor-ner-cns-cells.md` pass-1 plus this layer's salience fields
against a human-annotated BioC sample (RESULTS x3, INTRO, FIG caption).

| Metric | Value |
|---|---|
| Span recall (overlap) | 27/27 = 1.00 |
| Span recall (exact offsets) | 22/27 = 0.81 |
| Span precision (overlap) | 27/33 = 0.82 |
| Specificity agreement | 17/27 = 0.63 |

**Zero misses** — every human annotation was found, including non-CNS cells in a
spinal-cord-injury passage (neutrophil, macrophage, leukocyte, epithelial cell) and cell
names embedded in mechanism acronyms (`Interneuron` inside "Pyramidal-Interneuron Gamma").

Both error modes were systematic and are now encoded as rules S1-S6:

1. **Specificity over-hedging (10/27).** Bare canonical names (`Interneuron`, `Pyramidal`,
   `leukocyte`, `inhibitory neurons`) were called `cell_vague`, and conjunctions of named
   types (`D1- or D2-SPNs`, `inhibitory and excitatory neurons`) were called `cell_hetero`,
   where the annotator used `cell_phenotype`. The root cause is conflating two independent
   axes: **specificity** asks whether the identity is groundable at all; the **qualifier**
   asks how exactly a class fits. The gold proves they are orthogonal — `inhibitory
   neurons` is `cell_phenotype` carrying `(skos:related)CL:0000498,CL:0000617`, and
   `D1- or D2-SPNs` is `cell_phenotype` carrying two `;`-separated exact ids.
2. **Span over-extension (5/27 overlap-only).** Trailing modifiers absorbed:
   `tanycytes lining the vmARH`, `typical ependymal cells`, `Lrig1+ cell lineages`.

Also confirmed from the gold: nested same-start spans are legal (`tanycyte subtypes` at
offset 233 length 17 alongside `tanycyte` at 233 length 8); `;` separates coordinated
slots while `,` separates candidates for one mention; marker-only sets (`Lrig1+ cell`,
`RFP+ cells`) are `cell_vague` with no id; and dataset-local cluster ids (`cluster 0
cells`) are deliberately NOT annotated.

**Not assessed:** `document_focus` (the sample is passages, not articles), MeSH-vs-content
disagreement (no MeSH in the sample), and ontology-id agreement (pass-1 emits no ids).
The salience axis is effectively untested — no prediction used `contrastive_aside` or
`incidental_mention`, and the one clear case (`immune cells` inside "work in a manner
similar to immune cells") was mis-tagged `supporting_evidence`/`this_study` instead of
`contrastive_aside`/`general_knowledge`.

## Per-annotation scores

```csv
passage,section,gold_text,gold_type,gold_ids,match,pred_text,pred_label,pred_specificity,pred_salience,spec_agree
32183906_146,RESULTS,Lrig1+ cell,cell_vague,,overlap,Lrig1+ cell lineages,CellType,cell_vague,subject_of_passage,True
32183906_146,RESULTS,RFP+ cells,cell_vague,,exact,RFP+ cells,CellType,cell_vague,subject_of_passage,True
39098920_6,INTRO,tanycyte,cell_phenotype,CL:0002085,exact,tanycyte,CellType,cell_phenotype,subject_of_passage,True
39098920_6,INTRO,tanycyte subtypes,cell_vague,,exact,tanycyte subtypes,CellType,cell_vague,subject_of_passage,True
39098920_6,INTRO,tanycyte,cell_phenotype,CL:0002085,exact,tanycyte,CellType,cell_phenotype,subject_of_passage,True
39098920_6,INTRO,tanycytes,cell_phenotype,CL:0002085,overlap,tanycytes lining the vmARH,CellType,cell_phenotype,subject_of_passage,True
39098920_6,INTRO,tanycytes,cell_phenotype,CL:0002085,overlap,tanycytes',CellType,cell_phenotype,subject_of_passage,True
39098920_6,INTRO,ependymal cells,cell_phenotype,CL:0000065,overlap,typical ependymal cells,CellType,cell_phenotype,subject_of_passage,True
37751468_50,FIG,D1- or D2-SPNs,cell_phenotype,CL:4030048;CL:4030049,exact,D1- or D2-SPNs,CellType,cell_hetero,subject_of_passage,False
37751468_50,FIG,D1-/D2-SPNs,cell_phenotype,CL:4030048;CL:4030049,exact,D1-/D2-SPNs,CellType,cell_hetero,subject_of_passage,False
37751468_50,FIG,D1-SPNs,cell_phenotype,CL:4030048,exact,D1-SPNs,CellType,cell_phenotype,subject_of_passage,True
37751468_50,FIG,D1-SPNs,cell_phenotype,CL:4030048,exact,D1-SPNs,CellType,cell_phenotype,subject_of_passage,True
37751468_50,FIG,D2-SPNs,cell_phenotype,CL:4030049,exact,D2-SPNs,CellType,cell_phenotype,subject_of_passage,True
37751468_50,FIG,D2-SPNs,cell_phenotype,CL:4030049,exact,D2-SPNs,CellType,cell_phenotype,subject_of_passage,True
34529655_54,RESULTS,inhibitory and excitatory neurons,cell_phenotype,CL:0000617;CL:0000679,exact,inhibitory and excitatory neurons,CellClass,cell_hetero,subject_of_passage,False
34529655_54,RESULTS,pacemaker excitatory cells,cell_phenotype,CL:0002072,overlap,excitatory cells,CellClass,cell_vague,supporting_evidence,False
34529655_54,RESULTS,Chattering neurons,cell_phenotype,CL:0002072,exact,Chattering neurons,CellType,cell_phenotype,subject_of_passage,True
34529655_54,RESULTS,Chattering,cell_phenotype,CL:0002072,exact,Chattering,CellType,cell_vague,supporting_evidence,False
34529655_54,RESULTS,inhibitory neurons,cell_phenotype,CL:0000498;CL:0000617,exact,inhibitory neurons,CellClass,cell_vague,subject_of_passage,False
34529655_54,RESULTS,Interneuron,cell_phenotype,CL:0000099,exact,Interneuron,CellType,cell_vague,supporting_evidence,False
34529655_54,RESULTS,Interneuron,cell_phenotype,CL:0000099,exact,Interneuron,CellType,cell_vague,supporting_evidence,False
34529655_54,RESULTS,Pyramidal,cell_phenotype,CL:0000598,exact,Pyramidal,CellType,cell_vague,supporting_evidence,False
35788904_83,RESULTS,epithelial cell,cell_phenotype,CL:0000066,exact,epithelial cell,CellType,cell_phenotype,supporting_evidence,True
35788904_83,RESULTS,neutrophil,cell_phenotype,CL:0000775,exact,neutrophil,CellType,cell_phenotype,supporting_evidence,True
35788904_83,RESULTS,macrophage,cell_phenotype,CL:0000235,exact,macrophage,CellType,cell_phenotype,supporting_evidence,True
35788904_83,RESULTS,immune cells,cell_hetero,,exact,immune cells,CellClass,cell_hetero,supporting_evidence,True
35788904_83,RESULTS,leukocyte,cell_phenotype,CL:0000738,exact,leukocyte,CellClass,cell_hetero,supporting_evidence,False
```
