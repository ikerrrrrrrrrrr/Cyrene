"""PyInstaller hook for simplexng — inject vendor path so vendored searx modules
are discoverable during build-time analysis, then collect everything.

simplexng vendors its own copy of searx under _vendor/searx/.  At runtime
simplexng/simplexng.py adds _vendor/ to sys.path before importing anything
else, but PyInstaller's static analysis does not execute that module — so
imports like ``import searx.unixthreadname`` fail during collection.

This hook mirrors the runtime path injection so that collect_all can recurse
into the vendored tree and discover all transitive dependencies.
"""

import sys
from pathlib import Path

import simplexng

_vendor = Path(simplexng.__path__[0]) / "_vendor"
if str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = collect_all("simplexng")

try:
    datas.extend(copy_metadata("simplexng"))
except Exception:
    pass
