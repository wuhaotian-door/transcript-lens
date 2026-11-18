"""准备更新，退出后原位替换程序并独立重启。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from core.updater import verify_checksum
from core.version import CURRENT_VERSION, is_newer

__all__ = [
    "APPLY_LOG_NAME",
    "BACKUP_EXE_DIR_NAME",
    "BAT_NAME",
    "UPDATE_DIR_NAME",
    "UPDATE_JSON_NAME",
    "ApplyResult",
    "apply_pending_update",
    "backup_current_exe",
    "backup_exe_dir",
    "clear_pending_update",
    "current_exe",
    "is_frozen",
    "launch_apply",
    "prepare_update",
    "read_pending_update",
    "render_apply_bat",
    "update_dir",
    "update_json_path",
    "write_apply_bat",
    "write_pending_update",
]

#: 更新中转目录（任务书 2.2 指定的路径）
UPDATE_DIR_NAME = "SGMS_Update"

#: 交接文件（任务书 2.2 指定的名字）
UPDATE_JSON_NAME = "update.json"

#: 替换脚本
BAT_NAME = "apply_update.bat"

#: 替换脚本的日志（见模块开头"为什么要写日志"）
APPLY_LOG_NAME = "apply_update.log"

#: 旧 exe 的备份目录（charter v1.8 第六节：``%LOCALAPPDATA%/SGMS/backup_exe/``）
BACKUP_EXE_DIR_NAME = "backup_exe"


def is_frozen() -> bool:
    """本进程是不是打包出来的 exe。

    ★ 这个判断很重要：**源码模式下根本没有 exe 可以替换**。
      不判断的话，开发时点一下更新就会去"备份 python.exe"、"替换 main.py"，
      那是灾难。
    """
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path | None:
    """当前正在跑的 exe；源码模式返回 ``None``。"""
    if not is_frozen():
        return None
    try:
        return Path(sys.executable).resolve()
    except (OSError, ValueError):  # pragma: no cover - 极端环境
        return None


def update_dir() -> Path:
    """``%TEMP%/SGMS_Update/``（拿不到 TEMP 就退到系统临时目录）。"""
    base = os.environ.get("TEMP") or os.environ.get("TMP")
    root = Path(base) if base else Path(os.path.abspath(os.sep) ) / "Windows" / "Temp"
    return root / UPDATE_DIR_NAME


def update_json_path() -> Path:
    return update_dir() / UPDATE_JSON_NAME


def backup_exe_dir(root: str | Path | None = None) -> Path:
    """``%LOCALAPPDATA%/SGMS/backup_exe/``（跟着数据根目录走，便于测试）。"""
    if root is not None:
        return Path(root) / BACKUP_EXE_DIR_NAME
    from core.crypto_store import default_root

    return default_root() / BACKUP_EXE_DIR_NAME


def _unique_path(target: Path) -> Path:
    """目标已存在就加 ``-2`` / ``-3`` —— **绝不覆盖已有备份**。

    第一版直接覆盖，于是"1.7→1.8 更新了两次"之后只剩最后一份备份。
    备份的意义就是"出事能退回去"，覆盖掉它等于没有。
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 100):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"备份目录里同名文件太多了：{target}")  # pragma: no cover


def backup_current_exe(
    target: str | Path | None = None,
    *,
    old_version: str = CURRENT_VERSION,
    root: str | Path | None = None,
) -> Path:
    """把当前 exe 备份到 ``backup_exe/transcript-lens_v{old}_backup.exe``。

    Args:
        target: 要备份的 exe。``None`` = 当前进程的 exe（源码模式会报错）。
        old_version: 旧版本号（决定备份文件名）。
        root: 数据根目录（测试用）。

    Returns:
        备份文件的路径。

    Raises:
        RuntimeError: 源码模式（没有 exe 可备份）或源文件不存在。
        OSError: 复制失败 —— **调用方必须因此拒绝替换**（任务书 2.4）。
    """
    source = Path(target) if target is not None else current_exe()
    if source is None:
        raise RuntimeError(
            "当前是以源码方式运行的，没有 exe 可以备份 —— 不能执行自动替换。"
        )
    if not source.exists():
        raise RuntimeError(f"找不到当前程序文件：{source}")

    folder = backup_exe_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    destination = _unique_path(
        folder / f"{source.stem}_v{old_version}_backup{source.suffix or '.exe'}"
    )
    with NamedTemporaryFile(dir=folder, prefix=".backup-", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size == 0 or temporary.stat().st_size != source.stat().st_size:
            raise OSError(f"备份文件大小不一致：{destination}")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _is_staged(path: Path, target: Path) -> bool:
    return (
        path.parent == target.parent
        and path.name.startswith(f".{target.name}.update-")
        and path.suffix == ".tmp"
    )


def prepare_update(
    downloaded: str | Path,
    target_path: str | Path,
    version: str,
    *,
    sha256: str = "",
    from_version: str = CURRENT_VERSION,
    root: str | Path | None = None,
    json_path: str | Path | None = None,
    backup: bool = True,
) -> Path:
    """在目标旁暂存安装文件。备份完成后才允许启动替换脚本。"""
    source = Path(downloaded).resolve(strict=True)
    target = Path(target_path).resolve(strict=True)
    if source == target or not source.is_file() or not target.is_file():
        raise ValueError("更新文件或当前程序路径无效")
    with NamedTemporaryFile(
        dir=target.parent, prefix=f".{target.name}.update-", suffix=".tmp", delete=False,
    ) as handle:
        staged = Path(handle.name)
    try:
        shutil.copyfile(source, staged)
        if not sha256:
            with source.open("rb") as handle:
                sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        if not verify_checksum(staged, sha256):
            raise ValueError("安装文件校验失败，请重新下载")
        saved = backup_current_exe(target, old_version=from_version, root=root) if backup else None
        previous = read_pending_update(json_path)
        state = write_pending_update(
            staged, target, version, from_version=from_version, sha256=sha256,
            path=json_path, backup_exe=saved, prepared=True,
        )
    except Exception:
        staged.unlink(missing_ok=True)
        raise

    if previous:
        prior = Path(str(previous["pending_exe"]))
        if prior != source and _is_staged(prior, target):
            try:
                prior.unlink(missing_ok=True)
            except OSError:
                pass
    return state


def write_pending_update(
    pending_exe: str | Path,
    target_path: str | Path,
    version: str = CURRENT_VERSION,
    *,
    from_version: str = CURRENT_VERSION,
    sha256: str = "",
    path: str | Path | None = None,
    backup_exe: Path | None = None,
    prepared: bool = False,
) -> Path:
    """写 ``update.json``（任务书 2.2 的字段 + 三个附加字段）。

    ==================  ====================================================
    字段                说明
    ==================  ====================================================
    ``pending_exe``     新 exe 的路径（任务书指定）
    ``target_path``     要被替换掉的当前 exe（任务书指定）
    ``version``         新版本号（任务书指定）
    ``from_version``    旧版本号（**附加**：备份文件名和日志要它）
    ``sha256``          新 exe 的哈希（**附加**：下次启动时再验一次）
    ``created``         写入时间（**附加**：排查用）
    ==================  ====================================================

    ★ 先写 ``.tmp`` 再 ``replace``：写一半断电的话，留下的是一个
      ``.tmp``，而不是一个"读得出来但字段残缺"的 update.json ——
      后者会让下次启动拿着空路径去替换。
    """
    payload: dict[str, Any] = {
        "pending_exe": str(pending_exe),
        "target_path": str(target_path),
        "version": str(version),
        "from_version": str(from_version),
        "sha256": str(sha256 or ""),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    if prepared:
        payload["prepared"] = True
        payload["backup_exe"] = str(backup_exe) if backup_exe else ""
    destination = Path(path) if path is not None else update_json_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=".update-", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_pending_update(path: str | Path | None = None) -> dict[str, Any] | None:
    """读 ``update.json``。**读不出来/字段不全一律返回 None**，绝不抛。

    启动路径上不允许抛异常 —— 一个坏掉的 update.json 不该让程序起不来。
    """
    source = Path(path) if path is not None else update_json_path()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("pending_exe") or not data.get("target_path"):
        return None
    return data


def clear_pending_update(path: str | Path | None = None) -> None:
    """删掉 ``update.json``（**只删这一个**，不动 exe）。"""
    source = Path(path) if path is not None else update_json_path()
    try:
        source.unlink()
    except OSError:
        pass


def render_apply_bat(
    pending_exe: str | Path,
    target_path: str | Path,
    update_json: str | Path,
    log_path: str | Path | None = None,
) -> str:
    """生成纯 ASCII 的替换脚本。路径只从 UTF-16 命令行参数读取。"""
    if not pending_exe or not target_path or not update_json:
        raise ValueError("需要更新文件、当前程序、交接记录")
    lines = [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        'set "SRC=%~1"',
        'set "DST=%~2"',
        'set "JSN=%~3"',
        'set "LOG=%~4"',
        'if not exist "%SRC%" exit /b 2',
        'if not exist "%DST%" exit /b 3',
        'if defined LOG echo [%DATE% %TIME%] Waiting to replace program>> "%LOG%"',
        "for /L %%i in (1,1,60) do (",
        '    move /Y "%SRC%" "%DST%" >nul 2>&1',
        "    if not errorlevel 1 goto :installed",
        "    timeout /t 1 /nobreak >nul 2>&1",
        ")",
        "goto :giveup",
        ":installed",
        'del "%JSN%" >nul 2>&1',
        'if defined LOG echo [%DATE% %TIME%] Installed; starting new instance>> "%LOG%"',
        'start "" "%DST%"',
        "exit /b %errorlevel%",
        ":giveup",
        'if defined LOG echo [%DATE% %TIME%] Replacement failed; old program retained>> "%LOG%"',
        "exit /b 1",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_apply_bat(
    pending_exe: str | Path,
    target_path: str | Path,
    update_json: str | Path,
    *,
    bat_path: str | Path | None = None,
    log_path: str | Path | None = None,
) -> Path:
    """把替换脚本写到磁盘，返回它的路径。

    ★ 用 ``encoding="ascii", errors="replace"`` 写：正文本来就全是 ASCII，
      这一行是**断言**不是转换 —— 万一以后有人往正文里加了中文，
      宁可写成 ``?`` 也不要让 cmd 按 GBK 解出半个汉字然后吞掉后面一个引号
      （AGENTS.md 1b 记的就是这个坑）。
    """
    destination = (
        Path(bat_path) if bat_path is not None else update_dir() / BAT_NAME
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = render_apply_bat(pending_exe, target_path, update_json, log_path)
    destination.write_text(text, encoding="ascii", errors="replace", newline="")
    return destination


def launch_apply(
    bat_path: str | Path,
    pending_exe: str | Path,
    target_path: str | Path,
    update_json: str | Path,
    *,
    log_path: str | Path | None = None,
    popen: Any = None,
) -> int:
    """在隐藏控制台等待解锁，给重启的单文件应用创建独立环境。"""
    runner = subprocess.Popen if popen is None else popen
    env = {
        **os.environ,
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
        "TL_UPDATE_SCRIPT": str(bat_path),
        "TL_UPDATE_SOURCE": str(pending_exe),
        "TL_UPDATE_TARGET": str(target_path),
        "TL_UPDATE_STATE": str(update_json),
        "TL_UPDATE_LOG": str(log_path) if log_path else "",
    }
    # cmd 只展开固定的变量名。文件名中的 %、& 和空格保留为参数内容。
    cmd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    command = (
        f'"{cmd}" /d /s /v:off /c ""%TL_UPDATE_SCRIPT%" "%TL_UPDATE_SOURCE%" '
        '"%TL_UPDATE_TARGET%" "%TL_UPDATE_STATE%" "%TL_UPDATE_LOG%""'
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = runner(
        command, executable=str(cmd), cwd=str(Path(target_path).parent), env=env,
        startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return int(getattr(process, "pid", 0) or 0)


def _append_log(message: str, log_path: Path | None) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


@dataclass(frozen=True)
class ApplyResult:
    """启动时处理"待重启的更新"的结论。

    ``handed_off=True`` 表示**已经生成 bat 并准备退出**了 —— 调用方
    必须紧接着 ``return 0``，不能再往下走启动流程（否则新旧两个版本
    会同时跑）。
    """

    handed_off: bool
    detail: str
    pending: dict[str, Any] | None = None


def apply_pending_update(
    *,
    root: str | Path | None = None,
    frozen: bool | None = None,
    json_path: str | Path | None = None,
    bat_path: str | Path | None = None,
    log_path: str | Path | None = None,
    popen: Any = None,
    backup: bool = True,
) -> ApplyResult:
    """在当前程序退出前交接替换。接受旧版「稍后」留下的交接记录。"""
    pending = read_pending_update(json_path)
    if pending is None:
        return ApplyResult(False, "")
    running_frozen = is_frozen() if frozen is None else bool(frozen)
    if not running_frozen:
        return ApplyResult(False, "源码运行时会跳过程序替换。", pending)

    resolved_log = Path(log_path) if log_path is not None else update_dir() / APPLY_LOG_NAME
    try:
        target = Path(str(pending["target_path"])).resolve()
        running_exe = current_exe()
        if running_exe is not None and running_exe != target:
            return ApplyResult(False, "待安装的更新属于另一个程序路径。", pending)
        source = Path(str(pending["pending_exe"])).resolve()
        version = str(pending.get("version") or "")
        if not is_newer(version, CURRENT_VERSION):
            clear_pending_update(json_path)
            return ApplyResult(False, "", pending)
        if not source.is_file():
            clear_pending_update(json_path)
            return ApplyResult(False, "待安装的文件已被移除。", pending)

        checksum = str(pending.get("sha256") or "")
        if pending.get("prepared"):
            if not _is_staged(source, target):
                raise ValueError("安装文件的位置不正确")
            saved = str(pending.get("backup_exe") or "")
            if backup and (not saved or not Path(saved).is_file()):
                raise ValueError("找不到旧程序备份，请重新准备更新")
            if not verify_checksum(source, checksum):
                raise ValueError("安装文件校验失败，请重新下载")
        else:
            state = prepare_update(
                source, target, version, sha256=checksum,
                from_version=str(pending.get("from_version") or CURRENT_VERSION),
                root=root, json_path=json_path, backup=backup,
            )
            prepared = read_pending_update(state)
            if prepared is None:
                raise OSError("无法读回更新交接记录")
            source = Path(prepared["pending_exe"])

        state_path = json_path or update_json_path()
        script = write_apply_bat(source, target, state_path, bat_path=bat_path, log_path=resolved_log)
        launch_apply(script, source, target, state_path, log_path=resolved_log, popen=popen)
    except Exception as exc:  # 启动时更新失败仍可进入当前程序。
        _append_log(f"准备或启动安装失败：{exc}", resolved_log)
        return ApplyResult(False, f"未能开始安装更新：{exc}", pending)

    _append_log(f"已交接给替换脚本，等待程序退出（目标 v{version}）", resolved_log)
    return ApplyResult(True, f"正在安装 v{version}，程序即将重启。", pending)
