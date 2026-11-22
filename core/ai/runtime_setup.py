"""推理后端的一键安装支持（v1.9.2）。

本模块给界面层提供四件事：

* 定位应用私有的安装目录（%LOCALAPPDATA%\\SGMS\\runtime\\site）；
* 判断推理引擎 llama_cpp 能不能 import；
* 找本机的系统 Python（打包版的 sys.executable 是 exe 自己，里面没有 pip）；
* 在后台线程里跑 pip install，把输出逐行交给界面。

设计约束：

* 只依赖标准库：不依赖 PySide6，也不 import 任何 HTTP 库。pip 是子进程，
  不是本程序的网络请求，所以 main._network_policy_report 那条零 HTTP import
  的闸门不需要放宽。
* 绝不静默安装：安装只在界面层被显式调用时才执行。
* 异常绝不冒出去：所有失败都转成 InstallResult，由界面层显示中文说明。
"""

from __future__ import annotations

import glob
import importlib.util
import logging
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional

from core.ai.inference import ENGINE_MODULE
from core.crypto_store import default_root

logger = logging.getLogger(__name__)

__all__ = [
    "BACKEND_READY_MESSAGE",
    "BackendInstallThread",
    "DEFAULT_PYTHON_COMMAND",
    "ENGINE_MODULE",
    "INSTALL_CANCELLED_MESSAGE",
    "INSTALL_OK_MESSAGE",
    "INSTALL_TIMEOUT",
    "InstallResult",
    "MISSING_PYTHON_MESSAGE",
    "PIP_EXTRA_INDEX",
    "PIP_PACKAGE",
    "PYTHON_PROBE_TIMEOUT",
    "RUNTIME_DIR_NAME",
    "backend_status",
    "ensure_runtime_on_path",
    "find_system_python",
    "install",
    "install_command_text",
    "is_backend_available",
    "pip_argv",
    "runtime_dir",
]

# 安装目录名，相对 core.crypto_store.default_root()。
RUNTIME_DIR_NAME = "runtime/site"
WINDOWS_RUNTIME_DIR_NAME = "runtime/windows-cpu"

# 官方包名：pip install llama-cpp-python。
PIP_PACKAGE = "llama-cpp-python"

# llama-cpp-python 项目发布的 CPU 预编译包。
PIP_EXTRA_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
PIP_INDEX = "https://pypi.org/simple"

# pip install 的总超时（秒）。llama-cpp-python 需要编译，慢机器上可能要几分钟。
INSTALL_TIMEOUT = 600

# 探测一个 Python 解释器是否可用（import pip）的超时（秒）。
PYTHON_PROBE_TIMEOUT = 20

# 一个候选都找不到时，命令文本里用的占位解释器名。
DEFAULT_PYTHON_COMMAND = "python"

# ``InstallResult.output`` 最多保留的尾部字符数（给"复制报错"用）。
OUTPUT_TAIL_MAX = 8000

INSTALL_OK_MESSAGE = "推理后端安装成功"
INSTALL_CANCELLED_MESSAGE = "已取消安装"
BACKEND_READY_MESSAGE = "推理后端已就绪"
MISSING_PYTHON_MESSAGE = (
    f"没有找到可用于安装的 Python 解释器。请先安装 Python "
    f"{sys.version_info.major}.{sys.version_info.minor}（{struct.calcsize('P') * 8} 位，含 pip），"
    "再重试或在 PowerShell 执行下方命令。"
)

# Windows 上绝不弹出黑框；其它平台为 0。
_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# 安装目录与导入路径
# ---------------------------------------------------------------------------


def runtime_dir() -> Path:
    """推理后端的安装目录：<默认数据根>/runtime/site。

    只计算路径，不创建目录；目录是 pip install --target 建出来的。
    """
    return Path(default_root()) / RUNTIME_DIR_NAME


def _path_key(raw) -> str:
    """把 sys.path 里的一个条目归一成可比较的键（大小写不敏感）。"""
    try:
        text = os.fspath(raw)
    except TypeError:
        return ""
    if not isinstance(text, str):
        text = os.fsdecode(text)
    return os.path.normcase(os.path.abspath(text))


def _path_listed(target: str, entries: Iterable[str]) -> bool:
    """判断 target 是否已经在 entries 里（不改变顺序）。"""
    key = os.path.normcase(os.path.abspath(target))
    return any(_path_key(entry) == key for entry in entries)


def ensure_runtime_on_path(directory=None) -> bool:
    """补充运行时目录，优先复用程序自带的 NumPy 等依赖。

    * 目录不存在：返回 False，sys.path 一个字节都不动；
    * 已经在 sys.path 里：返回 True，绝不改变已有条目的顺序；
    * 否则追加路径并返回 True。
    """
    if directory is None:
        managed = Path(default_root()) / WINDOWS_RUNTIME_DIR_NAME
        if (managed / ENGINE_MODULE).is_dir() and ensure_runtime_on_path(managed):
            return True
    target = Path(directory) if directory is not None else runtime_dir()
    try:
        if not target.is_dir():
            return False
        absolute = os.path.abspath(os.fspath(target))
    except OSError:
        return False
    if _path_listed(absolute, sys.path):
        return True
    sys.path.append(absolute)
    return True


def is_backend_available() -> bool:
    """推理引擎 llama_cpp 现在能不能 import。只探测，不 import 引擎本体。"""
    ensure_runtime_on_path()
    # 已经成功 import 过就不必再查 find_spec（find_spec 不会看已加载模块的子模块）。
    if sys.modules.get(ENGINE_MODULE) is not None:
        return True
    try:
        return importlib.util.find_spec(ENGINE_MODULE) is not None
    except (ImportError, AttributeError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 找本机的系统 Python
# ---------------------------------------------------------------------------


def _iter_system_drive_roots() -> Iterator[Path]:
    """依次给出可能需要扫描的盘符根目录（C 盘优先）。"""
    yield Path("C:/")
    system_drive = os.environ.get("SystemDrive", "").strip()
    if system_drive:
        candidate = Path(f"{system_drive}/")
        if os.path.normcase(str(candidate)) != os.path.normcase("C:/"):
            yield candidate


def _sorted_glob(pattern: str) -> List[str]:
    """glob 一次并按路径排序；任何 OSError 都当成没匹配到。"""
    try:
        matches = [str(item) for item in glob.glob(pattern) if Path(item).is_file()]
    except OSError:
        return []
    return sorted(matches)


def _iter_candidate_pythons() -> Iterator[str]:
    """按优先级产出候选解释器路径（只产出，不探测）。

    顺序：SGMS_PYTHON 环境变量 -> PATH 里的 python/python3 -> 用户级安装
    -> C 盘根目录安装 -> anaconda3/miniconda3。同一个路径只产出一次。
    """
    seen = set()

    def remember(raw) -> Optional[str]:
        if not raw:
            return None
        try:
            text = os.fspath(raw)
        except TypeError:
            return None
        if not isinstance(text, str):
            text = os.fsdecode(text)
        text = text.strip().strip('"').strip("'")
        if not text:
            return None
        try:
            if not Path(text).is_file():
                return None
        except OSError:
            return None
        key = os.path.normcase(os.path.abspath(text))
        if key in seen:
            return None
        seen.add(key)
        return text

    env_python = remember(os.environ.get("SGMS_PYTHON", ""))
    if env_python:
        yield env_python

    for command in ("python", "python3"):
        found = remember(shutil.which(command))
        if found:
            yield found

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        pattern = str(Path(local_appdata) / "Programs" / "Python" / "Python3*" / "python.exe")
        for candidate in _sorted_glob(pattern):
            found = remember(candidate)
            if found:
                yield found
        managed = Path(local_appdata) / "Python" / "pythoncore-*"
        for candidate in _sorted_glob(str(managed / "python.exe")):
            found = remember(candidate)
            if found:
                yield found

    for root in _iter_system_drive_roots():
        for candidate in _sorted_glob(str(root / "Python3*" / "python.exe")):
            found = remember(candidate)
            if found:
                yield found

    anaconda_parents: List[Path] = [Path.home()]
    if local_appdata:
        anaconda_parents.append(Path(local_appdata))
    anaconda_parents.extend(_iter_system_drive_roots())
    for parent in anaconda_parents:
        for name in ("anaconda3", "miniconda3"):
            found = remember(parent / name / "python.exe")
            if found:
                yield found


def _stop_process_quietly(process) -> None:
    """终止子进程；terminate 不行就 kill。任何异常都吞掉。"""
    for action in (process.terminate, process.kill):
        try:
            action()
        except Exception:  # noqa: BLE001 - 停止失败不能影响安装结果
            logger.debug("无法停止安装子进程", exc_info=True)


def _probe_python(path: str, timeout: float = PYTHON_PROBE_TIMEOUT) -> bool:
    """只选与运行程序的 Python 版本、平台及架构一致且带 pip 的解释器。"""
    signature = (sys.implementation.cache_tag, sysconfig.get_platform(), struct.calcsize("P"))
    script = (
        "import struct, sys, sysconfig; "
        "signature = (sys.implementation.cache_tag, sysconfig.get_platform(), struct.calcsize('P')); "
        f"sys.exit(1) if signature != {signature!r} else None; import pip"
    )
    argv = [path, "-I", "-c", script]
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, ValueError):
        return False
    try:
        return process.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        _stop_process_quietly(process)
        return False
    except (OSError, ValueError):
        return False


def find_system_python() -> Optional[str]:
    """找一个装了 pip 的系统 Python；找不到返回 None。

    候选顺序见 _iter_candidate_pythons。打包版里 sys.executable 是 exe 自己，
    里面没有 pip，所以这里只认真实存在的 python.exe。
    """
    for candidate in _iter_candidate_pythons():
        if _probe_python(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# pip 命令
# ---------------------------------------------------------------------------


def pip_argv(python=None, *, extra_index: bool = True) -> List[str]:
    """优先使用匹配的解释器，回退到指定版本的 Windows Python 启动器。"""
    return _pip_argv(python or find_system_python(), extra_index=extra_index)


def _pip_argv(python, *, extra_index: bool = True) -> List[str]:
    executable = [os.fspath(python)] if python else (
        ["py", f"-{sys.version_info.major}.{sys.version_info.minor}"] if _IS_WINDOWS
        else [f"python{sys.version_info.major}.{sys.version_info.minor}"]
    )
    argv = [
        *executable,
        "-I",
        "-m",
        "pip",
        "--isolated",
        "install",
        "--only-binary=:all:",
        "--upgrade",
        "--disable-pip-version-check",
        "--no-input",
        "--progress-bar",
        "off",
        "--retries",
        "2",
        "--timeout",
        "15",
        "--index-url",
        PIP_INDEX,
        "--target",
        str(runtime_dir()),
    ]
    if extra_index:
        argv.extend(("--extra-index-url", PIP_EXTRA_INDEX))
    argv.append(PIP_PACKAGE)
    return argv


def install_command_text(python=None, *, detect_python: bool = True) -> str:
    """返回在 PowerShell 可直接执行的完整命令。"""
    argv = pip_argv(python) if detect_python else _pip_argv(python)
    if _IS_WINDOWS:
        return "& " + " ".join("'" + item.replace("'", "''") + "'" for item in argv)
    from shlex import join

    return join(argv)


def _check_installed_engine(python: str) -> bool:
    """在兼容的 Python 子进程里同时验证 Python 模块和原生 DLL。"""
    try:
        result = subprocess.run(
            [python, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from llama_cpp import Llama",
             str(runtime_dir())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW, timeout=PYTHON_PROBE_TIMEOUT, check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# 后台安装线程
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    """一次安装的结果。失败信息永远是中文说明，不抛异常。

    字段同时满足两套契约（v1.9.2 收敛，见总纲 §26.6）：

    * 界面（``ui.widgets.backend_setup``）读 ``ok`` / ``message`` / ``lines`` / ``command``；
    * 数据层与脚本读 ``ok`` / ``cancelled`` / ``reason`` / ``output`` / ``command``
      —— ``reason`` 是 ``message`` 的别名，``output`` 是 ``lines`` 拼起来的尾部输出。
    """

    ok: bool
    message: str
    lines: List[str] = field(default_factory=list)
    command: str = ""
    #: 只有"用户点了取消"才是 True（超时/失败都不算取消）。
    cancelled: bool = False

    @property
    def reason(self) -> str:
        """中文人话原因（``message`` 的别名，不写空话、不带 traceback）。"""
        return self.message

    @property
    def output(self) -> str:
        """累计输出（stdout+stderr 合并）的尾部，最多 :data:`OUTPUT_TAIL_MAX` 字符。"""
        return "\n".join(self.lines)[-OUTPUT_TAIL_MAX:]


def _tail_lines(lines: Iterable[str], count: int = 3) -> List[str]:
    """取最后几行非空输出；一行都没有时给一句人话。"""
    tail = [line.strip() for line in lines]
    tail = [line for line in tail if line][-count:]
    return tail or ["（命令没有输出任何内容）"]


class BackendInstallThread(threading.Thread):
    """在后台线程里执行 pip install，把输出逐行回调给界面。

    Qt 界面不要直接开 subprocess：pip 装 llama-cpp-python 可能要几分钟，
    在主线程里跑会整窗卡死。由界面层创建本线程，用 QTimer 轮询 is_alive()
    与 result，再用 QTimer.singleShot(0, ...) 回主线程更新控件。

    用法::

        thread = BackendInstallThread(on_line=show_line)
        thread.start()
        ...
        thread.cancel()   # 用户点了取消

    结果永远落在 ``result`` 上（InstallResult），不会抛异常。
    """

    def __init__(
        self,
        *,
        on_line: Optional[Callable[[str], None]] = None,
        python: Optional[str] = None,
        timeout: float = INSTALL_TIMEOUT,
    ) -> None:
        super().__init__(name="sgms-backend-install", daemon=True)
        self._on_line = on_line
        self._python = str(python).strip() if python else None
        self._timeout = max(0.01, float(timeout))
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._timed_out = False
        self._finished = False
        self._process = None
        self._lines: List[str] = []
        #: 线程结束后一定是一个 InstallResult
        self.result: Optional[InstallResult] = None

    # -- 供界面调用 ---------------------------------------------------------

    def cancel(self) -> None:
        """请求取消：先置位，再 terminate，最后 kill 兜底。"""
        with self._lock:
            self._cancel_event.set()
        self._stop_process(force=True)

    # -- 内部工具 -----------------------------------------------------------

    def _popen_kwargs(self) -> dict:
        """子进程参数：合并 stdout/stderr，文本模式，绝不弹黑框。"""
        return {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "creationflags": _CREATE_NO_WINDOW,
        }

    def _record_line(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def _handle_line(self, raw) -> None:
        """收一行输出：存起来，再回调界面（回调异常绝不能打断安装）。"""
        line = str(raw).rstrip("\r\n")
        self._record_line(line)
        if self._on_line is None:
            return
        try:
            self._on_line(line)
        except Exception:  # noqa: BLE001 - 界面的回调出错不该毁掉安装
            logger.debug("安装输出回调异常", exc_info=True)

    def _collect_output(self, process) -> None:
        stream = getattr(process, "stdout", None)
        if stream is None:
            return
        try:
            for raw in stream:
                self._handle_line(raw)
        except (OSError, ValueError):
            self._record_line("（读取安装输出时出错，安装仍在继续）")

    def _stop_process(self, *, force: bool) -> None:
        with self._lock:
            process = self._process
        if process is None:
            return
        actions = (process.terminate, process.kill) if force else (process.terminate,)
        for action in actions:
            try:
                action()
            except Exception:  # noqa: BLE001
                logger.debug("无法停止安装子进程", exc_info=True)

    def _on_timeout(self) -> None:
        with self._lock:
            if self._finished or self._timed_out:
                return
            process = self._process
        if process is None:
            return
        try:
            if process.poll() is not None:
                return
        except Exception:  # noqa: BLE001 - 读不到状态就按超时处理
            logger.debug("读取子进程状态失败", exc_info=True)
        with self._lock:
            if self._finished:
                return
            self._timed_out = True
        self._stop_process(force=True)

    def _snapshot_lines(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def _safe_command(self) -> str:
        """异常兜底时也要能给出可复制的命令。"""
        try:
            return install_command_text(self._python, detect_python=False)
        except Exception:  # noqa: BLE001
            return ""

    # -- 线程主体 -----------------------------------------------------------

    def run(self) -> None:
        try:
            self.result = self._install()
        except Exception:  # noqa: BLE001 - 线程里绝不许冒出异常
            logger.exception("推理后端安装线程异常退出")
            self.result = InstallResult(
                ok=False,
                message="安装过程中出现内部错误，已终止安装。可以复制下面的命令手动安装。",
                lines=self._snapshot_lines(),
                command=self._safe_command(),
            )

    def _install(self) -> InstallResult:
        if getattr(sys, "frozen", False) and self._python is None:
            from core.startup_environment import EnvironmentStatus, install_missing, supports_bundled_engine
            from core.updater import DownloadCancelled, DownloadError

            if supports_bundled_engine():
                try:
                    install_missing(
                        EnvironmentStatus(needs_engine=True),
                        progress=lambda message, _percent: self._handle_line(message),
                        cancel=self._cancel_event.is_set,
                    )
                except DownloadCancelled:
                    return InstallResult(ok=False, message=INSTALL_CANCELLED_MESSAGE, cancelled=True)
                except (DownloadError, OSError) as exc:
                    return InstallResult(ok=False, message=str(exc), lines=self._snapshot_lines())
                ensure_runtime_on_path()
                return InstallResult(ok=True, message=INSTALL_OK_MESSAGE, lines=self._snapshot_lines())

        self._handle_line("正在查找兼容的 Python…")
        python = self._python
        if not python:
            found = find_system_python()
            if not found:
                return InstallResult(
                    ok=False,
                    message=MISSING_PYTHON_MESSAGE,
                    lines=[],
                    command=install_command_text(detect_python=False),
                )
            python = found
            self._python = python

        argv = pip_argv(python)
        command = install_command_text(python)
        if self._cancel_event.is_set():
            return InstallResult(ok=False, message=INSTALL_CANCELLED_MESSAGE, cancelled=True, command=command)
        self._handle_line("正在准备下载官方预编译包…")

        try:
            process = subprocess.Popen(argv, **self._popen_kwargs())
        except (OSError, ValueError) as exc:
            return InstallResult(
                ok=False,
                message=f"启动安装程序失败：{exc}。请确认这个 Python 能正常运行。",
                lines=[],
                command=command,
            )

        with self._lock:
            self._process = process
        if self._cancel_event.is_set():
            self._stop_process(force=True)

        timer = threading.Timer(self._timeout, self._on_timeout)
        timer.daemon = True
        timer.start()
        try:
            self._collect_output(process)
            process.wait()
        finally:
            timer.cancel()

        with self._lock:
            self._finished = True
            self._process = None
        returncode = getattr(process, "returncode", None)
        cancelled = self._cancel_event.is_set()
        timed_out = self._timed_out
        lines = self._snapshot_lines()

        if cancelled:
            return InstallResult(
                ok=False,
                message=INSTALL_CANCELLED_MESSAGE,
                lines=lines,
                command=command,
            )
        if timed_out:
            return InstallResult(
                ok=False,
                message=(
                    f"安装超时（超过 {int(self._timeout)} 秒没有完成），已停止安装。"
                    "可以复制下面的命令手动安装。"
                ),
                lines=lines,
                command=command,
            )
        if returncode == 0:
            self._handle_line("正在验证推理核心…")
            if not _check_installed_engine(python):
                return InstallResult(
                    ok=False, message="推理核心已下载，但加载验证失败。请查看安装输出或复制命令排查。",
                    lines=self._snapshot_lines(), command=command,
                )
            ensure_runtime_on_path()
            return InstallResult(
                ok=True,
                message=INSTALL_OK_MESSAGE,
                lines=self._snapshot_lines(),
                command=command,
            )
        tail = "\n".join(_tail_lines(lines))
        return InstallResult(
            ok=False,
            message=(
                f"推理后端安装失败（返回码 {returncode}）。pip 输出的最后几行如下：\n"
                f"{tail}\n"
                "可以复制下面的命令手动安装。"
            ),
            lines=lines,
            command=command,
        )


# ---------------------------------------------------------------------------
# 脚本 / 测试用的阻塞入口
# ---------------------------------------------------------------------------


def install(*, on_line: Optional[Callable[[str], None]] = None,
            cancel: Optional[threading.Event] = None,
            python: Optional[str] = None,
            timeout: float = INSTALL_TIMEOUT) -> InstallResult:
    """**阻塞**执行一次 pip 安装（界面请直接用 :class:`BackendInstallThread`）。

    Args:
        on_line: 每读到一行 pip 输出就回调一次；回调抛异常只记日志，不打断安装。
        cancel: ``threading.Event``；``set()`` 后终止子进程（terminate + kill 兜底）。
        python: 指定解释器路径；``None``/空串时走 :func:`find_system_python`。
        timeout: 总超时秒数，默认 :data:`INSTALL_TIMEOUT`。

    Returns:
        :class:`InstallResult`：

        * 成功：``ok=True``（返回码 0 **且** 复核 ``is_backend_available()`` 通过）；
        * 用户取消：``ok=False``、``cancelled=True``、``reason="已取消安装"``；
        * 找不到系统 Python：``ok=False``，``reason`` 以
          "没有找到可用于安装的 Python 解释器" 开头，``command`` 给可复制命令；
        * 返回码非 0：``ok=False``，``reason`` 里带**完整安装命令**；
        * 返回码 0 但复核不过：``ok=False``，
          ``reason`` 以 "安装命令执行完了，但导入 llama-cpp-python 仍然失败" 开头。

    ★ **绝不抛异常**（内部异常也只落在返回值上），绝不 import ``llama_cpp``，
      绝不联网 —— 联网的是 pip 子进程。
    """
    thread = BackendInstallThread(on_line=on_line, python=python, timeout=timeout)
    thread.start()
    user_cancelled = False
    if cancel is not None:
        while thread.is_alive():
            if cancel.wait(0.05):
                user_cancelled = True
                thread.cancel()
                break
    thread.join()
    result = thread.result or InstallResult(
        ok=False,
        message="安装线程没有返回结果，请重试或复制下面的命令手动安装。",
        lines=[],
        command=install_command_text(python),
    )
    if user_cancelled:
        result.ok = False
        result.cancelled = True
        result.message = INSTALL_CANCELLED_MESSAGE
        return result
    if result.ok and not is_backend_available():
        return InstallResult(
            ok=False,
            message=(
                "安装命令执行完了，但导入 llama-cpp-python 仍然失败。"
                "可以复制下面的命令手动排查。"
            ),
            lines=result.lines,
            command=result.command,
        )
    if not result.ok and result.command and result.command not in result.message:
        result.message = f"{result.message}\n安装命令：{result.command}"
    return result


# ---------------------------------------------------------------------------
# 界面用的状态说明
# ---------------------------------------------------------------------------


def backend_status(*, probe_python: bool = True) -> tuple[bool, str]:
    """返回 (后端是否可用, 中文说明)，直接给界面显示，永远是纯文本。"""
    if is_backend_available():
        return True, BACKEND_READY_MESSAGE
    target = runtime_dir()
    try:
        installed = target.is_dir()
    except OSError:
        installed = False
    if installed:
        first = f"程序目录里有一个没装完的推理后端（{target}），它现在还不能用，可以重新安装。"
    else:
        first = "程序目录里还没有安装推理后端。"
    if not probe_python:
        return False, first
    python = find_system_python()
    if python:
        tail = "本机找到了可用的系统 Python，可以一键安装。"
    else:
        tail = "本机没有找到可用的系统 Python，请复制下面的命令在命令行里执行。"
    return False, first + tail
