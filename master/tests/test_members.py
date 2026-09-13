"""P3 项目成员管理契约测试：授权/列表/改角色/移除 + 保护规则（aiosqlite 内存库）。"""

from app.core.security import create_access_token
from app.models.project import Project
from app.models.user import User


def _auth(username: str, role: str = "viewer") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _add_user(db_session, username: str, global_role: str = "viewer") -> None:
    db_session.add(User(username=username, password_hash="x", role=global_role))
    await db_session.commit()


async def _add_member(db_session, project_id: int, username: str, role: str) -> None:
    from app.models.project_member import ProjectMember

    db_session.add(
        ProjectMember(
            project_id=project_id, username=username, role=role, granted_by="system"
        )
    )
    await db_session.commit()


# ---------- 门禁：401/3030/3031 ----------


async def test_grant_requires_token(client) -> None:
    r = await client.post(
        "/api/v1/projects/1/members", json={"username": "bob", "role": "编辑者"}
    )
    assert r.status_code == 401


async def test_non_member_grant_rejected_3030(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "bob", "role": "编辑者"},
        headers=_auth("eve"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_viewer_cannot_grant_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "bob")
    await _add_member(db_session, pid, "bob", "viewer")
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "carol", "role": "编辑者"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


# ---------- 授权：3035 / 3032 / 成功 ----------


async def test_grant_user_not_exist_3035(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "ghost", "role": "编辑者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3035


async def test_grant_duplicate_3032(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "alice")
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "alice", "role": "观察者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3032


async def test_grant_success(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "bob")
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "bob", "role": "编辑者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["username"] == "bob"
    assert data["role"] == "编辑者"  # 出参中文
    assert data["granted_by"] == "alice"

    # 库内存英文小写
    from sqlalchemy import select

    from app.models.project_member import ProjectMember

    row = (
        (
            await db_session.execute(
                select(ProjectMember).where(ProjectMember.project_id == pid)
            )
        )
        .scalars()
        .all()
    )
    bob = [m for m in row if m.username == "bob"][0]
    assert bob.role == "editor"


async def test_admin_can_grant_without_membership(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "bob")
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "bob", "role": "观察者"},
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 200


# ---------- 列表 ----------


async def test_member_list_viewer_and_fuzzy(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "bob")
    await _add_user(db_session, "carol")
    await _add_member(db_session, pid, "bob", "viewer")
    await _add_member(db_session, pid, "carol", "editor")

    # viewer 可列
    r = await client.get(f"/api/v1/projects/{pid}/members", headers=_auth("bob"))
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 3  # alice(owner) + bob + carol

    # 用户名模糊
    r = await client.get(
        f"/api/v1/projects/{pid}/members?username=bo", headers=_auth("bob")
    )
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 1
    assert r.json()["data"]["items"][0]["username"] == "bob"


# ---------- 改角色：3033 / 3034 创建者 / 3034 末位 owner / 成功 ----------


async def test_update_role_member_not_exist_3033(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.put(
        f"/api/v1/projects/{pid}/members/ghost",
        json={"role": "观察者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3033


async def test_creator_cannot_downgrade_3034(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    # 再加一个 owner，使末位 owner 校验通过，隔离「创建者保护」
    await _add_user(db_session, "bob")
    await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "bob", "role": "项目管理员"},
        headers=_auth("alice"),
    )
    r = await client.put(
        f"/api/v1/projects/{pid}/members/alice",
        json={"role": "编辑者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3034


async def test_last_owner_cannot_downgrade_3034(client, db_session) -> None:
    # 构造无创建者、仅一个 owner 的项目，隔离「末位 owner 保护」
    project = Project(name="孤儿项目", description="", created_by="")
    db_session.add(project)
    await db_session.commit()
    await _add_member(db_session, project.id, "carol", "owner")

    r = await client.put(
        f"/api/v1/projects/{project.id}/members/carol",
        json={"role": "编辑者"},
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3034


async def test_update_editor_to_viewer_success(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "bob")
    await _add_member(db_session, pid, "bob", "editor")
    r = await client.put(
        f"/api/v1/projects/{pid}/members/bob",
        json={"role": "观察者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["role"] == "观察者"


# ---------- 移除：3034 创建者 / 3034 末位 owner / 成功 / 3033 ----------


async def test_creator_cannot_remove_3034(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.delete(
        f"/api/v1/projects/{pid}/members/alice", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3034


async def test_last_owner_cannot_remove_3034(client, db_session) -> None:
    project = Project(name="孤儿项目2", description="", created_by="")
    db_session.add(project)
    await db_session.commit()
    await _add_member(db_session, project.id, "carol", "owner")

    r = await client.delete(
        f"/api/v1/projects/{project.id}/members/carol",
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3034


async def test_remove_member_success_and_repeat_3033(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _add_user(db_session, "bob")
    await _add_member(db_session, pid, "bob", "viewer")

    r = await client.delete(
        f"/api/v1/projects/{pid}/members/bob", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed"] is True

    r = await client.delete(
        f"/api/v1/projects/{pid}/members/bob", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3033


# ---------- 参数校验：422（不触库） ----------


async def test_invalid_role_422(client) -> None:
    pid = 99999
    r = await client.post(
        f"/api/v1/projects/{pid}/members",
        json={"username": "bob", "role": "超级管理员"},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


async def test_blank_username_422(client) -> None:
    r = await client.post(
        "/api/v1/projects/1/members",
        json={"username": "   ", "role": "编辑者"},
        headers=_auth("alice"),
    )
    assert r.status_code == 422
