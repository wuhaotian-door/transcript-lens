"""运行期日志：把"界面卡住 / 静默异常"变成能查的文本。

**为什么需要它**：打包成 ``--windowed`` 的 exe 之后没有控制台，
Python 的 traceback 写到 stderr —— 用户看不到、我们更看不到。
v1.9.2 之后连着出了两次"点了登录不动"的现场，都因为没有日志而只能猜。

所以这里做三件小事（都很便宜，不联网、不引依赖）：

1. :func:`write` —— 关键步骤各写一行（谁、什么时候、走到哪一步）。
2. :func:`install_excepthook` —— 未捕获的异常落盘**再**交给默认钩子。
3. :func:`install_faulthandler` —— 真卡死时用 ``faulthandler`` 把线程栈 dump 出来。

日志写 ``<root>/logs/``，默认 ``%LOCALAPPDATA%\\SGMS\\logs\\``。
**只写字节数很小的文本**：一行几十字节，超过 :data:`MAX_BYTES` 就滚一次。
"""

from __future__ import annotations

import faulthandler
import os
import sys
import time
import traceback
from pathlib import Path
from typing import IO

#: 单个日志文件的上限。超过就改名成 ``.1``（只留一代，够查最近一次现场）
MAX_BYTES = 512 * 1024

#: 目录名（相对数据根）
LOG_DIR_NAME = "logs"

#: 主日志文件名
LOG_FILE_NAME = "app.log"

_fault: IO[bytes] | None = None


def log_dir(root: str | Path | None = None) -> Path:
    """日志目录。``root`` 为 None 时用默认数据根（不 import crypto_store，避免环）。"""
    if root is None:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        root = Path(base) / "SGMS"
    return Path(root) / LOG_DIR_NAME


def log_path(root: str | Path | None = None) -> Path:
    return log_dir(root) / LOG_FILE_NAME


def write(tag: str, message: str = "", *, root: str | Path | None = None) -> None:
    """写一行日志。**任何失败都吞掉** —— 日志写不进去不能反过来炸程序。"""
    try:
        path = log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        _roll(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} [{tag}] {message}".rstrip()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    except Exception:  # noqa: BLE001 - 日志永远不能抛
        pass


def _roll(path: Path) -> None:
    """超过上限就把旧文件挪成 ``.1``（不删，留给下一次核对）。"""
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            os.replace(path, backup)
    except OSError:
        pass


def install_excepthook(root: str | Path | None = None) -> None:
    """未捕获异常 → 写日志 + 照常交给原来的钩子（开发时照样能看到栈）。"""
    previous = sys.excepthook

    def _hook(exc_type, exc, tb) -> None:
        try:
            detail = "".join(traceback.format_exception(exc_type, exc, tb))
            write("crash", detail.replace("\n", "\n    "), root=root)
        except Exception:  # noqa: BLE001
            pass
        previous(exc_type, exc, tb)

    sys.excepthook = _hook


def install_faulthandler(root: str | Path | None = None) -> bool:
    """把线程栈 dump 到 ``logs/fault.log``。卡死时按需调用 :func:`dump_stack`。"""
    global _fault
    try:
        path = log_dir(root) / "fault.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        _fault = path.open("a", encoding="utf-8", errors="replace")
        faulthandler.enable(file=_fault, all_threads=True)
        return True
    except Exception:  # noqa: BLE001 - 某些环境拿不到 stderr 句柄
        return False


def dump_stack(note: str = "") -> None:
    """立刻把当前线程栈写进 fault.log（用于"疑似卡住"的兜底）。"""
    try:
        if _fault is not None:
            _fault.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {note}\n")
            _fault.flush()
        if faulthandler.is_enabled():
            faulthandler.dump_traceback()
    except Exception:  # noqa: BLE001
        pass


def tail(lines: int = 40, root: str | Path | None = None) -> str:
    """读最后几行（自检/错误弹窗里给用户看）。读不到就返回空串。"""
    try:
        path = log_path(root)
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except OSError:
        return ""
