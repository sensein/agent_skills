# Vendored: `synthscholar`

`vendor/synthscholar/` is a copy of the SynthScholar Python package, included so
this skill is self-contained: it does not need the application repository, a
development checkout, or `PYTHONPATH`. The scripts that drive the pipeline or
its exporters import this copy first (`scripts/_vendor.py`), even if another
`synthscholar` is installed, so the skill always runs the code it was tested with.

| | |
| --- | --- |
| Source | SynthScholar application repository (`prisma-review-agent`), package `synthscholar/` |
| Commit | `67e8de2` (content identical to `411ea90`) |
| Package version | 0.0.11 (the PyPI release of the same number predates the provenance fields and whole-document reading this skill needs) |
| Licence | Apache-2.0, `vendor/LICENSE` |
| Modified | No. The files are byte-identical to that commit; caches were left out |

Third-party libraries it needs are listed in `requirements.txt` at the skill
root (`pip install -r requirements.txt`; `python scripts/check_deps.py` checks
them).

To refresh it from a newer application commit, copy the package over this
directory without `__pycache__`, update the commit above, and run the skill's
tests (`python -m pytest tests -q`). Do not edit files here; a change belongs in
the application and arrives with the next refresh.
