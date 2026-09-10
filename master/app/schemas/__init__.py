"""请求/响应模型：auth / agent / run / script / scenario / schedule。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    name: str
    version: str
    file_key: str
    data_files: list | None
    params: list | None
    description: str


class ThreadGroupSettingIn(BaseModel):
    """场景内单个线程组的加压参数（创建/更新场景时传入）。"""

    thread_group_name: str
    testclass: str = "ThreadGroup"
    num_threads: int = 1
    ramp_time: int = 0
    loops: int = 1  # -1 表示无限循环
    scheduler: bool = False
    duration: int = 0  # scheduler=false 时为 0


class ScenarioScriptIn(BaseModel):
    """场景关联的单个脚本及其压力机选择 + 线程组设置。"""

    script_id: int
    order_index: int = 0
    # 本脚本的压力机选择：按 Agent 标签过滤，如 ["机房A"]
    agent_tags: list[str] = []
    # 本脚本需要的压力机数量
    agent_count: int = 1
    thread_groups: list[ThreadGroupSettingIn] = []


class ScenarioIn(BaseModel):
    name: str
    # 场景级 JVM 参数覆盖 {"host": "api.demo.com"}，执行时拼 -J 参数
    param_overrides: dict = {}
    description: str = ""
    scripts: list[ScenarioScriptIn] = []


class ThreadGroupSettingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    thread_group_name: str
    testclass: str
    num_threads: int
    ramp_time: int
    loops: int
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
    name: str
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
