"""环境清单契约测试：鉴权门禁 + 请求体校验（不落库）+ CRUD/预检/级联（aiosqlite 落库）。"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.main import app
from app.models.environment import Environment
from app.models.project_member import ProjectMember
from app.schemas import EnvironmentIn

_TOKEN = create_access_token("tester", "viewer")


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _create_env(
    client, project_id: int, payload: dict, username: str = "alice"
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_id}/environments",
        json=payload,
        headers=_auth(username),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------- schema 单元测试 ----------


def test_environment_in_strips_fields() -> None:
    payload = EnvironmentIn(name="  生产环境  ", env_code=" prod ")
    assert payload.name == "生产环境"
    assert payload.env_code == "prod"


def test_environment_in_blank_fields_raise() -> None:
    with pytest.raises(ValidationError):
        EnvironmentIn(name="\t ", env_code="prod")
    with pytest.raises(ValidationError):
        EnvironmentIn(name="生产", env_code="  ")


# ---------- 鉴权 / 422（不落库） ----------


async def test_create_env_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/environments", json={"name": "e", "env_code": "e"}
        )
    assert resp.status_code == 401


async def test_list_envs_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/projects/1/environments")
    assert resp.status_code == 401


async def test_create_env_missing_name_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/environments",
            json={"env_code": "prod"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_envs_invalid_page_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects/1/environments?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


# ---------- 权限门禁 ----------


async def test_create_env_non_member_3030(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/environments",
        json={"name": "e", "env_code": "e"},
        headers=_auth("eve"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_create_env_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.post(
        f"/api/v1/projects/{pid}/environments",
        json={"name": "e", "env_code": "e"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_create_env_by_editor_success(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()
    data = await _create_env(
        client, pid, {"name": "测试环境", "env_code": "test"}, username="bob"
    )
    assert data["name"] == "测试环境"
    assert data["project_id"] == pid


async def test_admin_create_env_without_membership(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/environments",
        json={"name": "管理员建的环境", "env_code": "adm"},
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["env_code"] == "adm"


# ---------- 创建 / 唯一性 ----------


async def test_create_env_full_json_roundtrip(client) -> None:
    pid = await _create_project(client, "项目A")
    payload = {
        "name": "  生产环境  ",
        "env_code": "prod",
        "base_url": "https://api.demo.com",
        "hosts": [{"name": "app-01", "host": "10.0.0.1", "port": 8080}],
        "db_connections": [{"name": "订单库", "dsn": "mysql://10.0.0.3:3306/o"}],
        "middleware_info": [{"type": "redis", "address": "10.0.0.2:6379"}],
        "variables": {"base_url": "https://api.demo.com"},
        "description": "变更需审批",
    }
    data = await _create_env(client, pid, payload)
    assert data["name"] == "生产环境"  # schema strip
    assert data["base_url"] == "https://api.demo.com"
    assert data["hosts"][0]["host"] == "10.0.0.1"
    assert data["variables"] == {"base_url": "https://api.demo.com"}
    assert "created_at" in data and data["id"] > 0


async def test_duplicate_env_code_same_project_3040(client) -> None:
    pid = await _create_project(client, "项目A")
    await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    r = await client.post(
        f"/api/v1/projects/{pid}/environments",
        json={"name": "生产二", "env_code": "prod"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3040


async def test_same_env_code_allowed_across_projects(client) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    await _create_env(client, pid1, {"name": "生产", "env_code": "prod"})
    data = await _create_env(client, pid2, {"name": "生产", "env_code": "prod"})
    assert data["project_id"] == pid2


# ---------- 列表 / 详情 ----------


async def test_list_envs_pagination_and_filters(client) -> None:
    pid = await _create_project(client, "项目A")
    await _create_env(client, pid, {"name": "生产环境", "env_code": "prod"})
    await _create_env(client, pid, {"name": "预发环境", "env_code": "staging"})
    await _create_env(client, pid, {"name": "开发环境", "env_code": "dev"})

    # id 倒序：最新创建的 dev 在前
    r = await client.get(f"/api/v1/projects/{pid}/environments", headers=_auth("alice"))
    body = r.json()["data"]
    assert body["total"] == 3
    assert [i["env_code"] for i in body["items"]] == ["dev", "staging", "prod"]

    # 名称模糊
    r = await client.get(
        f"/api/v1/projects/{pid}/environments?name=环境", headers=_auth("alice")
    )
    assert r.json()["data"]["total"] == 3
    r = await client.get(
        f"/api/v1/projects/{pid}/environments?name=生产", headers=_auth("alice")
    )
    items = r.json()["data"]["items"]
    assert len(items) == 1 and items[0]["env_code"] == "prod"

    # 编码精确
    r = await client.get(
        f"/api/v1/projects/{pid}/environments?env_code=dev", headers=_auth("alice")
    )
    items = r.json()["data"]["items"]
    assert len(items) == 1 and items[0]["name"] == "开发环境"

    # 分页
    r = await client.get(
        f"/api/v1/projects/{pid}/environments?page=1&page_size=2",
        headers=_auth("alice"),
    )
    body = r.json()["data"]
    assert body["total"] == 3 and len(body["items"]) == 2


async def test_get_env_detail_and_scope_errors(client) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    env = await _create_env(client, pid1, {"name": "生产", "env_code": "prod"})

    r = await client.get(
        f"/api/v1/projects/{pid1}/environments/{env['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["env_code"] == "prod"

    # 不存在
    r = await client.get(
        f"/api/v1/projects/{pid1}/environments/999999", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3041

    # 跨项目访问
    r = await client.get(
        f"/api/v1/projects/{pid2}/environments/{env['id']}", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3042


# ---------- 更新 ----------


async def test_update_env_success(client) -> None:
    pid = await _create_project(client, "项目A")
    env = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    r = await client.put(
        f"/api/v1/projects/{pid}/environments/{env['id']}",
        json={"name": "  生产环境  ", "variables": {"k": "v"}},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "生产环境"
    assert data["variables"] == {"k": "v"}
    # 未传字段保持不变
    assert data["env_code"] == "prod"


async def test_update_env_empty_body_422(client) -> None:
    pid = await _create_project(client, "项目A")
    env = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    r = await client.put(
        f"/api/v1/projects/{pid}/environments/{env['id']}",
        json={},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


async def test_update_env_duplicate_code_3040(client) -> None:
    pid = await _create_project(client, "项目A")
    e1 = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    await _create_env(client, pid, {"name": "预发", "env_code": "staging"})
    r = await client.put(
        f"/api/v1/projects/{pid}/environments/{e1['id']}",
        json={"env_code": "staging"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3040


async def test_update_env_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    env = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.put(
        f"/api/v1/projects/{pid}/environments/{env['id']}",
        json={"name": "改名"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


# ---------- 删除预检 / 删除 ----------


async def test_precheck_env_delete(client) -> None:
    pid = await _create_project(client, "项目A")
    env = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    r = await client.get(
        f"/api/v1/projects/{pid}/environments/{env['id']}/delete-precheck",
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"environment_id": env["id"], "scenarios": 0}


async def test_delete_env_requires_owner(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    env = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()
    # editor 不可删（owner+）
    r = await client.delete(
        f"/api/v1/projects/{pid}/environments/{env['id']}", headers=_auth("bob")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_delete_env_success(client) -> None:
    pid = await _create_project(client, "项目A")
    env = await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    r = await client.delete(
        f"/api/v1/projects/{pid}/environments/{env['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["deleted"] is True and data["force"] is False

    # 删除后列表为空
    r = await client.get(f"/api/v1/projects/{pid}/environments", headers=_auth("alice"))
    assert r.json()["data"]["total"] == 0
    # 再查详情 3041
    r = await client.get(
        f"/api/v1/projects/{pid}/environments/{env['id']}", headers=_auth("alice")
    )
    assert r.json()["code"] == 3041


# ---------- 项目级联 ----------


async def test_project_force_delete_cascades_envs(client, db_session) -> None:
    """项目 force 删除必须连带清理环境（FK RESTRICT，不清理会导致项目删除失败）。"""
    pid = await _create_project(client, "级联项目")
    await _create_env(client, pid, {"name": "生产", "env_code": "prod"})
    await _create_env(client, pid, {"name": "预发", "env_code": "staging"})

    # 严格模式：存在环境拒绝 3023，提示含环境计数
    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("alice"))
    assert r.status_code == 400
    assert r.json()["code"] == 3023
    assert "2 个环境" in r.json()["message"]

    # 预检接口返回 environments 计数
    r = await client.get(
        f"/api/v1/projects/{pid}/delete-precheck", headers=_auth("alice")
    )
    assert r.json()["data"]["environments"] == 2

    # force 级联：环境随项目清理
    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed_environments"] == 2

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(Environment)
            .where(Environment.project_id == pid)
        )
    ).scalar()
    assert int(count) == 0
