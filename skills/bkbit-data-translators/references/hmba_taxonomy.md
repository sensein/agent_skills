# taxonomy2jsonld: HMBA cell-type taxonomy to BKE Taxonomy objects

Source: `bkbit/data_translators/HMBA_annotation_translator.py`.

Builds a [BKE Taxonomy](https://github.com/brain-bican/models) graph from the
Human and Mammalian Brain Atlas (HMBA) basal ganglia taxonomy CSVs. It writes
four levels of `CellTypeTaxon` (Neighborhood > Class > Subclass > Group), plus
`DisplayColor`, `Abbreviation`, `CellTypeSet`, and `SpatialProportions`.

## Command

```bash
bkbit taxonomy2jsonld ANNOTATION_CSV ABBREVIATION_CSV DESCRIPTION_CSV [-o OUTPUT] [-f jsonld|turtle]
```

| Option | Meaning |
| --- | --- |
| `-o`, `--output_file` | Output path. Default `HMBA_BG_taxonomy.jsonld`. |
| `-f`, `--output_format` | `jsonld` (default) or `turtle`. |

The flags here are **the other way round from the rest of bkbit**: `-o` is
the output *file* and `-f` is the *format*. The command writes to the file,
not stdout.

Current bkbit (see `HMBA_annotation_translator.py`) accepts `-f turtle` but
always writes JSON-LD. For Turtle, convert the result afterwards:

```python
from bkbit.utils.serialize_to_ttl import convert_jsonld_to_ttl
ttl = convert_jsonld_to_ttl(open("HMBA_BG_taxonomy.jsonld").read())
```

## Input files

All three files must exist (Click checks). They are read with `pandas.read_csv`.

**Abbreviation CSV**: columns `token`, `meaning`, `type`,
`primary_identifier`, `secondary_identifier`. `type` must be one of
`cell_type`, `gene`, `anatomical`. Rows with any other type are printed and
skipped.

**Annotation CSV**: one row per Group. The translator picks each level's
columns **by position**, not by name:

| Column positions (0-based) | Level |
| --- | --- |
| 0–25 | Group |
| 26–32 | Subclass |
| 33–39 | Class |
| 40–46 | Neighborhood |

Within each level it reads these columns by name, where `<level>` is the
lowercase level name:

- `<Level>`: taxon name (for example `Group`, `Subclass`)
- `accession_<level>`, `display_order_<level>`
- `tokens_<level>`: `|`-separated abbreviation tokens, resolved against the
  abbreviation file
- `CL_ID_<level>`: Cell Ontology xref
- `color_hex_<level>`: becomes a `DisplayColor`

Group rows can also have `spatial_regional_proportions`,
`spatial_proportions_{marmoset,macaque,human}` (as
`STR:0.5, GPe:0.2, ...`, where region keys are the `Region` enum names `STR`,
`GPe`, `GPi`, `SN`, `Adj`, `STH`) and
`curated_markers_to_{primates,mouse}` (comma-separated gene symbols).

Because columns are picked by position, a file with columns reordered, added,
or removed gives silently wrong taxa. Before running, confirm the column order
with the user or with `head -1 annotation.csv | tr ',' '\n' | cat -n`. An
unknown region key raises `KeyError`, and an empty `tokens_<level>` cell
raises `AttributeError: 'float' object has no attribute 'split'`.

**Description CSV**: columns `name`, `label`, `description`, `order`. `name`
must be one of `Neighborhood`, `Class`, `Subclass`, `Group`. Each row becomes a
`CellTypeSet` holding every taxon at that level.

## Notes

- The translator prints debug lines (`Parsing spatial proportions...`, whole
  rows) to stdout. They are noise, not errors.
- IDs are `urn:bkbit:<sha256 of attributes>`, and a child's attributes include
  its parent's ID. So a change high in the hierarchy changes the IDs of
  everything below it.

## Python API

```python
import pandas as pd
from bkbit.data_translators.HMBA_annotation_translator import BKETaxonomy

tax = BKETaxonomy()
tax.parse_abbreviations(pd.read_csv("abbrev.csv").fillna(""))
for _, row in pd.read_csv("annotation.csv").iterrows():
    n = tax.parse_taxonomy_level(row.iloc[40:47], "Neighborhood")
    c = tax.parse_taxonomy_level(row.iloc[33:40], "Class", parent_id=n)
    s = tax.parse_taxonomy_level(row.iloc[26:33], "Subclass", parent_id=c)
    tax.parse_taxonomy_level(row.iloc[0:26], "Group", parent_id=s)
tax.parse_cell_type_set(pd.read_csv("description.csv"))
# Objects: tax.group_ctt, tax.subclass_ctt, tax.class_ctt, tax.neighborhood_ctt,
#          tax.display_colors, tax.abbreviations, tax.cell_type_sets, tax.spatial_proportions
```

The Python API lets you change the column slices for a differently laid-out
annotation file without editing bkbit.
