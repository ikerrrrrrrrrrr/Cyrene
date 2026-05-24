"""Version helpers shared by runtime components and build metadata."""

from __future__ import annotations

import importlib.metadata
import sys
from functools import lru_cache
from pathlib import Path


def _bundle_contents_dir() -> Path | None:
    exe = Path(sys.executable).resolve()
    parts = exe.parts
    for idx, part in enumerate(parts):
        if part.endswith(".app") and idx + 2 < len(parts) and parts[idx + 1] == "Contents":
            return Path(*parts[: idx + 2])
    return None


def _pyproject_candidates() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "pyproject.toml")
    bundle_contents = _bundle_contents_dir()
    if bundle_contents is not None:
        candidates.append(bundle_contents / "Resources" / "pyproject.toml")
        candidates.append(bundle_contents / "Frameworks" / "pyproject.toml")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "pyproject.toml")
    return candidates


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the application version, preferring pyproject.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-relevant-import]

    for pyproject in _pyproject_candidates():
        if pyproject.exists():
            with open(pyproject, "rb") as f:
                return tomllib.load(f)["project"]["version"]

    try:
        return importlib.metadata.version("cyrene")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def get_version_label() -> str:
    """Return the user-facing version label."""
    return f"v{get_version()}"
