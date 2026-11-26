"""清理 Windows 单文件包留在本应用解包目录的陈旧缓存。"""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

CACHE_FOLDER = "SGMS_RunCache"
MARKER = ".zhaoxi-onefile.json"
BUNDLE = re.compile(r"_MEI([0-9a-fA-F]{8})[0-9A-Za-z]+")
EXPIRED_SECONDS = 24 * 60 * 60


def _process_gone(pid: int) -> bool:
    # 对拒绝访问和未知状态均按仍在运行处理。
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    handle = open_process(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
    code = wintypes.DWORD()
    try:
        return bool(kernel.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code))) and code.value != 259
    finally:
        kernel.CloseHandle(wintypes.HANDLE(handle))


def clean_stale(root: Path, current: Path, *, process_gone=_process_gone,
                now: float | None = None) -> list[str]:
    if root.is_symlink() or root.is_junction():
        return []
    root = root.resolve()
    current = current.resolve()
    cutoff = (time.time() if now is None else now) - EXPIRED_SECONDS
    removed = []
    for target in root.iterdir():
        match = BUNDLE.fullmatch(target.name)
        if not match or target.is_symlink() or target.is_junction():
            continue
        marker = target / MARKER
        try:
            location = target.resolve(strict=True)
            if location == current or location.parent != root or not location.is_dir():
                continue
            if not marker.is_file() or marker.is_symlink() or marker.stat().st_mtime > cutoff:
                continue
            with marker.open("r", encoding="utf-8") as handle:
                proof = json.loads(handle.read(512))
            if not isinstance(proof, dict):
                continue
            parent, child = proof.get("parent"), proof.get("child")
            if type(parent) is not int or type(child) is not int or not (0 < child <= 0xFFFFFFFF):
                continue
            if not parent or parent != int(match.group(1), 16) or not all(map(process_gone, (parent, child))):
                continue
            # 在 Windows 上 rmtree 不穿越目录联接。也不处理标记验证后被替换的路径。
            if target.is_symlink() or target.is_junction() or target.resolve(strict=True) != location:
                continue
            shutil.rmtree(location)
            removed.append(location.name)
        except (OSError, ValueError):
            continue
    return removed


def start_cleanup() -> None:
    local = os.environ.get("LOCALAPPDATA")
    bundle = getattr(sys, "_MEIPASS", None)
    if sys.platform != "win32" or not local or not bundle:
        return

    def maintain():
        try:
            root = Path(local) / CACHE_FOLDER
            current = Path(bundle).resolve(strict=True)
            match = BUNDLE.fullmatch(current.name)
            if root.is_symlink() or root.is_junction() or not match or current.parent != root.resolve():
                return
            parent = os.getppid()
            if parent != int(match.group(1), 16):
                return
            (current / MARKER).write_text(
                json.dumps({"parent": parent, "child": os.getpid()}), encoding="utf-8",
            )
            clean_stale(root, current)
        except (OSError, ValueError):
            return

    threading.Thread(target=maintain, name="bundle-cache-cleanup", daemon=True).start()
