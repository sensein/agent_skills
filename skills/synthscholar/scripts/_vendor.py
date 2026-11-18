"""Make the vendored ``synthscholar`` package the one these scripts import.

The skill carries its own copy of SynthScholar in ``vendor/synthscholar``
(see ``vendor/VENDORED.md``), so it needs no application checkout and no
``PYTHONPATH``. Importing this module puts that copy first on ``sys.path``,
ahead of any other installed ``synthscholar``, so the skill always runs the
code it was tested with.
"""
from __future__ import annotations

import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor"

if (VENDOR / "synthscholar" / "__init__.py").is_file():
    if str(VENDOR) in sys.path:
        sys.path.remove(str(VENDOR))
    sys.path.insert(0, str(VENDOR))
    for name in [m for m in sys.modules if m == "synthscholar" or m.startswith("synthscholar.")]:
        mod = sys.modules[name]
        if not str(getattr(mod, "__file__", "") or "").startswith(str(VENDOR)):
            del sys.modules[name]        # an other copy was imported first: drop it
