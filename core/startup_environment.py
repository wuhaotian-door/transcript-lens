"""Windows 分发版的本地预检和可选组件补装。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from core import applog
from core.ai.runtime_setup import WINDOWS_RUNTIME_DIR_NAME
from core.crypto_store import default_root
from core.updater import DownloadCancelled, DownloadError, download_exe

PROBE_ARG = "--probe-local-ai-runtime"
MODEL_NAME = "Qwen2.5-0.5B-Instruct"
MODEL_BYTES = 491_400_032
_MODEL_URLS = (
    "https://modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/"
    "2e50b77b0eee3083842019e257b74854323d880a/qwen2.5-0.5b-instruct-q4_k_m.gguf",
    "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/"
    "9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf",
)
MODEL_SHA256 = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"

# 固定上游 Windows x64 CPU 构建。NumPy、Jinja2 和 typing_extensions 已随 EXE 分发。
_WHEELS = (
    (
        "llama.whl",
        "https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.36/"
        "llama_cpp_python-0.3.36-py3-none-win_amd64.whl",
        "ae5e88a2cf464e5a2dde7c8898c2d5c71cc24d03cac6396bf7509456bde0b9fb",
        "llama_cpp/",
    ),
    (
        "diskcache.whl",
        "https://files.pythonhosted.org/packages/3f/27/4570e78fc0bf5ea0ca45eb1de3818a23787af9b390c0b0a0033a1b8236f9/"
        "diskcache-5.6.3-py3-none-any.whl",
        "5e31b2d5fbad117cc363ebaf6b689474db18a1f6438bc82358b024abd4c2ca19",
        "diskcache/",
    ),
)


@dataclass(frozen=True)
class EnvironmentStatus:
    needs_engine: bool = False
    needs_model: bool = False


def supports_bundled_engine() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32" and sysconfig.get_platform() == "win-amd64"


def probe_process(directory: Path | None = None) -> int:
    """仅供子进程调用。原生库坏损时也不影响桌面主进程。"""
    from core.ai.inference import _import_engine
    from core.ai.runtime_setup import ensure_runtime_on_path

    try:
        ensure_runtime_on_path(directory)
        return 0 if callable(_import_engine().Llama) else 1
    except Exception:
        return 1


def probe_backend(directory: Path | None = None) -> bool:
    if getattr(sys, "frozen", False):
        argv = [sys.executable, PROBE_ARG]
        # 子进程沿用单文件程序已解包的运行时。
        env = {key: value for key, value in os.environ.items() if key != "PYINSTALLER_RESET_ENVIRONMENT"}
    else:
        argv = [sys.executable, "-E", str(Path(__file__).resolve().parents[1] / "main.py"), PROBE_ARG]
        env = None
    if directory is not None:
        argv.append(str(directory))
    try:
        return subprocess.run(
            argv, env=env, timeout=15, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _has_model() -> bool:
    from core.ai.model_registry import local_model_files

    for path in local_model_files().values():
        try:
            with path.open("rb") as stream:
                if stream.read(4) == b"GGUF":
                    return True
        except OSError:
            continue
    return False


def required_environment_problem(root: str | Path | None = None) -> str:
    """在启动时只检查成绩与账户功能所需的条件。"""
    root = Path(root) if root is not None else default_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=root):
            pass
    except OSError:
        return f"无法写入应用数据目录 {root}。请检查目录权限和磁盘空间后重新启动。"

    required = ("PySide6", "pandas", "openpyxl", "xlrd", "cryptography")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        return "应用内置组件缺失。请使用完整的安装向导覆盖安装，或重新获取单文件 EXE。"
    return ""


def _has_engine_files(root: Path) -> bool:
    return any(
        (root / directory / "llama_cpp/__init__.py").is_file()
        for directory in (WINDOWS_RUNTIME_DIR_NAME, "runtime/site")
    )


def initial_ai_setup() -> EnvironmentStatus | None:
    """在首次进入主界面后判断是否需要推荐可选组件。"""
    root = default_root()
    marker = root / "runtime/startup-ai-offered"
    if marker.exists():
        return None
    # 组件启动验证留给安装与实际推理，避免每次打开软件都启动子进程。
    status = EnvironmentStatus(needs_engine=not _has_engine_files(root), needs_model=not _has_model())
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=False)
    except FileExistsError:
        return None  # 同时打开多个窗口时只提示一次。
    except OSError as exc:
        applog.write("environment", f"未能保存 AI 引导状态：{type(exc).__name__}", root=root)
    return status if status.needs_engine or status.needs_model else None


@contextmanager
def _install_lock():
    """跨进程保护同一用户的安装目录，进程退出后由系统释放锁。"""
    import msvcrt

    root = default_root() / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "environment.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise DownloadError("另一个应用窗口正在安装组件，请稍后重试。") from exc
        try:
            yield root
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _cancel_if_requested(cancel) -> None:
    if cancel():
        raise DownloadCancelled("已取消安装。")


def _install_engine(root: Path, cancel) -> None:
    final = default_root() / WINDOWS_RUNTIME_DIR_NAME
    with tempfile.TemporaryDirectory(prefix="ai-setup-", dir=root) as temporary:
        work = Path(temporary)
        staged = work / "engine"
        staged.mkdir()
        for filename, url, digest, prefix in _WHEELS:
            _cancel_if_requested(cancel)
            wheel = download_exe(
                url, work / filename, cancel=cancel,
                expected_sha256=digest, min_bytes=1, timeout=15,
            )
            with ZipFile(wheel) as archive:
                # 只取已经通过 SHA-256 的轮子中的包目录，跳过重复的 bin 等附属文件。
                for member in archive.namelist():
                    if member.startswith(prefix) and ".." not in Path(member).parts:
                        archive.extract(member, staged)
        _cancel_if_requested(cancel)
        if not probe_backend(staged):
            raise DownloadError("推理核心下载完成，但本机未能加载。请重试或暂时跳过。")
        _cancel_if_requested(cancel)

        backup = work / "previous"
        if final.exists():
            final.rename(backup)
        try:
            staged.rename(final)
        except OSError:
            if backup.exists():
                backup.rename(final)
            raise


def _install_model(progress, cancel) -> None:
    from core.ai.model_downloader import stage_model

    last_percent = -1

    def model_progress(done: int, _total: int | None) -> None:
        nonlocal last_percent
        percent = min(99, done * 100 // MODEL_BYTES)
        if percent != last_percent:
            last_percent = percent
            message = "正在下载入门 AI 模型…" if done < MODEL_BYTES else "正在校验入门 AI 模型…"
            progress(message, percent)

    # 两处都是 Qwen 发布，同一文件哈希允许跨镜像续传。
    primary, fallback = _MODEL_URLS
    progress("正在下载入门 AI 模型…", 0)
    try:
        stage_model(primary, MODEL_NAME, expected_sha256=MODEL_SHA256, progress=model_progress, cancel=cancel)
    except DownloadError:
        _cancel_if_requested(cancel)
        progress("正在切换模型下载源…", -1)
        last_percent = -1
        stage_model(fallback, MODEL_NAME, expected_sha256=MODEL_SHA256, progress=model_progress, cancel=cancel)


def install_missing(status: EnvironmentStatus, *, progress, cancel) -> None:
    """安装已获用户授权的缺项。下载和验证均在调用方的后台线程执行。"""
    _cancel_if_requested(cancel)
    with _install_lock() as root:
        needs_engine = status.needs_engine and not (_has_engine_files(root.parent) and probe_backend())
        needs_model = status.needs_model and not _has_model()
        download_size = (35 * 1024 * 1024 if needs_engine else 0) + (
            MODEL_BYTES if needs_model else 0
        )
        if shutil.disk_usage(root).free < download_size:
            raise DownloadError("可用磁盘空间不足。请腾出空间后重试。")
        if needs_engine:
            progress("正在下载与验证推理核心…", -1)
            _install_engine(root, cancel)
        _cancel_if_requested(cancel)
        if needs_model:
            _install_model(progress, cancel)
