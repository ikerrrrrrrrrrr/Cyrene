"""Installed external skill storage and prompt injection helpers."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cyrene.config import DATA_DIR
from cyrene.settings_store import get as get_setting, set_ as set_setting

_SKILLS_DIR = DATA_DIR / "installed_skills"


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


def extract_skill_summary(path: Path) -> tuple[str, str, str]:
    text = read_skill_text(path)
    lines = [line.rstrip() for line in text.splitlines()]
    name = path.stem
    desc = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            name = stripped.lstrip("#").strip() or name
            continue
        desc = stripped
        break
    if not desc:
        desc = "External skill file"
    return name, desc[:240], text[:12000]


def skill_payload_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    stored_path = Path(str(record.get("stored_path") or "")).expanduser()
    if not stored_path.exists() or not stored_path.is_file():
        return None
    name, desc, preview = extract_skill_summary(stored_path)
    stat = stored_path.stat()
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or name),
        "desc": str(record.get("desc") or desc),
        "enabled": bool(record.get("enabled", True)),
        "installed": True,
        "installed_at": str(record.get("installed_at") or ""),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "size_bytes": stat.st_size,
        "source_path": str(record.get("source_path") or ""),
        "stored_path": str(stored_path),
        "file_name": stored_path.name,
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
    records = skill_settings_records()
    source_resolved = str(source_path.resolve())
    for record in records:
        if str(record.get("source_path") or "").strip() == source_resolved:
            return {"ok": True, "skill": skill_payload_from_record(record), "already_installed": True}

    base_id = slugify_skill_id(source_path.stem)
    skill_id = unique_skill_id(base_id, records)
    dest = skills_storage_dir() / f"{skill_id}{source_path.suffix}"
    shutil.copy2(source_path, dest)
    name, desc, _preview = extract_skill_summary(dest)
    record = {
        "id": skill_id,
        "name": name,
        "desc": desc,
        "enabled": True,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source_path": source_resolved,
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
        "The user installed the following local skills. Treat them as additional operating instructions and preferred workflows when relevant.",
    ]
    budget = max_chars
    for skill in active_skills:
        preview = str(skill.get("preview") or "").strip()
        header = f"### {skill.get('name') or skill.get('id')}\nSource: {skill.get('file_name') or skill.get('stored_path')}\nSummary: {skill.get('desc') or '—'}\n"
        chunk = header + (preview[:3000] if preview else "")
        if len(chunk) > budget:
            chunk = chunk[:budget]
        if not chunk:
            break
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(parts).strip()
