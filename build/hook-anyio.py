"""PyInstaller hook for anyio — ensure all backends and submodules are collected.

anyio uses importlib.import_module() to dynamically load backends
(anyio._backends._asyncio / anyio._backends._trio), which PyInstaller's
static AST-based tracing cannot follow.  On Windows the problem is
especially acute because the ProactorEventLoop requires specific
sub-modules that are not reachable through the normal import graph.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

# ---- submodules (belt-and-suspenders: both collect_all and explicit list) ----
datas, binaries, hiddenimports = collect_all("anyio")

# Double-ensure the dynamically-loaded backends are in the list
for _mod in ("anyio._backends._asyncio", "anyio._backends._trio"):
    if _mod not in hiddenimports:
        hiddenimports.append(_mod)

# Also make sure the core eventloop module is explicitly listed
for _mod in (
    "anyio._core._eventloop",
    "anyio._core._exceptions",
    "anyio._core._synchronization",
    "anyio._core._tasks",
    "anyio._core._fileio",
    "anyio._core._sockets",
    "anyio._core._streams",
    "anyio._core._subprocesses",
    "anyio._core._signals",
    "anyio._core._resources",
    "anyio._core._testing",
    "anyio._core._typedattr",
    "anyio._core._tempfile",
    "anyio._core._contextmanagers",
    "anyio._core._asyncio_selector_thread",
    "anyio.abc",
    "anyio.abc._eventloop",
    "anyio.abc._resources",
    "anyio.abc._sockets",
    "anyio.abc._streams",
    "anyio.abc._subprocesses",
    "anyio.abc._tasks",
    "anyio.abc._testing",
    "anyio.streams",
    "anyio.streams.buffered",
    "anyio.streams.file",
    "anyio.streams.memory",
    "anyio.streams.stapled",
    "anyio.streams.text",
    "anyio.streams.tls",
    "anyio.from_thread",
    "anyio.functools",
    "anyio.lowlevel",
    "anyio.pytest_plugin",
    "anyio.to_interpreter",
    "anyio.to_process",
    "anyio.to_thread",
):
    if _mod not in hiddenimports:
        hiddenimports.append(_mod)

try:
    datas.extend(copy_metadata("anyio"))
except Exception:
    pass
