from fastapi import APIRouter

from app.api.v1.dashboard import admin, annotator, reviewer

router = APIRouter()
router.include_router(admin.router)
router.include_router(reviewer.router)
router.include_router(annotator.router)

__all__ = ["router"]
