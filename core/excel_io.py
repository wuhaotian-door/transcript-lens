"""Excel 读取的**唯一真源** —— 按**内容**挑引擎，而不是按扩展名。

★ 为什么必须有这个模块（实测出来的"紧急 bug"，2026-09-27）
=========================================================

用户报的现象是：**导入一个 ``.xlsx`` 文件，却弹出一个提到 xlrd 的错误。**
听着像胡说，实测完全复现得出来：

===========================  ==========================================  ==================================
文件                          内容                                        ``pd.read_excel(...)`` 的结果
===========================  ==========================================  ==================================
``成绩表.xlsx``               ZIP（真正的 xlsx）                          正常，走 openpyxl
``成绩表.xlsx``               **OLE2**（老格式 .xls 改了扩展名）           ``ImportError: Missing optional
                                                                          dependency 'xlrd'. Install xlrd
                                                                          >= 2.0.1 for xls Excel support
                                                                          Use pip or conda to install xlrd.``
``成绩表.xls``                OLE2                                        同上
===========================  ==========================================  ==================================

**根因**：pandas 不看扩展名，它看**内容的魔数**
（``pandas.io.excel._base.inspect_excel_format``）。碰到 OLE2 头
（``D0 CF 11 E0 A1 B1 1A E1``）就判定成 ``"xls"``，于是去调 **xlrd** ——
而 xlrd 根本没装。于是：

1. 用户导的是 ".xlsx"，看到的却是 xlrd 的错 —— **莫名其妙**；
2. 错误信息里带着 **原始 pip 指令**（``Use pip or conda to install xlrd.``）——
   辅导员不该看到这个（任务书 3.4 / 验收标准 5）。

而这在现实中**一点都不罕见**：教务系统导出的"Excel"有很大一部分是
老格式（BIFF/OLE2），只是文件名被存成了 ``.xlsx``。

★ 为什么显式指定引擎是**必须**的，不是锦上添花
=============================================

实测（同一天，xlrd 2.0.2 已安装）：

* ``pd.read_excel("真的.xls")`` —— 即使 xlrd 装好了，对**裸 BIFF 流**
  （没有 OLE2 外壳的 Excel 2.0/3.0 文件）依然报
  ``ValueError: Excel file format cannot be determined, you must specify an
  engine manually.``
* 加上 ``engine="xlrd"`` —— 立刻正常读出 ``Name/Score/Zhang/91``。

所以"按扩展名/内容选引擎"这件事，pandas 自己**做不全**；我们得替它做。

★ 铁律一：``core/parser.py`` 一个字符都不许动
============================================

charter v1.8 第七节写的是"``core/parser.py``：``pd.read_excel(path,
engine='openpyxl')`` 显式指定"，可第三节铁律 1 又写着
"calculator/parser/validator/tests 一个字符不许改"。**文档自己打架。**

这里的解法是：**一个字都不动 parser.py，但让每一次 ``pd.read_excel`` 调用
都带上显式 engine。** 因为 ``core/parser.parse_excel`` 的签名里本来就有
``**read_excel_kwargs``，并且原样转发给 ``pd.read_excel`` ——
那个扩展点就是为这种情况留的::

    # core/parser.py:947（冻结，未改动）
    df = pd.read_excel(path, sheet_name=sheet_name, header=None, **read_excel_kwargs)

调用方 ::

    parse_excel(path, engine=engine_for(path))     # ← 到达 pd.read_excel 的就是 engine='xlrd'

于是"显式指定"这条要求**逐字满足**，而冻结文件零改动。两边都不用让步。

★ 引擎版本约束（实测，不是抄来的）
================================

* ``openpyxl`` —— pandas 2.3.3 的默认 ``.xlsx`` reader。
* ``xlrd`` —— pandas 2.3.3 的 ``.xls`` reader，**要求 >= 2.0.1**。

  ★ 任务书 3.2 写的是 ``xlrd<2.0``。**那个版本装上去只会更坏**，实测：

  ===================  ==========================================================
  xlrd 版本             用 pandas 2.3.3 读 .xls 的结果
  ===================  ==========================================================
  1.2.0（``<2.0``）     ``ImportError: Pandas requires version '2.0.1' or newer
                        of 'xlrd' (version '1.2.0' currently installed).``
  2.0.2               正常读出
  ===================  ==========================================================

  而且 1.2.0 那条例外里**同样带着版本指令** —— 等于花力气把错误信息换了个写法。
  所以这里按实测取 ``xlrd>=2.0.1``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "ENGINE_CSV",
    "ENGINE_OPENPYXL",
    "ENGINE_XLRD",
    "MIN_XLRD_VERSION",
    "ExcelEngineError",
    "MissingEngineError",
    "available_engines",
    "engine_for",
    "friendly_error",
    "minimum_xlrd_ok",
    "read_frame",
    "sanitize_message",
    "sheet_names",
    "sniff_engine",
    "xlrd_version",
]

ENGINE_OPENPYXL = "openpyxl"
ENGINE_XLRD = "xlrd"
ENGINE_CSV = "csv"

#: ZIP 容器头 = 真正的 Office Open XML（.xlsx / .xlsm）
ZIP_MAGIC = b"PK\x03\x04"

#: OLE2 复合文档头 —— 老格式 .xls（BIFF5/8）的外壳
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: 裸 BIFF 流（Excel 2.0/3.0/4.0，**没有 OLE2 外壳**）的 BOF 记录。
#:
#: ★ 别把这一串抄错 —— 第一版我写成 ``09 00 04 00 00 00 10 00``，
#:   第 5~6 字节是 **version=0x0002**（不是 0 填充），于是真文件
#:   （``09 00 04 00 02 00 10 00``）一个都没匹配上，全部掉进扩展名分支。
#:   BOF = 记录类型 2 字节 + 长度 2 字节 + version 2 字节 + 子类型 2 字节。
BIFF_BOF_TYPES = (b"\x09\x00", b"\x09\x02", b"\x09\x04", b"\x09\x08")
BIFF_BOF_VERSIONS = (b"\x02\x00", b"\x03\x00", b"\x04\x00", b"\x05\x00", b"\x06\x00")

#: ``sanitize_message`` 要抹掉的东西 —— 用户不该看到包管理指令。
_RAW_INSTALL_HINTS = (
    "use pip or conda to install",
    "pip install",
    "conda install",
    "install xlrd",
    "missing optional dependency",
)


def _looks_like_raw_biff(head: bytes) -> bool:
    """裸 BIFF 流（没有 OLE2 外壳的老 Excel）。"""
    return head[:2] in BIFF_BOF_TYPES and head[2:4] in BIFF_BOF_VERSIONS


class ExcelEngineError(RuntimeError):
    """读不了这个表。``str(exc)`` **已经是可以直接显示给用户的话**。

    ★ 分这个类的意义：调用方只要 ``except ExcelEngineError as exc:
      QMessageBox.critical(..., str(exc))`` 就够了，**不需要**再判断
      "这句能不能给用户看" —— 能，:func:`friendly_error` 已经过滤过了。
    """


class MissingEngineError(ExcelEngineError):
    """这个格式需要某个引擎，而它没装。"""


def available_engines() -> set[str]:
    """当前环境里真的能 import 的引擎集合。"""
    import importlib.util

    found: set[str] = set()
    for name in (ENGINE_OPENPYXL, ENGINE_XLRD):
        try:
            if importlib.util.find_spec(name) is not None:
                found.add(name)
        except (ImportError, ValueError):  # pragma: no cover - 环境损坏
            pass
    return found


#: pandas 2.x 要求的最低 xlrd 版本。**实测出来的**，不是抄文档的：
#: 装 xlrd 1.2.0 时 pandas 2.3.3 直接抛
#: ``ImportError: Pandas requires version '2.0.1' or newer of 'xlrd'``。
MIN_XLRD_VERSION = (2, 0, 1)


def xlrd_version() -> str:
    """已安装的 xlrd 版本号；没装返回空串。"""
    try:
        import xlrd
    except ImportError:
        return ""
    return str(getattr(xlrd, "__version__", "") or "")


def minimum_xlrd_ok() -> bool:
    """:func:`xlrd_version` 满不满足 pandas 的底线（没装→``False``）。"""
    text = xlrd_version()
    if not text:
        return False
    import re as _re

    match = _re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:  # pragma: no cover - 版本号格式没见过
        return False
    parts = tuple(int(part) for part in match.groups(default="0"))
    return parts >= MIN_XLRD_VERSION


def _head(path: str | Path, size: int = 8) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def sniff_engine(path: str | Path) -> str:
    """按**内容**判断该用哪个引擎。

    ★ 顺序有讲究：先看魔数，再看扩展名。反过来的话，
      一个"扩展名写着 .xlsx、内容其实是 OLE2"的文件会被送进 openpyxl，
      报一句 ``BadZipFile: File is not a zip file`` —— 用户照样看不懂。

    Args:
        path: 文件路径。

    Returns:
        :data:`ENGINE_OPENPYXL` / :data:`ENGINE_XLRD` / :data:`ENGINE_CSV`。

    Raises:
        ExcelEngineError: 既认不出内容、扩展名也不是已知的 Excel/CSV。
    """
    text = str(path)
    if text.lower().endswith(".csv"):
        return ENGINE_CSV

    head = _head(path)
    if head.startswith(ZIP_MAGIC):
        return ENGINE_OPENPYXL
    if head.startswith(OLE2_MAGIC):
        # ★ 就是这一条修掉了"导 .xlsx 却报 xlrd 错"：
        #   老格式无论叫什么名字，都明确走 xlrd。
        return ENGINE_XLRD
    if _looks_like_raw_biff(head):
        return ENGINE_XLRD

    lower = text.lower()
    if lower.endswith((".xlsx", ".xlsm")):
        return ENGINE_OPENPYXL
    if lower.endswith(".xls"):
        return ENGINE_XLRD
    raise ExcelEngineError(
        f"认不出这个文件的格式：{Path(text).name}\n\n"
        "支持的格式是 .xlsx / .xlsm / .xls / .csv。\n"
        "如果它确实是一份成绩表，请用 Excel 或 WPS 打开它，"
        "「另存为」成 .xlsx 之后再导入。"
    )


def engine_for(path: str | Path) -> str:
    """:func:`sniff_engine` 再**确认引擎真的可用**。返回值可直接传给 pandas。

    Raises:
        MissingEngineError: 认得出格式，但对应引擎没装。
    """
    engine = sniff_engine(path)
    if engine == ENGINE_CSV:
        return engine
    if engine not in available_engines():
        raise MissingEngineError(_missing_engine_message(engine))
    return engine


def _missing_engine_message(engine: str) -> str:
    """引擎缺失时给用户看的话。**刻意不写包管理指令。**"""
    if engine == ENGINE_XLRD:
        return (
            "这是一份「老格式」的 Excel 文件（.xls，Excel 97-2003）。"
            "本程序缺少读取老格式的组件，暂时打不开它。\n\n"
            "两个办法，任选其一：\n"
            "1. 用 Excel 或 WPS 打开它，「另存为」成 .xlsx 格式再导入（推荐）；\n"
            "2. 用 Excel 打开后「另存为」CSV（逗号分隔）再导入。\n\n"
            "如果这是打包版程序，请联系开发者重新打包（读取组件没打进去）。"
        )
    return (
        "本程序缺少读取 .xlsx 的组件，暂时打不开这个文件。\n\n"
        "请先用 Excel 或 WPS 把它「另存为」成 CSV 再导入；"
        "如果这是打包版程序，请联系开发者重新打包。"
    )


def sanitize_message(text: str) -> str:
    """把任何**包管理指令**从一句话里抹掉（纵深防御的最后一道）。

    ★ 为什么还要这一步：上面几条消息是我们自己写的，当然干净。但
      ``friendly_error`` 会把**原始异常**的文本拼进兜底消息里，而
      pandas / openpyxl / xlrd 的原话里就带 ``Use pip or conda to install…``。
      过滤放在**出口**（而不是指望每个分支都记得清理），
      这样以后新加分支也不会漏。
    """
    cleaned = " ".join(str(text).split())
    low = cleaned.lower()
    for hint in _RAW_INSTALL_HINTS:
        index = low.find(hint)
        while index >= 0:
            # 从提示词开始，砍到句末（pandas 的原文就是一句话）
            end = cleaned.find(".", index)
            end = len(cleaned) if end < 0 else end + 1
            cleaned = (cleaned[:index] + cleaned[end:]).strip()
            low = cleaned.lower()
            index = low.find(hint)
    return cleaned.strip()


def friendly_error(exc: BaseException, path: str | Path = "") -> str:
    """把任何读取异常翻译成**能给辅导员看**的一段话。

    签名收 ``BaseException`` 而不是 ``Exception``：调用点常常是
    ``except Exception``，但也要经得起 ``KeyboardInterrupt`` 之类被误传进来。

    Returns:
        纯文本（QLabel / QMessageBox 直接显示，**不含 Markdown 记号**，
        也**不含包管理指令** —— charter 铁律 9 + 验收标准 5）。
    """
    name = Path(str(path)).name or "这个文件"
    kind = type(exc).__name__
    raw = sanitize_message(str(exc))

    if isinstance(exc, MissingEngineError):
        return sanitize_message(str(exc))

    lowered = raw.lower()

    # 引擎/依赖类 —— 即使不是我们主动抛的，也别说 pip
    if "xlrd" in lowered or "openpyxl" in lowered or isinstance(exc, ImportError):
        return sanitize_message(
            f"打不开「{name}」：缺少读取这种 Excel 格式的组件。\n\n"
            "请用 Excel 或 WPS 打开它，「另存为」成 .xlsx 或 CSV 之后再导入。"
        )

    # ★ xlrd 自己的异常：文件不是有效的 OLE2/Biff 复合文档。
    #   不加这一条的话会掉进兜底分支，把 `CompDocError: Expected "little-endian"
    #   marker, found b'\x00\x00'` 这种原文摆给辅导员看 —— 那不是人话。
    if kind in ("XLRDError", "CompDocError"):
        return sanitize_message(
            f"「{name}」不是有效的 Excel 老格式文件（.xls）。\n\n"
            "请用 Excel 或 WPS 打开它，确认能正常显示之后"
            "「另存为」成 .xlsx 再导入。"
        )

    # 文件被占用 —— Windows 上最常见的一种（Excel/WPS 开着它）
    if isinstance(exc, PermissionError) or "permission" in lowered:
        return sanitize_message(
            f"打不开「{name}」：文件正被别的程序占用。\n\n"
            "请先关闭 Excel / WPS 里打开的这个文件，再试一次。"
        )

    # 根本不是 xlsx/xls（zip / OLE2 都对不上）
    if kind in ("BadZipFile", "InvalidFileException"):
        return sanitize_message(
            f"「{name}」不是有效的 Excel 文件。\n\n"
            "请确认它没有损坏，并且确实是 .xlsx / .xls。"
            "必要的话用 Excel 或 WPS 打开后重新「另存为」一次。"
        )

    # 空表 / 读不出内容
    if kind == "EmptyDataError" or "no columns to parse" in lowered:
        return sanitize_message(f"「{name}」里没有任何内容，读不出成绩数据。")

    # 兜底：保留原始信息（排查要用），但**先说人话**
    return sanitize_message(
        f"打不开「{name}」。请先确认：\n"
        "1. 文件没有被 Excel / WPS 打开（占用会导致读取失败）；\n"
        "2. 文件格式是 .xlsx / .xlsm / .xls / .csv；\n"
        "3. 文件本身没有损坏。\n\n"
        f"详细错误（{kind}）：{raw or '（无附加信息）'}"
    )


def _engine_chain(primary: str) -> list[str]:
    """真正去试的引擎顺序：认定的那个在前，其它**可用**的在后兜底。

    ★ 为什么要兜底：魔数识别覆盖不了所有情况 —— 实测就有"扩展名 .xlsx、
      内容是裸 BIFF、而 BIFF 的 BOF 版本字节不常见"的文件。那种文件
      openpyxl 一定打不开，但 xlrd 打得开。多试一个的成本是零，
      而"本来能读的文件读不出来"是用户实打实的损失。
    """
    chain = [primary]
    for candidate in (ENGINE_OPENPYXL, ENGINE_XLRD):
        if candidate != primary and candidate in available_engines():
            chain.append(candidate)
    return chain


def read_frame(
    path: str | Path,
    *,
    header: int | None = None,
    dtype: Any = object,
    sheet_name: int | str = 0,
) -> Any:
    """把表读成 DataFrame —— **按内容选引擎**，出错时抛说人话的异常。

    ★ 用 ``with pd.ExcelFile(...)``：Windows 上不显式关闭句柄的话，
      Excel 还开着这个文件时会读失败（踩过）。

    Args:
        path: 文件路径。
        header: 传给 pandas 的 ``header``。``None`` = 不把首行当表头。
        dtype: 传给 pandas 的 ``dtype``。默认 ``object``（保留原样）。
        sheet_name: 工作表名或下标。

    Raises:
        ExcelEngineError: 一切读不了的状况（``str(exc)`` 可直接显示）。
    """
    import pandas as pd

    engine = engine_for(path)          # 可能抛 MissingEngineError（已是人话）
    if engine == ENGINE_CSV:
        try:
            return pd.read_csv(path, header=header, dtype=dtype)
        except Exception as exc:  # noqa: BLE001 - 统一翻译成人话再抛
            raise ExcelEngineError(friendly_error(exc, path)) from exc

    chain = _engine_chain(engine)
    errors: list[tuple[str, BaseException]] = []
    for candidate in chain:
        try:
            with pd.ExcelFile(path, engine=candidate) as book:
                return pd.read_excel(
                    book, sheet_name=sheet_name, header=header, dtype=dtype
                )
        except Exception as exc:  # noqa: BLE001 - 统一翻译成人话再抛
            errors.append((candidate, exc))

    # ★ 报**第一个**引擎的错误，不报最后一个：
    #   第一个才是"这个文件的魔数/扩展名说自己是什么"，它的话最贴近真相。
    #   报兜底引擎的错误会把用户往错误方向带（比如一个坏掉的 xlsx
    #   最终报出 xlrd 的话，正是我们要修的那个 bug 的翻版）。
    first_engine, first_error = errors[0]
    raise ExcelEngineError(friendly_error(first_error, path)) from first_error


def sheet_names(path: str | Path) -> list[str]:
    """列出工作表名（出错时抛人说人话的 :class:`ExcelEngineError`）。"""
    import pandas as pd

    engine = engine_for(path)
    if engine == ENGINE_CSV:
        return []
    try:
        with pd.ExcelFile(path, engine=engine) as book:
            return [str(name) for name in book.sheet_names]
    except Exception as exc:  # noqa: BLE001
        raise ExcelEngineError(friendly_error(exc, path)) from exc
