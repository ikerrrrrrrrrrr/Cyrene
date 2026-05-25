"""Backup & Restore — export/import all agent state as a portable zip archive.

Exports:
  - SOUL.md (personality)
  - conversations/*.md (daily archives)
  - data/short_term.json (compressed memory)
  - data/web_settings.json (runtime settings)
  - data/state.json (current session — only when live)
  - store/cyrene.db (scheduled tasks, stats, token_usage)

Restore replaces all of the above from a zip. The agent must be idle during
restore to avoid state corruption.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED

from cyrene.config import BASE_DIR, DATA_DIR, DB_PATH, STORE_DIR, WORKSPACE_DIR

logger = logging.getLogger(__name__)

_EXPORT_INCLUDE: list[tuple[Path, str]] = [
    (WORKSPACE_DIR / "SOUL.md", "workspace/SOUL.md"),
    (WORKSPACE_DIR / "conversations", "conversations/"),
    (DATA_DIR / "short_term.json", "data/short_term.json"),
    (DATA_DIR / "web_settings.json", "data/web_settings.json"),
    (DATA_DIR / "state.json", "data/state.json"),
]

_BACKUP_DIR = BASE_DIR / "backups"


def ensure_backup_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


async def export_backup(*, include_db: bool = True) -> dict[str, Any]:
    """Export all agent state into a timestamped zip file.

    Returns::
        {"ok": True, "path": "/path/to/backup.zip", "size": 12345, "entries": [...]}
    """
    ensure_backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = _BACKUP_DIR / f"cyrene_backup_{timestamp}.zip"

    entries: list[dict[str, Any]] = []
    try:
        with ZipFile(backup_path, "w", ZIP_DEFLATED) as zf:
            for source, arcname in _EXPORT_INCLUDE:
                if arcname.endswith("/"):
                    if source.exists() and source.is_dir():
                        for file in sorted(source.rglob("*")):
                            if file.is_file():
                                rel = file.relative_to(WORKSPACE_DIR)
                                zf.write(file, str(rel))
                                entries.append({"name": str(rel), "size": file.stat().st_size})
                elif source.exists():
                    zf.write(source, arcname)
                    entries.append({"name": arcname, "size": source.stat().st_size})

            # Include db unless excluded
            if include_db and DB_PATH.exists():
                zf.write(DB_PATH, "store/cyrene.db")
                entries.append({"name": "store/cyrene.db", "size": DB_PATH.stat().st_size})

            # Include an export manifest
            manifest = {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "version": "0.4",
                "entries": [e["name"] for e in entries],
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        size = backup_path.stat().st_size
        logger.info("Backup created: %s (%d bytes, %d entries)", backup_path, size, len(entries))
        return {"ok": True, "path": str(backup_path), "size": size, "entries": entries}
    except Exception as exc:
        logger.exception("Backup failed")
        return {"ok": False, "error": str(exc)}


async def restore_backup(zip_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Restore agent state from a backup zip.

    Returns::
        {"ok": True, "restored": ["file1", ...], "errors": [...]}
    """
    path = Path(zip_path).resolve()
    if not path.exists():
        return {"ok": False, "error": f"backup file not found: {zip_path}"}

    restored: list[str] = []
    errors: list[str] = []

    try:
        with ZipFile(path, "r") as zf:
            # Validate manifest
            if "manifest.json" in zf.namelist():
                manifest = json.loads(zf.read("manifest.json"))
                _version = str(manifest.get("version", ""))
            else:
                _version = "unknown"

            namelist = zf.namelist()

            # Order matters: restore store/ first, then data/, then workspace/
            priority = lambda n: (0 if n.startswith("store/") else 1 if n.startswith("data/") else 2)
            for name in sorted(namelist, key=priority):
                if name.endswith("/") or name == "manifest.json":
                    continue
                if dry_run:
                    restored.append(name)
                    continue
                # Resolve target path
                if name.startswith("store/"):
                    target = STORE_DIR / name[len("store/"):]
                elif name.startswith("data/"):
                    target = DATA_DIR / name[len("data/"):]
                elif name.startswith("workspace/"):
                    target = WORKSPACE_DIR / name[len("workspace/"):]
                else:
                    target = WORKSPACE_DIR / name

                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
                    restored.append(name)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")

        if errors:
            logger.warning("Restore completed with %d errors: %s", len(errors), errors)
        logger.info("Restored %d files from %s", len(restored), path.name)
        return {"ok": True, "restored": restored, "errors": errors, "version": _version}
    except Exception as exc:
        logger.exception("Restore failed")
        return {"ok": False, "error": str(exc)}


def list_backups() -> list[dict[str, Any]]:
    """Return available backup files sorted by creation time (newest first)."""
    ensure_backup_dir()
    backups: list[dict[str, Any]] = []
    for f in sorted(_BACKUP_DIR.glob("cyrene_backup_*.zip"), reverse=True):
        try:
            backups.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
        except Exception:
            continue
    return backups


async def delete_backup(name: str) -> bool:
    """Delete a backup file by name. Name must be a plain filename, not a path."""
    safe = Path(name).name  # strip any path components
    target = _BACKUP_DIR / safe
    if not target.exists() or not target.name.startswith("cyrene_backup_"):
        return False
    try:
        target.unlink()
        return True
    except Exception:
        return False
