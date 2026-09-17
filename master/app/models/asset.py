"""文档资产表：用户上传的测试方案/环境清单/交易清单/SLA/架构文档等。

文件本体存 MinIO（key=assets/{asset_id}/{filename}），元数据与解析状态入 MySQL。
(project_id, hash_sha256) 项目内唯一：同内容重复上传直接复用原 asset_id，
避免 MinIO 重复存储与重复解析消耗。asset_type 决定 D3 解析管道走向，
status 为 PENDING→PARSING→READY/FAILED 状态机（D1 仅落 PENDING）。

与项目关系：FK→test_project 默认 RESTRICT（同 environment/transaction 口径），
项目 force 删除在应用层 bulk delete 资产（projects.py 级联清理）。
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class Asset(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_asset"
    __table_args__ = (
        UniqueConstraint("project_id", "hash_sha256", name="uq_test_asset_hash"),
    )

    # 所属项目：资产为项目内文档，所有资产接口均按项目作用域嵌套访问
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_project.id", name="fk_test_asset_project"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(256), default="")
    # 资产类型（AssetType value）：决定 D3 解析管道与列映射规则
    asset_type: Mapped[str] = mapped_column(String(32), index=True)
    # 解析状态（AssetStatus value）：D1 上传即 PENDING，D3 异步推进
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 原始文件名（含扩展名），供前端展示与解析管道识别格式
    filename: Mapped[str] = mapped_column(String(256), default="")
    # MinIO object key：assets/{asset_id}/{filename}
    file_key: Mapped[str] = mapped_column(String(512), default="")
    # 文件内容 sha256（64 位十六进制）：项目内同 hash 复用，去重存储
    hash_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    content_type: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(String(512), default="")
    # 解析元数据：D3 写入未匹配列名、抽取行数等，供 D5 列映射修正接口读取
    parse_meta: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="")
