"""pytest 公共夹具：aiosqlite 内存库 + get_db 依赖覆盖（权限类测试用）。

仅权限类测试请求这些夹具；既有不落库契约测试（401/422）不受影响。
StaticPool 让同库内多会话（含 handler 内的嵌套查询）共享同一连接，
避免 :memory: 每连接各建一份空库。
"""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.models.base import Base


@pytest_asyncio.fixture
async def db_env():
    """异步引擎 + sessionmaker：建表一次，供 API 会话与 WS 模块共享。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_env):
    async with db_env() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)
