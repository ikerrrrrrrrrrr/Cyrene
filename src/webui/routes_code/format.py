"""Code formatting API using ruff."""
import asyncio
import tempfile
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class FormatBody(BaseModel):
    code: str
    language: str = "python"


def _check_ruff() -> bool:
    import shutil
    return shutil.which("ruff") is not None


@router.post("/format")
async def format_code(body: FormatBody):
    """Format code using ruff (Python) or return unchanged for other languages."""
    if body.language not in ("python", "py"):
        return {"formatted": body.code, "changed": False}

    if not _check_ruff():
        return {"formatted": body.code, "changed": False, "warning": "ruff not available"}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(body.code)
        temp_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "ruff", "format", "--quiet", temp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        with open(temp_path, "r", encoding="utf-8") as f:
            formatted = f.read()

        changed = formatted != body.code

        return {"formatted": formatted, "changed": changed}
    except FileNotFoundError:
        return {"formatted": body.code, "changed": False, "warning": "ruff not found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
