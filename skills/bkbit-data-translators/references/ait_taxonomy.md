# ait2jsonld: AIT (.h5ad) cell-type taxonomies to Cell Taxonomy objects

Source: `bkbit/data_translators/ait_taxonomy_translator.py`.

Reads an Allen Institute Taxonomy (AIT) file, an AnnData `.h5ad` following the
[AIT schema](https://github.com/AllenInstitute/AllenInstituteTaxonomy/blob/main/docs/schema.md).
It writes [Cell Taxonomy](https://github.com/brain-bican/models) objects:

- one `CellTypeTaxonomy`;
- one `CellTypeSet` per hierarchy level (from `uns['hierarchy']`);
- one `CellTypeTaxon` per name at each level;
- one `Cluster` per cluster, in one `ClusterSet`;
- `ExpressionMatrix`, `Embedding`, `DisplayColor` and `Abbreviation`.

It reads **only metadata**: `uns`, the columns of `uns['cluster_info']` the
model refers to, and the keys of `obsm` / `X` / `raw`. It never loads the
expression matrix. Remote files are read with HTTP range requests, so a
300 GB file needs no download.

Where each value comes from is not hard-coded. Every slot in the model carries
an `ait_location` annotation, and the translator reads from that location.
When it can't, it does something else and records that in the report (below),
never silently.

## Install: not on bkbit `main` yet

`ait2jsonld` is in [brain-bican/bkbit#105](https://github.com/brain-bican/bkbit/pull/105).
The `curated_markers` fix and `--gene_annotation` are in
[djarecka/BrainKit#1](https://github.com/djarecka/BrainKit/pull/1), on top of
it. Until they merge, `pip install git+https://github.com/brain-bican/bkbit`
gives a `bkbit` **without** `ait2jsonld`. Install the branch instead:

```bash
pip install "git+https://github.com/puja-trivedi/bkbit@fix-curated-markers-iri"
bkbit ait2jsonld --help   # must list --gene_annotation
```

`h5py`, `anndata`, `fsspec` and `aiohttp` are regular dependencies of that
branch. Once both PRs are merged, install from `brain-bican/bkbit` as for the
other translators.

## Command

```bash
bkbit ait2jsonld PATH --out taxonomy.jsonld [OPTIONS]
```

| Option | Meaning |
| --- | --- |
| `PATH` | Local `.h5ad`, or an `https://` URL. See *Remote files*. |
| `--out` | JSON-LD output file. **Without it nothing is written**: the command only prints the object counts. |
| `--report` | Provenance report (JSON); see *Check the report*. Write one on every run. |
| `--gene_annotation` | **One** genome annotation for the taxonomy's species: `gff2jsonld` output, Turtle or JSON-LD. Needed for `curated_markers`; see *Marker genes*. Giving it twice is an error. |
| `--abbreviations` | Table giving each label token (`tokens_<level>`) a meaning, type and ontology term. A CSV path, or the bundled name `hmba-basalganglia`. No table is used unless one is named. |
| `--species` | A label for the report only. It has no effect on the graph. |
| `--counts` | Also compute `number_of_cells` / `number_of_observations` by reading `obs` codes. Slower. |
| `--cache` | Local directory for the block cache of remote reads. Speeds up repeated runs on the same file. |
| `--context` | JSON-LD context to embed. Default: the copy bundled with bkbit. |

The output **embeds** its `@context`, unlike `gff2jsonld`, which links to it by
URL. So reading or converting the output needs no network.

There is no Turtle option. Convert afterwards; no network is needed, because
the context is embedded:

```python
from bkbit.utils.serialize_to_ttl import convert_jsonld_to_ttl
ttl = convert_jsonld_to_ttl(open("taxonomy.jsonld").read())
```

## Remote files

Use the **`https://` form of an S3 URL**:

```
https://<bucket>.s3.<region>.amazonaws.com/<key>
```

`s3://` URLs fail. Without `s3fs` installed, fsspec can't open them; with it,
they fail with `TypeError: AioSession.__init__() got an unexpected keyword
argument 'block_size'`. Either way, rewrite the URL to the https form. The
taxonomy's `content_url` is still recorded as `s3://<bucket>/<key>`.

Example, the HMBA human basal ganglia taxonomy:

```bash
bkbit ait2jsonld \
  "https://released-taxonomies-802451596237-us-west-2.s3.us-west-2.amazonaws.com/HMBA/BasalGanglia/BICAN_05072025_pre-print_release/Human_HMBA_basalganglia_AIT_pre-print.h5ad" \
  --abbreviations hmba-basalganglia \
  --gene_annotation annotation-ENSEMBL-9606-109.ttl \
  --cache ./h5cache --species Human \
  --out Human_BG.jsonld --report Human_BG.report.json
```

## Marker genes (`--gene_annotation`)

`CellTypeTaxon.curated_markers` holds **links to `GeneAnnotation` objects**,
not gene symbols. The AIT file stores symbols (`"PDGFRA, DCN"`), so the
translator looks each one up in the genome annotation you name. It matches:

1. on the exact `symbol`;
2. on the same symbol in another case (`Pou6f2` → `POU6F2`);
3. on an exact `synonym`.

A symbol that matches several genes, or none, is left out and listed in the
report, never guessed.

- **One file, the taxonomy's species.** The annotation's species (the
  `NCBITaxon:<n>` of its genes' `OrganismTaxon`) must equal the h5ad's
  `organism_ontology_term_id`. If they differ, the run stops with an error
  (`... is NCBITaxon:9544, but this taxonomy is NCBITaxon:9606`). Pick the file
  by species:

  | Taxonomy species | Annotation used for the HMBA taxonomies |
  | --- | --- |
  | Human (`NCBITaxon:9606`) | `annotation-ENSEMBL-9606-109` |
  | Macaque (`NCBITaxon:9544`) | `annotation-NCBI-9544-103` |
  | Marmoset (`NCBITaxon:9483`) | `annotation-NCBI-9483-105` |

  These files are made with `gff2jsonld`
  ([genome_annotation.md](genome_annotation.md)).
- **Turtle or JSON-LD.** The format is read from the file's content and must
  agree with its extension. `gff2jsonld` JSON-LD links its context by URL;
  `ait2jsonld` uses the bundled copy instead, so nothing is fetched.
- **Same bkbit release.** `GeneAnnotation` IDs are hashes, and they changed
  between bkbit versions (e.g. after brain-bican/bkbit#112). Make the
  annotation with the bkbit release the rest of your graph uses, or the marker
  links point to IDs that exist nowhere else. The species check cannot catch
  this.
- **Recorded.** `CellTypeTaxonomy.gene_annotation_version` is set to the
  annotation used (e.g. `ENSEMBL-9606-109`) when the h5ad leaves it blank. A
  value the h5ad does have is kept, and the difference is reported.
- **Without `--gene_annotation`,** `curated_markers` is left out (and
  reported). A symbol is not a valid value for a link slot, and written as
  text it would become a meaningless `file:///.../ACTA2` node.

## Check the report

Every run prints `resolved=… deviations=… unpopulated=…`, and `--report`
writes the details. **Read `deviations` before handing over the output.** It
holds:

- each value the translator could not take from its declared location, and
  what it did instead;
- markers that were not linked, or were linked by a weaker match;
- values dropped from link slots because they were neither an absolute IRI nor
  a CURIE with a known prefix (the translator checks this for every class);
- values a model pattern rejected (e.g. a non-CL value in `CL:ID_<level>`).

`unpopulated` lists slots the file has no value for.

For the HMBA basal ganglia files, expect these markers to be reported as not
linked: `Six3 and Foxp1` (free text in all three species) and `Ctip2` (macaque
and marmoset; an alias of BCL11B that isn't an NCBI synonym). These are
problems in the source h5ad. Report them; don't patch the output.

## Notes

- IDs are `urn:bkbit:<sha256 of attributes>`, and a child's attributes include
  its parent's ID. So any change to the `CellTypeTaxonomy`, including a
  different `--gene_annotation` (via `gene_annotation_version`), changes the ID
  of every object below it.
- String columns written by anndata ≥ 0.11 (`nullable-string-array`) are
  supported.
- The `--species` label and the file's own organism are unrelated: only
  `organism_ontology_term_id` is checked against `--gene_annotation`.

## Python API

```python
from bkbit.data_translators import ait_taxonomy_translator as ait

ents, rep = ait.build(
    "Human_HMBA_basalganglia_AIT_pre-print.h5ad",
    species="Human",
    abbreviations="hmba-basalganglia",
    gene_annotation="annotation-ENSEMBL-9606-109.ttl",
)
ait.guard_links(ents, rep)          # the CLI does this; drops non-IRI values from link slots
doc = ait.to_jsonld(ents, ait.CONTEXT)
report = rep.to_dict()              # {"summary": ..., "resolved": [...], "deviations": [...], ...}
```

`ents` maps class name to a list of attribute dicts. `ait.load_gene_annotation(path)`
on its own gives a symbol lookup (`.resolve("ACTA2")` returns the
`GeneAnnotation` IRI and the match tier).
