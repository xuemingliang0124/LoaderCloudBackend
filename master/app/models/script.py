"""JMeter 脚本表：文件存 MinIO，占位符参数定义随脚本维护。"""

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class Script(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "jmeter_script"

    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(32), default="v1")
    # MinIO object key，如 scripts/{script_id}/{version}/order.jmx
    file_key: Mapped[str] = mapped_column(String(256), default="")
    # 数据文件附件 [{"key": "scripts/{id}/{version}/data.csv", "filename": "data.csv"}]
    # filename 为 JMX 内引用的原始文件名，下发时 save_as=filename 保证 JMeter 能找到
    data_files: Mapped[list | None] = mapped_column(JSON, default=list)
    # 占位符定义 [{"key": "threads", "default": "10", "desc": "并发线程数"}]
    params: Mapped[list | None] = mapped_column(JSON, default=list)
    # 第三方插件依赖 [{"key": "scripts/{id}/{version}/plugins/x.jar", "filename": "x.jar"}]
    # 下发时校验 Agent 已装；缺失则随任务下发，Agent 运行时装入 plugin_dir（免改镜像）
    plugins: Mapped[list | None] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
