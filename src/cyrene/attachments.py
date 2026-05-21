import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx
from PIL import Image
from pypdf import PdfReader

from cyrene.config import DATA_DIR
from cyrene.llm import _truncate

UPLOADS_DIR = DATA_DIR / "webui_uploads"

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
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    endpoints = [f"{base_url}/chat/completions"]
    if not base_url.endswith("/v1"):
        endpoints.append(f"{base_url}/v1/chat/completions")
    content_prompt = prompt.strip() or "Describe this image in detail and extract any visible text."
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": content_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                ],
            }
        ],
        "max_tokens": 1200,
    }
    if "deepseek" in model.lower():
        payload["thinking"] = {"type": "disabled"}
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.lower() not in ("lmstudio", "dummy", ""):
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        last_error: Exception | None = None
        for endpoint in list(dict.fromkeys(endpoints)):
            try:
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                message = ((data.get("choices") or [{}])[0].get("message") or {})
                content = message.get("content")
                if isinstance(content, str):
                    vision_text = content.strip()
                elif isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            parts.append(str(item.get("text") or ""))
                    vision_text = "".join(parts).strip()
                else:
                    vision_text = ""
                return {
                    "vision_model": model,
                    "vision_prompt": content_prompt,
                    "vision_text": _truncate(vision_text, 12000),
                }
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    return {}


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
        if payload["multimodal_model"]:
            payload.update(await _vision_analysis(path, prompt=prompt))
        else:
            payload["note"] = "Current model does not appear to support vision input."
    else:
        payload["kind"] = "file"
        payload["note"] = "No built-in parser for this file type."

    payload["preview"] = _build_attachment_preview(payload)
    _write_sidecar(path, payload)
    return payload
