"""Agent 端插件同步器：与 Master 全局插件池对齐 plugin_dir。

职责：
- 启动时按 expected_plugins（register 响应附带）做全量 diff：下载缺失、删除多余
- 收到 MSG_PLUGIN_SYNC 后台安装新插件（install）
- 收到 MSG_PLUGIN_REMOVE 删本地 jar（remove）
- 装完/删完重算 sha256 集合，发 MSG_PLUGIN_ACK 上报

并发限速：Semaphore(2) 限制同时下载的 jar 数（避免带宽挤占）
串行化：asyncio.Lock 串行化下载/删除，避免任务执行期访问 plugin_dir 文件竞争
延后清理：Agent 正在跑用某插件的任务时，该插件标记延后删除（任务结束后清理）
"""

import asyncio
from pathlib import Path

from loguru import logger

from pt_agent.config import AgentSettings
from pt_agent.plugins import _sha256, download_jar
from pt_agent.protocol import MSG_PLUGIN_ACK, Envelope


class PluginSyncer:
    """插件同步器：启动注册 + 在线推送 + 延后清理三入口。"""

    # 并发下载限速：同时最多 2 个 jar 下载，避免带宽挤占
    _CONCURRENCY = 2

    def __init__(self, settings: AgentSettings, reporter) -> None:
        self._settings = settings
        self._reporter = reporter
        self._dir = Path(settings.plugin_dir_path)
        # 串行化同步：与任务执行期 _runner 访问 plugin_dir 不竞争
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self._CONCURRENCY)
        # 延后清理集合：Master 推 remove 时若 Agent 正在跑任务，
        # 把 sha 入此集合，任务结束后统一清理
        self._pending_removes: set[str] = set()

    def snapshot(self) -> list[dict]:
        """扫描 plugin_dir 下所有 jar，返回 [{name, sha256, size}]。

        lib/ext 内置 jar 不算（由镜像负责，Master 也不下发）。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        out: list[dict] = []
        for jar in self._dir.glob("*.jar"):
            try:
                sha = _sha256(jar)
            except OSError as exc:
                logger.warning(f"计算 sha256 失败 {jar.name}: {exc}")
                continue
            out.append({"name": jar.name, "sha256": sha, "size": jar.stat().st_size})
        return out

    async def sync_from_expected(self, expected: list[dict]) -> None:
        """启动注册响应触发的全量对齐。

        expected: [{id, name, version, sha256, size, url}]
        算法：
        - want = expected 中的 sha256 集合
        - local = 当前 plugin_dir 实际 sha256 集合
        - to_download = expected - local（缺失的）
        - to_remove = local - want（多余的，可能 Master 已删/禁用）
        """
        if not expected:
            # 没有期望插件：清空 plugin_dir（让 Agent 与 Master 完全对齐）
            async with self._lock:
                await self._remove_all_local()
            await self._ack()
            return

        async with self._lock:
            local = {p["sha256"]: p["name"] for p in self.snapshot()}
            want = {p["sha256"]: p for p in expected}
            to_download = [p for sha, p in want.items() if sha not in local]
            to_remove = [local[sha] for sha in local if sha not in want]
            if to_download:
                logger.info(
                    f"插件全量对齐：下载 {len(to_download)} 个，"
                    f"删除 {len(to_remove)} 个"
                )
            await self._download_all(to_download)
            await self._remove_all(to_remove)
        await self._ack()

    async def install(self, plugins: list[dict]) -> None:
        """MSG_PLUGIN_SYNC 触发：后台下载新插件（不删多余的）。

        Agent 接收消息时 create_task 包装本方法，避免阻塞 WS 接收循环。
        """
        if not plugins:
            return
        async with self._lock:
            await self._download_all(plugins)
        await self._ack()

    async def remove(self, sha256_list: list[str]) -> None:
        """MSG_PLUGIN_REMOVE 触发：删本地匹配 sha 的 jar。

        若 Agent 正在执行任务（reporter.busy），延后清理：
        - 把 sha 入 _pending_removes 集合
        - 任务结束触发 self._flush_pending_removes()
        """
        if not sha256_list:
            return

        # 任务执行中：延后清理，避免 JMeter 已加载类被删导致 ClassNotFound
        if self._reporter.is_busy():
            self._pending_removes.update(sha256_list)
            logger.info(f"Agent 忙，{len(sha256_list)} 个插件延后清理：{sha256_list}")
            return

        async with self._lock:
            local = {p["sha256"]: p["name"] for p in self.snapshot()}
            targets = [local[sha] for sha in sha256_list if sha in local]
            await self._remove_all(targets)
        await self._ack()

    async def flush_pending_removes(self) -> None:
        """任务结束后调用：清理延后删除队列里的插件。"""
        if not self._pending_removes:
            return
        sha_list = list(self._pending_removes)
        self._pending_removes.clear()
        logger.info(f"任务结束，清理延后删除插件：{sha_list}")
        async with self._lock:
            local = {p["sha256"]: p["name"] for p in self.snapshot()}
            targets = [local[sha] for sha in sha_list if sha in local]
            await self._remove_all(targets)
        await self._ack()

    async def _download_all(self, plugins: list[dict]) -> None:
        """并发下载并校验 sha256（Semaphore 限速 2）。"""
        tasks = [self._download_one(p) for p in plugins]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _download_one(self, plugin: dict) -> None:
        """下载单个 jar 并校验 sha256。

        - 已存在且 sha 匹配 → 跳过
        - 下载失败 / sha 不匹配 → 抛错（gather return_exceptions 收集）
        """
        async with self._sem:
            name = str(plugin.get("name") or "")
            url = str(plugin.get("url") or "")
            expected_sha = str(plugin.get("sha256") or "")
            if not name.endswith(".jar") or not url:
                logger.warning(f"插件清单非法: {plugin}")
                return
            jar_path = self._dir / name
            # 已存在且 sha 匹配 → 跳过
            if jar_path.exists():
                try:
                    actual_sha = _sha256(jar_path)
                except OSError:
                    actual_sha = ""
                if actual_sha == expected_sha:
                    logger.info(f"插件已存在且 sha 匹配，跳过下载: {name}")
                    return
                # sha 不匹配：删除重下
                jar_path.unlink()
            logger.info(f"下载插件 {name} v{plugin.get('version', '')}")
            try:
                await download_jar(url, jar_path)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"下载插件 {name} 失败: {exc}")
                if jar_path.exists():
                    jar_path.unlink()
                raise
            # sha256 校验
            try:
                actual_sha = _sha256(jar_path)
            except OSError as exc:
                logger.error(f"计算 sha256 失败 {name}: {exc}")
                raise
            if actual_sha != expected_sha:
                jar_path.unlink()
                raise RuntimeError(
                    f"插件 {name} sha256 校验失败: 期望 {expected_sha} 实际 {actual_sha}"
                )

    async def _remove_all(self, names: list[str] | None = None) -> None:
        """删除本地 jar。names 为 None 时清空 plugin_dir（极端对齐）。"""
        if names is None:
            # 全量清理：删除 plugin_dir 下所有 jar
            self._dir.mkdir(parents=True, exist_ok=True)
            for jar in self._dir.glob("*.jar"):
                try:
                    jar.unlink()
                    logger.info(f"已卸载插件 {jar.name}")
                except OSError as exc:
                    logger.warning(f"卸载 {jar.name} 失败: {exc}")
            return
        for name in names:
            jar_path = self._dir / name
            if jar_path.exists():
                try:
                    jar_path.unlink()
                    logger.info(f"已卸载插件 {name}")
                except OSError as exc:
                    logger.warning(f"卸载 {name} 失败: {exc}")

    async def _remove_all_local(self) -> None:
        """清空 plugin_dir（用于 expected 为空场景）。"""
        await self._remove_all(None)

    async def _ack(self) -> None:
        """上报当前 plugin_dir 实际清单，Master 据此刷新 agent_plugin 表。"""
        plugins = self.snapshot()
        await self._reporter.send(Envelope.now(MSG_PLUGIN_ACK, {"plugins": plugins}))
