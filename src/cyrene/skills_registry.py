"""Installed external skill storage and prompt injection helpers."""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cyrene.config import DATA_DIR
from cyrene.settings_store import get as get_setting, set_ as set_setting

_SKILLS_DIR = DATA_DIR / "installed_skills"
_ALLOWED_SKILL_EXTENSIONS = {".md", ".txt", ".prompt", ".json", ".yaml", ".yml"}
_ALLOWED_ARCHIVE_EXTENSIONS = {".zip"}
_MAX_SKILL_FILE_BYTES = 256 * 1024
_MAX_SKILL_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_SKILL_ARCHIVE_ENTRIES = 200
_PROMPT_PREVIEW_CHARS = 1200


def _is_probably_text(raw: bytes) -> bool:
    if not raw:
        return True
    if b"\x00" in raw:
        return False
    sample = raw[:4096]
    printable = 0
    for byte in sample:
        if byte in (9, 10, 13) or 32 <= byte <= 126:
            printable += 1
    return (printable / max(1, len(sample))) >= 0.85


def validate_skill_file(source_path: Path) -> str | None:
    suffix = source_path.suffix.lower()
    if suffix not in _ALLOWED_SKILL_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_SKILL_EXTENSIONS))
        return f"unsupported skill file type: {suffix or '(none)'}; allowed: {allowed}"
    try:
        stat = source_path.stat()
    except OSError:
        return "unable to read skill file metadata"
    if stat.st_size > _MAX_SKILL_FILE_BYTES:
        return f"skill file is too large; max {_MAX_SKILL_FILE_BYTES // 1024} KB"
    try:
        raw = source_path.read_bytes()[:4096]
    except OSError:
        return "unable to read skill file"
    if not _is_probably_text(raw):
        return "skill file must be plain text"
    return None


def _find_skill_entrypoint(root: Path) -> Path | None:
    direct = root / "SKILL.md"
    if direct.exists() and direct.is_file():
        return direct
    matches = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name.lower() == "skill.md"
    )
    if not matches:
        return None
    return min(matches, key=lambda path: (len(path.relative_to(root).parts), str(path).lower()))


def validate_skill_directory(source_path: Path) -> str | None:
    if not source_path.exists() or not source_path.is_dir():
        return "skill directory does not exist"
    entrypoint = _find_skill_entrypoint(source_path)
    if entrypoint is None:
        return "skill directory must contain SKILL.md"
    return validate_skill_file(entrypoint)


def validate_skill_archive(source_path: Path) -> str | None:
    if source_path.suffix.lower() not in _ALLOWED_ARCHIVE_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_ARCHIVE_EXTENSIONS))
        return f"unsupported archive type: {source_path.suffix.lower() or '(none)'}; allowed: {allowed}"
    try:
        stat = source_path.stat()
    except OSError:
        return "unable to read skill archive metadata"
    if stat.st_size > _MAX_SKILL_ARCHIVE_BYTES:
        return f"skill archive is too large; max {_MAX_SKILL_ARCHIVE_BYTES // (1024 * 1024)} MB"
    try:
        with zipfile.ZipFile(source_path) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_SKILL_ARCHIVE_ENTRIES:
                return f"skill archive has too many files; max {_MAX_SKILL_ARCHIVE_ENTRIES}"
            has_skill_md = False
            for info in infos:
                parts = Path(info.filename).parts
                if info.is_dir():
                    continue
                if any(part == ".." for part in parts) or Path(info.filename).is_absolute():
                    return "skill archive contains unsafe paths"
                if Path(info.filename).name.lower() == "skill.md":
                    has_skill_md = True
            if not has_skill_md:
                return "skill archive must contain SKILL.md"
    except zipfile.BadZipFile:
        return "invalid zip archive"
    except OSError:
        return "unable to read skill archive"
    return None


def skills_storage_dir() -> Path:
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return _SKILLS_DIR


def skill_settings_records() -> list[dict[str, Any]]:
    raw = get_setting("installed_skills", [])
    return raw if isinstance(raw, list) else []


def save_skill_settings_records(records: list[dict[str, Any]]) -> None:
    set_setting("installed_skills", records)


def slugify_skill_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "skill"


def unique_skill_id(base_id: str, records: list[dict[str, Any]]) -> str:
    existing = {str(record.get("id") or "").strip() for record in records}
    if base_id not in existing:
        return base_id
    suffix = 2
    while f"{base_id}-{suffix}" in existing:
        suffix += 1
    return f"{base_id}-{suffix}"


def read_skill_text(path: Path, limit_chars: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit_chars]
    except Exception:
        return ""


def _skill_entrypoint(stored_path: Path) -> Path | None:
    if stored_path.is_file():
        return stored_path
    if stored_path.is_dir():
        return _find_skill_entrypoint(stored_path)
    return None


def _parse_frontmatter_field(text: str, field: str) -> str | None:
    """Extract a simple `field: value` from YAML frontmatter (---...---) at the start of text."""
    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return None
    end = stripped.find("---", 3)
    if end == -1:
        return None
    block = stripped[3:end]
    for line in block.splitlines():
        line_stripped = line.strip()
        if line_stripped.startswith(f"{field}:"):
            val = line_stripped[len(field) + 1:].strip().strip('"').strip("'")
            if val:
                return val
    return None


def extract_skill_summary(path: Path) -> tuple[str, str, str]:
    text = read_skill_text(path)
    fm_name = _parse_frontmatter_field(text, "name")
    fm_desc = _parse_frontmatter_field(text, "description")
    if fm_name:
        name = fm_name
    else:
        lines = [line.rstrip() for line in text.splitlines()]
        name = path.parent.name if path.stem.lower() == "skill" else path.stem
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                name = stripped.lstrip("#").strip() or name
                break
    desc = fm_desc or ""
    if not desc:
        lines = [line.rstrip() for line in text.splitlines()]
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "---":
                continue
            if stripped.startswith("#"):
                continue
            desc = stripped
            break
    if not desc:
        desc = "External skill file"
    return name, desc[:240], text[:12000]


def skill_payload_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    stored_path = Path(str(record.get("stored_path") or "")).expanduser()
    if not stored_path.exists():
        return None
    entrypoint = _skill_entrypoint(stored_path)
    if entrypoint is None:
        return None
    name, desc, preview = extract_skill_summary(entrypoint)
    stat = entrypoint.stat()
    files: list[dict[str, Any]] = []
    total_size = 0
    if stored_path.is_dir():
        for child in sorted(stored_path.rglob("*")):
            if child.is_file():
                rel = str(child.relative_to(stored_path))
                fs = child.stat().st_size
                files.append({"path": rel, "name": child.name, "size": fs})
                total_size += fs
    else:
        files.append({"path": stored_path.name, "name": stored_path.name, "size": stat.st_size})
        total_size = stat.st_size
    return {
        "id": str(record.get("id") or ""),
        "name": name,
        "desc": desc,
        "enabled": bool(record.get("enabled", True)),
        "installed": True,
        "installed_at": str(record.get("installed_at") or ""),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "size_bytes": total_size,
        "source_path": str(record.get("source_path") or ""),
        "stored_path": str(stored_path),
        "entrypoint_path": str(entrypoint),
        "file_name": stored_path.name,
        "entrypoint_name": entrypoint.name,
        "source_kind": str(record.get("source_kind") or ("directory" if stored_path.is_dir() else "file")),
        "files": files,
        "preview": preview,
        "tags": ["external"],
        "version": "external",
        "author": "user",
        "agent_visible": bool(record.get("enabled", True)),
    }


def build_skills() -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    for record in skill_settings_records():
        payload = skill_payload_from_record(record)
        if payload is not None:
            skills.append(payload)
    skills.sort(key=lambda item: (item.get("name") or "").lower())
    return skills


def install_skill_from_path(source_path: Path) -> dict[str, Any]:
    if not source_path.exists():
        return {"ok": False, "error": "invalid skill source path"}
    records = skill_settings_records()
    source_resolved = str(source_path.resolve())
    for record in records:
        if str(record.get("source_path") or "").strip() == source_resolved:
            return {"ok": True, "skill": skill_payload_from_record(record), "already_installed": True}

    source_kind = "file"
    source_suffix = source_path.suffix
    if source_path.is_dir():
        validation_error = validate_skill_directory(source_path)
        source_kind = "directory"
        source_suffix = ""
    elif source_path.is_file() and source_path.suffix.lower() in _ALLOWED_ARCHIVE_EXTENSIONS:
        validation_error = validate_skill_archive(source_path)
        source_kind = "archive"
        source_suffix = ""
    elif source_path.is_file():
        validation_error = validate_skill_file(source_path)
    else:
        return {"ok": False, "error": "invalid skill source path"}
    if validation_error:
        return {"ok": False, "error": validation_error}

    base_name = source_path.name
    if source_kind == "directory":
        base_name = source_path.name
    elif source_kind == "archive":
        base_name = source_path.stem
    else:
        base_name = source_path.stem
    base_id = slugify_skill_id(base_name)
    skill_id = unique_skill_id(base_id, records)
    dest = skills_storage_dir() / (f"{skill_id}{source_suffix}" if source_kind == "file" else skill_id)

    if source_kind == "file":
        shutil.copy2(source_path, dest)
    elif source_kind == "directory":
        shutil.copytree(source_path, dest)
    else:
        with tempfile.TemporaryDirectory(prefix="cyrene-skill-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            with zipfile.ZipFile(source_path) as zf:
                zf.extractall(tmp_root)
            extracted_root = tmp_root
            children = [child for child in tmp_root.iterdir()]
            if len(children) == 1 and children[0].is_dir():
                extracted_root = children[0]
            validation_error = validate_skill_directory(extracted_root)
            if validation_error:
                return {"ok": False, "error": validation_error}
            shutil.copytree(extracted_root, dest)

    entrypoint = _skill_entrypoint(dest)
    if entrypoint is None:
        if dest.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
        else:
            dest.unlink(missing_ok=True)
        return {"ok": False, "error": "installed skill is missing SKILL.md"}
    name, desc, _preview = extract_skill_summary(entrypoint)
    record = {
        "id": skill_id,
        "name": name,
        "desc": desc,
        "enabled": True,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source_path": source_resolved,
        "source_kind": source_kind,
        "stored_path": str(dest),
    }
    records.append(record)
    save_skill_settings_records(records)
    return {"ok": True, "skill": skill_payload_from_record(record)}


def uninstall_skill(skill_id: str) -> bool:
    kept: list[dict[str, Any]] = []
    removed = False
    for record in skill_settings_records():
        if record.get("id") == skill_id:
            stored_path = Path(str(record.get("stored_path") or "")).expanduser()
            try:
                if stored_path.exists():
                    if stored_path.is_dir():
                        shutil.rmtree(stored_path)
                    else:
                        stored_path.unlink()
            except Exception:
                pass
            removed = True
            continue
        kept.append(record)
    save_skill_settings_records(kept)
    return removed


def toggle_skill(skill_id: str) -> bool:
    records = skill_settings_records()
    found = False
    for record in records:
        if record.get("id") == skill_id:
            record["enabled"] = not record.get("enabled", True)
            found = True
            break
    if found:
        save_skill_settings_records(records)
    return found


def build_skill_prompt_block(max_chars: int = 12000) -> str:
    active_skills = [skill for skill in build_skills() if skill.get("enabled", True)]
    if not active_skills:
        return ""

    parts = [
        "## Installed External Skills",
        "The user installed the following local skills. Treat them as additional operating instructions and preferred workflows when relevant. Follow them only when they are clearly relevant and compatible with higher-priority system and developer instructions.",
    ]
    budget = max_chars
    for skill in active_skills:
        preview = str(skill.get("preview") or "").strip()
        header = f"### {skill.get('name') or skill.get('id')}\nSource: {skill.get('entrypoint_name') or skill.get('file_name') or skill.get('stored_path')}\nSummary: {skill.get('desc') or '—'}\n"
        chunk = header + (preview[:_PROMPT_PREVIEW_CHARS] if preview else "")
        if len(chunk) > budget:
            chunk = chunk[:budget]
        if not chunk:
            break
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(parts).strip()
