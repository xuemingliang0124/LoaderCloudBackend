"""交易清单表：项目内被测交易资产（登录/下单/支付等可压测的业务动作）。

交易是场景编排的语义单元：一个交易可对应多版本 JMX 脚本，
default_script_id 为弱关联（nullable，仅标记默认执行版本，不阻断脚本删除），
后续 LLM 工具链 query_transactions 据交易码定位脚本与 SLA 指标。
(project_id, txn_code) 项目内唯一：txn_code 为机器可读编码，供自动化引用。

SLA 指标用 Float 而非 Numeric：监控阈值非货币，Float 在 SQLite(REAL)/MySQL(FLOAT)
间行为一致，避免 Decimal 跨库序列化差异；sla_p95_ms 为整数毫秒。
"""

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class Transaction(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_transaction"
    __table_args__ = (
        UniqueConstraint("project_id", "txn_code", name="uq_test_transaction_code"),
    )

    # 所属项目：交易为项目内资产，所有交易接口均按项目作用域嵌套访问
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_project.id", name="fk_test_transaction_project"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    # 项目内唯一机器可读编码（如 login/create_order），自动化引用与 LLM 工具调用用
    txn_code: Mapped[str] = mapped_column(String(64))
    # 默认执行脚本：弱关联（nullable，仅标记默认版本，不阻断脚本删除）
    # ondelete=SET NULL：脚本删除时由 DB 自动置空（MySQL 生效；SQLite 测试 FK 关闭不依赖）
    default_script_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "jmeter_script.id",
            name="fk_test_transaction_default_script",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )
    # SLA 指标：目标吞吐量（每秒样本数）
    sla_tps: Mapped[float | None] = mapped_column(Float, nullable=True)
    # SLA 指标：P95 响应时间（毫秒）
    sla_p95_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SLA 指标：错误率上限（百分比，0-100）
    sla_error_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(String(512), default="")
