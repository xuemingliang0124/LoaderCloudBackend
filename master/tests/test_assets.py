"""文档资产契约测试：鉴权门禁 + 上传去重 + CRUD + 项目级联（aiosqlite 落库）。

上传依赖 MinIO，测试用 monkeypatch 替换 storage.upload_bytes / delete_object，
断言落库元数据与 MinIO key 规范（assets/{asset_id}/{filename}）。
"""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.main import app
from app.models.asset import Asset
from app.models.project_member import ProjectMember
from app.schemas import AssetUpdateIn

_TOKEN = create_access_token("tester", "viewer")


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


def _files(content: bytes = b"hello", filename: str = "env.xlsx") -> dict:
    return {"file": (filename, content, "application/octet-stream")}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- schema 单元测试 ----------


def test_asset_update_in_at_least_one() -> None:
    with pytest.raises(ValidationError):
        AssetUpdateIn()
    AssetUpdateIn(name="改名")
    AssetUpdateIn(description="x")
    AssetUpdateIn(asset_type="plan_doc")


def test_asset_update_in_strips_name() -> None:
    payload = AssetUpdateIn(name="  生产清单  ")
    assert payload.name == "生产清单"


def test_asset_update_in_blank_name_raises() -> None:
    with pytest.raises(ValidationError):
        AssetUpdateIn(name="   ")


# ---------- 鉴权 / 422（不落库） ----------


async def test_upload_asset_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/assets",
            data={"asset_type": "env_inventory"},
            files=_files(),
        )
    assert resp.status_code == 401


async def test_list_assets_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/projects/1/assets")
    assert resp.status_code == 401


async def test_upload_asset_missing_file_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/assets",
            data={"asset_type": "env_inventory"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_upload_asset_invalid_type_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/assets",
            data={"asset_type": "not_a_type"},
            files=_files(),
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_assets_invalid_page_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects/1/assets?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


# ---------- 权限门禁 ----------


async def test_upload_asset_non_member_3030(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "env_inventory"},
        files=_files(),
        headers=_auth("eve"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_upload_asset_viewer_3031(client, db_session, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "env_inventory"},
        files=_files(),
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_upload_asset_by_editor_success(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "env_inventory", "name": "  环境清单  "},
        files=_files(b"content", "prod.xlsx"),
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "环境清单"  # strip
    assert data["asset_type"] == "env_inventory"
    assert data["status"] == "pending"
    assert data["filename"] == "prod.xlsx"
    assert data["file_key"] == f"assets/{data['id']}/prod.xlsx"
    assert data["hash_sha256"] == _sha256(b"content")
    assert data["file_size"] == len(b"content")
    assert data["project_id"] == pid
    assert data["reused"] is False
    assert "created_at" in data


async def test_admin_upload_asset_without_membership(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "plan_doc"},
        files=_files(b"doc", "plan.docx"),
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["asset_type"] == "plan_doc"


# ---------- 扩展名校验 ----------


async def test_upload_extension_mismatch_3060(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    # env_inventory 仅允许 xlsx/xls，传 docx 应拒绝
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "env_inventory"},
        files=_files(b"x", "env.docx"),
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3060


async def test_upload_plan_doc_accepts_pdf(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "plan_doc"},
        files=_files(b"pdf", "plan.pdf"),
        headers=_auth("alice"),
    )
    assert r.status_code == 200


# ---------- hash 去重 ----------


async def _fake_upload(object_key: str, data: bytes, content_type: str = "") -> None:
    """测试用假上传：仅记录 key，不触 MinIO。"""
    return None


async def test_same_hash_reuses_asset(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    uploaded_keys: list[str] = []

    async def _record_upload(object_key, data, content_type=""):
        uploaded_keys.append(object_key)

    monkeypatch.setattr("app.services.storage.upload_bytes", _record_upload)

    content = b"dedup-content"
    r1 = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "env_inventory"},
        files=_files(content, "a.xlsx"),
        headers=_auth("alice"),
    )
    assert r1.status_code == 200
    first = r1.json()["data"]
    assert first["reused"] is False
    assert len(uploaded_keys) == 1

    # 同内容不同文件名：hash 相同，应复用原资产
    r2 = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": "txn_inventory"},
        files=_files(content, "b.xlsx"),
        headers=_auth("alice"),
    )
    assert r2.status_code == 200
    second = r2.json()["data"]
    assert second["reused"] is True
    assert second["id"] == first["id"]
    # 未重复上传 MinIO
    assert len(uploaded_keys) == 1


async def test_same_hash_allowed_across_projects(client, monkeypatch) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)

    content = b"same"
    r1 = await client.post(
        f"/api/v1/projects/{pid1}/assets",
        data={"asset_type": "env_inventory"},
        files=_files(content, "a.xlsx"),
        headers=_auth("alice"),
    )
    r2 = await client.post(
        f"/api/v1/projects/{pid2}/assets",
        data={"asset_type": "env_inventory"},
        files=_files(content, "a.xlsx"),
        headers=_auth("alice"),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    # 不同项目同 hash 各自建资产（去重仅项目内）
    assert r1.json()["data"]["id"] != r2.json()["data"]["id"]
    assert r1.json()["data"]["reused"] is False
    assert r2.json()["data"]["reused"] is False


# ---------- 列表 / 详情 ----------


async def _upload(
    client, pid, content=b"data", filename="a.xlsx", asset_type="env_inventory"
) -> dict:
    r = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": asset_type},
        files=_files(content, filename),
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    return r.json()["data"]


async def test_list_assets_filters_and_pagination(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)

    await _upload(client, pid, b"1", "env.xlsx", "env_inventory")
    await _upload(client, pid, b"2", "txn.xlsx", "txn_inventory")
    await _upload(client, pid, b"3", "plan.docx", "plan_doc")

    # 默认列表按 id 倒序
    r = await client.get(f"/api/v1/projects/{pid}/assets", headers=_auth("alice"))
    body = r.json()["data"]
    assert body["total"] == 3
    assert [i["asset_type"] for i in body["items"]] == [
        "plan_doc",
        "txn_inventory",
        "env_inventory",
    ]

    # 按类型过滤
    r = await client.get(
        f"/api/v1/projects/{pid}/assets?asset_type=env_inventory",
        headers=_auth("alice"),
    )
    items = r.json()["data"]["items"]
    assert len(items) == 1 and items[0]["filename"] == "env.xlsx"

    # 分页
    r = await client.get(
        f"/api/v1/projects/{pid}/assets?page=1&page_size=2",
        headers=_auth("alice"),
    )
    body = r.json()["data"]
    assert body["total"] == 3 and len(body["items"]) == 2


async def test_get_asset_detail_and_scope_errors(client, monkeypatch) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    asset = await _upload(client, pid1, b"x", "a.xlsx")

    r = await client.get(
        f"/api/v1/projects/{pid1}/assets/{asset['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["filename"] == "a.xlsx"

    # 不存在
    r = await client.get(
        f"/api/v1/projects/{pid1}/assets/999999", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3061

    # 跨项目
    r = await client.get(
        f"/api/v1/projects/{pid2}/assets/{asset['id']}", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3062


# ---------- 更新 ----------


async def test_update_asset_success(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    asset = await _upload(client, pid, b"x", "a.xlsx", "env_inventory")

    r = await client.put(
        f"/api/v1/projects/{pid}/assets/{asset['id']}",
        json={"name": "  新名  ", "description": "新描述"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "新名"
    assert data["description"] == "新描述"
    assert data["asset_type"] == "env_inventory"  # 未传保持不变


async def test_update_asset_type_extension_check_3060(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    asset = await _upload(client, pid, b"x", "a.xlsx", "env_inventory")

    # 把 xlsx 类型资产改成 plan_doc（plan_doc 不允许 xlsx）
    r = await client.put(
        f"/api/v1/projects/{pid}/assets/{asset['id']}",
        json={"asset_type": "plan_doc"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3060


async def test_update_asset_viewer_3031(client, db_session, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    asset = await _upload(client, pid, b"x", "a.xlsx")

    r = await client.put(
        f"/api/v1/projects/{pid}/assets/{asset['id']}",
        json={"name": "改名"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


# ---------- 删除 ----------


async def test_delete_asset_success(client, db_session, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    deleted_keys: list[str] = []

    async def _fake_delete(object_key: str) -> None:
        deleted_keys.append(object_key)

    monkeypatch.setattr("app.services.storage.delete_object", _fake_delete)
    asset = await _upload(client, pid, b"x", "a.xlsx")

    r = await client.delete(
        f"/api/v1/projects/{pid}/assets/{asset['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True
    # MinIO 对象被清理
    assert deleted_keys == [asset["file_key"]]

    # 删库
    remaining = await db_session.scalar(
        select(func.count()).select_from(Asset).where(Asset.id == asset["id"])
    )
    assert int(remaining) == 0


async def test_delete_asset_viewer_3031(client, db_session, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    asset = await _upload(client, pid, b"x", "a.xlsx")

    r = await client.delete(
        f"/api/v1/projects/{pid}/assets/{asset['id']}", headers=_auth("bob")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


# ---------- 项目级联 ----------


async def test_project_force_delete_cascades_assets(client, db_session, monkeypatch):
    """项目 force 删除必须连带清理文档资产（FK RESTRICT）。"""
    pid = await _create_project(client, "级联项目")
    monkeypatch.setattr("app.services.storage.upload_bytes", _fake_upload)
    await _upload(client, pid, b"1", "env.xlsx", "env_inventory")
    await _upload(client, pid, b"2", "plan.docx", "plan_doc")

    # 严格模式：存在资产拒绝 3023，提示含资产计数
    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("alice"))
    assert r.status_code == 400
    assert r.json()["code"] == 3023
    assert "2 个文档资产" in r.json()["message"]

    # 预检接口返回 assets 计数
    r = await client.get(
        f"/api/v1/projects/{pid}/delete-precheck", headers=_auth("alice")
    )
    assert r.json()["data"]["assets"] == 2

    # force 级联：资产随项目清理
    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed_assets"] == 2

    count = (
        await db_session.execute(
            select(func.count()).select_from(Asset).where(Asset.project_id == pid)
        )
    ).scalar()
    assert int(count) == 0
