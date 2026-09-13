"""JMX 线程组扫描响应模型。"""

from pydantic import BaseModel


class ThreadGroupOut(BaseModel):
    """线程组参数（变量引用已按作用域解析，前端展示与修改用）。"""

    name: str
    testclass: str
    # 线程组节点自身启用状态（enabled 属性缺省为 true）；禁用组仍返回，
    # 供前端展示开关与静态配置，执行时整组不运行
    enabled: bool = True
    num_threads: int
    ramp_time: int
    loops: int  # -1 表示无限循环
    scheduler: bool
    duration: int  # 0 表示未设置或 scheduler=false
    # 组内启用的常量吞吐量定时器换算的目标 TPS（TPM/60，允许小数），
    # 0 表示无定时器/定时器禁用/值无法解析（不限速）
    tps: float = 0.0


class JmxScanOut(BaseModel):
    """JMX 扫描结果。"""

    thread_groups: list[ThreadGroupOut]
