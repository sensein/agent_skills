---
name: bkbit-data-translators
description: Turn BICAN source data into JSON-LD or Turtle with the bkbit data translators. Use when the user wants BICAN / Brain Knowledge Base objects from Specimen Portal (NIMP) NHash IDs (specimen2jsonld), a Brain Knowledge Platform file manifest or specimen metadata CSV (filemanifest2jsonld, list-library-aliquot), an NCBI or Ensembl GFF3 genome annotation (gff2jsonld), an HMBA cell-type taxonomy CSV set (taxonomy2jsonld), or anatomical-structure / parcellation CSVs. Also use when a bkbit translator command fails, or the user asks which bkbit command fits their data, what inputs it needs, or how to check the output.
---

# bkbit Data Translators

[bkbit](https://github.com/brain-bican/bkbit) turns BICAN source data into
instances of the [BICAN Knowledgebase data models](https://brain-bican.github.io/models/)
and writes them out as JSON-LD (`{"@context": ..., "@graph": [...]}`), and for
some translators as Turtle. Use this skill to pick the right translator,
collect the inputs it needs, run it, and check what it produced.

## Pick The Translator

| The user has... | Command | Model | Details |
| --- | --- | --- | --- |
| Specimen Portal NHash IDs (`DO-…`, `LP-…`, `LA-…`), or a file with one per line | `bkbit specimen2jsonld` | Library Generation | [references/specimen_portal.md](references/specimen_portal.md) |
| A **File Manifest** CSV from the Brain Knowledge Platform | `bkbit filemanifest2jsonld` (known upstream bug, see reference) | Library Generation | [references/brain_knowledge_platform.md](references/brain_knowledge_platform.md) |
| A **Metadata** (specimen list) CSV from the Brain Knowledge Platform | `bkbit list-library-aliquot` | none, prints IDs | [references/brain_knowledge_platform.md](references/brain_knowledge_platform.md) |
| A GFF3 URL from NCBI or Ensembl | `bkbit gff2jsonld` | Genome Annotation | [references/genome_annotation.md](references/genome_annotation.md) |
| HMBA annotation, abbreviation, and description CSVs | `bkbit taxonomy2jsonld` | BKE Taxonomy | [references/hmba_taxonomy.md](references/hmba_taxonomy.md) |
| A folder of anatomical-structure / parcellation CSVs | Python API only (`AnS`) | Anatomical Structure | [references/anatomical_structure.md](references/anatomical_structure.md) |

Read only the reference file for the translator you are using. Each one gives
the exact inputs, options, examples, and known problems.

## Workflow

1. **Identify the input.** Ask what the user has if it isn't clear: an ID, a
   CSV (and which Brain Knowledge Platform export), a URL, or a folder. Match it
   to the table above.
2. **Make sure bkbit is installed.** Run `bkbit --help`. If the command is not
   found:
   ```bash
   pip install bkbit
   # Temporary fix the bkbit README asks for:
   pip install git+https://github.com/linkml/schemasheets
   ```
   Use a virtual environment or the project's own environment. Don't install
   into the system Python.
3. **Check what the translator needs before running it.** That can be a
   credential (`jwt_token`), network access (Specimen Portal, NCBI/Ensembl FTP,
   the JSON-LD context on `raw.githubusercontent.com`), required CSV columns, or
   an extra option (`-a` for Ensembl). Check these first. A missing input
   usually fails late or fails without an error message.
4. **Run it and redirect stdout.** Most translators print the whole document to
   stdout, so always redirect it (`> output.jsonld`). Two exceptions:
   `specimen2jsonld` given a *file* of IDs writes one `<NHASH_ID>.jsonld` per
   line into the current directory, and `taxonomy2jsonld` writes to `-o`
   (default `HMBA_BG_taxonomy.jsonld`).
5. **Check the output.** Run the bundled summarizer on every file produced:
   ```bash
   python skills/bkbit-data-translators/scripts/summarize_jsonld.py output.jsonld
   ```
   It checks that the file is JSON-LD with an `@context` and a non-empty
   `@graph`, counts objects by type, and flags leftover error text. An empty or
   tiny `@graph` usually means the translator skipped records and only printed
   a warning. Scroll back through stderr and report those messages to the user.
6. **Report back.** Say which command you ran, where the output went, and the
   object counts by type. Pass on any skipped records or warnings exactly as
   printed.

## Output Formats

- `-o turtle` works for `specimen2jsonld` and `gff2jsonld`.
- `taxonomy2jsonld` accepts `-f turtle` (its `-o` sets the output file) but
  currently writes JSON-LD anyway. `filemanifest2jsonld` and the
  anatomical-structure translator write JSON-LD only. To get Turtle from any of
  these, convert the JSON-LD with `bkbit.utils.serialize_to_ttl.convert_jsonld_to_ttl`.
- Converting to Turtle fetches the remote JSON-LD `@context`, so it needs
  network access. If conversion fails, bkbit prints `Error during conversion:
  …` in place of Turtle and still exits 0. Check the output (step 5) instead of
  relying on the exit code.

## Using bkbit From Python

Every CLI command wraps a class you can import. Use the class directly when the
user wants the objects themselves, for a notebook, or to batch runs:

```python
from bkbit.data_translators.genome_annotation_translator import Gff3

gff3 = Gff3(url, assembly_accession=None, assembly_strain=None)
gff3.setup()            # parses the URL, downloads the file
gff3.parse_gff3_file()  # organism, assembly, checksum, annotation objects
gff3.parse()            # gene annotations
jsonld = gff3.serialize_to_jsonld()
```

Each reference file covers the matching class (`SpecimenPortal`,
`BKETaxonomy`, `AnS`, ...).

## Guardrails

- **Never print, log, or commit the Specimen Portal token.** Tell the user to
  set it with `export jwt_token=...` in their own shell, and check only that it
  is set (`test -n "$jwt_token"`).
- Translators overwrite fixed filenames in the current directory
  (`<NHASH_ID>.jsonld`, `HMBA_BG_taxonomy.jsonld`,
  `file_manifest_library_aliquots.txt`, `gff3_translator_*.log`). Run them from
  a dedicated output directory.
- Don't edit the generated JSON-LD by hand to fix a failed record. Fix the
  input, or report the translator bug upstream at
  <https://github.com/brain-bican/bkbit/issues>.
- Object `id`s like `urn:bkbit:<sha256>` are hashes of the object's
  attributes, so the same input gives the same IDs every run. Different IDs for
  the same input mean the input changed.
