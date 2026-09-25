# Anatomical structure CSVs to Anatomical Structure objects (Python only)

Source: `bkbit/data_translators/anatomical_structure_translator.py`.

This translator has **no `bkbit` subcommand**. Call it from Python. It reads
every `*.csv` in a folder and writes
[Anatomical Structure](https://brain-bican.github.io/models/index_anatomical_structure/)
objects (atlases, spaces, image datasets, parcellation terminologies, term
sets, terms, annotations, color schemes) to one JSON-LD file in **that same
folder**.

```python
from bkbit.data_translators.anatomical_structure_translator import AnS

AnS().provide_data("path/to/csv_dir", "anatomical_structure.jsonld")
# -> path/to/csv_dir/anatomical_structure.jsonld
```

## How CSVs are matched

The translator identifies each CSV by its **exact set of header columns**
(order doesn't matter). The headers must match one of the `generate_*`
methods' parameter names exactly. A CSV with a missing, extra, or misspelled
column is **skipped without a warning**. If a class is missing from the
output, check that file's header against this table first:

| Object | Required header (exact set) |
| --- | --- |
| `ParcellationAtlas` | `label,name,description,specialization_of,revision_of,version,anatomical_space_label,anatomical_annotation_set_label,parcellation_terminology_label` |
| `AnatomicalSpace` | `label,name,description,version,image_dataset_label` |
| `AnatomicalAnnotationSet` | `label,name,description,revision_of,version,anatomical_space_label` |
| `ParcellationAnnotation` | `internal_identifier,anatomical_annotation_set_label,voxel_count` |
| `ParcellationTerminology` | `label,name,description,revision_of,version` |
| `ParcellationTermSet` | `label,name,description,parcellation_terminology_label,parcellation_term_set_order,parcellation_parent_term_set_label` |
| `ParcellationTerm` | `name,symbol,description,parcellation_term_set_label,parcellation_terminology_label,parcellation_term_identifier,parcellation_term_order,parcellation_parent_term_set_label,parcellation_parent_term_identifier` |
| `ParcellationAnnotationTermMap` | `internal_identifier,anatomical_annotation_set_label,parcellation_term_identifier,parcellation_term_set_label,parcellation_terminology_label` |
| `ParcellationColorScheme` | `label,name,description,revision_of,version,parcellation_terminology_label` |
| `ParcellationColorAssignment` | `parcellation_color_scheme_label,parcellation_term_identifier,parcellation_terminology_label,color_hex_triplet` |
| `ImageDataset` | `label,name,description,revision_of,version,x_direction,y_direction,z_direction,x_size,y_size,z_size,x_resolution,y_resolution,z_resolution,unit` |

To check headers quickly:

```bash
for f in path/to/csv_dir/*.csv; do echo "$f: $(head -1 "$f")"; done
```

## Value notes

- `ImageDataset` direction columns take an `ANATOMICALDIRECTION` name:
  `left_to_right`, `posterior_to_anterior`, `inferior_to_superior`,
  `superior_to_inferior`, or `anterior_to_posterior` (`-` works in place of
  `_`). `unit` takes a `DISTANCEUNIT` **name** (`millimeter`, `micrometer`,
  `meter`), not the abbreviation (`mm`). Unknown values raise
  `AttributeError`.
- Empty CSV cells come through as empty strings (`"revision_of": ""`), not
  as missing values.
- `label` values become the objects' `id`s as given, so use CURIEs or IRIs.
- Every run overwrites the output file in the input folder. Don't use a name
  that matches one of the input files.
