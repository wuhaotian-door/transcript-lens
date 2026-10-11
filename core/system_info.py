"""本机配置探测：物理内存 / 显卡显存 / 逻辑核数。

设计约束（三条，都来自本仓库的既有规矩）
========================================

1. **零第三方依赖。** ``AGENTS.md`` 不许为一个数字引入新依赖
   （``core/ai/model_registry.py`` 的模块注释里记着这条决定的由来）。
   内存走 Windows 自带的 ``GlobalMemoryStatusEx``（``ctypes``），
   显存走系统里现成的 ``nvidia-smi``（子进程），一个包都不装。
2. **零 Qt、零网络。** 本模块不 import PySide6，也不碰 ``urllib`` / ``socket`` ——
   它只回答"这台机器有什么"，怎么显示是界面层的事。
3. **探测失败一律返回 ``None`` / 空元组，绝不编数字。**
   ``None`` 的语义是"我不知道"，**不是**"内存是 0"。
   调用方（模型中心的负载标注）拿到 ``None`` 时按"放行"处理 ——
   宁可让用户自己判断，也不要因为"没测出来"就禁止他下载。

★ 为什么不用 psutil / wmi
=========================

``psutil`` 本机虽然装着，但它是**要打进 exe 的新依赖**，而我们只要两个数字；
``wmi`` 本机根本没有、还得再装一个。``GlobalMemoryStatusEx`` 一次调用就把
"总量 + 可用量"都给了，和模型中心原本那段探测是**同一个 API** ——
所以本模块成了内存探测的唯一真源（``model_registry.local_ram_gb()`` 转调这里）。

★ 显存只认 N 卡
==============

``nvidia-smi`` 是 N 卡驱动自带的，用它查显存**零依赖**。A 卡 / 集显没有这个
命令行工具，于是 :func:`gpu_infos` 返回空元组、界面上只说内存 ——
这比"猜一个显存数字"诚实。查询带超时，驱动卡死时不会把界面拖住。

★ 关于子进程
============

``nvidia-smi`` 是**控制台程序**，直接在 GUI 进程里起会闪一下黑框，
所以带上 ``CREATE_NO_WINDOW``（非 Windows 上取 0，等价于不带这个标志）。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from dataclasses import dataclass

__all__ = [
    "GPU_EXE",
    "GPU_QUERY_ARGS",
    "GPU_TIMEOUT_S",
    "GPUInfo",
    "SystemInfo",
    "available_ram_gb",
    "clear_cache",
    "describe_system",
    "discrete_gpu_vram_gb",
    "gpu_infos",
    "logical_cpu_count",
    "memory_status",
    "snapshot",
    "total_ram_gb",
]

#: 显存查询用的命令行工具名（N 卡驱动自带）。找不到就说明没有可查的显存。
GPU_EXE = "nvidia-smi"

#: ``nvidia-smi`` 的查询参数：名字 + 显存总量，CSV、无表头、单位 MB。
#: ``nounits`` 让输出是纯数字，省掉"12227 MiB"里那个单位的解析。
GPU_QUERY_ARGS: tuple[str, ...] = (
    "--query-gpu=name,memory.total",
    "--format=csv,noheader,nounits",
)

#: 显存查询的超时（秒）。驱动异常时最多等这么久 —— 界面在等它。
GPU_TIMEOUT_S = 5.0

#: 起子进程时不弹控制台窗口（只在 Windows 上有意义）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 1 GB = 1024 MB。``nvidia-smi`` 报的是 MiB，这里按 1024 换算。
_MB_PER_GB = 1024.0


class _MEMORYSTATUSEX(ctypes.Structure):
    """``GlobalMemoryStatusEx`` 的入参结构体（``ctypes`` 版本）。"""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


# ---------------------------------------------------------------------------
# 内存
# ---------------------------------------------------------------------------


def memory_status() -> tuple[float, float] | None:
    """``(物理内存总量 GB, 当前可用 GB)``；测不到返回 ``None``。

    整个模块只有这一处真正调系统 API —— 其余都是它的包装。
    """
    if os.name != "nt":
        return None
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
        available = int(status.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        return None
    if total <= 0:
        return None
    if available < 0:
        available = 0
    return total / (1024 ** 3), available / (1024 ** 3)


def total_ram_gb() -> float | None:
    """物理内存总量（GB）；测不到返回 ``None``。

    ★ 这是**内存探测的唯一真源**：``core.ai.model_registry.local_ram_gb()``
      转调这里，省得同一件事两处实现、哪天悄悄漂移。
    """
    got = memory_status()
    return None if got is None else got[0]


def available_ram_gb() -> float | None:
    """当前可用内存（GB）；测不到返回 ``None``。"""
    got = memory_status()
    return None if got is None else got[1]


def logical_cpu_count() -> int | None:
    """逻辑核数（``os.cpu_count()``）；拿不到返回 ``None``。

    ★ 只是显示用 —— 模型推理的线程数是引擎自己的事，这里不替它决定。
    """
    try:
        count = os.cpu_count()
    except (AttributeError, OSError, ValueError):
        return None
    if not count or count <= 0:
        return None
    return int(count)


# ---------------------------------------------------------------------------
# 显卡
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GPUInfo:
    """一块能查到显存的显卡。

    ==============  =====================================================
    字段             说明
    ==============  =====================================================
    ``name``        驱动报的型号（例 ``"NVIDIA GeForce RTX 5070 Ti Laptop GPU"``）
    ``vram_gb``     显存总量（GB）；驱动报 ``N/A`` 时为 ``0.0``
    ``source``      这个数字从哪来（默认 ``"nvidia-smi"``）
    ==============  =====================================================
    """

    name: str = ""
    vram_gb: float = 0.0
    source: str = GPU_EXE

    @property
    def vram_text(self) -> str:
        """人类可读的显存（例 ``"12.0 GB"``）；不知道就是"显存未知"。"""
        if self.vram_gb <= 0:
            return "显存未知"
        return f"{self.vram_gb:.1f} GB"

    @property
    def display(self) -> str:
        """界面上那一行的文字：``型号（12.0 GB 显存）``。"""
        name = self.name.strip() or "未知显卡"
        return f"{name}（{self.vram_text}显存）"


def _parse_gpu_lines(text: str) -> tuple[GPUInfo, ...]:
    """把 ``nvidia-smi`` 的 CSV 输出解析成 :class:`GPUInfo`。

    只认 ``名字, 显存MB`` 两列；认不出的行**跳过**（驱动版本不同会多给列，
    那时宁可少显示一块卡，也不要报一个错的显存数）。
    """
    found: list[GPUInfo] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or "," not in line:
            continue
        name, _, rest = line.rpartition(",")
        size_text = rest.strip().split()[0] if rest.strip() else ""
        name = name.strip()
        if not name:
            continue
        try:
            mb = float(size_text)
        except (TypeError, ValueError):
            mb = 0.0
        if mb < 0:
            mb = 0.0
        found.append(GPUInfo(name=name, vram_gb=mb / _MB_PER_GB))
    return tuple(found)


def gpu_infos(*, timeout: float = GPU_TIMEOUT_S) -> tuple[GPUInfo, ...]:
    """本机能查到的显卡（目前只有 N 卡查得到）；没有就返回空元组。

    **绝不抛异常**：没有 ``nvidia-smi``、驱动没装、命令超时、输出是乱码 ——
    一律当作"查不到"，返回空元组。
    """
    exe = shutil.which(GPU_EXE)
    if not exe:
        return ()
    try:
        done = subprocess.run(
            [exe, *GPU_QUERY_ARGS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(0.1, float(timeout)),
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()
    if done.returncode != 0:
        return ()
    return _parse_gpu_lines(done.stdout or "")


def discrete_gpu_vram_gb(*, timeout: float = GPU_TIMEOUT_S) -> float | None:
    """最大的一块显存（GB）；查不到返回 ``None``。

    ★ 多卡机器取最大那块：模型要么装一块卡上，要么不装 ——
      把显存加起来会得出"32GB 能跑 13B"这种假结论。
    """
    cards = gpu_infos(timeout=timeout)
    if not cards:
        return None
    best = max(card.vram_gb for card in cards)
    return best if best > 0 else None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemInfo:
    """一次探测的全部结果（**值对象**，界面拿着它画）。

    ======================  ===============================================
    字段                     说明
    ======================  ===============================================
    ``total_ram_gb``        物理内存总量（GB）；``None`` = 没测到
    ``available_ram_gb``    当前可用内存（GB）；``None`` = 没测到
    ``cpu_count``           逻辑核数；``None`` = 没测到
    ``gpus``                查到的显卡（空元组 = 没有 N 卡 / 查不到）
    ``probed_gpu``          这次到底有没有去问过 ``nvidia-smi``
    ======================  ===============================================
    """

    total_ram_gb: float | None = None
    available_ram_gb: float | None = None
    cpu_count: int | None = None
    gpus: tuple[GPUInfo, ...] = ()
    probed_gpu: bool = True

    @property
    def has_discrete_gpu(self) -> bool:
        """有没有查到能用的独立显存。"""
        return self.max_vram_gb is not None

    @property
    def max_vram_gb(self) -> float | None:
        """最大的一块显存（GB）；查不到 ``None``。"""
        if not self.gpus:
            return None
        best = max(card.vram_gb for card in self.gpus)
        return best if best > 0 else None

    @property
    def ram_text(self) -> str:
        """内存那一句（例 ``"31.9 GB（可用 18.2 GB）"`` / ``"内存未测到"``）。"""
        if self.total_ram_gb is None:
            return "内存未测到"
        if self.available_ram_gb is None:
            return f"{self.total_ram_gb:.1f} GB"
        return f"{self.total_ram_gb:.1f} GB（可用 {self.available_ram_gb:.1f} GB）"

    @property
    def gpu_text(self) -> str:
        """显卡那一句（例 ``"RTX 5070 Ti Laptop GPU（12.0 GB 显存）"``）。"""
        if self.gpus:
            return "、".join(card.display for card in self.gpus)
        if not self.probed_gpu:
            return "未查显卡"
        return "没有查到独立显卡显存"

    def describe(self) -> str:
        """给界面用的一段中文（**纯文本，不写 Markdown** —— 铁律 9）。"""
        parts = [f"内存 {self.ram_text}"]
        if self.cpu_count:
            parts.append(f"{self.cpu_count} 个逻辑核")
        parts.append(self.gpu_text)
        return " · ".join(parts)


#: :func:`snapshot` 的进程内缓存。显卡查询要起子进程（几十到几百毫秒），
#: 一次会话里问很多遍没有意义。
_CACHE: SystemInfo | None = None


def snapshot(*, force: bool = False, probe_gpu: bool = True) -> SystemInfo:
    """探测一次本机配置（带进程内缓存）。

    Args:
        force: 忽略缓存重新探测（测试与"刷新"按钮用）。
        probe_gpu: 要不要去问 ``nvidia-smi``。关掉它时返回的
            ``SystemInfo.probed_gpu`` 是 ``False``，界面据此说"未查显卡"
            而不是"没有独显"—— 这两句话不是一回事。

    ★ 缓存的是**探测结果**，不是"永远不变的事实"：可用内存在运行期会变，
      所以要刷新就显式 ``force=True``。
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    got = memory_status()
    info = SystemInfo(
        total_ram_gb=None if got is None else got[0],
        available_ram_gb=None if got is None else got[1],
        cpu_count=logical_cpu_count(),
        gpus=gpu_infos() if probe_gpu else (),
        probed_gpu=bool(probe_gpu),
    )
    _CACHE = info
    return info


def clear_cache() -> None:
    """清掉 :func:`snapshot` 的缓存（测试用；也是"重新检测"按钮的落点）。"""
    global _CACHE
    _CACHE = None


def describe_system(*, probe_gpu: bool = True) -> str:
    """一句话概括本机配置（模型中心顶部那行说明用它）。"""
    return snapshot(probe_gpu=probe_gpu).describe()
