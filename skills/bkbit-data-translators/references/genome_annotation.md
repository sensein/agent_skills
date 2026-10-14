# gff2jsonld: GFF3 genome annotations to Genome Annotation objects

Source: `bkbit/data_translators/genome_annotation_translator.py`.

Downloads a GFF3 file from NCBI or Ensembl and hashes it. It then writes
[Genome Annotation](https://brain-bican.github.io/models/index_genome_annotation/)
objects: one `OrganismTaxon`, one `GenomeAssembly`, one `GenomeAnnotation`,
one `Checksum`, and one `GeneAnnotation` per `gene` / `pseudogene` /
`ncRNA_gene` feature.

The `OrganismTaxon` has `xref` and `exact_match` both set to
`NCBITaxon:<taxid>`. The `@context` URL is pinned to a fixed
`brain-bican/models` commit (`CONTEXT_URL` in the translator) rather than
`main`, so output from one bkbit version always resolves against the same
context.

## Command

```bash
bkbit gff2jsonld [OPTIONS] CONTENT_URL > output.jsonld
```

| Option | Meaning |
| --- | --- |
| `-a`, `--assembly_accession` | Assembly accession (for example `GCF_003339765.1`). **Required for Ensembl URLs**; NCBI URLs carry it in the path. |
| `-s`, `--assembly_strain` | Strain recorded on the `GenomeAssembly`. |
| `-l`, `--log_level` | `DEBUG`/`INFO`/`WARNING` (default)/`ERROR`/`CRITICAL`. |
| `-f`, `--log_to_file` | Takes a **value**, not a flag: `-f True` logs to `./gff3_translator_<timestamp>.log`. Leave it out to log to the console. Any non-empty value, even `-f False`, turns file logging on. |
| `-o`, `--output_format` | `jsonld` (default) or `turtle`. |

## Supported URLs

`CONTENT_URL` must be an `http(s)` URL on an `ncbi` or `ensembl` host that
ends in `.gff.gz` or `.gff3.gz`. The translator reads the taxon, release, and
assembly from the URL path, so the URL has to follow one of these layouts:

```
# NCBI
https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/<taxid>/<release>/<acc>_<asm>/<acc>_<asm>_genomic.gff.gz
https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/<taxid>/<acc>-<release>/<acc>_<asm>_genomic.gff.gz
# Ensembl
https://ftp.ensembl.org/pub/release-<release>/gff3/<species>/<Species>.<asm>.<release>.gff3.gz
```

Local files, mirrors, and other hosts are **not supported**. If the user has a
local file, find the matching NCBI/Ensembl URL. A URL with another extension
fails with `AttributeError: 'NoneType' object has no attribute 'get'`. A URL
on another host fails with `The provided content URL is not supported`.

## Examples

```bash
# NCBI (pig)
bkbit gff2jsonld 'https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/9823/106/GCF_000003025.6_Sscrofa11.1/GCF_000003025.6_Sscrofa11.1_genomic.gff.gz' > pig.jsonld

# Ensembl (rhesus macaque): -a is required
bkbit gff2jsonld -a 'GCF_003339765.1' 'https://ftp.ensembl.org/pub/release-104/gff3/macaca_mulatta/Macaca_mulatta.Mmul_10.104.gff3.gz' > macaque.jsonld
```

### Temporary files

Since brain-bican/bkbit#109, `gff2jsonld` deletes its downloaded file when it
finishes, including after a failed parse or download, and reads the `.gz`
directly without writing a decompressed copy. So a run needs disk space for
one compressed GFF3 at a time. Older bkbit installs leave **two** files per
run in the temp directory (the download and a full decompressed copy, often
more than 1 GB together) and never delete them. On those versions, batch runs
can fill the disk. Either upgrade bkbit, or give each run its own `TMPDIR` and
delete it afterwards. To check the installed version, look for `Gff3.cleanup`:
`python -c "from bkbit.data_translators.genome_annotation_translator import Gff3; print(hasattr(Gff3, 'cleanup'))"`.

### Run time

GFF3 files for large genomes are hundreds of MB. The download and parse both
show `tqdm` progress bars on stderr and can take many minutes. Run the command
in the background, or with a long timeout.

## NCBI taxonomy lookups

The translator looks up organism names through
`bkbit.utils.ncbi_taxonomy_cache`, which needs no setup:

1. A bundled subset (every taxon with a GenBank common name) answers almost
   every lookup offline.
2. On a miss, it downloads the full NCBI taxonomy (`taxdmp.zip`) once and
   caches it in the per-user cache directory.

Useful controls:

```bash
bkbit download-ncbi-taxonomy            # pre-fetch the full taxonomy (for containers, CI, air-gapped runs)
export BKBIT_DATA_DIR=/path/to/cache    # put the cache somewhere else
export BKBIT_NO_DOWNLOAD=1              # on a miss, raise instead of downloading
```

`Taxon ID '<id>' was not found in the NCBI taxonomy` means the taxon ID from
the URL (NCBI) or the species name (Ensembl) isn't in the taxonomy. Check the
URL.

## Python API

```python
from bkbit.data_translators.genome_annotation_translator import Gff3

gff3 = Gff3(content_url, assembly_accession=None, assembly_strain=None,
            log_level="WARNING", log_to_file=False)
try:
    gff3.setup()             # parse URL + download to a temp file (gff3.gff_file, gff3.hash_values)
    gff3.parse_gff3_file()   # taxon, assembly, checksum, genome annotation
    gff3.parse()             # gff3.gene_annotations: {id: GeneAnnotation}
finally:
    gff3.cleanup()           # delete the downloaded temp file
jsonld = gff3.serialize_to_jsonld()
```

`setup()` downloads the GFF3 to a temporary file. **Always call `cleanup()`**
when you're done, in a `finally` block as above, or the file (often hundreds
of MB) is left in the temp directory. The CLI does this itself. `cleanup()`
is safe to call twice, and `serialize_to_jsonld()` doesn't need the file, so
it can come after `cleanup()`.

`parse(feature_filter=("gene", "pseudogene", "ncRNA_gene"))` takes other
GFF3 feature types if the user needs them.
