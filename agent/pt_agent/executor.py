"""任务生命周期：下载 → 执行 → 上传 → 结果上报。

实时指标（_metrics_loop）与终态汇总（_execute）均解析 JTL 产出真实数据。
插件不再随任务下发：由 PluginSyncer 在启动/在线推送时对齐 plugin_dir。
"""

import asyncio
import zipfile
from pathlib import Path

import httpx
from loguru import logger

from pt_agent.config import get_settings
from pt_agent.jtl_parser import parse_increment, parse_summary
from pt_agent.plugins import scan_plugin_paths
from pt_agent.protocol import MSG_METRICS, MSG_RESULT, Envelope
from pt_agent.runner import JMeterRunner
from pt_agent.state import AgentPhase, AgentState


class TaskExecutor:
    def __init__(self, state: AgentState, runner: JMeterRunner, reporter) -> None:
        self._state = state
        self._runner = runner
        self._reporter = reporter
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def submit(self, data: dict) -> None:
        """处理 Master 下发的 task 消息（幂等：忙时拒绝）。"""
        run_id = str(data.get("run_id", ""))
        if not run_id:
            return
        if self.busy:
            await self._reporter.send_task_ack(
                run_id, accepted=False, message="Agent 忙碌中"
            )
            return
        self._stopping = False
        self._task = asyncio.create_task(self._execute(data))
        await self._reporter.send_task_ack(run_id, accepted=True)

    async def stop(self, run_id: str) -> None:
        """停止当前任务：杀进程树后，主流程会走 stopped 分支。"""
        if self._state.current_run_id != run_id:
            await self._reporter.send_task_ack(
                run_id, accepted=False, message="未在执行该任务"
            )
            return
        self._stopping = True
        await self._runner.stop(run_id)

    async def _execute(self, data: dict) -> None:
        run_id = str(data["run_id"])
        scenario_script_id = data.get("scenario_script_id")
        settings = get_settings()
        jmeter_args = {
            str(k): str(v) for k, v in (data.get("jmeter_args") or {}).items()
        }
        try:
            self._state.set(AgentPhase.DOWNLOADING, run_id)
            await self._reporter.send_status(run_id, AgentPhase.DOWNLOADING)
            run_dir = Path(settings.work_dir).resolve() / run_id
            jmx_path = await self._prepare_files(
                run_id, data.get("files") or [], run_dir
            )

            jtl_path = str(run_dir / f"{run_id}.jtl")
            report_dir = str(run_dir / "report")

            # 插件已由 PluginSyncer 对齐到位，直接扫 plugin_dir 拼 search_paths
            plugin_paths = scan_plugin_paths(settings.plugin_dir_path)

            self._state.set(AgentPhase.RUNNING, run_id)
            await self._reporter.send_status(run_id, AgentPhase.RUNNING)
            await self._runner.start(
                run_id,
                jmx_path,
                jmeter_args,
                jtl_path,
                report_dir,
                plugin_paths=plugin_paths,
            )

            metrics_task = asyncio.create_task(self._metrics_loop(run_id, jtl_path))
            exit_code = await self._runner.wait()
            metrics_task.cancel()
            # 非停止导致的非零退出码：JMeter 本身执行失败，直接报真实原因，
            # 避免流入上传阶段被"JTL 不存在"掩盖
            if exit_code != 0 and not self._stopping:
                raise RuntimeError(
                    f"JMeter 执行失败，退出码 {exit_code}，请查看 JMeter 控制日志"
                )

            self._state.set(AgentPhase.UPLOADING, run_id)
            await self._reporter.send_status(run_id, AgentPhase.UPLOADING)
            artifacts = await self._upload_artifacts(
                run_id, jtl_path, report_dir, data.get("upload") or {}
            )

            # 解析 JTL 生成真实汇总（samples/errors/p95/max_tps/by_label）
            # 即便停止也把已采集样本汇总上报，summary.failed 单独标记
            summary = await parse_summary(jtl_path, failed=self._stopping)
            if self._stopping:
                await self._reporter.send_status(run_id, AgentPhase.STOPPED)
            else:
                await self._reporter.send_status(run_id, AgentPhase.FINISHED)
            await self._reporter.send(
                Envelope.now(
                    MSG_RESULT,
                    {
                        "run_id": run_id,
                        "scenario_script_id": scenario_script_id,
                        "summary": summary,
                        "artifacts": artifacts,
                    },
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{run_id}] 执行异常")
            await self._reporter.send_status(
                run_id, AgentPhase.FAILED, message=str(exc)
            )
            await self._reporter.send(
                Envelope.now(
                    MSG_RESULT,
                    {
                        "run_id": run_id,
                        "scenario_script_id": scenario_script_id,
                        "summary": {"failed": True, "message": str(exc)},
                        "artifacts": [],
                    },
                )
            )
        finally:
            self._state.set(AgentPhase.IDLE)
            # 任务结束后清理延后删除的插件（PluginSyncer.remove 标记的 pending_remove）
            syncer = getattr(self._reporter, "_plugin_syncer", None)
            if syncer is not None:
                try:
                    await syncer.flush_pending_removes()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{run_id}] 清理延后删除插件失败: {exc}")

    async def _prepare_files(self, run_id: str, files: list, run_dir: Path) -> str:
        """经 Master 预签名的 MinIO URL 下载任务文件，返回 JMX 本地路径。

        files 格式（Master 下发）：
        [{"key": "scripts/1/v1/a.jmx", "save_as": "a.jmx", "url": "<presigned GET>"}]
        """
        if not files:
            raise RuntimeError("任务文件清单为空（Master 未生成下载清单）")
        run_dir.mkdir(parents=True, exist_ok=True)
        jmx_path = ""
        for item in files:
            save_as = str(item.get("save_as") or Path(str(item.get("key", ""))).name)
            url = str(item.get("url") or "")
            if not url:
                raise RuntimeError(f"文件缺少下载地址: {save_as}")
            dest = run_dir / save_as
            await asyncio.to_thread(self._download_sync, url, dest)
            logger.info(f"[{run_id}] 已下载 {save_as} ({dest.stat().st_size} bytes)")
            if not jmx_path and save_as.lower().endswith(".jmx"):
                jmx_path = str(dest)
        if not jmx_path:
            raise RuntimeError("文件清单中未找到 .jmx 脚本")
        return jmx_path

    @staticmethod
    def _download_sync(url: str, dest: Path) -> None:
        """流式下载到本地（阻塞实现，调用方用 to_thread 包装）。"""
        with httpx.Client(
            timeout=httpx.Timeout(600.0), follow_redirects=True
        ) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        f.write(chunk)

    async def _metrics_loop(self, run_id: str, jtl_path: str) -> None:
        """运行期周期上报指标：tail JTL 增量解析，按 label 计算 TPS/RT 分位数/错误率。

        JMeter 持续追加写入 JTL，本循环每 metrics_interval 秒读一次新增行。
        offset 落在行中间时由 parser 丢弃残行，保证不解析错位。
        本批无新样本时上报零值，保持 ES 曲线连续。
        """
        settings = get_settings()
        last_offset = 0
        # 跨批记忆「是否出现过 request 行」，供全事务配置下的全局口径兜底
        parse_state = {"saw_request": False}
        while True:
            await asyncio.sleep(settings.metrics_interval)
            try:
                metrics, last_offset = await parse_increment(
                    jtl_path, last_offset, settings.metrics_interval, parse_state
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{run_id}] 增量解析失败: {exc}")
                continue
            metrics["run_no"] = run_id
            await self._reporter.send(Envelope.now(MSG_METRICS, metrics))

    async def _upload_artifacts(
        self, run_id: str, jtl_path: str, report_dir: str, upload: dict
    ) -> list:
        """经预签名 PUT 上传 JTL 与 HTML 报告，返回 artifacts 清单 [{type, key}]。

        upload 格式（Master 下发，按 agent 区分）：
        {"jtl": {"key": ..., "url": ...}, "report": {"key": ..., "url": ...}}
        JTL 是结果数据，上传失败视为任务失败；报告上传失败仅告警。
        """
        artifacts: list[dict] = []
        jtl_target = upload.get("jtl") or {}
        if jtl_target.get("url") and Path(jtl_path).exists():
            await self._upload_file(jtl_target["url"], jtl_path)
            artifacts.append({"type": "jtl", "key": jtl_target.get("key", "")})
            logger.info(f"[{run_id}] JTL 已上传: {jtl_target.get('key')}")
        else:
            raise RuntimeError(f"JTL 不存在或缺少上传地址: {jtl_path}")

        report_target = upload.get("report") or {}
        if report_target.get("url") and Path(report_dir).is_dir():
            zip_path = f"{report_dir}.zip"
            await asyncio.to_thread(self._zip_dir, report_dir, zip_path)
            try:
                await self._upload_file(report_target["url"], zip_path)
                artifacts.append(
                    {"type": "report", "key": report_target.get("key", "")}
                )
                logger.info(f"[{run_id}] HTML 报告已上传: {report_target.get('key')}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{run_id}] 报告上传失败（不影响结果上报）: {exc}")
        else:
            logger.warning(
                f"[{run_id}] 报告目录不存在或缺少上传地址，跳过: {report_dir}"
            )
        return artifacts

    @staticmethod
    async def _upload_file(url: str, path: str) -> None:
        """预签名 PUT 上传（阻塞实现用 to_thread 包装）。

        签名未包含 Content-Type，因此请求不可携带该头，否则 MinIO 拒签。
        """

        def _put() -> None:
            with (
                open(path, "rb") as f,
                httpx.Client(timeout=httpx.Timeout(600.0)) as client,
            ):
                resp = client.put(url, content=f)
                resp.raise_for_status()

        await asyncio.to_thread(_put)

    @staticmethod
    def _zip_dir(src_dir: str, zip_path: str) -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in Path(src_dir).rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(src_dir))
