"""JMeter 子进程管理：启动 / 停止（进程树清理）。

JMeter 是 JVM 进程，直接 terminate 可能留下子进程，
统一用 psutil 处理整棵进程树。
"""

import asyncio
import os
import subprocess
import sys

import psutil
from loguru import logger


class JMeterRunner:
    def __init__(self, jmeter_bin: str, work_dir: str) -> None:
        self.jmeter_bin = jmeter_bin
        self.work_dir = work_dir
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(
        self,
        run_id: str,
        jmx_path: str,
        jmeter_args: dict[str, str],
        jtl_path: str,
        report_dir: str,
    ) -> None:
        """以命令行模式启动 JMeter：-n -t x.jmx -J覆盖参数 -l x.jtl -e -o report。"""
        if self.running:
            raise RuntimeError("已有任务在执行中")
        os.makedirs(self.work_dir, exist_ok=True)
        # 预创建报告目录：JMeter -o 要求目录不存在或为空，且父目录必须已存在
        os.makedirs(report_dir, exist_ok=True)
        cmd = [
            self.jmeter_bin, "-n",
            "-t", jmx_path,
            "-l", jtl_path,
            "-e", "-o", report_dir,
            # 逐行落盘 JTL：默认 false 会缓冲到测试结束才 flush，
            # 增量 tail 解析（_metrics_loop）将读不到实时数据
            "-Jjmeter.save.saveservice.autoflush=true",
        ]
        for key, value in jmeter_args.items():
            cmd.append(f"-J{key}={value}")
        logger.info(f"[{run_id}] 启动 JMeter: {' '.join(cmd)}")

        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self._process = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.work_dir, **popen_kwargs
        )

    async def wait(self) -> int:
        assert self._process is not None
        code = await self._process.wait()
        logger.info(f"JMeter 退出码: {code}")
        return code

    async def stop(self, run_id: str) -> bool:
        """停止执行：终止整棵进程树（JVM 及其子进程）。"""
        if not self.running or self._process is None:
            return False
        logger.info(f"[{run_id}] 停止 JMeter 进程树")
        try:
            parent = psutil.Process(self._process.pid)
            procs = [parent, *parent.children(recursive=True)]
        except psutil.NoSuchProcess:
            self._process = None
            return False
        for proc in procs:
            proc.terminate()
        _, alive = await asyncio.to_thread(psutil.wait_procs, procs, timeout=10)
        for proc in alive:
            proc.kill()
        self._process = None
        return True
