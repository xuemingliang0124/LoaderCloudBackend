"""P1 权限基础层契约测试：JWT role claim + 旧 token 拒绝（不落库）。"""

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.core.config import get_settings
from app.core.security import create_access_token, decode_token, decode_token_payload
from app.main import app


def _legacy_token() -> str:
    """构造无 role claim 的改造前旧 token。"""
    settings = get_settings()
    return jwt.encode(
        {"sub": "legacy", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )


def test_token_carries_role_claim() -> None:
    token = create_access_token("alice", "editor")
    payload = decode_token_payload(token)
    assert payload is not None
    assert payload["sub"] == "alice"
    assert payload["role"] == "editor"
    assert decode_token(token) == "alice"


def test_legacy_token_without_role_rejected_by_decode() -> None:
    # 缺 role claim 的旧 token：decode_token 一律拒绝，强制重新登录
    assert decode_token(_legacy_token()) is None


async def test_legacy_token_without_role_gets_401() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer {_legacy_token()}"}
        )
    assert resp.status_code == 401


async def test_tampered_token_gets_401() -> None:
    token = create_access_token("alice", "viewer")[:-2] + "xx"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 401
