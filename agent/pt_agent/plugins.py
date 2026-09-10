"""JMeter 插件管理：启动扫描已装插件 + 运行期按需下载（免改镜像）。

两个插件来源：
- JMETER_HOME/lib/ext：镜像内置插件（JMeter 启动自动加载，无需 search_paths）
- plugin_dir（默认 work_dir/plugins）：Master 下发的第三方插件 jar 落在这里，
  运行 JMeter 时通过 -Jsearch_paths=<jar 路径> 注入类路径

JMeter NewDriver 的 search_paths 支持按 OS 路径分隔符分隔的文件/目录列表，
这里直接给 jar 文件绝对路径。
"""

import asyncio
import shutil
from pathlib import Path

import httpx
from loguru import logger


def resolve_jmeter_home(jmeter_bin: str) -> Path | None:
    """从 jmeter 可执行文件路径推导 JMETER_HOME（…/bin/jmeter 的上两级）。"""
    bin_path = shutil.which(jmeter_bin) or jmeter_bin
    p = Path(bin_path).expanduser().resolve()
    if p.parent.name.lower() == "bin":
        return p.parent.parent
    return p.parent if p.parent.name else None


def scan_plugins(jmeter_bin: str, plugin_dir: str) -> list[str]:
    """扫描 lib/ext + plugin_dir 下的 jar，返回已安装插件文件名（排序去重）。"""
    names: set[str] = set()
    home = resolve_jmeter_home(jmeter_bin)
    dirs: list[Path] = []
    if home is not None:
        dirs.append(home / "lib" / "ext")
    dirs.append(Path(plugin_dir))
    for d in dirs:
        if d.is_dir():
            for jar in d.glob("*.jar"):
                names.add(jar.name)
    return sorted(names)


async def ensure_plugins(required: list[dict], plugin_dir: str) -> list[str]:
    """按需下载缺失插件到 plugin_dir，返回需注入 search_paths 的 jar 绝对路径。

    required: [{"filename": "x.jar", "url": "<presigned GET>"}]
    已在 plugin_dir 的 jar 跳过下载（仍需加入 search_paths）；
    镜像内置（lib/ext）插件 Master 不会下发，因此不在这里处理。
    """
    target_dir = Path(plugin_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    search_paths: list[str] = []
    for item in required:
        filename = str(item.get("filename") or "")
        url = str(item.get("url") or "")
        if not filename.endswith(".jar") or not url:
            raise RuntimeError(f"插件清单非法: {item}")
        jar_path = target_dir / filename
        if not jar_path.exists():
            logger.info(f"下载插件 {filename}")
            await asyncio.to_thread(_download, url, jar_path)
        else:
            logger.info(f"插件已存在，跳过下载: {filename}")
        search_paths.append(str(jar_path))
    return search_paths


def _download(url: str, dest: Path) -> None:
    """流式下载到本地（阻塞实现，调用方用 to_thread 包装）。"""
    with httpx.Client(timeout=httpx.Timeout(600.0), follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 16):
                    f.write(chunk)
