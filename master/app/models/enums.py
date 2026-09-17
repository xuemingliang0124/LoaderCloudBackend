"""领域枚举：全部状态集中定义，禁止魔法字符串。"""

from enum import Enum


class AgentStatus(str, Enum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"


class AgentPhase(str, Enum):
    """Agent 单任务生命周期阶段（status 消息上报）。"""

    DOWNLOADING = "downloading"
    RUNNING = "running"
    UPLOADING = "uploading"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    # 停止指令已下发，等待 Agent 回报终态；回报收齐（或看门狗超时）才置 STOPPED
    STOPPING = "stopping"
    FINISHED = "finished"
    PARTIAL = "partial"
    FAILED = "failed"
    STOPPED = "stopped"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class ScenarioType(str, Enum):
    """压测场景类型，四选一。"""

    SINGLE_BASELINE = "单交易基准"
    SINGLE_LOAD = "单交易负载"
    MIXED = "混合场景"
    STABILITY = "稳定性"


class ProjectRole(str, Enum):
    """项目成员角色，三选一。DB 存英文 name 小写（owner/editor/viewer），API 收/出中文。"""

    OWNER = "项目管理员"
    EDITOR = "编辑者"
    VIEWER = "观察者"


class GlobalRole(str, Enum):
    """sys_user 全局角色。DB 存英文 name 小写（admin/user），API 收/出中文。

    历史数据中的 viewer 与 user 同为非管理员语义（鉴权只判断 == admin），
    UserOut 输出时统一按非管理员渲染为「普通用户」。
    """

    ADMIN = "管理员"
    USER = "普通用户"


class AssetType(str, Enum):
    """文档资产类型：决定 D3 解析管道的处理路径与列映射规则。

    - plan_doc：测试方案文档（docx/pdf）→ 文本切片入向量库
    - env_inventory：环境交付清单（xlsx）→ 结构化抽取到 test_environment
    - txn_inventory：交易清单（xlsx）→ 结构化抽取到 test_transaction
    - sla_doc：SLA 指标文档（docx/pdf）→ 文本切片入向量库
    - architecture_doc：架构说明文档（docx/pdf/pptx）→ 文本切片入向量库
    """

    PLAN_DOC = "plan_doc"
    ENV_INVENTORY = "env_inventory"
    TXN_INVENTORY = "txn_inventory"
    SLA_DOC = "sla_doc"
    ARCHITECTURE_DOC = "architecture_doc"


class AssetStatus(str, Enum):
    """文档资产解析状态机：PENDING → PARSING → READY / FAILED。

    D1 仅落 PENDING（上传即入库，解析由 D3 异步管道推进）；
    FAILED 状态支持前端一键重试解析（D3）。
    """

    PENDING = "pending"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"
