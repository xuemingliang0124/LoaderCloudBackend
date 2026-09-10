"""用户服务。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.db.session import SessionLocal
from app.models.user import User
from app.services.exceptions import BusinessError


async def ensure_default_user() -> None:
    """开发兜底：无任何用户时创建 admin/admin123（生产环境务必修改）。"""
    async with SessionLocal() as db:
        exists = (await db.execute(select(User).limit(1))).scalars().first()
        if exists is None:
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password("admin123"),
                    role="admin",
                )
            )
            await db.commit()


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    return (
        (await db.execute(select(User).where(User.username == username)))
        .scalars()
        .first()
    )


async def authenticate(db: AsyncSession, username: str, password: str) -> User:
    user = await get_by_username(db, username)
    if user is None or not verify_password(password, user.password_hash):
        raise BusinessError("用户名或密码错误", code=1001)
    return user
