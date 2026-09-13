"""JMeter 脚本表：文件存 MinIO，占位符参数定义随脚本维护。

插件依赖不再在脚本级声明：已迁移到全局插件池 jmeter_plugin 表，
由 PluginSyncer 在 Agent 启动/在线推送时统一对齐 plugin_dir。
"""

from sqlalchemy import JSON, BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class Script(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "jmeter_script"

    # 所属项目：脚本为项目内资产，所有脚本管理接口均按项目作用域访问
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_project.id", name="fk_jmeter_script_project"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(32), default="v1")
    # MinIO object key，如 scripts/{script_id}/{version}/order.jmx
    file_key: Mapped[str] = mapped_column(String(256), default="")
    # 数据文件附件 [{"key": "scripts/{id}/{version}/data.csv", "filename": "data.csv"}]
    # filename 为 JMX 内引用的原始文件名，下发时 save_as=filename 保证 JMeter 能找到
    data_files: Mapped[list | None] = mapped_column(JSON, default=list)
    # 占位符定义 [{"key": "threads", "default": "10", "desc": "并发线程数"}]
    params: Mapped[list | None] = mapped_column(JSON, default=list)
    # 历史 plugins 字段已下线：插件归 jmeter_plugin 表统一管理
    description: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
