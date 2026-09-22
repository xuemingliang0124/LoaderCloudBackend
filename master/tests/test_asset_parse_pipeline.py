"""文档解析管道集成测试（D3 + D4 + P3 Stage 1 重构）：上传 → 解析 → 结构化抽取/向量入库 → 状态机/重试。

P3 Stage 1 重构变更：
- mock 路径从 asset_parser.EmbeddingClient 改为 langchain_pipeline.get_embeddings
  + langchain_pipeline.get_vector_store（asset_parser 文本路径委托 langchain_pipeline）
- chunk 数精确断言改区间（RecursiveCharacterTextSplitter 边界与原 chunk_text 略有差异）
- NFR-01 降级：未配置 Embedding 时 FakeEmbeddings 入库（indexed=true, degraded=true），
  不再跳过入库（indexed=false 已废弃）

策略：
- MinIO upload_bytes/get_object_bytes 用 monkeypatch 替换（与 test_assets.py 口径一致）
- run_asset_parse 直接以 db_env（内存库会话工厂）为 session_factory 同步驱动，
  绕过 APScheduler（调度器未启动时 enqueue 仅告警）
- Embeddings/VectorStore 通过替换 langchain_pipeline 命名空间内的调用实现可控注入
- get_settings 在 asset_parser 命名空间内打桩，隔离本机 .env 的 Embedding 配置
- 断言一律用 db_env 新开会话，规避 db_session 身份映射读到旧对象（异步会话陷阱②）
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.asset import Asset
from app.models.environment import Environment
from app.models.script import Script
from app.models.transaction import Transaction
from app.services.asset_parser import run_asset_parse


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


def _env_xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io_bytes()
    wb.save(buf)
    return buf.getvalue()


def io_bytes():
    import io

    return io.BytesIO()


def _docx(paragraphs: list[str]) -> bytes:
    import io

    from docx import Document

    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def _upload(
    client,
    pid: int,
    data: bytes,
    filename: str,
    asset_type: str,
    name: str | None = None,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{pid}/assets",
        data={"asset_type": asset_type, **({"name": name} if name else {})},
        files={"file": (filename, data, "application/octet-stream")},
        headers=_auth("alice"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _patch_storage(monkeypatch) -> None:
    async def _fake_upload(object_key, data, content_type=""):
        return None

    async def _fake_get(object_key):
        return _FAKE_FILES[object_key]

    _FAKE_FILES = {}

    def _fake_upload_recorder(object_key, data, content_type=""):
        _FAKE_FILES[object_key] = data

    async def _upload_and_record(object_key, data, content_type=""):
        _fake_upload_recorder(object_key, data, content_type)

    monkeypatch.setattr(
        "app.services.asset_parser.storage.upload_bytes", _upload_and_record
    )
    monkeypatch.setattr("app.services.asset_parser.storage.get_object_bytes", _fake_get)
    monkeypatch.setattr("app.services.storage.upload_bytes", _upload_and_record)


def _make_settings(**overrides) -> SimpleNamespace:
    values = dict(
        embedding_provider="",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
        embedding_batch_size=64,
        embedding_timeout=5,
        embedding_max_retries=1,
        embedding_dims=4,
        # P3 Stage 1：langchain_pipeline.build_chunker 消费
        chunk_size=500,
        chunk_overlap=50,
        timezone="Asia/Shanghai",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeStore:
    def __init__(self) -> None:
        self.upserted: list = []

    async def upsert_chunks(self, points) -> None:
        self.upserted.extend(points)


class FakeEmbeddings:
    """模拟 LangChain Embeddings（aembed_documents 返回固定 4 维向量）。"""

    async def aembed_documents(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def aembed_query(self, text):
        return [0.1, 0.2, 0.3, 0.4]


@pytest.fixture
def fake_vector_store(monkeypatch) -> FakeStore:
    """P3 Stage 1：mock 路径改为 langchain_pipeline.get_vector_store。"""
    store = FakeStore()
    monkeypatch.setattr("app.services.langchain_pipeline.get_vector_store", lambda: store)
    return store


@pytest.fixture
def fake_embeddings(monkeypatch) -> FakeEmbeddings:
    """P3 Stage 1：mock langchain_pipeline.get_embeddings 返回 FakeEmbeddings。"""
    embeddings = FakeEmbeddings()
    monkeypatch.setattr("app.services.langchain_pipeline.get_embeddings", lambda: embeddings)
    return embeddings


# ---------- 环境清单结构化抽取 ----------


async def test_env_inventory_creates_environments(client, db_env, monkeypatch):
    _patch_storage(monkeypatch)
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    pid = await _create_project(client, "环境项目")
    data = _env_xlsx(
        [
            ["环境名称", "环境编码", "基础URL", "主机", "变量", "备注列"],
            [
                "生产环境",
                "prod",
                "http://prod.example.com",
                "10.0.0.1,10.0.0.2",
                "base_url=http://prod.example.com;token=abc",
                "张三",
            ],
            ["预发环境", "staging", "http://staging.example.com", "", "", ""],
        ]
    )
    asset = await _upload(client, pid, data, "环境交付清单.xlsx", "env_inventory")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "ready"
        assert row.parse_meta["environments"] == 2
        assert row.parse_meta["unmatched_columns"] == ["备注列"]
        envs = {
            e.env_code: e
            for e in (
                await db.execute(
                    select(Environment).where(Environment.project_id == pid)
                )
            )
            .scalars()
            .all()
        }
    assert set(envs) == {"prod", "staging"}
    assert envs["prod"].name == "生产环境"
    assert envs["prod"].base_url == "http://prod.example.com"
    assert envs["prod"].hosts == ["10.0.0.1", "10.0.0.2"]
    assert envs["prod"].variables == {
        "base_url": "http://prod.example.com",
        "token": "abc",
    }
    assert envs["staging"].name == "预发环境"


async def test_env_inventory_dedupe_and_missing_code(client, db_env, monkeypatch):
    _patch_storage(monkeypatch)
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    pid = await _create_project(client, "去重项目")
    # 预置项目内已有 prod，触发项目内去重
    async with db_env() as db:
        db.add(Environment(project_id=pid, name="已有", env_code="prod"))
        await db.commit()

    data = _env_xlsx(
        [
            ["环境名称", "环境编码"],
            ["生产环境", "prod"],  # 与库中冲突
            ["预发环境", "staging"],  # 保留
            ["预发二", "staging"],  # 文件内冲突
            ["无编码", ""],  # 缺 env_code 跳过
        ]
    )
    asset = await _upload(client, pid, data, "清单.xlsx", "env_inventory")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "ready"
        assert row.parse_meta["environments"] == 1
        warnings = " ".join(row.parse_meta["warnings"])
        assert "prod" in warnings and "staging" in warnings
        assert any("缺少环境编码" in w for w in row.parse_meta["warnings"])
        envs = (
            await db.execute(select(Environment).where(Environment.project_id == pid))
        ).scalars()
        assert sorted(e.env_code for e in envs) == ["prod", "staging"]


# ---------- 交易清单结构化抽取 ----------


async def test_txn_inventory_creates_transactions_with_script_link(
    client, db_env, monkeypatch
):
    _patch_storage(monkeypatch)
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    pid = await _create_project(client, "交易项目")
    async with db_env() as db:
        db.add(Script(project_id=pid, name="登录脚本", file_key="scripts/1/v1/a.jmx"))
        await db.commit()

    data = _env_xlsx(
        [
            ["交易名称", "交易码", "目标TPS", "P95(ms)", "错误率", "默认脚本", "备注"],
            ["登录", "login", "500", "120ms", "5%", "登录脚本", "核心链路"],
            ["下单", "create_order", "300", "200", "1%", "不存在脚本", ""],
        ]
    )
    asset = await _upload(client, pid, data, "交易清单.xlsx", "txn_inventory")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "ready"
        assert row.parse_meta["transactions"] == 2
        txns = {
            t.txn_code: t
            for t in (
                await db.execute(
                    select(Transaction).where(Transaction.project_id == pid)
                )
            )
            .scalars()
            .all()
        }
    assert txns["login"].name == "登录"
    assert txns["login"].sla_tps == 500.0
    assert txns["login"].sla_p95_ms == 120
    assert txns["login"].sla_error_rate == 5.0
    assert txns["login"].default_script_id is not None  # 按名称关联成功
    assert txns["create_order"].default_script_id is None
    assert any("不存在脚本" in w and "未关联" in w for w in row.parse_meta["warnings"])


# ---------- 文档类资产向量入库 ----------


async def test_docx_plan_doc_chunks_indexed(
    client, db_env, monkeypatch, fake_vector_store, fake_embeddings
):
    """P3 Stage 1：mock 路径改为 langchain_pipeline.get_embeddings/get_vector_store。"""
    _patch_storage(monkeypatch)
    # get_embeddings/get_vector_store 已由 fixture mock；get_settings 仍需打桩
    # （控制 degraded 判定：未配 api_key → degraded=true）
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(embedding_dims=4),
    )
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    pid = await _create_project(client, "方案项目")
    data = _docx([f"性能测试方案第{i}节：" + "说明内容" * 15 for i in range(12)])
    asset = await _upload(client, pid, data, "性能测试方案.docx", "plan_doc")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "ready"
        assert row.parse_meta["indexed"] is True
        # 12 段每段约 150 字，总 ~1800 字，chunk_size=500 → 3-5 块（区间断言）
        assert 2 <= row.parse_meta["chunks"] <= 8

    assert len(fake_vector_store.upserted) == row.parse_meta["chunks"]
    first = fake_vector_store.upserted[0]
    assert first.asset_id == asset["id"]
    assert first.project_id == pid
    assert first.asset_type == "plan_doc"
    assert first.source_type == "asset"
    assert first.source_ref == f"assets/{asset['id']}/性能测试方案.docx"
    assert first.embedding == [0.1, 0.2, 0.3, 0.4]


async def test_docx_without_embedding_config_degrades(
    client, db_env, monkeypatch, fake_vector_store, fake_embeddings
):
    """NFR-01 降级（P3 Stage 1 重构）：未配置 → FakeEmbeddings 入库，indexed=true degraded=true。"""
    _patch_storage(monkeypatch)
    # 未配置 embedding_api_key/embedding_model → degraded=true
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(embedding_dims=4),
    )
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    pid = await _create_project(client, "降级项目")
    data = _docx(["架构说明", "系统分层设计" + "细节" * 100])
    asset = await _upload(client, pid, data, "架构说明.docx", "architecture_doc")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "ready"  # 结构化/解析不受影响
        assert row.parse_meta["chunks"] > 0
        assert row.parse_meta["indexed"] is True  # FakeEmbeddings 入库（不再跳过）
        assert row.parse_meta["degraded"] is True  # 降级标记
    # 向量库有数据（FakeEmbeddings 入库，不再为空）
    assert len(fake_vector_store.upserted) == row.parse_meta["chunks"]


async def test_embedding_failure_marks_failed(
    client, db_env, monkeypatch, fake_vector_store
):
    """已配置但调用失败 → 抛异常 → asset_parser 标记 FAILED。"""

    class FailingEmbeddings:
        async def aembed_documents(self, texts):
            raise RuntimeError("上游 503")

        async def aembed_query(self, text):
            raise RuntimeError("上游 503")

    _patch_storage(monkeypatch)
    # get_settings 配置为已配置（embedding_api_key/model 非空）→ 走真实 OpenAIEmbeddings 路径
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(
            embedding_api_key="k", embedding_model="m", embedding_dims=4
        ),
    )
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings",
        lambda: _make_settings(embedding_api_key="k", embedding_model="m"),
    )
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_embeddings", lambda: FailingEmbeddings()
    )
    pid = await _create_project(client, "失败项目")
    data = _docx(["报告内容" * 50])
    asset = await _upload(client, pid, data, "sla说明.docx", "sla_doc")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "failed"
        assert "上游 503" in row.parse_meta["error"]


# ---------- 失败路径与状态机 ----------


async def test_unsupported_legacy_format_failed(client, db_env, monkeypatch):
    _patch_storage(monkeypatch)
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    pid = await _create_project(client, "旧格式项目")
    asset = await _upload(client, pid, b"legacy doc", "老方案.doc", "plan_doc")
    await run_asset_parse(asset["id"], db_env)

    async with db_env() as db:
        row = (
            await db.execute(select(Asset).where(Asset.id == asset["id"]))
        ).scalar_one()
        assert row.status == "failed"
        assert ".docx" in row.parse_meta["error"]


async def test_missing_file_key_failed(client, db_env, monkeypatch):
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    async with db_env() as db:
        db.add(
            Asset(
                project_id=1,
                name="孤儿资产",
                asset_type="plan_doc",
                status="pending",
                filename="a.docx",
                hash_sha256="x",
                file_key="",  # 文件对象缺失
            )
        )
        await db.commit()
        asset_id = (
            await db.execute(select(Asset.id).where(Asset.name == "孤儿资产"))
        ).scalar_one()
    await run_asset_parse(asset_id, db_env)
    async with db_env() as db:
        row = (await db.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        assert row.status == "failed"
        assert "file_key" in row.parse_meta["error"]


async def test_parse_idempotent_guard_while_parsing(client, db_env, monkeypatch):
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    async with db_env() as db:
        db.add(
            Asset(
                project_id=1,
                name="解析中",
                asset_type="plan_doc",
                status="parsing",
                filename="a.docx",
                hash_sha256="y",
                file_key="assets/999/a.docx",
            )
        )
        await db.commit()
        asset_id = (
            await db.execute(select(Asset.id).where(Asset.name == "解析中"))
        ).scalar_one()
    # CAS 抢占失败：不解析、不抛错、状态保持 parsing
    await run_asset_parse(asset_id, db_env)
    async with db_env() as db:
        row = (await db.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        assert row.status == "parsing"
        assert row.parse_meta in (None, {})


# ---------- API 集成：投递与重试 ----------


async def test_upload_enqueues_parse_and_reuse_skips(client, monkeypatch):
    _patch_storage(monkeypatch)
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )

    def _record(asset_id, delay_seconds=2.0):
        scheduled.append(asset_id)

    monkeypatch.setattr("app.api.v1.assets.schedule_asset_parse", _record)
    pid = await _create_project(client, "投递项目")
    data = _docx(["内容"])
    first = await _upload(client, pid, data, "a.docx", "plan_doc")
    assert scheduled == [first["id"]]

    # 同内容二次上传：复用资产，不重复投递解析
    second = await _upload(client, pid, data, "b.docx", "plan_doc")
    assert second["reused"] is True
    assert second["id"] == first["id"]
    assert scheduled == [first["id"]]


async def test_retry_parse_flow(client, db_env, monkeypatch):
    _patch_storage(monkeypatch)
    monkeypatch.setattr(
        "app.services.asset_parser.get_settings", lambda: _make_settings()
    )
    # P3 Stage 1：mock langchain_pipeline 避免 run_asset_parse 连真实 Qdrant
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings", lambda: _make_settings()
    )
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_embeddings", lambda: FakeEmbeddings()
    )
    store = FakeStore()
    monkeypatch.setattr("app.services.langchain_pipeline.get_vector_store", lambda: store)
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.api.v1.assets.schedule_asset_parse",
        lambda asset_id, delay_seconds=2.0: scheduled.append(asset_id),
    )
    pid = await _create_project(client, "重试项目")
    asset = await _upload(client, pid, _docx(["内容"]), "a.docx", "plan_doc")

    # ready 状态拒绝重试
    await run_asset_parse(asset["id"], db_env)
    resp = await client.post(
        f"/api/v1/projects/{pid}/assets/{asset['id']}/retry-parse",
        headers=_auth("alice"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3063

    # 置为 failed 后可重试：状态重置 pending 并重新投递
    async with db_env() as db:
        await db.execute(update_asset_status(asset["id"], "failed"))
        await db.commit()
    resp = await client.post(
        f"/api/v1/projects/{pid}/assets/{asset['id']}/retry-parse",
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "pending" and body["queued"] is True
    assert scheduled[-1] == asset["id"]


def update_asset_status(asset_id: int, status: str):
    from sqlalchemy import update

    return update(Asset).where(Asset.id == asset_id).values(status=status)


async def test_retry_parse_viewer_denied(client, db_env, monkeypatch):
    from app.models.project_member import ProjectMember

    _patch_storage(monkeypatch)
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.api.v1.assets.schedule_asset_parse",
        lambda asset_id, delay_seconds=2.0: scheduled.append(asset_id),
    )
    pid = await _create_project(client, "门禁项目")
    async with db_env() as db:
        db.add(
            ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
        )
        await db.commit()
    asset = await _upload(client, pid, _docx(["内容"]), "a.docx", "plan_doc")
    resp = await client.post(
        f"/api/v1/projects/{pid}/assets/{asset['id']}/retry-parse",
        headers=_auth("bob"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3031
    assert scheduled == [asset["id"]]  # 仅上传时的投递
