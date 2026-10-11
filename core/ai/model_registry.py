"""模型清单数据层 —— 把远端 ``update/ai_models.json`` 变成界面能直接用的对象。

职责
====

1. **解析**远端清单（:func:`parse_model_list`）—— 宽容解析，坏一条跳一条，
   绝不因为一条脏数据把整张清单废掉；
2. **取**远端清单（:func:`fetch_model_list`）—— 只负责调用和翻译状态，
   **不抛异常**（调用方多半是线程里的网络动作，抛出去没人接）；
3. **记账**（:func:`load_installed` / :func:`save_installed` /
   :func:`record_installed` / :func:`forget_installed`）——
   ``models/installed.json`` 记下"哪个 id 装过、装的是哪一版哈希"；
4. **合成**（:func:`build_choices`）—— 把"账本 + 磁盘 + 本机内存"三件事
   合成为一个 :class:`ModelChoice`，界面拿着它就能直接画。

★ 铁律：本模块（以及 ``core/ai/`` 下所有模块）零 HTTP import
==========================================================

``main.py --selfcheck`` 的联网红线检查会静态扫 ``core/ai/*.py``（含 ``.pyc``）
里以 import / from 开头的行，命中一次就 FAIL。所以这里**一行 urllib、requests、
socket 都不能有**。

HTTP 只从 :mod:`core.updater` 借：``_get_json`` 拿 JSON、``download_exe`` 拿
字节流（后者由界面层调用）。本模块只负责"把拿到的东西变成对象" ——
这样"零 HTTP import"和"能联网下模型"两条规则都不用让步。

★ 磁盘是唯一真相，installed.json 只是账本
=======================================

用户随时可能在资源管理器里手删掉 ``models/<名字>/``。所以
:func:`installed_state` **先看磁盘上有没有那个 ``.gguf``**：
账本说"装过"、文件却没了 —— 那就是"未装"，
不能让一个已经不存在的模型在界面上显示成已装。

★ 为什么内存用 ctypes 而不是 psutil
=================================

``AGENTS.md`` 不许为一个数字引入新依赖。``GlobalMemoryStatusEx`` 是 Windows
自带的，一次调用就拿到物理内存总量，够用了。拿不到（不是 Windows / 调用失败）
返回 ``None``，而 ``None`` **一律放行** —— 宁可让用户自己判断，
也不要因为"没测出来"就禁止他下载。
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from core.system_info import discrete_gpu_vram_gb, snapshot
from core.updater import DEFAULT_TIMEOUT, _get_json, normalize_sha256

__all__ = [
    "DEFAULT_GROUP",
    "GITEE_RAW_MODELS",
    "INSTALLED_FILE",
    "INSTALLED_SCHEMA",
    "LOAD_COLORS",
    "LOAD_EASY",
    "LOAD_HARD",
    "LOAD_LABELS",
    "LOAD_TIGHT",
    "LOCAL_GROUP",
    "SHA256_MIN_LEN",
    "STATE_INSTALLED",
    "STATE_MISSING",
    "STATE_OUTDATED",
    "ModelChoice",
    "RemoteModel",
    "build_choices",
    "check_memory",
    "default_checked",
    "describe_download_plan",
    "fetch_model_list",
    "forget_installed",
    "group_choices",
    "human_size_bytes",
    "installed_file",
    "installed_state",
    "load_level",
    "load_note",
    "load_installed",
    "local_ram_gb",
    "merged_with_local",
    "parse_model_list",
    "record_installed",
    "save_installed",
]

#: 远端清单的默认地址。Gitee 的 raw 直链，只读 GET，不需要 token。
#:
#: ★ 走 Gitee 而不是 GitHub：这台机器上的用户大多直连得到 Gitee，
#:   GitHub 常常要等很久。清单本身只有几 KB，随便哪边都行，
#:   但"能连上"比"地址好看"重要。
GITEE_RAW_MODELS = (
    "https://gitee.com/wuhaotiansystem/transcript-lens"
    "/raw/master/update/ai_models.json"
)

#: 清单里 sha256 的合法长度（64 位小写十六进制）。
SHA256_MIN_LEN = 64

#: 分组兜底名。清单里没写 ``group`` 的模型归到这里，它永远排第一。
DEFAULT_GROUP = "推荐"

#: 本机已有、但远端清单里没列的模型用的分组名。
LOCAL_GROUP = "本机已有"

#: 账本文件名。放在 ``models/`` 下 —— 它是**文件**不是目录，
#: 而 ``installed_models()`` 只认目录，所以不会把它误当成一个模型。
INSTALLED_FILE = "installed.json"

#: 账本结构版本。将来改结构时靠它做迁移。
INSTALLED_SCHEMA = 1

#: 安装状态的三种取值。界面文案直接用它，免得各处自己拼字符串。
STATE_INSTALLED = "已装"
STATE_MISSING = "未装"
STATE_OUTDATED = "待更新"

#: 大小比对允许的相对偏差。清单里的体积常常是四舍五入过的
#: （``1.12 GB`` 这种写法），要求逐字节相等会把正常文件判死。
#: 1% 足够挡住"下回来一个网盘错误页"，又不会误伤。
SIZE_TOLERANCE = 0.01

#: 内存限制判据里的宽限系数：``min_ram_gb * 0.7`` 之上算「勉强」。
#:
#: ★ 这是**产品定义的阈值**，不是估算出来的经验值。写死在这里，
#:   三条判据（:func:`load_level`）之外任何地方都不许再乘第二次。
RAM_TIGHT_RATIO = 0.7

#: 没有声明 ``minVramGb`` 时，:func:`load_note` 会在末尾补的这句话。
#: 它说明的是"没要求"而不是"不用显存" —— 集显会从内存里借，所以不写清
#: 用户会以为独显与集显一模一样。
VRAM_OPTIONAL_REMINDER = "（这一条没有声明显存要求，集显会从内存里借，速度会慢一些）"

# ---------------------------------------------------------------------------
# 负载等级（B4）
# ---------------------------------------------------------------------------

#: 负载等级：本机配置**够用**（内存达标，且显存要求满足或本来就没要求）。
LOAD_EASY = "easy"

#: 负载等级：**勉强**能跑 —— 内存不到 ``minRamGb``，但还够 ``0.7`` 倍。
LOAD_TIGHT = "tight"

#: 负载等级：**困难** —— 内存连 ``0.7 * minRamGb`` 都不到。
LOAD_HARD = "hard"

#: 三档的中文标签。界面直接用它，不要在别处再拼一份字符串。
LOAD_LABELS: dict[str, str] = {
    LOAD_EASY: "轻松",
    LOAD_TIGHT: "勉强",
    LOAD_HARD: "困难",
}

#: 三档的十六进制颜色（界面用 QSS ``border-radius`` 圆点或富文本上色）。
#:
#: ★ 绿 / 琥珀 / 红 用的是和分析页警示色同一套语感；**只给颜色，不给 emoji**
#:   （``AGENTS.md`` 4l：开屏标题里一个 emoji 值 241.7ms，界面文案一律不用）。
LOAD_COLORS: dict[str, str] = {
    LOAD_EASY: "#16A34A",
    LOAD_TIGHT: "#D97706",
    LOAD_HARD: "#DC2626",
}



# ---------------------------------------------------------------------------
# 小工具：把 JSON 里的脏数据收敛成能用的值
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """收敛成"去掉首尾空白的字符串"；``None`` 和容器一律当空串。

    ★ 容器要单独排掉：``str({"a": 1})`` 不会报错，但它会变成一个
      看着像模像样的模型名，那种脏数据比空串更难查。
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _positive_int(value: Any) -> int | None:
    """取正整数，取不到返回 ``None``。

    ★ ``bool`` 必须先排掉：``True`` 是 ``int`` 的子类，
      不排的话 ``"size": true`` 会静悄悄变成 1 字节。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            parsed = int(text)
            return parsed if parsed > 0 else None
    return None


def _number(value: Any) -> float:
    """取浮点数（内存 GB 这种可以带小数），取不到当 0。

    ★ 只给"认不出不算错"的字段用。``minRamGb`` **不许**走这里 ——
      见 :func:`_memory_gb`：那边"读不懂"必须出声。
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _memory_gb(value: Any, *, field: str = "minRamGb") -> tuple[float, str]:
    """解析 ``minRamGb`` / ``minVramGb``，返回 ``(GB, 警告)`` —— 警告空串表示没问题。

    ★ 为什么不能像别的字段一样"认不出就当 0"（**这是测出来的真坑**）：
      ``minRamGb`` 是**内存闸门**，而 :func:`check_memory` 对
      ``need <= 0`` 是直接放行的（"这个模型没有声明内存要求"）。
      于是清单里手滑写成 ``"8GB"``（带单位，人最容易这么写）时，
      老实现静默给 0.0，闸门就**悄悄失效**：4 GB 内存的机器会把 3B 模型
      判成"够跑"，一加载就被系统杀掉，而且界面上一句提示都没有。

      所以这里分三档：
      1. 没写 → 0.0，**不警告**（发布方有权不声明）；
      2. 写了、认得出（含 ``"8GB"`` 这种带单位的写法）→ 用那个数；
      3. 写了、读不懂 → 0.0 **+ 一句人话**，告诉用户这条没被拦住、自己看着办。

    Args:
        value: 清单里那个字段的原值。
        field: 字段名 —— **只用于警告文案**。``minVramGb`` 复用本函数，
            警告里必须出现的是它自己的名字，否则用户拿着
            "minRamGb 读不出一个 GB 数" 去清单里找一个根本没错的字段。

    ★ ``min_vram_gb`` 的缺省值是 ``None``（= 没要求）而不是 ``0.0``：
      两者在 :func:`load_level` 里判据相同，但 ``None`` 才能在
      :func:`load_note` 里说清"这一条压根没声明显存"，而不是"要求 0 GB"。
    """
    if value is None:
        return 0.0, ""
    if isinstance(value, bool):
        return 0.0, f"{field} 写成了布尔值（{value}），按「没写」处理。"
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return 0.0, ""
        for unit in ("gb", "g", "吉字节"):
            if text.endswith(unit):
                text = text[: -len(unit)].strip()
                break
        try:
            number = float(text)
        except ValueError:
            return 0.0, (
                f"{field} 写的是「{value}」，读不出一个 GB 数 —— "
                "这条不会被拦住，请自己确认机器能不能跑。"
            )
    if number != number or number < 0:          # NaN / 负数
        return 0.0, f"{field} 写的是「{value}」，不是合法的 GB 数，按「没写」处理。"
    return number, ""



def _flag(value: Any) -> bool:
    """取布尔值，容忍 ``"true"`` / ``1`` / ``"是"`` 这几种写法。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return False


def _last_url_segment(url: str) -> str:
    """从直链里抠出文件名，用作最后的名字兜底。"""
    text = str(url or "").split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return text.rsplit("/", 1)[-1].strip()


def human_size_bytes(size: Any) -> str:
    """把字节数写成 ``"1.1 GB"`` 这种人话；取不到就返回空串。

    ★ 为什么**没有**直接复用 ``core.ai.model_downloader._human_size``：
      那个函数的入参是 :class:`pathlib.Path`（它自己 ``stat()`` 拿大小），
      而清单里的体积是**远端声称的字节数**，此刻磁盘上还没有这个文件。
      这里照抄了它的量纲和位数约定（1024 进制、B 不带小数、其余一位小数），
      保证"清单里的 1.1 GB"和"下回来之后的 1.1 GB"长得一模一样。
    """
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return ""  # pragma: no cover


# ---------------------------------------------------------------------------
# 远端清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteModel:
    """清单里的一条模型。

    ================  =====================================================
    字段               说明
    ================  =====================================================
    ``id``            稳定标识（也是磁盘上的目录名）
    ``label``         界面上的中文名；空则退回 ``id``
    ``group``         分组名；空则 :attr:`group_or_default` 给 ``"推荐"``
    ``file``          落地文件名（``xxx.gguf``）
    ``url``           直链；空串 = 没有可下地址（本机补进来的那种）
    ``size``          字节数（**正整数**，解析阶段已经把关）
    ``sha256``        64 位小写十六进制；空串 = 这条没法校验完整性
    ``min_ram_gb``    跑它至少需要多少 GB 内存；``<= 0`` = 没要求
    ``min_vram_gb``   跑它建议多少 GB 显存；``None`` / ``<= 0`` = 没要求
    ``default_check`` 清单建议默认勾上（★ 现在**只用于展示**，见下）
    ``note``          给用户看的一句话说明
    ================  =====================================================

    ★ ``default_check`` 的语义**降级了**：v1.9 B4 起"默认勾选"由
      :func:`load_level` 决定（只勾「轻松」），清单里这个标志
      只拿来给界面挂一个「推荐」角标。
      一个模型**不会**因为 ``defaultCheck=true`` 就在非轻松时被预勾 ——
      理由：清单作者写这个标志时并不知道用户这台机器是什么配置。
    """

    id: str
    label: str = ""
    group: str = ""
    file: str = ""
    url: str = ""
    size: int = 0
    sha256: str = ""
    min_ram_gb: float = 0.0
    min_vram_gb: float | None = None
    default_check: bool = False
    note: str = ""

    @property
    def display(self) -> str:
        """列表里显示的名字（``label`` 优先，空了用 ``id``）。"""
        return self.label or self.id

    @property
    def size_text(self) -> str:
        """人类可读的体积，例 ``"1.1 GB"``。"""
        return human_size_bytes(self.size)

    @property
    def group_or_default(self) -> str:
        """分组名；清单里没写就当作推荐组。"""
        return self.group.strip() or DEFAULT_GROUP

    @property
    def has_checksum(self) -> bool:
        """有没有可用的 sha256（没有就只能校大小）。"""
        return bool(self.sha256)


def parse_model_list(payload: Any) -> tuple[list[RemoteModel], list[str]]:
    """宽容解析清单。返回 ``(模型列表, 警告列表)``，**不抛异常**。

    ★ 为什么是"跳一条 + 一句人话"而不是抛异常：
      清单是发布方手写的 JSON，错一条的概率远大于全错。整张清单因为
      第 3 条少了个字段就报废，对用户来说等于"功能坏了"。
      所以这里的原则是 —— 能救的救活（比如 sha256 认不出来，
      整条照样保留，只是标成"没法校验完整性"），
      救不活的跳过并**用中文说清是第几条、错在哪**。

    Args:
        payload: 已经 ``json.loads`` 过的对象。

    Returns:
        ``(模型列表, 警告列表)``。警告是给界面直接显示的人话。
    """
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return [], ["清单格式不对：顶层应该是一个 JSON 对象。"]

    schema = payload.get("schema")
    if schema is not None and schema != INSTALLED_SCHEMA:
        warnings.append(
            f"清单的 schema 是 {schema}，本版本只认 {INSTALLED_SCHEMA}，"
            "仍按老格式解析。"
        )

    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        # ★ 别把上面攒的警告丢掉（子代理报过一次：schema 提示被这句 return 吃掉）
        return [], warnings + ["清单格式不对：models 字段应该是一个列表。"]

    models: list[RemoteModel] = []
    seen: set[str] = set()

    for index, item in enumerate(raw_models, start=1):
        if not isinstance(item, dict):
            warnings.append(f"第 {index} 条不是一个对象，已跳过。")
            continue

        url = _text(item.get("url"))
        if not url:
            warnings.append(f"第 {index} 条缺少 url，已跳过。")
            continue

        size = _positive_int(item.get("size"))
        if size is None:
            warnings.append(f"第 {index} 条缺少可用的 size（要正整数），已跳过。")
            continue

        # id 三级兜底：id → file → url 最后一段。三样都空才丢。
        file_name = _text(item.get("file"))
        model_id = _text(item.get("id")) or file_name or _last_url_segment(url)
        if not model_id:
            warnings.append(f"第 {index} 条从 id、file、url 都取不出名字，已跳过。")
            continue

        if model_id in seen:
            warnings.append(
                f"第 {index} 条的 id（{model_id}）和前面重复了，保留第一条。"
            )
            continue

        raw_sha = _text(item.get("sha256"))
        sha256 = normalize_sha256(raw_sha)
        if not raw_sha:
            warnings.append(f"第 {index} 条（{model_id}）没写 sha256，这条没法校验完整性。")
        elif not sha256:
            warnings.append(
                f"第 {index} 条（{model_id}）的 sha256 不是 64 位十六进制，"
                "这条没法校验完整性。"
            )

        min_ram_gb, ram_note = _memory_gb(
            item.get("minRamGb", item.get("min_ram_gb"))
        )
        if ram_note:
            warnings.append(f"第 {index} 条（{model_id}）{ram_note}")

        # ★ 显存是可选的第二道要求：清单里绝大多数条目根本没写它。
        #   写了就得认（数字和 "6GB" 都认），认不出一样要出声 ——
        #   静默当成"没要求"会让一台集显机器把 7B 判成「轻松」。
        vram_gb, vram_note = _memory_gb(
            item.get("minVramGb", item.get("min_vram_gb")),
            field="minVramGb",
        )
        if vram_note:
            warnings.append(f"第 {index} 条（{model_id}）{vram_note}")
        min_vram_gb = vram_gb if vram_gb > 0 else None

        seen.add(model_id)
        models.append(
            RemoteModel(
                id=model_id,
                label=_text(item.get("label")) or model_id,
                group=_text(item.get("group")),
                file=file_name,
                url=url,
                size=size,
                sha256=sha256,
                min_ram_gb=min_ram_gb,
                min_vram_gb=min_vram_gb,
                default_check=_flag(
                    item.get("defaultCheck", item.get("default_check"))
                ),
                note=_text(item.get("note")),
            )
        )

    return models, warnings


def fetch_model_list(
    url: str = GITEE_RAW_MODELS,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[RemoteModel], list[str], str]:
    """去远端取清单。返回 ``(模型, 警告, 状态说明)``，**不抛异常**。

    ★ 状态说明是给人看的，所以每一种失败都要说清"是不是故障"：

    * 连不上 → 断网或 Gitee 抽风，**稍后可以再试**；
    * 404 → 远端还没发布这个文件，**不是故障**（和
      ``AGENTS.md`` 里 "/releases/latest 404 不是故障" 是同一条道理）；
    * 200 但不是合法 JSON → 清单写坏了，得等发布方修。

    把这三件事混成一句"读取失败"，用户就只能干瞪眼。

    Args:
        url: 清单地址。
        timeout: 单次请求超时（秒）。

    Returns:
        ``(模型列表, 警告列表, 状态说明)``。失败时模型列表为空。
    """
    try:
        status, payload = _get_json(url, timeout)
    except Exception as exc:  # pragma: no cover - _get_json 自己已兜底
        return [], [], f"读取清单时出了意外（{type(exc).__name__}），这次先跳过。"

    if status is None:
        return [], [], "连不上服务器（可能是断网或 Gitee 抽风），稍后再试。"

    if status == 404:
        return [], [], "远端还没发布 update/ai_models.json（404 不是故障）。"

    if status != 200:
        return [], [], f"服务器返回 HTTP {status}，这次没取到清单。"

    if payload is None:
        return [], [], "清单不是合法 JSON，这次没取到任何模型。"

    models, warnings = parse_model_list(payload)
    if not models:
        return [], warnings, "清单取到了，但里面一条能用的模型都没有。"

    return models, warnings, f"已从远端取到 {len(models)} 个模型。"


# ---------------------------------------------------------------------------
# 本机账本（installed.json）
# ---------------------------------------------------------------------------


def installed_file(dest_dir: str | Path | None = None) -> Path:
    """账本文件路径 ``models/installed.json``。"""
    from core.ai.model_downloader import default_models_dir

    base = Path(dest_dir) if dest_dir is not None else default_models_dir()
    return base / INSTALLED_FILE


def load_installed(dest_dir: str | Path | None = None) -> dict[str, dict]:
    """读账本。**文件不存在、读不了、JSON 坏了都返回 ``{}``，绝不抛异常。**

    ★ 坏文件**不删**：它可能是用户唯一的线索（比如手工改坏之前的样子），
      而且"悄悄删掉用户的文件"是比"这次少显示几个模型"严重得多的行为。
      要不要提示、要不要重建，交给上层决定。
    """
    path = installed_file(dest_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        return {}
    records: dict[str, dict] = {}
    for key, value in raw_models.items():
        if isinstance(key, str) and isinstance(value, dict):
            records[key] = dict(value)
    return records


def save_installed(
    data: dict[str, dict],
    dest_dir: str | Path | None = None,
) -> Path:
    """把账本原子写回磁盘，返回写成的路径。

    ★ 先写 ``.tmp`` 再 ``os.replace``：``os.replace`` 在同一个卷上是原子的，
      所以断电/被杀进程只会留下一个 ``.tmp``，**不会**留下半个
      ``installed.json``（账本读坏了就等于所有模型都白下）。
    """
    path = installed_file(dest_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": INSTALLED_SCHEMA,
        "models": {str(key): dict(value) for key, value in dict(data).items()},
    }
    #: ``ensure_ascii=False`` —— 账本里的 label 是中文，写成人能读的样子；
    #: ``indent=2`` + 末尾换行 —— 万一用户要自己看一眼，diff 也友好。
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)
    return path


def record_installed(
    model_id: str,
    *,
    sha256: str,
    size: int,
    file: str,
    label: str = "",
    dest_dir: str | Path | None = None,
) -> None:
    """记一条"这个模型装好了"（读-改-写）。

    ``sha256`` 会过一遍 :func:`core.updater.normalize_sha256`；
    清单里没给哈希时传空串，将来远端补上哈希，这条就会显示成"待更新"。
    """
    data = load_installed(dest_dir)
    data[str(model_id)] = {
        "sha256": normalize_sha256(sha256),
        "size": int(size),
        "file": str(file),
        "label": str(label or ""),
    }
    save_installed(data, dest_dir)


def forget_installed(
    model_id: str,
    *,
    dest_dir: str | Path | None = None,
) -> bool:
    """把一条从账本里划掉。返回"原本有没有这条"。

    ★ 只动账本，**不动磁盘上的模型文件** —— 删目录是不可逆的，
      该由用户明确点"删除模型"来触发，不是"忘了它"的副作用。
    """
    data = load_installed(dest_dir)
    key = str(model_id)
    if key not in data:
        return False
    del data[key]
    save_installed(data, dest_dir)
    return True


def local_model_files(dest_dir: str | Path | None = None) -> dict[str, Path]:
    """磁盘上"真的装好了"的模型：``{目录名: .gguf 路径}``。

    ``installed_models()`` 只认"目录里有一个非空的 .gguf"，
    所以下载中途留下的空目录 / ``.part`` 都不会出现在这里。
    """
    from core.ai.model_downloader import installed_models

    files: dict[str, Path] = {}
    for info in installed_models(dest_dir):
        if info.path is not None:
            files[info.name] = info.path
    return files


def _state_from(
    model: RemoteModel,
    files: dict[str, Path],
    ledger: dict[str, dict],
) -> str:
    """三种状态的判据（供 :func:`installed_state` 和 :func:`build_choices` 共用）。"""
    # 第一步永远是磁盘 —— 账本说装过、文件没了，那就是没装。
    if model.id not in files:
        return STATE_MISSING
    record = ledger.get(model.id)
    if not isinstance(record, dict):
        return STATE_MISSING
    local_sha = normalize_sha256(record.get("sha256"))
    if not model.sha256 or not local_sha:
        # 有一边没有可比的哈希：能确定的只有"装过"。
        return STATE_INSTALLED
    return STATE_INSTALLED if local_sha == model.sha256 else STATE_OUTDATED


def installed_state(
    model: RemoteModel,
    dest_dir: str | Path | None = None,
) -> str:
    """这个模型在本机的状态：``"已装" / "未装" / "待更新"``。

    ★ **磁盘是唯一真相**：账本里记着、磁盘上却找不到那个 ``.gguf``，
      返回的是"未装"。用户手删过模型目录的场景必须显示正确，
      否则界面上会出现一个永远下不动的"已装"。
    """
    return _state_from(model, local_model_files(dest_dir), load_installed(dest_dir))


# ---------------------------------------------------------------------------
# 内存
# ---------------------------------------------------------------------------


class _MemoryStatusEx(ctypes.Structure):
    """``MEMORYSTATUSEX`` 的 ctypes 镜像（字段顺序必须和 Windows 头文件一致）。

    ★ v1.9 B4 起**内存探测的唯一真源是** :func:`core.system_info.total_ram_gb`
      （见 :func:`local_ram_gb`）。这个结构体和它下面那两个模块级 import
      （``ctypes`` / ``os``）**刻意留着**：它们是
      ``tests/test_model_registry.py`` 里两条用例的注入点
      （``mr.ctypes.windll.kernel32.GlobalMemoryStatusEx`` / ``mr.os``），
      删掉会让那两条与本次改动无关的用例报 AttributeError。
      留着不产生第二份探测逻辑 —— :func:`local_ram_gb` 已经不读它了。
    """

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


def local_ram_gb() -> float | None:
    """本机物理内存总量（GB）。测不到返回 ``None``。

    ``None`` 的语义是"我不知道"，**不是**"内存是 0" ——
    调用方（:func:`check_memory`）拿它当放行信号。

    ★ 本函数**转调** :func:`core.system_info.total_ram_gb`（同一个
      ``GlobalMemoryStatusEx``）—— 那是全项目内存探测的唯一真源。
      一个数字两处实现，迟早漂移成"设置页写 16GB、模型中心写 8GB"。
    """
    if getattr(os, "name", "") != "nt":
        # ``MEMORYSTATUSEX`` 是 Windows 专属结构。别的平台上"我不知道"
        # （``None``）就是正确回答 —— 调用方见 None 一律放行。
        return None
    from core.system_info import total_ram_gb

    return total_ram_gb()


def check_memory(
    model: RemoteModel,
    *,
    ram_gb: float | None = None,
) -> tuple[bool, str]:
    """够不够内存跑它。返回 ``(够不够, 人话说明)``。

    * ``min_ram_gb <= 0`` → **一律放行**，而且连探测都不做
      （清单没提要求的模型不该被一个探测失败拖下水）；
    * 传 ``ram_gb`` → 用传进来的数（测试和批量调用用得上，免得反复探测）；
    * 探测返回 ``None`` → 放行，说明里写明"没测到"。
    """
    need = float(model.min_ram_gb or 0.0)
    if need <= 0:
        return True, "这个模型没有声明内存要求"

    have = local_ram_gb() if ram_gb is None else float(ram_gb)
    if have is None:
        return True, "没测到本机内存，先放行（装之前自己看一眼内存）"
    if have + 1e-9 < need:
        return False, f"内存不足：本机 {have:.1f} GB，需要 {need:g} GB"
    return True, f"本机内存 {have:.1f} GB，够跑（需要 {need:g} GB）"


def default_checked(
    models: Iterable[RemoteModel],
    *,
    ram_gb: float | None = None,
    vram_gb: float | None = None,
) -> list[str]:
    """默认勾选的模型 id：**负载等级是「轻松」的那些**。

    ★ v1.9 B4 改了语义（这是有意的）：以前是
      "清单标了 ``defaultCheck`` 且内存够"，现在是
      "本机跑它轻松" —— 清单里的 ``defaultCheck`` 只用于展示（「推荐」角标），
      **一个模型不会因为标了它就在非轻松时被预勾**。
      理由：清单作者写那个标志时不知道用户这台机器是什么配置；
      "默认帮我勾上"在用户眼里就是"程序建议我下这个"，
      而一个「勉强」的模型下回来只会让 AI 功能变慢。

    Args:
        models: 清单（或"清单 + 本机已有"）。
        ram_gb: 本机内存（GB）；``None`` = 探测（见 :func:`load_level`）。
        vram_gb: 本机最大显存（GB）；``None`` = 没查过，需要时才探测。

    Returns:
        默认勾选的 id 列表，顺序和入参一致。
    """
    return [
        model.id
        for model in models
        if load_level(model, ram_gb=ram_gb, vram_gb=vram_gb) == LOAD_EASY
    ]


# ---------------------------------------------------------------------------
# 负载等级（B4）—— 判据写死在这里，别处不许再算一遍
# ---------------------------------------------------------------------------


class _MachineSnapshot:
    """两个探测结果的**最小取值对象**：内存（GB）与最大显存（GB）。

    ★ 为什么不直接用 :class:`core.system_info.SystemInfo`：那一份还带着
      CPU 核数、``probed_gpu`` 这些本模块用不上的字段，而
      :func:`_machine_ram_gb` / :func:`_machine_vram_gb` **只认
      ``total_ram_gb`` / ``max_vram_gb`` 两个属性** —— 于是测试塞一个
      ``SimpleNamespace(total_ram_gb=8.0, max_vram_gb=None)`` 进来就能当
      探测结果用，不必去构造真的 ``SystemInfo``。
    """

    __slots__ = ("total_ram_gb", "max_vram_gb")

    def __init__(self, total_ram_gb: float | None, max_vram_gb: float | None) -> None:
        self.total_ram_gb = total_ram_gb
        self.max_vram_gb = max_vram_gb


def _probe_system() -> Any:
    """探测本机内存 / 显存，返回一个带那两个属性的小对象。

    ★ 单独包一层是为了**测试能整块桩掉**：:func:`load_level` 一旦走到探测，
      :func:`snapshot` 就会起 ``nvidia-smi`` 子进程（几十到几百毫秒）。
      测试要的是判据，不是真去问显卡。

    ★ 内存走 :func:`local_ram_gb` —— **本模块自己的取值入口**（它内部再转调
      ``core.system_info.total_ram_gb``，真源仍然只有一处）。这样"一批只探一次"
      是**可观测**的：桩 ``mr.local_ram_gb`` 就能数出次数；绕开它直接问
      ``system_info`` 的话，那条契约在模块外面就看不见了。

    ★ 显存走 ``snapshot().max_vram_gb``（多卡取最大那块 —— 模型装一块卡上，
      把显存加起来会得出"32GB 能跑 13B"这种假结论）；
      ``snapshot`` 自己有进程内缓存，一次会话里反复问不会反复起子进程。
      拿不到 ``nvidia-smi`` 时它是 ``None``（= 没查到独显），**不是 0**。
    """
    got_ram = local_ram_gb()
    got_vram: float | None = None
    try:
        info = snapshot()
    except Exception:  # noqa: BLE001 - 探测是尽力而为
        info = None
    if info is not None:
        got_vram = _machine_vram_gb(info)
    if got_vram is None:
        try:
            got_vram = discrete_gpu_vram_gb()
        except Exception:  # noqa: BLE001
            got_vram = None
    return _MachineSnapshot(got_ram, got_vram)


def _required_vram_gb(model: RemoteModel) -> float | None:
    """这条模型要多少显存；没要求返回 ``None``。"""
    need = model.min_vram_gb
    if need is None:
        return None
    value = float(need)
    return value if value > 0 else None


def _load_level_of(
    model: RemoteModel,
    have_ram: float | None,
    have_vram: float | None,
) -> str:
    """三档判据的**唯一实现**（:func:`load_level` 负责把两个数字准备好）。

    判据（照施工单逐字实现，不许自己加权重）：

    1. 内存 ≥ ``minRamGb`` **且**（没要求 ``minVramGb`` **或** 有独显且显存 ≥
       ``minVramGb``）→ :data:`LOAD_EASY`；
    2. 否则内存 ≥ ``minRamGb * 0.7`` → :data:`LOAD_TIGHT`；
    3. 否则 → :data:`LOAD_HARD`。

    ★ **探不到内存时不拦**（和 :func:`check_memory` 同一个态度）：
      内存未知时第 1 条按"让行"处理 → 给 :data:`LOAD_EASY`，由
      :func:`load_note` 写明"未探测到，按最低要求估算"。
      反过来判成「困难」才是错的 —— 那会让一台探测失败的机器上
      **一条模型都勾不上**，用户除了自己去改代码没有任何出路。
      注意"显存未知"和"内存未知"不是一回事：显存未知时第 1 条**不满足**
      （"有独显且显存 ≥ 要求"是三个条件与在一起），落到 0.7 倍那条。
    """
    need_ram = float(model.min_ram_gb or 0.0)
    need_vram = _required_vram_gb(model)

    have_ram_known = have_ram is not None
    vram_ok = need_vram is None or (have_vram is not None and have_vram >= need_vram)
    ram_ok = True if have_ram is None else have_ram + 1e-9 >= need_ram

    if ram_ok and vram_ok:
        return LOAD_EASY

    if not have_ram_known:
        # 内存都没测到，0.7 倍那条无从谈起 —— 一律按"不拦"处理。
        return LOAD_EASY
    if have_ram + 1e-9 >= need_ram * RAM_TIGHT_RATIO:
        return LOAD_TIGHT
    return LOAD_HARD


def _machine_ram_gb(system: Any) -> float | None:
    """从探测结果里取内存（GB）；没有就是 ``None``（= 没测到）。"""
    got = getattr(system, "total_ram_gb", None) if system is not None else None
    return None if got is None else float(got)


def _machine_vram_gb(system: Any) -> float | None:
    """从探测结果里取最大显存（GB）；没有就是 ``None``（= 没查到独显）。"""
    got = getattr(system, "max_vram_gb", None) if system is not None else None
    return None if got is None else float(got)


def load_level(
    model: RemoteModel,
    system: Any = None,
    *,
    ram_gb: float | None = None,
    vram_gb: float | None = None,
) -> str:
    """这台机器跑它有多吃力：``LOAD_EASY`` / ``LOAD_TIGHT`` / ``LOAD_HARD``。

    Args:
        model: 清单里的一条。
        system: 已经探测好的 :class:`core.system_info.SystemInfo`
            （批量调用时传进来，免得每条都探一次）。给了它就以它为准。
        ram_gb: 显式指定本机内存（GB）。给了就不再探测。
        vram_gb: 显式指定本机最大显存（GB）。给了就不再探测。

    ★ 探测**最多做一次**：``system`` / 两个显式数字都没有时才去问系统，
      而且只在**这条模型真的声明显存要求**时才需要显存那个数字
      （绝大多数清单条目不写 ``minVramGb``，那就不必起 ``nvidia-smi`` 子进程）。
    """
    need_vram = _required_vram_gb(model)
    if system is None and (ram_gb is None or (vram_gb is None and need_vram is not None)):
        try:
            system = _probe_system()
        except Exception:  # noqa: BLE001 - 探测是尽力而为，失败就当没测到
            system = None
    return _load_level_of(model, _resolved_ram(system, ram_gb), _resolved_vram(system, vram_gb))


def _resolved_ram(system: Any, ram_gb: float | None) -> float | None:
    """``ram_gb`` 优先，没给就从探测结果里取。"""
    return ram_gb if ram_gb is not None else _machine_ram_gb(system)


def _resolved_vram(system: Any, vram_gb: float | None) -> float | None:
    """``vram_gb`` 优先，没给就从探测结果里取（取不到 = 没查到独显）。"""
    return vram_gb if vram_gb is not None else _machine_vram_gb(system)


def load_note(
    model: RemoteModel,
    system: Any = None,
    *,
    ram_gb: float | None = None,
    vram_gb: float | None = None,
) -> str:
    """一句中文，说清"本机多少 / 需要多少"。

    例：``内存 15.4 GB / 需要 8 GB，显卡 11.9 GB / 需要 6 GB``。

    ★ **纯文本，不写 Markdown**（``AGENTS.md`` 铁律 9 / 第 24 节：
      ``QLabel`` 会把 ``**`` 原样画出来）。

    ★ 两句话都不是猜的：内存没测到就说"未探测到…按最低要求估算"，
      显存没要求就说"没有声明显存要求" —— 绝不把"不知道"写成"是 0"。
    """
    need_ram = float(model.min_ram_gb or 0.0)
    need_vram = _required_vram_gb(model)
    probed = system is not None
    if not probed and (ram_gb is None or (vram_gb is None and need_vram is not None)):
        try:
            system = _probe_system()
        except Exception:  # noqa: BLE001
            system = None
        probed = system is not None

    have_ram = _resolved_ram(system, ram_gb)
    have_vram = _resolved_vram(system, vram_gb)

    parts: list[str] = []
    if need_ram > 0:
        if have_ram is None:
            parts.append(
                f"未探测到本机内存 / 需要 {need_ram:g} GB，按最低要求估算"
                if probed
                else f"内存未指定 / 需要 {need_ram:g} GB"
            )
        else:
            parts.append(f"内存 {have_ram:.1f} GB / 需要 {need_ram:g} GB")

    if need_vram is not None:
        if have_vram is None:
            # ★ 探测过才敢说"没查到独显"；没探测就只说"未探测到"。
            parts.append(
                f"未查到独立显卡显存 / 需要 {need_vram:g} GB，集显会从内存里借"
                if probed
                else f"显存未指定 / 需要 {need_vram:g} GB"
            )
        else:
            parts.append(f"显卡 {have_vram:.1f} GB / 需要 {need_vram:g} GB")
    elif probed or ram_gb is not None or vram_gb is not None:
        parts.append(VRAM_OPTIONAL_REMINDER)

    return "，".join(parts)


# ---------------------------------------------------------------------------
# 合成：一条模型在界面上需要的全部信息
# ---------------------------------------------------------------------------


@dataclass
class ModelChoice:
    """界面上一行模型需要的全部信息。

    ================  ====================================================
    字段               说明
    ================  ====================================================
    ``model``         清单里的那条（或本机补进来的那条）
    ``state``         ``"已装" / "未装" / "待更新"``
    ``memory_ok``     本机内存够不够跑它（**内存闸门**，看 ``minRamGb``）
    ``memory_note``   为什么够 / 为什么不够 —— 直接当界面文案用
    ``load_level``    **负载等级**三档（B4）—— 看内存 + 显存两条要求
    ``load_note``     本机多少 / 需要多少（中文一句）
    ``checked``       界面上这个勾**当前**勾没勾上
    ================  ====================================================

    ★ ``checked`` 是"当前状态"，和 :attr:`checked_by_default`
      （"打开对话框时的初始状态"）是两件事。
      :func:`build_choices` 会把 ``checked`` 初始化为默认勾选，
      用户之后改的是 ``checked``。

    ★ ``memory_ok`` 和 ``load_level`` **不是一回事，别搞混**（施工单点名的坑）：

    * ``memory_ok`` 是**内存闸门**（``AGENTS.md`` 第 31 节）：只看
      ``minRamGb``。不够 → 整行没有 ``ItemIsEnabled``，勾都勾不动。
    * ``load_level`` 是**负载指示**：内存 + 显存一起看，分三档，
      只影响"默认勾不勾"和"下载按钮点不点得动"。
      「勉强」和「困难」的行**照样可选可勾**（用户有权自己决定），
      只是不替他预勾 + 「困难」时下载按钮置灰。
    """

    model: RemoteModel
    state: str = STATE_MISSING
    memory_ok: bool = True
    memory_note: str = ""
    checked: bool = False
    load_level: str = LOAD_EASY
    load_note: str = ""

    @property
    def checked_by_default(self) -> bool:
        """默认勾选 = **本机跑它轻松**（``LOAD_EASY``）。

        ★ v1.9 B4 起不再看清单的 ``defaultCheck`` —— 见
          :func:`default_checked` 的说明。
        """
        return self.load_level == LOAD_EASY

    @property
    def load_text(self) -> str:
        """负载那一列的中文（``"轻松" / "勉强" / "困难"``）。"""
        return LOAD_LABELS.get(self.load_level, LOAD_LABELS[LOAD_EASY])

    @property
    def load_color(self) -> str:
        """负载指示用的十六进制颜色（界面画那个彩色圆点）。"""
        return LOAD_COLORS.get(self.load_level, LOAD_COLORS[LOAD_EASY])

    @property
    def runnable(self) -> bool:
        """这台机器**大概率跑得动**它：``easy`` 或 ``tight``。

        ★ 「困难」不等于不能下 —— 用户可能就是想在大内存机器上先下好、
          也可能只是想试试。所以 :attr:`selectable` 不看这个属性；
          它只决定"要不要把下载按钮置灰 + 给一句提示"。
        """
        return self.load_level in (LOAD_EASY, LOAD_TIGHT)

    @property
    def selectable(self) -> bool:
        """内存不够的行不许勾 —— 勾了也只是装上一个跑不动的模型。"""
        return bool(self.memory_ok)

    @property
    def status_text(self) -> str:
        """状态列的文字：内存不足时**原因优先**，否则显示安装状态。"""
        if not self.model.url:
            return LOCAL_GROUP
        if not self.memory_ok:
            return self.memory_note or "内存不足"
        return self.state


def build_choices(
    models: Iterable[RemoteModel],
    *,
    dest_dir: str | Path | None = None,
    ram_gb: float | None = None,
    vram_gb: float | None = None,
) -> list[ModelChoice]:
    """把一批模型合成界面能直接用的 :class:`ModelChoice`。

    ★ 内存 / 显存只探测**一次**再分给每条：``GlobalMemoryStatusEx`` 虽然便宜，
      但一批十条就是十次系统调用，而且十次的结果可能不一样
      （中间被别的程序吃掉一截内存），界面上会显得自相矛盾。
      显存更贵 —— 它要起 ``nvidia-smi`` 子进程。

    ★ **没有一条要求显存时，压根不去问显卡**：清单里绝大多数条目都不写
      ``minVramGb``，为它们起子进程纯属白等（实测几十到几百毫秒）。
      传进来的 ``vram_gb`` / ``ram_gb`` 一律优先，测试就靠它注入假配置。
    """
    have = ram_gb
    have_vram = vram_gb
    system = None
    # ★ list() 一次：这里要遍历两遍，而入参可能是生成器。
    batch = list(models)
    needs_vram = any(_required_vram_gb(model) is not None for model in batch)
    if batch and (have is None or (have_vram is None and needs_vram)):
        try:
            system = _probe_system()
        except Exception:  # noqa: BLE001 - 探测失败就当没测到
            system = None
    if have is None:
        have = _machine_ram_gb(system)
    if have_vram is None and needs_vram:
        have_vram = _machine_vram_gb(system)

    files = local_model_files(dest_dir)
    ledger = load_installed(dest_dir)

    choices: list[ModelChoice] = []
    for model in batch:
        memory_ok, memory_note = check_memory(model, ram_gb=have)
        level = _load_level_of(model, have, have_vram)
        choice = ModelChoice(
            model=model,
            state=_state_from(model, files, ledger),
            memory_ok=memory_ok,
            memory_note=memory_note,
            load_level=level,
            load_note=load_note(model, ram_gb=have, vram_gb=have_vram),
        )
        # ★ 默认勾选还要**有下载地址**：``merged_with_local()`` 补进来的
        #   "本机已有"条目没有 url，勾上它只会让用户白点一次下载
        #   （``describe_download_plan()`` 压根算不出它的体积）。
        #   负载等级 = 轻松是**必要条件**，不是全部条件。
        choice.checked = choice.checked_by_default and bool(model.url)
        choices.append(choice)
    return choices


def group_choices(
    choices: Iterable[ModelChoice],
) -> list[tuple[str, list[ModelChoice]]]:
    """按 ``group`` 分组。``"推荐"`` 永远排第一，其余按出现顺序。

    ★ "推荐"置顶是刻意的：第一次打开对话框的人不该自己去一堆分组里找
      "我该下哪个"。清单作者说推荐哪几个，哪几个就在最上面。
    """
    buckets: dict[str, list[ModelChoice]] = {}
    for choice in choices:
        buckets.setdefault(choice.model.group_or_default, []).append(choice)

    groups: list[tuple[str, list[ModelChoice]]] = []
    if DEFAULT_GROUP in buckets:
        groups.append((DEFAULT_GROUP, buckets.pop(DEFAULT_GROUP)))
    groups.extend(buckets.items())
    return groups


def merged_with_local(
    models: Iterable[RemoteModel],
    *,
    dest_dir: str | Path | None = None,
) -> list[RemoteModel]:
    """把"本机已有、但远端清单没列"的模型补进列表。

    ★ 为什么要有这一步：用户可能换过机器、用过老版本的清单，
      或者清单作者把某个模型下架了。如果界面上只剩远端清单，
      这些文件就会变成"看不见但占着几个 GB"的幽灵 ——
      用户既不知道它们在，也没有入口删掉。

    补进来的条目：``group="本机已有"``、``url=""``（没有可下地址）、
    ``default_check=False``（本机已有的东西不需要再下一次）。
    """
    known = {model.id for model in models}
    merged = list(models)

    for name, path in sorted(local_model_files(dest_dir).items()):
        if name in known:
            continue
        known.add(name)
        try:
            size = int(path.stat().st_size)
        except OSError:  # pragma: no cover - 刚扫到的文件突然没了
            size = 0
        merged.append(
            RemoteModel(
                id=name,
                label=name,
                group=LOCAL_GROUP,
                file=path.name,
                url="",
                size=size,
                sha256="",
                min_ram_gb=0.0,
                default_check=False,
                note="这个模型在本机目录里，但远端清单没有列出它。",
            )
        )

    return merged


def describe_download_plan(choices: Iterable[ModelChoice]) -> str:
    """人话汇总这次要下多少，例 ``"已选 2 个模型，共约 2.1 GB"``。

    ★ 判据是 ``choice.checked``（界面上**当前**勾没勾），不是
      ``checked_by_default`` —— 后者只是打开对话框时的初始状态。
      :func:`build_choices` 会把 ``checked`` 初始化成默认勾选，
      所以"直接拿 build_choices 的结果来问"也能得到合理答案。
    """
    picked = [c for c in choices if c.checked and c.model.url]
    if not picked:
        return "还没有勾选任何模型"
    total = sum(int(c.model.size or 0) for c in picked)
    return f"已选 {len(picked)} 个模型，共约 {human_size_bytes(total) or '0B'}"
