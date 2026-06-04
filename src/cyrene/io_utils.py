"""Atomic file I/O helpers.

Prevents JSON state files from being left in a half-written (corrupt) state
if the process crashes mid-write.  The temp-file + os.replace pattern is
atomic on POSIX as long as source and destination share the same filesystem,
which is guaranteed by passing dir=path.parent to mkstemp.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, data: Any) -> None:
    """Serialize *data* as JSON and write it to *path* atomically.

    Writes to a uniquely-named sibling temp file first, then calls
    os.replace so readers always see either the previous complete file or
    the new complete file, never a partial write.  The temp file is
    removed on failure.
    """
    content = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json_safe(path: Path) -> Any:
    """Read and parse a JSON file, returning None on missing file or corruption.

    On JSONDecodeError the damaged file is renamed to <name>.corrupt so it
    can be inspected later, and a WARNING is logged.  Other I/O errors
    (PermissionError, etc.) propagate to the caller.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        corrupt = path.with_suffix(".corrupt")
        try:
            path.rename(corrupt)
            logger.warning("Corrupt JSON at %s — moved to %s (%s)", path.name, corrupt.name, exc)
        except OSError:
            logger.warning("Corrupt JSON at %s (could not rename): %s", path.name, exc)
        return None
