# specimen2jsonld: Specimen Portal records to Library Generation objects

Source: `bkbit/data_translators/library_generation_translator.py`.

Fetches records from the [BICAN Specimen Portal](https://brain-specimenportal.org/)
(NIMP) by NHash ID. It then walks the record's lineage **up** to the Donor
(default) or **down** to the Library Pools (`-d`), and writes one
[Library Generation](https://brain-bican.github.io/models/index_library_generation/)
object for each record it visits.

## Requirements

- A Specimen Portal **Personal API Token** in the environment variable
  `jwt_token` (lowercase). The user gets it from their Specimen Portal profile.
  If it is unset the command raises `ValueError: JWT token is required`.
  ```bash
  export jwt_token='<token>'   # the user runs this; never echo the value
  test -n "$jwt_token" && echo "jwt_token is set"
  ```
- Network access to `https://brain-specimenportal.org/api/v1/nhash_ids/`.

## Command

```bash
bkbit specimen2jsonld [-d] [-o jsonld|turtle] NHASH_ID_OR_FILE
```

| Option | Meaning |
| --- | --- |
| `-d`, `--descendants` | Walk down to Library Pools instead of up to the Donor. |
| `-o`, `--output_format` | `jsonld` (default) or `turtle`. |

`NHASH_ID_OR_FILE` is either a single ID or the path to a text file with one
ID per line. The command treats the argument as a file if a file with that
name exists.

## Examples

```bash
# One record and all its ancestors, to stdout
bkbit specimen2jsonld 'LP-CVFLMQ819998' > LP-CVFLMQ819998.jsonld

# A donor and everything derived from it
bkbit specimen2jsonld -d 'DO-GICE7463' > DO-GICE7463.jsonld

# Many IDs: writes ./<ID>.jsonld (or .ttl) for each line, prints nothing
bkbit specimen2jsonld input_nhash_ids.txt
bkbit specimen2jsonld -d -o turtle donors.txt
```

When the input is a file, IDs run in parallel (`multiprocessing.Pool`), and
the output files go into the **current directory**. Run the command from an
output folder, and make sure the file has no blank lines or stray whitespace.
A blank line is sent as an empty ID and produces an empty `.jsonld` file.

## Supported record categories

The translator maps these NIMP `category` values to model classes. Records of
any other category are skipped with `Unsupported category`:

| NIMP category | Class |
| --- | --- |
| Donor | `Donor` |
| Slab | `BrainSlab` |
| Specimen Dissected ROI | `DissectionRoiPolygon` |
| Tissue | `TissueSample` |
| Dissociated Cell Sample | `DissociatedCellSample` |
| Enriched Cell Sample | `EnrichedCellSample` |
| Barcoded Cell Sample | `BarcodedCellSample` |
| Amplified cDNA | `AmplifiedCdna` |
| Library | `Library` |
| Library Aliquot | `LibraryAliquot` |
| Library Pool | `LibraryPool` |

## Failure modes

The translator **prints errors and carries on** instead of exiting non-zero,
so read stdout and stderr, and check the output:

- `ValueError retrieving ancestors/descendants for '<id>'` or an HTTP status
  code: the token is wrong or expired, or the ID doesn't exist. The output
  will have an empty `@graph`.
- `Missing required field: <field>`: the Specimen Portal record lacks a
  field the model requires, so that one record is left out.
- `Unsupported category`: see the table above.
- Enum-valued fields whose portal value isn't in the model's value set are set
  to null without a warning. If a field the user expects is missing, compare
  the raw record with the model's enum.
- With the default (ancestors) mode, `was_derived_from` links to parents
  outside the fetched set are dropped. Links between records in the output are
  rewritten to the new `urn:bkbit:` IDs.

## Python API

```python
from bkbit.data_translators.library_generation_translator import SpecimenPortal

sp = SpecimenPortal(jwt_token)
sp.parse_nhash_id_bottom_up("LA-...")   # or parse_nhash_id_top_down("DO-...")
jsonld_str = sp.serialize_to_jsonld()
objects = sp.generated_objects          # {nhash_id: pydantic object}
```

## Getting NHash IDs from the Brain Knowledge Platform

To translate every Library Aliquot in a Brain Knowledge Platform project,
export the project's specimen **Metadata** CSV and pipe it through
`bkbit list-library-aliquot` (see
[brain_knowledge_platform.md](brain_knowledge_platform.md)). That produces an
ID file you can pass straight to this command.
