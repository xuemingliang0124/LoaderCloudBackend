"""JMeter 插件管理：启动扫描 + sha256 计算 + 运行期 search_paths 注入。

两个插件来源：
- JMETER_HOME/lib/ext：镜像内置插件（JMeter 启动自动加载，无需 search_paths）
  sha256 不计算内置 jar（由镜像负责，Master 也不下发）
- plugin_dir（默认 work_dir/plugins）：Master 下发的第三方插件 jar 落在这里，
  运行 JMeter 时通过 -Jsearch_paths=<jar 路径> 注入类路径

JMeter NewDriver 的 search_paths 支持按 OS 路径分隔符分隔的文件/目录列表，
这里直接给 jar 文件绝对路径。
"""

import asyncio
import hashlib
import shutil
from pathlib import Path

import httpx


def resolve_jmeter_home(jmeter_bin: str) -> Path | None:
    """从 jmeter 可执行文件路径推导 JMETER_HOME（…/bin/jmeter 的上两级）。"""
    bin_path = shutil.which(jmeter_bin) or jmeter_bin
    p = Path(bin_path).expanduser().resolve()
    if p.parent.name.lower() == "bin":
        return p.parent.parent
    return p.parent if p.parent.name else None


def scan_plugins(jmeter_bin: str, plugin_dir: str) -> list[dict]:
    """扫描 lib/ext + plugin_dir 下的 jar，返回已安装插件清单（含 sha256）。

    lib/ext 内置插件：只列文件名，不算 sha256（sha256="" 表示由镜像负责）
    plugin_dir 下发插件：算 sha256 供 Master 比对内容指纹
    """
    names_seen: set[str] = set()
    out: list[dict] = []
    home = resolve_jmeter_home(jmeter_bin)
    dirs: list[Path] = []
    if home is not None:
        dirs.append(home / "lib" / "ext")
    dirs.append(Path(plugin_dir))
    for d in dirs:
        if not d.is_dir():
            continue
        for jar in d.glob("*.jar"):
            if jar.name in names_seen:
                continue
            names_seen.add(jar.name)
            # 仅 plugin_dir 内的 jar 算 sha256（与 Master 下发逻辑对齐）
            is_managed = d == Path(plugin_dir)
            sha = _sha256(jar) if is_managed else ""
            out.append(
                {
                    "name": jar.name,
                    "sha256": sha,
                    "size": jar.stat().st_size,
                    "managed": is_managed,
                }
            )
    return out


def scan_plugin_paths(plugin_dir: str) -> list[str]:
    """扫描 plugin_dir 下的 jar，返回供 -Jsearch_paths 注入的绝对路径列表。

    任务执行时调用：插件由 PluginSyncer 已对齐到位，本函数仅扫目录拼路径。
    """
    d = Path(plugin_dir)
    if not d.is_dir():
        return []
    return [str(jar.resolve()) for jar in d.glob("*.jar")]


def _sha256(path: Path) -> str:
    """计算文件 sha256（流式读，避免大 jar 占内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_sha256(path: Path) -> str:
    """_sha256 的公开别名，供 PluginSyncer 复用。"""
    return _sha256(path)


async def download_jar(url: str, dest: Path) -> None:
    """流式下载 jar 到 dest（异步包装的阻塞实现）。"""
    await asyncio.to_thread(_download, url, dest)


def _download(url: str, dest: Path) -> None:
    """流式下载到本地（阻塞实现，调用方用 to_thread 包装）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(600.0), follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 16):
                    f.write(chunk)
