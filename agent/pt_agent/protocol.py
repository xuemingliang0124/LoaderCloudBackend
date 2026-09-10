"""Agent ↔ Master 消息协议（信封 + 消息类型常量）。

⚠️ 与 master/app/ws/protocol.py 保持一致，两端必须同分支一次提交修改。
信封格式：{"type": "<消息类型>", "data": {...}, "ts": <unix秒>}
"""

import time
from typing import Any

from pydantic import BaseModel

# master -> agent
MSG_TASK = "task"  # data: run_id, files[{key,save_as,url}], plugins[{filename,url}],
#                        #   upload{jtl{key,url},report{key,url}}, jmeter_args, start_at
MSG_STOP = "stop"  # data: run_id
MSG_PING = "ping"
MSG_PONG = "pong"

# agent -> master
MSG_REGISTER = "register"  # data: agent_id, tags（握手 query 另带 ip/hostname/plugins/cpu_cores/mem_total_gb）
MSG_HEARTBEAT = "heartbeat"  # data: cpu, mem, net_in, net_out, cpu_cores, mem_total_gb, status, current_run_id
MSG_TASK_ACK = "task_ack"  # data: run_id, accepted, message
MSG_STATUS = "status"  # data: run_id, phase, message
MSG_METRICS = "metrics"  # data: run_no, interval_tps, avg_rt, p95_rt, err_rate, threads, by_label[]
MSG_RESULT = "result"  # data: run_id, summary{samples,errors,p95_rt,max_tps,failed,by_label[{label,samples,errors,p95_rt,max_tps}]}, artifacts


class Envelope(BaseModel):
    type: str
    data: dict[str, Any] = {}
    ts: int = 0

    @classmethod
    def now(cls, type_: str, data: dict[str, Any] | None = None) -> "Envelope":
        return cls(type=type_, data=data or {}, ts=int(time.time()))
