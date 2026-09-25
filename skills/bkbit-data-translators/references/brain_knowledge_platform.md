# Brain Knowledge Platform exports: filemanifest2jsonld and list-library-aliquot

Sources: `bkbit/data_translators/file_manifest_translator.py` and
`bkbit/data_translators/specimen_metadata_translator.py`.

Both commands read CSV exports from the
[Brain Knowledge Platform](https://knowledge.brain-map.org/). To get the CSVs:

1. Open **Data/Projects** (top right menu).
2. Filter **Specimen Type** to "Library Aliquot".
3. Open the project (for example "BICAN Rapid Release Inventory: Single cell
   transcriptomics and epigenomics") and click **BROWSE SPECIMENS**.
4. Click the download icon and choose **File Manifest** (for
   `filemanifest2jsonld`) or **Metadata** (for `list-library-aliquot`).

## filemanifest2jsonld: file manifest to DigitalAsset objects

```bash
bkbit filemanifest2jsonld [-l] FILE_MANIFEST_CSV > output.jsonld
```

| Option | Meaning |
| --- | --- |
| `-l`, `--list_library_aliquots` | Also write the unique `Specimen ID`s to `./file_manifest_library_aliquots.txt`. |

> **Known issue (bkbit main as of 2026-09):** every run fails with
> `ValidationError: 1 validation error for DigitalAsset content_url: Input
> should be a valid string`. The translator passes `content_url` as a list,
> but the current Library Generation model defines it as a single string. If
> the user's installed bkbit shows this error, tell them it's an upstream bug
> and point them to <https://github.com/brain-bican/bkbit/issues>. The fix is
> one line in `file_manifest_translator.py` (`content_url=row['Archive URI']`).
> Don't patch their installed package unless they ask. `-l` fails the same
> way, since the ID file is only written after the objects are built.

Required CSV columns, spelled exactly like this: `Specimen ID`, `File Name`,
`Checksum`, `File Type`, `Archive`, `Archive URI`. A missing column raises
`KeyError: '<column>'`.

For each row the command writes:

- one `DigitalAsset` with `id` = `<Archive>:<File Name>`, `was_derived_from` =
  `NIMP:<Specimen ID>`, `content_url` = `[Archive URI]`, and `data_type` = the
  read type taken from the file name;
- one `Checksum` (MD5, the `Checksum` column) with a random `urn:uuid:` ID.

The read type is the second-to-last `_` token of the file name, with
everything after the first `.` removed. It assumes Illumina-style names
(`<sample>_S1_L001_R1_001.fastq.gz` gives `R1`), so other naming schemes get
a meaningless `data_type`.
Checksum IDs are random UUIDs, so they differ on every run, unlike the
hash-based IDs from the other translators.

Output is JSON-LD only; there is no Turtle option.

To link the files to their specimens, run `-l`, then pass
`file_manifest_library_aliquots.txt` to `bkbit specimen2jsonld` (see
[specimen_portal.md](specimen_portal.md)). The `was_derived_from` values
(`NIMP:LA-...`) refer to the Specimen Portal IDs of those Library Aliquots.

## list-library-aliquot: specimen metadata to Library Aliquot IDs

```bash
bkbit list-library-aliquot SpecimenMetadata.csv > library_aliquots.txt
```

Needs a `Specimen ID` column (it raises `ValueError` otherwise) and prints
each value that starts with `LA`, one per line. The output file works as input
to `bkbit specimen2jsonld library_aliquots.txt`.

The command is `list-library-aliquot` with hyphens. Click derives the name
from the Python function `list_library_aliquot`.
