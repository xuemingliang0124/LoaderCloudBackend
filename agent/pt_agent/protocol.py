"""Agent ↔ Master 消息协议（信封 + 消息类型常量）。

⚠️ 与 master/app/ws/protocol.py 保持一致，两端必须同分支一次提交修改。
信封格式：{"type": "<消息类型>", "data": {...}, "ts": <unix秒>}
"""

import time
from typing import Any

from pydantic import BaseModel

# master -> agent
MSG_TASK = "task"  # data: run_id, files[{key,save_as,url}], upload{jtl{key,url},report{key,url}}, jmeter_args, start_at
# 注意：MSG_TASK 不再携带 plugins 字段，插件由 PluginSyncer 在启动/在线推送时对齐
MSG_STOP = "stop"  # data: run_id
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_PLUGIN_SYNC = (
    "plugin_sync"  # data: action=install, plugins[{id,name,version,sha256,size,url}]
)
MSG_PLUGIN_REMOVE = "plugin_remove"  # data: sha256_list[]

# agent -> master
MSG_REGISTER = "register"  # data: agent_id, tags（握手 query 另带 ip/hostname/plugins/cpu_cores/mem_total_gb）
MSG_HEARTBEAT = "heartbeat"  # data: cpu, mem, net_in, net_out, cpu_cores, mem_total_gb, status, current_run_id, plugin_hashes[]
MSG_TASK_ACK = "task_ack"  # data: run_id, accepted, message
MSG_STATUS = "status"  # data: run_id, phase, message
MSG_METRICS = "metrics"  # data: run_no, samples, success, interval_tps, avg_rt, min_rt, max_rt, p95_rt, err_rate, errors, threads, by_label[{label,sample_type,samples,success,interval_tps,avg_rt,min_rt,max_rt,p95_rt,err_rate,errors,threads}]，sample_type=request|transaction
MSG_RESULT = "result"  # data: run_id, summary{samples,success,errors,min_rt,max_rt,avg_rt,p95_rt,avg_tps,failed,by_label[{label,sample_type,samples,success,errors,min_rt,max_rt,avg_rt,p95_rt,avg_tps}]}, artifacts
MSG_PLUGIN_ACK = (
    "plugin_ack"  # data: plugins[{name,sha256,size}] Agent 端插件清单变更后上报
)


class Envelope(BaseModel):
    type: str
    data: dict[str, Any] = {}
    ts: int = 0

    @classmethod
    def now(cls, type_: str, data: dict[str, Any] | None = None) -> "Envelope":
        return cls(type=type_, data=data or {}, ts=int(time.time()))
