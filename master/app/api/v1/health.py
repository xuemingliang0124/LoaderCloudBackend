"""健康检查。"""

from fastapi import APIRouter

from app.schemas.common import ok

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return ok({"status": "up"})
