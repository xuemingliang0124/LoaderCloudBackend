"""交易清单契约测试：鉴权门禁 + 请求体校验（不落库）+ CRUD/预检/级联（aiosqlite 落库）。

覆盖：
- schema strip/422（不落库）
- 401 鉴权、422 查询参数、3030 非成员、3031 角色不足
- 3050 txn_code 项目内重复、3051 不存在、3052 跨项目、3054 默认脚本不存在/跨项目
- CRUD 全字段往返、分页/过滤、更新部分字段、删除预检/删除、admin 直通
- SLA 边界校验（负值/超限 422）
- 项目级联：严格阻断提示含交易计数 + force 级联清理交易
"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.main import app
from app.models.script import Script
from app.models.transaction import Transaction
from app.models.project_member import ProjectMember
from app.schemas import TransactionIn

_TOKEN = create_access_token("tester", "viewer")


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _create_txn(
    client, project_id: int, payload: dict, username: str = "alice"
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_id}/transactions",
        json=payload,
        headers=_auth(username),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------- schema 单元测试 ----------


def test_transaction_in_strips_fields() -> None:
    payload = TransactionIn(name="  登录交易  ", txn_code=" login ")
    assert payload.name == "登录交易"
    assert payload.txn_code == "login"


def test_transaction_in_blank_fields_raise() -> None:
    with pytest.raises(ValidationError):
        TransactionIn(name="\t ", txn_code="login")
    with pytest.raises(ValidationError):
        TransactionIn(name="登录", txn_code="  ")


# ---------- 鉴权 / 422（不落库） ----------


async def test_create_txn_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/transactions", json={"name": "t", "txn_code": "t"}
        )
    assert resp.status_code == 401


async def test_list_txns_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/projects/1/transactions")
    assert resp.status_code == 401


async def test_create_txn_missing_name_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/transactions",
            json={"txn_code": "login"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_txns_invalid_page_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects/1/transactions?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_create_txn_sla_out_of_range_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # sla_tps 负值
        resp = await client.post(
            "/api/v1/projects/1/transactions",
            json={"name": "t", "txn_code": "t", "sla_tps": -1},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert resp.status_code == 422
        # sla_error_rate 超过 100
        resp = await client.post(
            "/api/v1/projects/1/transactions",
            json={"name": "t", "txn_code": "t", "sla_error_rate": 101},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert resp.status_code == 422


# ---------- 权限门禁 ----------


async def test_create_txn_non_member_3030(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/transactions",
        json={"name": "t", "txn_code": "t"},
        headers=_auth("eve"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_create_txn_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.post(
        f"/api/v1/projects/{pid}/transactions",
        json={"name": "t", "txn_code": "t"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_create_txn_by_editor_success(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()
    data = await _create_txn(
        client, pid, {"name": "登录交易", "txn_code": "login"}, username="bob"
    )
    assert data["name"] == "登录交易"
    assert data["project_id"] == pid


async def test_admin_create_txn_without_membership(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/transactions",
        json={"name": "管理员建的交易", "txn_code": "adm"},
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["txn_code"] == "adm"


# ---------- 创建 / 唯一性 ----------


async def test_create_txn_full_json_roundtrip(client) -> None:
    pid = await _create_project(client, "项目A")
    payload = {
        "name": "  登录交易  ",
        "txn_code": "login",
        "sla_tps": 100.0,
        "sla_p95_ms": 500,
        "sla_error_rate": 1.0,
        "description": "登录接口压测交易",
    }
    data = await _create_txn(client, pid, payload)
    assert data["name"] == "登录交易"  # schema strip
    assert data["sla_tps"] == 100.0
    assert data["sla_p95_ms"] == 500
    assert data["sla_error_rate"] == 1.0
    assert data["default_script_id"] is None
    assert "created_at" in data and data["id"] > 0


async def test_duplicate_txn_code_same_project_3050(client) -> None:
    pid = await _create_project(client, "项目A")
    await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    r = await client.post(
        f"/api/v1/projects/{pid}/transactions",
        json={"name": "登录二", "txn_code": "login"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3050


async def test_same_txn_code_allowed_across_projects(client) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    await _create_txn(client, pid1, {"name": "登录", "txn_code": "login"})
    data = await _create_txn(client, pid2, {"name": "登录", "txn_code": "login"})
    assert data["project_id"] == pid2


# ---------- 默认脚本弱关联 ----------


async def test_create_txn_default_script_not_exist_3054(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/transactions",
        json={"name": "登录", "txn_code": "login", "default_script_id": 999999},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3054


async def test_create_txn_default_script_cross_project_3054(client, db_session) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    # 脚本属于项目B
    db_session.add(Script(project_id=pid2, name="s1", file_key="scripts/x/v1/a.jmx"))
    await db_session.commit()
    script_id = (
        await db_session.execute(select(Script.id).where(Script.project_id == pid2))
    ).scalar_one()
    r = await client.post(
        f"/api/v1/projects/{pid1}/transactions",
        json={"name": "登录", "txn_code": "login", "default_script_id": script_id},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3054


async def test_create_txn_with_default_script_in_project_success(
    client, db_session
) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(Script(project_id=pid, name="s1", file_key="scripts/x/v1/a.jmx"))
    await db_session.commit()
    script_id = (
        await db_session.execute(select(Script.id).where(Script.project_id == pid))
    ).scalar_one()
    data = await _create_txn(
        client,
        pid,
        {"name": "登录", "txn_code": "login", "default_script_id": script_id},
    )
    assert data["default_script_id"] == script_id


# ---------- 列表 / 详情 ----------


async def test_list_txns_pagination_and_filters(client) -> None:
    pid = await _create_project(client, "项目A")
    await _create_txn(client, pid, {"name": "登录交易", "txn_code": "login"})
    await _create_txn(client, pid, {"name": "下单交易", "txn_code": "order"})
    await _create_txn(client, pid, {"name": "支付交易", "txn_code": "pay"})

    # id 倒序：最新创建的 pay 在前
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions", headers=_auth("alice")
    )
    body = r.json()["data"]
    assert body["total"] == 3
    assert [i["txn_code"] for i in body["items"]] == ["pay", "order", "login"]

    # 名称模糊
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions?name=交易", headers=_auth("alice")
    )
    assert r.json()["data"]["total"] == 3
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions?name=登录", headers=_auth("alice")
    )
    items = r.json()["data"]["items"]
    assert len(items) == 1 and items[0]["txn_code"] == "login"

    # 编码精确
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions?txn_code=pay", headers=_auth("alice")
    )
    items = r.json()["data"]["items"]
    assert len(items) == 1 and items[0]["name"] == "支付交易"

    # 分页
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions?page=1&page_size=2", headers=_auth("alice")
    )
    body = r.json()["data"]
    assert body["total"] == 3 and len(body["items"]) == 2


async def test_get_txn_detail_and_scope_errors(client) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    txn = await _create_txn(client, pid1, {"name": "登录", "txn_code": "login"})

    r = await client.get(
        f"/api/v1/projects/{pid1}/transactions/{txn['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["txn_code"] == "login"

    # 不存在
    r = await client.get(
        f"/api/v1/projects/{pid1}/transactions/999999", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3051

    # 跨项目访问
    r = await client.get(
        f"/api/v1/projects/{pid2}/transactions/{txn['id']}", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3052


# ---------- 更新 ----------


async def test_update_txn_success(client) -> None:
    pid = await _create_project(client, "项目A")
    txn = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    r = await client.put(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}",
        json={"name": "  登录交易  ", "sla_tps": 200.0, "sla_p95_ms": 300},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "登录交易"
    assert data["sla_tps"] == 200.0
    assert data["sla_p95_ms"] == 300
    # 未传字段保持不变
    assert data["txn_code"] == "login"


async def test_update_txn_empty_body_422(client) -> None:
    pid = await _create_project(client, "项目A")
    txn = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    r = await client.put(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}",
        json={},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


async def test_update_txn_duplicate_code_3050(client) -> None:
    pid = await _create_project(client, "项目A")
    t1 = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    await _create_txn(client, pid, {"name": "下单", "txn_code": "order"})
    r = await client.put(
        f"/api/v1/projects/{pid}/transactions/{t1['id']}",
        json={"txn_code": "order"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3050


async def test_update_txn_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    txn = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.put(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}",
        json={"name": "改名"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_update_txn_default_script_cross_project_3054(
    client, db_session
) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    txn = await _create_txn(client, pid1, {"name": "登录", "txn_code": "login"})
    db_session.add(Script(project_id=pid2, name="s1", file_key="scripts/x/v1/a.jmx"))
    await db_session.commit()
    script_id = (
        await db_session.execute(select(Script.id).where(Script.project_id == pid2))
    ).scalar_one()
    r = await client.put(
        f"/api/v1/projects/{pid1}/transactions/{txn['id']}",
        json={"default_script_id": script_id},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3054


# ---------- 删除预检 / 删除 ----------


async def test_precheck_txn_delete(client) -> None:
    pid = await _create_project(client, "项目A")
    txn = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}/delete-precheck",
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    assert r.json()["data"] == {
        "transaction_id": txn["id"],
        "scenarios": 0,
        "test_plans": 0,
    }


async def test_delete_txn_requires_owner(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    txn = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()
    # editor 不可删（owner+）
    r = await client.delete(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}", headers=_auth("bob")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_delete_txn_success(client) -> None:
    pid = await _create_project(client, "项目A")
    txn = await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    r = await client.delete(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["deleted"] is True and data["force"] is False

    # 删除后列表为空
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions", headers=_auth("alice")
    )
    assert r.json()["data"]["total"] == 0
    # 再查详情 3051
    r = await client.get(
        f"/api/v1/projects/{pid}/transactions/{txn['id']}", headers=_auth("alice")
    )
    assert r.json()["code"] == 3051


# ---------- 项目级联 ----------


async def test_project_strict_block_message_includes_transactions(client) -> None:
    """严格模式 3023 提示应包含交易计数。"""
    pid = await _create_project(client, "级联项目")
    await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    await _create_txn(client, pid, {"name": "下单", "txn_code": "order"})

    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("alice"))
    assert r.status_code == 400
    assert r.json()["code"] == 3023
    assert "2 个交易" in r.json()["message"]

    # 预检接口返回 transactions 计数
    r = await client.get(
        f"/api/v1/projects/{pid}/delete-precheck", headers=_auth("alice")
    )
    assert r.json()["data"]["transactions"] == 2


async def test_project_force_delete_cascades_txns(client, db_session) -> None:
    """项目 force 删除必须连带清理交易（FK RESTRICT，不清理会导致项目删除失败）。"""
    pid = await _create_project(client, "级联项目")
    await _create_txn(client, pid, {"name": "登录", "txn_code": "login"})
    await _create_txn(client, pid, {"name": "下单", "txn_code": "order"})

    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed_transactions"] == 2

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.project_id == pid)
        )
    ).scalar()
    assert int(count) == 0


async def test_project_force_delete_cascades_txn_with_script(
    client, db_session
) -> None:
    """force 级联先删脚本再删交易：default_script_id 弱关联不应阻断级联。"""
    pid = await _create_project(client, "级联项目")
    db_session.add(Script(project_id=pid, name="s1", file_key="scripts/x/v1/a.jmx"))
    await db_session.commit()
    script_id = (
        await db_session.execute(select(Script.id).where(Script.project_id == pid))
    ).scalar_one()
    await _create_txn(
        client,
        pid,
        {"name": "登录", "txn_code": "login", "default_script_id": script_id},
    )

    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed_transactions"] == 1
    assert r.json()["data"]["removed_scripts"] == 1
