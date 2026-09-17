"""请求/响应模型：auth / agent / run / script / scenario / schedule / project / member / user。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    AssetStatus,
    AssetType,
    GlobalRole,
    ProjectRole,
    ScenarioType,
)


class LoginIn(BaseModel):
    username: str
    password: str


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: str
    ip: str
    hostname: str
    tags: list | None
    jmeter_version: str
    plugins: list | None
    cpu_cores: int
    mem_total_gb: float
    status: str
    cpu_percent: float
    mem_percent: float
    current_run_no: str | None
    last_heartbeat: datetime | None


class AgentRegisterIn(BaseModel):
    # Agent 启动时上报的宿主机 IP（Master 据此返回固定 agent_id）
    ip: str
    hostname: str = ""
    tags: list[str] = []
    jmeter_version: str = ""
    # 已安装插件清单：[{name, sha256, size}]（plugin_dir 实际扫描结果，
    # lib/ext 内置的不算，由镜像负责）
    plugins: list[dict] = []
    cpu_cores: int = 0
    mem_total_gb: float = 0.0


class AgentRegisterOut(BaseModel):
    agent_id: str
    ip: str
    hostname: str
    # true 表示首次注册（Master 新建了节点）
    is_new: bool


class RunCreateIn(BaseModel):
    scenario_id: int
    # 不传则按场景配置自动选机
    agent_ids: list[str] | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_no: str
    scenario_id: int
    # ORM 无此列：由接口层按 scenario_id 关联 test_scenario.name 填充
    scenario_name: str = ""
    status: str
    trigger: str
    agent_ids: list | None
    start_time: datetime | None
    end_time: datetime | None
    error_message: str
    created_by: str


class ScriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    version: str
    file_key: str
    data_files: list | None
    params: list | None
    description: str


class ThreadGroupSettingIn(BaseModel):
    """场景内单个线程组的加压参数（创建/更新场景时传入）。

    调度器与运行时长不再由线程组级传入：落库统一 scheduler=True，
    duration 使用场景级运行时间（见 scenarios.py 存储逻辑）。
    循环次数不再可配：非基准场景执行期统一无限循环（由场景时长收口），
    单交易基准固定 100 次（见 jmx_assembler）。

    tps：目标吞吐量（每秒样本数），0 表示不限速；执行期 ×60 换算为 TPM
    写入线程组内常量吞吐量定时器（ConstantThroughputTimer）。
    """

    thread_group_name: str = Field(..., examples=["登录接口压测"])
    testclass: str = Field("ThreadGroup", examples=["ThreadGroup"])
    # 线程组启用开关：缺省取脚本扫描结果；false 时执行期整组不运行
    enabled: bool = Field(True, examples=[True])
    num_threads: int = Field(1, examples=[100])
    ramp_time: int = Field(0, examples=[10])
    tps: int = Field(0, ge=0, examples=[100])


class ScenarioScriptIn(BaseModel):
    """场景关联的单个脚本及其压力机选择 + 线程组设置。"""

    script_id: int = Field(..., examples=[1])
    order_index: int = Field(0, examples=[0])
    # 本脚本的压力机选择：按 Agent 标签过滤，如 ["机房A"]
    agent_tags: list[str] = Field(default_factory=list, examples=[["机房A"]])
    # 本脚本需要的压力机数量
    agent_count: int = Field(1, examples=[2])
    thread_groups: list[ThreadGroupSettingIn] = Field(default_factory=list)


class ScenarioIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "登录接口全链路压测",
                "scenario_type": "单交易基准",
                "duration": 600,
                "environment_id": 1,
                "param_overrides": {"host": "api.demo.com"},
                "description": "模拟高峰时段登录请求",
                "scripts": [
                    {
                        "script_id": 1,
                        "order_index": 0,
                        "agent_tags": ["机房A"],
                        "agent_count": 2,
                        "thread_groups": [
                            {
                                "thread_group_name": "登录接口压测",
                                "testclass": "ThreadGroup",
                                "enabled": True,
                                "num_threads": 100,
                                "ramp_time": 10,
                                "tps": 100,
                            }
                        ],
                    }
                ],
            }
        }
    )

    name: str
    scenario_type: ScenarioType
    # 场景级运行时间（秒）
    duration: int = 0
    # 绑定的被测环境（可选）：执行期把 environment.variables 注入 -J 参数
    # 作为 param_overrides 的基础层；不传或传 null 表示不绑定环境
    environment_id: int | None = None
    # 场景级 JVM 参数覆盖 {"host": "api.demo.com"}，执行时拼 -J 参数；
    # 优先级高于 environment.variables
    param_overrides: dict = {}
    description: str = ""
    scripts: list[ScenarioScriptIn] = []


class ScenarioUpdateIn(BaseModel):
    """场景更新请求：基础字段 + 关联脚本（全量替换）。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "登录接口全链路压测",
                "scenario_type": "单交易基准",
                "duration": 600,
                "environment_id": 1,
                "param_overrides": {"host": "api.demo.com"},
                "description": "模拟高峰时段登录请求",
                "scripts": [
                    {
                        "script_id": 1,
                        "order_index": 0,
                        "agent_tags": ["机房A"],
                        "agent_count": 2,
                        "thread_groups": [
                            {
                                "thread_group_name": "登录接口压测",
                                "testclass": "ThreadGroup",
                                "enabled": True,
                                "num_threads": 100,
                                "ramp_time": 10,
                                "tps": 100,
                            }
                        ],
                    }
                ],
            }
        }
    )

    name: str
    scenario_type: ScenarioType
    duration: int = 0
    # 绑定的被测环境：传 null 表示解绑，传 int 表示绑定到新环境
    environment_id: int | None = None
    param_overrides: dict = {}
    description: str = ""
    # 全量替换：传空数组表示清空场景下所有脚本关联
    scripts: list[ScenarioScriptIn] = []


class ThreadGroupSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    thread_group_name: str
    testclass: str
    enabled: bool
    num_threads: int
    ramp_time: int
    tps: int
    scheduler: bool
    duration: int


class ScenarioScriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    script_id: int
    order_index: int
    agent_tags: list | None
    agent_count: int
    script_name: str = ""
    thread_groups: list[ThreadGroupSettingOut] = []


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    scenario_type: ScenarioType
    duration: int
    # 绑定的被测环境（nullable：未绑定为 null）
    environment_id: int | None
    param_overrides: dict | None
    description: str
    scripts: list[ScenarioScriptOut] = []


class ScheduleIn(BaseModel):
    name: str
    scenario_id: int
    # 标准 5 段 crontab：分 时 日 月 周
    cron: str


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    scenario_id: int
    cron: str
    enabled: bool
    last_run_no: str | None


class ProjectIn(BaseModel):
    """新建项目请求。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "电商交易链路压测",
                "description": "覆盖下单/支付/库存核心链路的性能测试项目",
            }
        }
    )

    name: str = Field(..., min_length=1, max_length=128, examples=["电商交易链路压测"])
    description: str = Field(default="", max_length=512, examples=["核心链路性能测试"])

    @field_validator("name")
    @classmethod
    def _strip_and_require_name(cls, v: str) -> str:
        # 统一去除首尾空白，纯空白名称视为非法（422）
        v = v.strip()
        if not v:
            raise ValueError("项目名称不能为空")
        return v


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    # 当前请求者在项目内的角色（中文：项目管理员/编辑者/观察者），
    # 由各接口按调用者身份填充，ORM 无此列（默认空串占位）
    my_role: str = ""


class ProjectUpdateIn(BaseModel):
    """更新项目请求：name 与 description 至少传一项。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def _strip_and_require_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # 与 ProjectIn 口径一致：去除首尾空白，纯空白名称视为非法（422）
        v = v.strip()
        if not v:
            raise ValueError("项目名称不能为空")
        return v

    @model_validator(mode="after")
    def _at_least_one(self) -> "ProjectUpdateIn":
        if self.name is None and self.description is None:
            raise ValueError("name 与 description 至少提供一项")
        return self


class EnvironmentIn(BaseModel):
    """新建被测环境请求：项目内 env_code 唯一。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "生产环境",
                "env_code": "prod",
                "base_url": "https://api.demo.com",
                "hosts": [
                    {"name": "app-01", "host": "10.0.0.1", "port": 8080, "role": "应用"}
                ],
                "db_connections": [
                    {
                        "name": "订单库",
                        "type": "mysql",
                        "dsn": "mysql://10.0.0.3:3306/orders",
                    }
                ],
                "middleware_info": [
                    {"type": "redis", "address": "10.0.0.2:6379", "remark": "缓存"}
                ],
                "variables": {"base_url": "https://api.demo.com"},
                "description": "生产集群，变更需审批",
            }
        }
    )

    name: str = Field(..., min_length=1, max_length=128, examples=["生产环境"])
    env_code: str = Field(..., min_length=1, max_length=64, examples=["prod"])
    base_url: str = Field(default="", max_length=512, examples=["https://api.demo.com"])
    # 主机/数据库/中间件均为清单结构，元素为自由 JSON 对象（结构后续随资产管道固化）
    hosts: list[dict] = Field(default_factory=list)
    db_connections: list[dict] = Field(default_factory=list)
    middleware_info: list[dict] = Field(default_factory=list)
    # 执行期注入 JMX 的 -J 键值覆盖
    variables: dict = Field(default_factory=dict)
    description: str = Field(default="", max_length=512)

    @field_validator("name", "env_code")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        # 与项目名称口径一致：去除首尾空白，纯空白视为非法（422）
        v = v.strip()
        if not v:
            raise ValueError("名称与环境编码不能为空")
        return v


class EnvironmentUpdateIn(BaseModel):
    """更新被测环境请求：所有字段可选，至少传一项。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    env_code: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=512)
    hosts: list[dict] | None = None
    db_connections: list[dict] | None = None
    middleware_info: list[dict] | None = None
    variables: dict | None = None
    description: str | None = Field(default=None, max_length=512)

    @field_validator("name", "env_code")
    @classmethod
    def _strip_and_require(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("名称与环境编码不能为空")
        return v

    @model_validator(mode="after")
    def _at_least_one(self) -> "EnvironmentUpdateIn":
        fields = (
            self.name,
            self.env_code,
            self.base_url,
            self.hosts,
            self.db_connections,
            self.middleware_info,
            self.variables,
            self.description,
        )
        if all(f is None for f in fields):
            raise ValueError("至少提供一个更新字段")
        return self


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    env_code: str
    base_url: str
    hosts: list | None
    db_connections: list | None
    middleware_info: list | None
    variables: dict | None
    description: str
    created_at: datetime
    updated_at: datetime


class TransactionIn(BaseModel):
    """新建被测交易请求：项目内 txn_code 唯一。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "登录交易",
                "txn_code": "login",
                "default_script_id": 1,
                "sla_tps": 100.0,
                "sla_p95_ms": 500,
                "sla_error_rate": 1.0,
                "description": "登录接口压测交易",
            }
        }
    )

    name: str = Field(..., min_length=1, max_length=128, examples=["登录交易"])
    txn_code: str = Field(..., min_length=1, max_length=64, examples=["login"])
    # 默认执行脚本：弱关联，仅标记默认版本，不阻断脚本删除；可空
    default_script_id: int | None = Field(default=None, examples=[1])
    sla_tps: float | None = Field(default=None, ge=0, examples=[100.0])
    sla_p95_ms: int | None = Field(default=None, ge=0, examples=[500])
    sla_error_rate: float | None = Field(default=None, ge=0, le=100, examples=[1.0])
    description: str = Field(default="", max_length=512)

    @field_validator("name", "txn_code")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        # 与环境编码口径一致：去除首尾空白，纯空白视为非法（422）
        v = v.strip()
        if not v:
            raise ValueError("名称与交易编码不能为空")
        return v


class TransactionUpdateIn(BaseModel):
    """更新被测交易请求：所有字段可选，至少传一项。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    txn_code: str | None = Field(default=None, min_length=1, max_length=64)
    default_script_id: int | None = Field(default=None)
    sla_tps: float | None = Field(default=None, ge=0)
    sla_p95_ms: int | None = Field(default=None, ge=0)
    sla_error_rate: float | None = Field(default=None, ge=0, le=100)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("name", "txn_code")
    @classmethod
    def _strip_and_require(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("名称与交易编码不能为空")
        return v

    @model_validator(mode="after")
    def _at_least_one(self) -> "TransactionUpdateIn":
        fields = (
            self.name,
            self.txn_code,
            self.default_script_id,
            self.sla_tps,
            self.sla_p95_ms,
            self.sla_error_rate,
            self.description,
        )
        if all(f is None for f in fields):
            raise ValueError("至少提供一个更新字段")
        return self


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    txn_code: str
    default_script_id: int | None
    sla_tps: float | None
    sla_p95_ms: int | None
    sla_error_rate: float | None
    description: str
    created_at: datetime
    updated_at: datetime


class MemberGrantIn(BaseModel):
    """项目成员授权请求。"""

    model_config = ConfigDict(
        json_schema_extra={"example": {"username": "zhangsan", "role": "编辑者"}}
    )

    username: str = Field(..., min_length=1, max_length=64)
    role: ProjectRole

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("用户名不能为空")
        return v


class MemberRoleUpdateIn(BaseModel):
    """项目成员角色变更请求（非法角色由枚举校验直接 422）。"""

    role: ProjectRole


class MemberOut(BaseModel):
    """项目成员响应：role 输出中文角色名。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    username: str
    role: str  # 中文角色名（项目管理员/编辑者/观察者）
    granted_by: str
    created_at: datetime
    updated_at: datetime


class UserCreateIn(BaseModel):
    """新建用户请求。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "zhangsan",
                "password": "secret123",
                "role": "普通用户",
            }
        }
    )

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=64)
    role: GlobalRole

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("用户名不能为空")
        return v


class UserUpdateIn(BaseModel):
    """用户更新请求：角色与密码至少传一项。"""

    role: GlobalRole | None = None
    password: str | None = Field(default=None, min_length=6, max_length=64)

    @model_validator(mode="after")
    def _at_least_one(self) -> "UserUpdateIn":
        if self.role is None and self.password is None:
            raise ValueError("role 与 password 至少提供一项")
        return self


class UserOut(BaseModel):
    """用户响应：不含密码，role 输出中文角色名。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str  # 中文角色名（管理员/普通用户）
    created_at: datetime
    updated_at: datetime


class AssetIn(BaseModel):
    """上传文档资产请求（multipart 表单字段校验模型）。

    上传接口实际用 Form 接收字段，再用本模型做统一校验；
    name 缺省时取上传文件名（去扩展名）。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "asset_type": "env_inventory",
                "name": "生产环境交付清单",
                "description": "2026Q3 生产环境主机与中间件清单",
            }
        }
    )

    asset_type: AssetType
    name: str | None = Field(default=None, max_length=256)
    description: str = Field(default="", max_length=512)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        return v or None


class AssetUpdateIn(BaseModel):
    """更新资产元数据请求：仅名称/描述/类型可改，文件本体不可替换。

    至少传一项；asset_type 变更受文件扩展名约束（与上传时校验一致）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=512)
    asset_type: AssetType | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("资产名称不能为空")
        return v

    @model_validator(mode="after")
    def _at_least_one(self) -> "AssetUpdateIn":
        if self.name is None and self.description is None and self.asset_type is None:
            raise ValueError("至少提供一个更新字段")
        return self


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    asset_type: AssetType
    status: AssetStatus
    filename: str
    file_key: str
    hash_sha256: str
    file_size: int
    content_type: str
    description: str
    parse_meta: dict | None
    created_by: str
    created_at: datetime
    updated_at: datetime
