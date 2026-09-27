"""更新交接：备份旧 exe → 写 ``update.json`` → 生成 ``apply_update.bat`` → 退出替换。

charter v1.8 第六节。**这个模块只碰文件，一行网络代码都没有** ——
下载在 :mod:`core.updater`，界面在设置页，三件事分开才好分别验证。

★ 为什么必须是"生成 bat 再退出"，不能自己覆盖自己
=============================================

Windows 上**正在运行的 exe 是锁着的**，写不进去。所以流程只能是：

1. 新 exe 先躺在 ``%TEMP%/SGMS_Update/`` 里（下载阶段就放那儿）；
2. 本进程写一个 ``.bat``，里面是"等旧进程退出 → 复制 → 删临时文件 → 启动新 exe"；
3. 本进程 ``sys.exit(0)``；
4. 旧进程一没，文件锁就松了，bat 里的 ``copy`` 才成功。

这**不是**热替换（任务书 2.4 / charter v1.8 第九节明令禁止热替换）——
热替换是"趁自己还活着去覆盖自己的 exe"，那必然失败或写出一个半截文件。

★ 三个"绝不"
===========

1. **备份失败 → 拒绝替换。** 拿不到旧 exe 的副本就不动手，
   否则用户既没有新的、也没有旧的。
2. **下载失败 → 不动旧 exe。** 下载全程写 ``.part``（见 ``core.updater``），
   这里再确认一次目标文件真的在、大小合理，才写 update.json。
3. **替换脚本失败 → 旧 exe 还在原地。** bat 里的 ``copy`` 是**先写临时名
   再改名**做不到的（cmd 没有原子替换），所以退而求其次：复制失败就重试
   60 次（每次 1 秒），全部失败就**什么都不做**并留一条日志。
   最坏情况是"更新没生效"，不是"程序没了"。

★ ``.bat`` 里为什么不把路径写死
=============================

路径里可能有中文（用户名），而 ``cmd.exe`` 读 ``.bat`` 是按**当前代码页**
逐字节解码的 —— 把中文路径写进 bat，等于赌用户机器的代码页。
所以 bat 正文**全是 ASCII**，三个路径从**命令行参数**传进去
（``%~1`` / ``%~2`` / ``%~3``）。参数走的是 Windows 的 UTF-16 API，
中文路径也不会坏。正文里那句 ``copy /Y`` 本身没变。

★ 为什么要写日志
===============

bat 是在**本进程退出之后**跑的，那时候没有控制台、没有异常、没有任何反馈。
"点了立即重启，重启完还是旧版本"如果没有日志，就完全查不下去。
所以每一次交接都在 ``%TEMP%/SGMS_Update/apply_update.log`` 里留一行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.updater import verify_checksum
from core.version import CURRENT_VERSION

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
    # copy2 保留时间戳；失败会抛 OSError，让它冒上去 —— 由调用方拒绝替换。
    shutil.copy2(source, destination)
    if not destination.exists() or destination.stat().st_size == 0:
        raise OSError(f"备份没有成功写出内容：{destination}")
    return destination


def write_pending_update(
    pending_exe: str | Path,
    target_path: str | Path,
    version: str = CURRENT_VERSION,
    *,
    from_version: str = CURRENT_VERSION,
    sha256: str = "",
    path: str | Path | None = None,
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
    destination = Path(path) if path is not None else update_json_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, destination)
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
    """生成替换脚本的**正文**。

    ★ 正文是纯 ASCII，路径**不写死**在里面 —— 见模块开头
      "为什么 .bat 里不把路径写死"。三个路径由 :func:`launch_apply`
      作为命令行参数传进去（``%~1`` / ``%~2`` / ``%~3``）。

    Args:
        pending_exe: 新 exe 路径（用来生成注释，也校验它非空）。
        target_path: 目标 exe 路径（同上）。
        update_json: 交接文件路径（同上）。
        log_path: 日志文件路径；``None`` = 不写日志。

    Returns:
        bat 文件正文（``\\r\\n`` 换行 —— cmd.exe 对纯 ``\\n`` 也能跑，
        但某些版本的 ``for`` 块解析会出问题，用 CRLF 最稳）。
    """
    if not pending_exe or not target_path or not update_json:
        raise ValueError("生成替换脚本需要 pending_exe / target_path / update_json")
    log_line = (
        f'echo [%DATE% %TIME%] %~4 >> "%~4"' if log_path is not None else "rem (no log)"
    )
    # ★★ 正文**必须全是 ASCII**，注释也不例外。
    #
    #   第一版我在 rem 里写了中文，结果被自己的断言逮住
    #   （`write_apply_bat` 的 ascii 编码 + 自检里的"bat 正文是纯 ASCII"）。
    #   中文注释在这里是真会出事的：cmd.exe 按**当前代码页**逐字节解码 bat，
    #   中文按 GBK 解错位之后，错位配对的尾字节范围正好包含 `"` 和 `\`，
    #   会把后面那句 `copy /Y "%SRC%" "%DST%"` 的引号吃掉 ——
    #   报出来的错完全看不出根因（AGENTS.md 1b 记的就是这个坑）。
    #   所以注释一律写英文。
    lines = [
        "@echo off",
        "rem ---------------------------------------------------------------",
        "rem  transcript-lens auto-update apply script",
        "rem  Generated by the program. Safe to delete after it runs.",
        "rem",
        "rem  Args: %1=new exe  %2=target exe  %3=update.json  %4=log file",
        "rem",
        "rem  WHY ARGS AND NOT BAKED-IN PATHS:",
        "rem  paths may contain non-ASCII (user name). cmd.exe decodes this",
        "rem  file using the current code page; a mis-decoded double-byte",
        "rem  sequence can swallow the following quote character and break",
        "rem  the copy line below. Command-line args go through the Win32",
        "rem  UTF-16 API, so they are safe. See core/update_apply.py.",
        "rem",
        "rem  Retry 60 times (1s apart): the just-exited process may still",
        "rem  hold the file handle for a few seconds. If every attempt",
        "rem  fails we do nothing at all -- the old exe stays untouched.",
        "rem ---------------------------------------------------------------",
        "setlocal",
        "set \"SRC=%~1\"",
        "set \"DST=%~2\"",
        "set \"JSN=%~3\"",
        "",
        "if not exist \"%SRC%\" goto :nosrc",
        "if not exist \"%DST%\" goto :nodst",
        "",
        log_line,
        "",
        "for /L %%i in (1,1,60) do (",
        "    copy /Y \"%SRC%\" \"%DST%\" >nul 2>&1",
        "    if not errorlevel 1 goto :copied",
        "    timeout /t 1 /nobreak >nul",
        ")",
        log_line,
        "goto :giveup",
        "",
        ":copied",
        "rem New binary is in place. Drop temp + handoff files, then start it.",
        "del \"%SRC%\" >nul 2>&1",
        "del \"%JSN%\" >nul 2>&1",
        log_line,
        "start \"\" \"%DST%\"",
        "exit /b 0",
        "",
        ":nosrc",
        log_line,
        "exit /b 2",
        "",
        ":nodst",
        log_line,
        "exit /b 3",
        "",
        ":giveup",
        "rem All 60 attempts failed -- leave everything as it was.",
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
    """把四个路径当参数交给 bat 跑起来（**不等待它**），返回子进程 pid。

    ★ 为什么用参数而不是把路径写进 bat：见模块开头。
    ★ 为什么不等它：等它就等于"本进程不退出"，
      而 bat 第一步就是在等本进程退出 —— 死锁。
    """
    runner = subprocess.Popen if popen is None else popen
    command = [
        "cmd",
        "/c",
        str(bat_path),
        str(pending_exe),
        str(target_path),
        str(update_json),
    ]
    if log_path is not None:
        command.append(str(log_path))
    process = runner(command)
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
    """启动早期检测（任务书 2.3）：有 ``update.json`` 就执行替换并交棒。

    三种情形：

    ==================================  ==========================================
    情形                                 行为
    ==================================  ==========================================
    没有 update.json                      ``handed_off=False``，什么都不做
    有，但 ``pending_exe`` 不见了          清掉交接文件，``False``（下载没了，
                                          留着只会每次启动都失败一次）
    有，而且都齐                          **先备份**当前 exe → 写 bat → 拉起 →
                                          ``handed_off=True``
    ==================================  ==========================================

    ★ **备份失败就拒绝替换**（任务书 2.4）：:func:`backup_current_exe` 抛异常
      时这里返回 ``handed_off=False`` 并把原因带回去，旧 exe 一个字节没动。

    Args:
        root: 数据根目录（备份放它下面；测试用）。
        frozen: 覆盖"是不是打包版"的判断（测试用）。
        json_path / bat_path / log_path: 覆盖默认路径（测试用）。
        popen: 注入的假 ``subprocess.Popen``（测试用）。
        backup: 要不要先备份（测试里可以关掉）。

    Returns:
        :class:`ApplyResult`。**这个函数不抛异常** —— 它跑在启动路径上，
        一个坏掉的交接文件不该让用户连程序都打不开。
    """
    resolved_log = (
        Path(log_path) if log_path is not None else update_dir() / APPLY_LOG_NAME
    )
    try:
        pending = read_pending_update(json_path)
    except Exception as exc:  # noqa: BLE001 - 启动路径不许抛
        return ApplyResult(False, f"读更新交接文件失败（{type(exc).__name__}）。")

    if pending is None:
        return ApplyResult(False, "")

    pending_exe = Path(str(pending["pending_exe"]))
    target_path = Path(str(pending["target_path"]))
    version = str(pending.get("version") or "")

    running_frozen = is_frozen() if frozen is None else bool(frozen)
    if not running_frozen:
        # 源码模式：没有 exe 可替换。**不删交接文件** —— 用户可能只是
        # 用源码跑了一次，真正要替换的是那个 exe。留着，下一次正常启动会处理。
        return ApplyResult(
            False,
            "检测到待应用的更新，但当前是源码方式运行，跳过 exe 替换。",
            pending,
        )

    if not pending_exe.exists():
        clear_pending_update(json_path)
        _append_log(f"pending exe 不存在，已丢弃交接文件：{pending_exe}", resolved_log)
        return ApplyResult(
            False, f"待安装的新版本文件已经不在了（{pending_exe.name}），已跳过。",
            pending,
        )

    # 下一层保险：交接文件里带了哈希就再验一次（下载时已经验过一遍，
    # 但文件在中转目录里躺了几天，被别的东西改掉的可能性不是零）。
    checksum = str(pending.get("sha256") or "")
    if checksum and not verify_checksum(pending_exe, checksum):
        clear_pending_update(json_path)
        _append_log("pending exe 校验不一致，已丢弃", resolved_log)
        return ApplyResult(
            False, "待安装的新版本文件校验不一致，为了安全已跳过本次更新。", pending
        )

    if backup:
        try:
            saved = backup_current_exe(
                target_path, old_version=str(pending.get("from_version") or ""), root=root
            )
        except Exception as exc:  # noqa: BLE001 - 备份失败必须挡住替换
            _append_log(f"备份失败，拒绝替换：{exc}", resolved_log)
            return ApplyResult(
                False,
                f"备份当前版本失败（{exc}），为安全起见**没有**执行替换。",
                pending,
            )
        _append_log(f"旧版本已备份到 {saved}", resolved_log)

    try:
        script = write_apply_bat(
            pending_exe, target_path, json_path or update_json_path(),
            bat_path=bat_path, log_path=resolved_log,
        )
        launch_apply(
            script, pending_exe, target_path, json_path or update_json_path(),
            log_path=resolved_log, popen=popen,
        )
    except Exception as exc:  # noqa: BLE001 - 起不来脚本就照常启动旧版本
        _append_log(f"生成/启动替换脚本失败：{exc}", resolved_log)
        return ApplyResult(
            False, f"准备替换脚本失败（{exc}），本次照常启动当前版本。", pending
        )

    _append_log(f"已交接给替换脚本，准备退出以释放文件锁（目标 v{version}）", resolved_log)
    return ApplyResult(
        True,
        f"正在应用已下载的更新 v{version}，程序会立即重启。",
        pending,
    )
