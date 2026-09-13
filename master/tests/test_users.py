"""用户管理 CRUD 契约测试：全局 admin 门禁 + 自保护/末位 admin + 级联清成员。"""

from sqlalchemy import select

from app.core.security import create_access_token, verify_password
from app.models.project_member import ProjectMember
from app.models.user import User


def _auth(username: str, role: str = "admin") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_user(
    client,
    username: str,
    role: str = "普通用户",
    password: str = "secret123",
    token: str = "root",
) -> dict:
    r = await client.post(
        "/api/v1/users",
        json={"username": username, "password": password, "role": role},
        headers=_auth(token),
    )
    assert r.status_code == 200
    return r.json()["data"]


# ---------- 门禁 ----------


async def test_create_user_requires_token(client) -> None:
    r = await client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "secret123", "role": "普通用户"},
    )
    assert r.status_code == 401


async def test_non_admin_rejected_1010(client) -> None:
    r = await client.get("/api/v1/users", headers=_auth("bob", "user"))
    assert r.status_code == 400
    assert r.json()["code"] == 1010


# ---------- 创建 ----------


async def test_create_user_success(client, db_session) -> None:
    data = await _create_user(client, "bob")
    assert data["username"] == "bob"
    assert data["role"] == "普通用户"
    assert "password_hash" not in data

    row = (
        await db_session.execute(select(User).where(User.username == "bob"))
    ).scalar_one()
    assert row.role == "user"
    assert verify_password("secret123", row.password_hash)


async def test_create_admin_role_cn(client) -> None:
    data = await _create_user(client, "boss", role="管理员")
    assert data["role"] == "管理员"


async def test_create_duplicate_1011(client) -> None:
    await _create_user(client, "bob")
    r = await client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "secret123", "role": "普通用户"},
        headers=_auth("root"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 1011


async def test_create_user_validation_422(client) -> None:
    base = {"username": "bob", "password": "secret123", "role": "普通用户"}

    r = await client.post(
        "/api/v1/users", json={**base, "role": "超级管理员"}, headers=_auth("root")
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/users", json={**base, "password": "123"}, headers=_auth("root")
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/v1/users", json={**base, "username": "   "}, headers=_auth("root")
    )
    assert r.status_code == 422


# ---------- 列表 / 详情 ----------


async def test_list_users_filter_and_page(client) -> None:
    await _create_user(client, "bob")
    await _create_user(client, "boss", role="管理员")

    r = await client.get("/api/v1/users?username=bo", headers=_auth("root"))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 2

    r = await client.get("/api/v1/users?role=管理员", headers=_auth("root"))
    assert r.json()["data"]["total"] == 1
    assert r.json()["data"]["items"][0]["username"] == "boss"

    r = await client.get("/api/v1/users?role=普通用户", headers=_auth("root"))
    assert r.json()["data"]["total"] == 1
    assert r.json()["data"]["items"][0]["username"] == "bob"


async def test_list_users_page_param_422(client) -> None:
    r = await client.get("/api/v1/users?page=0", headers=_auth("root"))
    assert r.status_code == 422


async def test_get_user_detail_and_404_1012(client) -> None:
    await _create_user(client, "bob")
    r = await client.get("/api/v1/users/bob", headers=_auth("root"))
    assert r.status_code == 200
    assert r.json()["data"]["username"] == "bob"

    r = await client.get("/api/v1/users/ghost", headers=_auth("root"))
    assert r.status_code == 400
    assert r.json()["code"] == 1012


# ---------- 更新 ----------


async def test_update_password_and_role(client, db_session) -> None:
    await _create_user(client, "bob")
    r = await client.put(
        "/api/v1/users/bob",
        json={"password": "newpass1"},
        headers=_auth("root"),
    )
    assert r.status_code == 200
    row = (
        await db_session.execute(select(User).where(User.username == "bob"))
    ).scalar_one()
    assert verify_password("newpass1", row.password_hash)

    r = await client.put(
        "/api/v1/users/bob",
        json={"role": "管理员"},
        headers=_auth("root"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["role"] == "管理员"


async def test_update_empty_body_422(client) -> None:
    await _create_user(client, "bob")
    r = await client.put("/api/v1/users/bob", json={}, headers=_auth("root"))
    assert r.status_code == 422


async def test_cannot_demote_self_1014(client, db_session) -> None:
    db_session.add(User(username="root", password_hash="x", role="admin"))
    await db_session.commit()
    r = await client.put(
        "/api/v1/users/root",
        json={"role": "普通用户"},
        headers=_auth("root"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 1014


async def test_last_admin_cannot_demote_1015(client, db_session) -> None:
    # DB 中只有 alice 一个 admin；操作者用 ghost 的 admin token（JWT 自包含）
    db_session.add(User(username="alice", password_hash="x", role="admin"))
    await db_session.commit()
    r = await client.put(
        "/api/v1/users/alice",
        json={"role": "普通用户"},
        headers=_auth("ghost"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 1015


# ---------- 删除 ----------


async def test_cannot_delete_self_1014(client, db_session) -> None:
    db_session.add(User(username="root", password_hash="x", role="admin"))
    await db_session.commit()
    r = await client.delete("/api/v1/users/root", headers=_auth("root"))
    assert r.status_code == 400
    assert r.json()["code"] == 1014


async def test_last_admin_cannot_delete_1015(client, db_session) -> None:
    db_session.add(User(username="alice", password_hash="x", role="admin"))
    await db_session.commit()
    r = await client.delete("/api/v1/users/alice", headers=_auth("ghost"))
    assert r.status_code == 400
    assert r.json()["code"] == 1015


async def test_delete_user_cascades_project_members(client, db_session) -> None:
    # 建用户 bob
    await _create_user(client, "bob")
    # alice（任意登录用户）建项目 → alice 为 owner
    r = await client.post(
        "/api/v1/projects", json={"name": "项目A"}, headers=_auth("alice", "user")
    )
    pid = r.json()["data"]["id"]
    # bob 被授权为项目成员
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()

    r = await client.delete("/api/v1/users/bob", headers=_auth("root"))
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True

    # 用户已删
    assert (
        await db_session.execute(select(User).where(User.username == "bob"))
    ).scalar_one_or_none() is None
    # 成员关系级联清除（alice 的 owner 行保留）
    remaining = (await db_session.execute(select(ProjectMember))).scalars().all()
    assert [m.username for m in remaining] == ["alice"]

    # 重复删除 → 1012
    r = await client.delete("/api/v1/users/bob", headers=_auth("root"))
    assert r.status_code == 400
    assert r.json()["code"] == 1012
