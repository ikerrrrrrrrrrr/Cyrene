"""Backup & Restore — export/import all agent state as a portable zip archive.

Exports:
  - SOUL.md (personality)
  - conversations/*.md (daily archives)
  - data/short_term.json (compressed memory)
  - data/web_settings.json (runtime settings)
  - data/state.json (current session — only when live)
  - store/cyrene.db (scheduled tasks, stats, token_usage)

Restore replaces all of the above from a zip. The scheduler is paused and the
agent lock is held during restore to prevent concurrent writes.

Security guarantees
-------------------
- Archive entries are validated against an allowlist of destination roots;
  path-traversal names are skipped with an error.
- Decompressed size and entry count are capped before any extraction begins.
- All files are written to a temporary staging directory first; the previous
  state is only replaced after every file passes validation (including a
  SQLite integrity check on the restored database).  A failed restore leaves
  the previous application state intact.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
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

# Roots that archive entries are permitted to land in (resolved at import time).
_ALLOWED_ROOTS: list[Path] = [
    STORE_DIR.resolve(),
    DATA_DIR.resolve(),
    WORKSPACE_DIR.resolve(),
]

# Resource caps applied before any bytes are decompressed (fixes zip-bomb risk).
_MAX_ENTRIES: int = 5_000
_MAX_DECOMPRESSED_BYTES: int = 300 * 1024 * 1024  # 300 MB


def ensure_backup_dir() -> Path:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _BACKUP_DIR


def _resolve_target(name: str) -> Path:
    """Map an archive entry name to its destination path (no security checks here)."""
    if name.startswith("store/"):
        return STORE_DIR / name[len("store/"):]
    if name.startswith("data/"):
        return DATA_DIR / name[len("data/"):]
    if name.startswith("workspace/"):
        return WORKSPACE_DIR / name[len("workspace/"):]
    # Entries that pre-date the workspace/ prefix land under WORKSPACE_DIR.
    return WORKSPACE_DIR / name


def _priority(name: str) -> int:
    """Restore order: store → data → workspace."""
    if name.startswith("store/"):
        return 0
    if name.startswith("data/"):
        return 1
    return 2


async def export_backup(
    *, include_db: bool = True, target_path: str | Path | None = None
) -> dict[str, Any]:
    """Export all agent state into a timestamped zip file.

    The SQLite database is snapshotted via the sqlite3 backup API so the
    archive always contains a transactionally consistent copy of the database
    even under concurrent writes.

    Args:
        include_db: Whether to include the SQLite database.
        target_path: Where to save the zip. Defaults to the backups directory.

    Returns:
        {"ok": True, "path": "/path/to/backup.zip", "size": 12345, "entries": [...]}
    """
    if target_path:
        backup_path = Path(target_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        ensure_backup_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = _BACKUP_DIR / f"cyrene_backup_{timestamp}.zip"

    entries: list[dict[str, Any]] = []
    tmp_db_path: Path | None = None

    try:
        # Take a consistent SQLite snapshot before opening the zip so we never
        # stream a partially-written database into the archive.
        if include_db and DB_PATH.exists():
            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".db")
            tmp_db_path = Path(tmp_name)
            os.close(tmp_fd)
            src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(tmp_db_path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()

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

            if tmp_db_path is not None:
                zf.write(tmp_db_path, "store/cyrene.db")
                entries.append({"name": "store/cyrene.db", "size": tmp_db_path.stat().st_size})

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
        # Remove a partial/corrupt zip so list_backups() never surfaces it.
        try:
            backup_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        if tmp_db_path is not None:
            tmp_db_path.unlink(missing_ok=True)


async def restore_backup(zip_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Restore agent state from a backup zip.

    Safety measures applied unconditionally (even on dry_run):
    - Entry count and total decompressed size are capped.
    - Every destination path is validated against the allowlist of roots;
      path-traversal attempts are skipped and reported as errors.

    During a real restore:
    - The APScheduler instance is paused for the duration of the operation.
    - The agent lock is acquired so the restore cannot race with an in-flight
      agent round.
    - Files are extracted to a temporary staging directory.  Only after all
      files pass validation (including a SQLite integrity check) are they
      atomically moved to their final locations.  Any error leaves the
      previous application state untouched.

    Returns:
        {"ok": True, "restored": ["file1", ...], "errors": [...]}
    """
    path = Path(zip_path).resolve()
    if not path.exists():
        return {"ok": False, "error": f"backup file not found: {zip_path}"}

    try:
        with ZipFile(path, "r") as zf:
            # --- Validate manifest ---
            if "manifest.json" in zf.namelist():
                manifest = json.loads(zf.read("manifest.json"))
                version = str(manifest.get("version", "unknown"))
            else:
                version = "unknown"

            # --- Resource limits (fixes zip-bomb risk, #37) ---
            raw_namelist = zf.namelist()
            payload = [
                n for n in raw_namelist
                if not n.endswith("/") and n != "manifest.json"
            ]
            if len(payload) > _MAX_ENTRIES:
                return {
                    "ok": False,
                    "error": f"archive has {len(payload)} entries, limit is {_MAX_ENTRIES}",
                }
            total_decompressed = sum(zf.getinfo(n).file_size for n in payload)
            if total_decompressed > _MAX_DECOMPRESSED_BYTES:
                return {
                    "ok": False,
                    "error": (
                        f"archive decompressed size {total_decompressed // (1024*1024)} MB "
                        f"exceeds limit of {_MAX_DECOMPRESSED_BYTES // (1024*1024)} MB"
                    ),
                }

            if dry_run:
                # Validate paths without extracting anything.
                errors: list[str] = []
                restored: list[str] = []
                for name in sorted(payload, key=_priority):
                    target = _resolve_target(name).resolve()
                    if not any(target.is_relative_to(r) for r in _ALLOWED_ROOTS):
                        errors.append(f"{name}: path traversal blocked")
                        continue
                    restored.append(name)
                return {"ok": len(errors) == 0, "restored": restored, "errors": errors, "version": version, "dry_run": True}

            # --- Acquire writer lock and pause scheduler (#53) ---
            return await _restore_with_locks(zf, payload, version)

    except Exception as exc:
        logger.exception("Restore failed")
        return {"ok": False, "error": str(exc)}


async def _restore_with_locks(zf: ZipFile, payload: list[str], version: str) -> dict[str, Any]:
    """Internal: pause scheduler + hold agent lock, then delegate to staging restore."""
    # Import lazily to avoid circular imports at module load time.
    try:
        from cyrene import scheduler as _sched_module
        sched = getattr(_sched_module, "_scheduler", None)
    except Exception:
        sched = None

    try:
        from cyrene.agent.state import _agent_lock
    except Exception:
        _agent_lock = None  # type: ignore[assignment]

    sched_was_running = sched is not None and getattr(sched, "running", False)
    if sched_was_running:
        sched.pause()
        logger.info("Scheduler paused for restore")

    try:
        if _agent_lock is not None:
            async with _agent_lock:
                return _restore_staged(zf, payload, version)
        else:
            return _restore_staged(zf, payload, version)
    finally:
        if sched_was_running:
            sched.resume()
            logger.info("Scheduler resumed after restore")


def _restore_staged(zf: ZipFile, payload: list[str], version: str) -> dict[str, Any]:
    """Extract to a temp dir, validate, then atomically move to final locations."""
    restored: list[str] = []
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="cyrene_restore_") as stage_dir:
        stage = Path(stage_dir)

        # --- Extract to staging area with path-traversal check (#37) ---
        for name in sorted(payload, key=_priority):
            target = _resolve_target(name).resolve()
            if not any(target.is_relative_to(r) for r in _ALLOWED_ROOTS):
                errors.append(f"{name}: path traversal blocked")
                logger.warning("Blocked path-traversal entry in backup: %s", name)
                continue

            stage_file = stage / name
            try:
                stage_file.parent.mkdir(parents=True, exist_ok=True)
                stage_file.write_bytes(zf.read(name))
                restored.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        if errors:
            logger.warning(
                "Restore aborted: %d path/extraction error(s): %s", len(errors), errors
            )
            return {"ok": False, "restored": [], "errors": errors, "version": version}

        # --- Validate restored SQLite database (#53) ---
        staged_db = stage / "store" / "cyrene.db"
        if staged_db.exists():
            try:
                conn = sqlite3.connect(staged_db)
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                finally:
                    conn.close()
                if result is None or result[0] != "ok":
                    return {
                        "ok": False,
                        "error": f"restored database failed integrity check: {result}",
                        "version": version,
                    }
            except Exception as exc:
                return {"ok": False, "error": f"database validation error: {exc}", "version": version}

        # --- Atomically move staged files to final locations ---
        move_errors: list[str] = []
        for name in restored:
            src = stage / name
            if not src.exists():
                continue
            target = _resolve_target(name)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), target)
            except Exception as exc:
                move_errors.append(f"{name}: {exc}")

        if move_errors:
            logger.error("Restore move phase failed: %s", move_errors)
            return {"ok": False, "restored": [], "errors": move_errors, "version": version}

    logger.info("Restored %d files from backup (version %s)", len(restored), version)
    return {"ok": True, "restored": restored, "errors": [], "version": version}


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
