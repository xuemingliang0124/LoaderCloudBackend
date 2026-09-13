"""认证：登录换 JWT。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_db
from app.schemas import LoginIn
from app.schemas.common import ok
from app.services import user_service

router = APIRouter()


@router.post("/auth/login")
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)) -> dict:
    user = await user_service.authenticate(db, payload.username, payload.password)
    return ok(
        {
            "token": create_access_token(user.username, user.role),
            "username": user.username,
            "role": user.role,
        }
    )
