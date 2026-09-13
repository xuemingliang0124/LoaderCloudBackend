"""鉴权工具：密码哈希（bcrypt 直连，避开 passlib 与新版 bcrypt 的兼容问题）+ JWT。"""

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings


def hash_password(raw: str) -> str:
    # bcrypt 限制 72 字节，超长部分在应用层截断（密码本身体量远小于此）
    return bcrypt.hashpw(raw.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str, role: str) -> str:
    """签发 JWT：sub=用户名，role=全局角色（随 token 携带，避免每次请求查库）。"""
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "role": role, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token_payload(token: str) -> dict | None:
    """解码完整 payload；非法/过期返回 None。"""
    try:
        settings = get_settings()
        return jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        return None


def decode_token(token: str) -> str | None:
    """取用户名；缺 role claim 的旧 token 一律拒绝，强制重新登录。"""
    payload = decode_token_payload(token)
    if payload is None or not payload.get("role"):
        return None
    return payload.get("sub")
