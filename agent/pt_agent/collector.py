"""本机资源采集（psutil，快速非阻塞）。"""

import socket

import psutil


def snapshot() -> dict:
    """采集资源快照：CPU / 内存 / 网络累计流量 + 规格（核数、内存总量）。"""
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "mem": vm.percent,
        "net_in": net.bytes_recv,
        "net_out": net.bytes_sent,
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "mem_total_gb": round(vm.total / (1024**3), 2),
    }


def local_ip() -> str:
    """取本机出口 IP（UDP 探测，不实际发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
