"""Code-related API routes — file operations, formatting, and diff."""
from fastapi import APIRouter

router = APIRouter(prefix="/api/code", tags=["code"])

from webui.routes_code.files import router as files_router
from webui.routes_code.format import router as format_router
from webui.routes_code.diff import router as diff_router

router.include_router(files_router)
router.include_router(format_router)
router.include_router(diff_router)
