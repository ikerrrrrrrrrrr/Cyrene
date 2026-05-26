import base64
import hashlib
import json
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any

from PIL import Image
from pypdf import PdfReader

from cyrene.config import DATA_DIR
from cyrene.call_llm import call_llm
from cyrene.llm import _assistant_text, _truncate

UPLOADS_DIR = DATA_DIR / "webui_uploads"
EXPORTS_DIR = DATA_DIR / "webui_exports"

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}
_PDF_EXTENSIONS = {".pdf"}
_MULTIMODAL_MODEL_HINTS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.5",
    "gpt-5",
    "gemini",
    "claude-3",
    "claude-4",
    "qwen",
    "qwen-vl",
    "vl",
    "vision",
    "glm-4v",
    "internvl",
    "minicpm-v",
)


def _sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".analysis.json")


def is_uploaded_attachment_path(path_str: str) -> bool:
    try:
        resolved = Path(path_str).resolve()
        root = UPLOADS_DIR.resolve()
        return resolved == root or root in resolved.parents
    except Exception:
        return False


def is_exported_attachment_path(path_str: str) -> bool:
    try:
        resolved = Path(path_str).resolve()
        root = EXPORTS_DIR.resolve()
        return resolved == root or root in resolved.parents
    except Exception:
        return False


def is_pdf_path(path: Path) -> bool:
    return path.suffix.lower() in _PDF_EXTENSIONS


def is_image_path(path: Path) -> bool:
    if path.suffix.lower() in _IMAGE_EXTENSIONS:
        return True
    guessed, _ = mimetypes.guess_type(str(path))
    return bool(guessed and guessed.startswith("image/"))


def model_supports_multimodal(model: str | None = None) -> bool:
    model_name = str(model or os.environ.get("OPENAI_MODEL", "")).strip().lower()
    if not model_name:
        return False
    return any(hint in model_name for hint in _MULTIMODAL_MODEL_HINTS)

def _safe_attachment_name(filename: str) -> str:
    raw = Path(str(filename or "file.bin")).name
    sanitized = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "_" for ch in raw).strip("._")
    return sanitized or "file.bin"


def attachment_kind_from_meta(content_type: str, filename: str) -> str:
    normalized_type = str(content_type or "").strip().lower()
    suffix = Path(str(filename or "")).suffix.lower()
    if normalized_type.startswith("image/") or suffix in _IMAGE_EXTENSIONS:
        return "image"
    if normalized_type == "application/pdf" or suffix in _PDF_EXTENSIONS:
        return "pdf"
    return "file"


def build_public_attachment_payload(item: dict[str, Any]) -> dict[str, Any]:
    attachment_id = str(item.get("id") or "").strip()
    url = str(item.get("url") or "").strip()
    if not url and attachment_id:
        path_str = str(item.get("path") or "").strip()
        if path_str and is_uploaded_attachment_path(path_str):
            url = f"/api/chat/upload/{attachment_id}"
        elif path_str and is_exported_attachment_path(path_str):
            url = f"/api/chat/export/{attachment_id}"
    return {
        "id": attachment_id,
        "name": str(item.get("name") or "file"),
        "content_type": str(item.get("content_type") or "application/octet-stream"),
        "size": int(item.get("size") or 0),
        "kind": str(item.get("kind") or "file"),
        "url": url,
        **({"width": int(item.get("width"))} if isinstance(item.get("width"), int) else {}),
        **({"height": int(item.get("height"))} if isinstance(item.get("height"), int) else {}),
    }


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def register_generated_attachment(path_str: str, display_name: str | None = None) -> dict[str, Any]:
    source = Path(path_str).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Attachment source not found: {source}")

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_display_name = _safe_attachment_name(display_name or source.name)
    safe_stem = Path(safe_display_name).stem or "file"
    suffix = Path(safe_display_name).suffix or source.suffix or ".bin"
    source_hash = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
    export_id = f"{safe_stem[:40]}_{source_hash}{suffix}"
    target = EXPORTS_DIR / export_id
    if source != target:
        shutil.copy2(source, target)

    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    kind = attachment_kind_from_meta(content_type, safe_display_name)
    width, height = _image_dimensions(target) if kind == "image" else (None, None)
    return {
        "id": target.name,
        "name": display_name or source.name,
        "path": str(target.resolve()),
        "content_type": content_type,
        "size": target.stat().st_size,
        "kind": kind,
        "url": f"/api/chat/export/{target.name}",
        **({"width": width} if isinstance(width, int) else {}),
        **({"height": height} if isinstance(height, int) else {}),
    }


def _build_attachment_preview(result: dict[str, Any]) -> str:
    kind = str(result.get("kind") or "file")
    if kind == "pdf":
        preview = str(result.get("text_preview") or "").strip()
        return preview or "PDF detected, but no text could be extracted."
    if kind == "image":
        preview = str(result.get("vision_text") or "").strip()
        if preview:
            return preview
        meta = result.get("image_meta", {})
        width = meta.get("width")
        height = meta.get("height")
        fmt = meta.get("format") or "image"
        if width and height:
            return f"Image metadata only: {fmt}, {width}x{height}."
        return "Image uploaded."
    return str(result.get("note") or "File uploaded.")


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    sidecar = _sidecar_path(path)
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_sidecar(path: Path, payload: dict[str, Any]) -> None:
    sidecar = _sidecar_path(path)
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _pdf_analysis(path: Path, max_chars: int = 12000) -> dict[str, Any]:
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    joined = "\n\n".join(part.strip() for part in pages if part and part.strip())
    return {
        "kind": "pdf",
        "page_count": len(reader.pages),
        "text_chars": len(joined),
        "text_preview": _truncate(joined, max_chars),
    }


def _image_metadata(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        return {
            "format": str(image.format or "").upper(),
            "width": int(image.width),
            "height": int(image.height),
            "mode": str(image.mode or ""),
        }


async def _vision_analysis(path: Path, prompt: str = "") -> dict[str, Any]:
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    content_prompt = prompt.strip() or "Describe this image in detail and extract any visible text."
    content = [
        {"type": "text", "text": content_prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
    ]
    return await run_vision_chat(content, content_prompt=content_prompt)


async def run_vision_chat(content: list[dict[str, Any]], content_prompt: str = "") -> dict[str, Any]:
    """Run a vision-capable LLM call with image content."""
    result = await call_llm(
        [{"role": "user", "content": content}],
        model_type="vision",
        thinking="disabled",
        caller="vision",
        publish_events=False,
        record_usage=False,
    )
    vision_text = _assistant_text(result) or ""
    return {
        "vision_model": result.get("model", ""),
        "vision_prompt": content_prompt,
        "vision_text": _truncate(vision_text.strip(), 12000),
    }


async def analyze_attachment(path_str: str, prompt: str = "", force_refresh: bool = False) -> dict[str, Any]:
    path = Path(path_str).resolve()
    cached = None if force_refresh else _read_sidecar(path)
    if cached:
        return cached

    payload: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size if path.exists() else 0,
        "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
    }
    if is_pdf_path(path):
        payload.update(_pdf_analysis(path))
    elif is_image_path(path):
        payload["kind"] = "image"
        payload["image_meta"] = _image_metadata(path)
        payload["multimodal_model"] = model_supports_multimodal()
        try:
            payload.update(await _vision_analysis(path, prompt=prompt))
        except Exception:
            if payload["multimodal_model"]:
                raise
            payload["note"] = "Current model does not appear to support vision input."
    else:
        payload["kind"] = "file"
        payload["note"] = "No built-in parser for this file type."

    payload["preview"] = _build_attachment_preview(payload)
    _write_sidecar(path, payload)
    return payload
