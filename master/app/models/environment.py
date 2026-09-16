"""被测环境清单表：项目内环境资产（dev/staging/prod 等）。

环境承载被测系统的接入信息（base_url、主机清单、数据库连接、中间件、变量），
后续场景绑定环境（A3）后由编排层在执行期把 variables 注入 JMX 的 -J 参数。
(project_id, env_code) 项目内唯一：env_code 是机器可读编码，供自动化引用。
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


class Environment(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_environment"
    __table_args__ = (
        UniqueConstraint("project_id", "env_code", name="uq_test_environment_code"),
    )

    # 所属项目：环境为项目内资产，所有环境接口均按项目作用域嵌套访问
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_project.id", name="fk_test_environment_project"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    # 项目内唯一机器可读编码（如 dev/staging/prod），执行期 -J 注入与自动化引用用
    env_code: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(512), default="")
    # 主机清单：[{"name": "app-01", "host": "10.0.0.1", "port": 8080, "role": "应用"}]
    hosts: Mapped[list | None] = mapped_column(JSON, default=list)
    # 数据库连接清单：[{"name": "订单库", "type": "mysql", "dsn": "..."}]
    db_connections: Mapped[list | None] = mapped_column(JSON, default=list)
    # 中间件清单：[{"type": "redis", "address": "10.0.0.2:6379", "remark": ""}]
    middleware_info: Mapped[list | None] = mapped_column(JSON, default=list)
    # 执行期注入 JMX 的 -J 键值覆盖：{"base_url": "https://api.demo.com"}
    variables: Mapped[dict | None] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(String(512), default="")
