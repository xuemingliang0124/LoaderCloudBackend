"""JMeter 插件注册表：平台级插件池，与脚本解耦。

历史脚本级插件（jmeter_script.plugins）已被本表替代：
- 上传 jar 时按 sha256 去重落 MinIO 与本表
- 启用/禁用控制是否进入 Agent expected_plugins 清单
- Agent 启动/在线推送时按本表对齐本地 plugin_dir
"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class JmeterPlugin(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "jmeter_plugin"

    # 插件文件名（JMeter 类加载用），如 JMeterPlugins-Standard.jar
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(32), default="v1")
    # MinIO object key，如 plugins/{plugin_id}/{filename}
    file_key: Mapped[str] = mapped_column(String(256), default="")
    # 内容指纹：上传时算 sha256，
    # - Agent 端校验避免半包/中间人
    # - 同内容不同文件名可去重（复用 MinIO 对象）
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    # 禁用的插件不进入 expected_plugins，Agent 收 remove 后卸载本地 jar
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
