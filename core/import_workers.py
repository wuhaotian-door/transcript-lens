"""批量导入德育分（v1.9）。

**为什么单独一个模块**：「保存德育分」按钮是**一人一格**手填的（0-100 的整数
微调框）。一个班四五十人还能忍，一个年级几百人就得从 Excel 抄 —— 而抄的过程
一定会出错，且没人核对得完。这个模块负责把"一张德育分表"变成"每个班一份德育分"。

**和既有代码的分工（一条都不重写）**：

* 读表 → :func:`core.excel_io.read_frame`（魔数选引擎，.xlsx / .xls / CSV 同一入口）
* 数字解析 → :func:`core.parser.to_float`（认 ``"85.000000"``、千分位、百分号）
* 表头归一 → :func:`core.parser.normalize_header`
* 落库 → :func:`core.scholarship.save_moral`（一个班一个加密文件，键是 ``sid:<学号>``）
* 线程 → 调用方用 :func:`ui.workers.run_async`（**本模块不 import 任何 Qt**）

**四条校验规则**（Owner 定死的封闭清单）：① 学号重复 ② 姓名为空
③ 分值不是数字 ④ 班级还没建过。另外两条不在清单里、但必须也算失败：
学号为空、德育分超出 0-100（写进去只会被 clamp 成错的分，不如不写）。

**两类"跳过"**（不是失败，绝不瞎写）：整行空白、学号不在该班名单里。

★ **为什么不静默**：跳过和失败都写进 ``export/import_errors_<batch>.csv``，
并在汇总里逐条报数。批量导入最危险的不是报错，而是"少导入了几个人、
但没人知道" —— 少算一个人就是奖学金算错一个人。

★ **批量导入不许覆盖得无声无息**：同一批里已经存在的德育分算"覆盖"，
汇总里和"新增"分开报。追加 ≠ 悄悄改分数。
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from core import scholarship
from core.crypto_store import CryptoStore, default_root
from core.excel_io import ExcelEngineError, friendly_error, read_frame, sanitize_message
from core.parser import normalize_header, strip_blank_marker, to_float
from core.user_store import load_user_json, save_user_json

__all__ = [
    "BATCH_FILE",
    "CLASS_HEADER_KEYWORDS",
    "ERROR_CSV_PREFIX",
    "HEADER_SCAN_ROWS",
    "ID_HEADER_KEYWORDS",
    "ImportApplyError",
    "ImportFormatError",
    "ImportPlan",
    "ImportSummary",
    "MAX_BATCH_RECORDS",
    "MORAL_MAX",
    "MORAL_MIN",
    "NAME_HEADER_KEYWORDS",
    "SCORE_HEADER_KEYWORDS",
    "ColumnMap",
    "ErrorRow",
    "RawRow",
    "apply_plan",
    "default_errors_dir",
    "detect_columns",
    "load_batches",
    "new_batch_id",
    "plan_import",
    "read_table",
    "record_batch",
    "run_import",
    "student_key",
    "write_error_csv",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 德育分的合法区间。和 ``ui/pages/scholarship_page.py`` 那个 0-100 的
#: 微调框、以及 ``core.scholarship.save_moral`` 里的 clamp 必须是同一个区间 ——
#: 三处一旦不一致，导入进来的分和手填的分就会是两个东西。
MORAL_MIN = 0
MORAL_MAX = 100

#: 表头关键词。**故意不共用 core.parser 的那几张表**：那边认的是成绩表的
#: 课程/身份列，这边认的是德育分表的四列，混用会让"备注"这类列互相污染。
ID_HEADER_KEYWORDS = ("学号", "学籍号", "学生学号", "考号", "studentid", "sid")
NAME_HEADER_KEYWORDS = ("姓名", "学生姓名", "名字", "考生姓名")
CLASS_HEADER_KEYWORDS = ("班级", "行政班", "所在班级", "班级名称", "班别")
SCORE_HEADER_KEYWORDS = (
    "德育分", "德育成绩", "德育", "品德分", "品德", "分值", "分数", "得分", "分值合计",
)

#: 表头最多往下找几行。德育分表常见"大标题 + 制表日期"两行在前面。
HEADER_SCAN_ROWS = 10

#: 认定为表头行至少要有几个关键词命中（学号 + 德育分是最低配置）。
HEADER_MIN_HITS = 2

#: 错误清单 CSV 的文件名前缀：``import_errors_<batch_id>.csv``。
ERROR_CSV_PREFIX = "import_errors_"

#: 批次台账的文件名。★ 后缀必须是 ``.json.enc``：``user_store.save_user_json``
#: 走的是 ``save_encrypted``，明文 ``.json`` 会被它**当场拒绝**（"磁盘上永远只有
#: 密文"）；这个后缀同时也是 ``scholarship.list_user_json`` 的默认筛选条件。
#: ★ 而且它**绝不能**匹配 ``scholarship._CLASS_FILE_RE``（``class_*.json.enc``，
#: 必须以 ``class_`` 开头），否则 ``list_classes`` 会把它当成一个班级。
BATCH_FILE = "moral_batches.json.enc"

#: 台账最多留最近多少批。台账是"我刚才导了什么"的追溯，不是审计流水，
#: 无上限增长只会让这个文件变成第二个需要维护的索引。
MAX_BATCH_RECORDS = 50

#: 学生 key 的前缀。和 ``ui.intentions.student_key`` 是同一条规矩：
#: 有学号用 ``sid:<学号>``，没有才退回 ``name:<姓名>``。
#: ★ core 不 import ui（分层），所以这里复制一份最小实现；
#: ``tests/test_import.py`` 里有一条断言盯着两边不许漂。
KEY_PREFIX_SID = "sid:"
KEY_PREFIX_NAME = "name:"


class ImportFormatError(Exception):
    """文件读不进来 / 表头认不出来。消息是给辅导员看的人话。"""


class ImportApplyError(Exception):
    """落库失败（账户没了、加密盘写不进去）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def student_key(student_id: str = "", student: str = "") -> str:
    """算学生标识：有学号用 ``sid:<学号>``，没有才退回 ``name:<姓名>``。

    ★ 和 :func:`ui.intentions.student_key` 必须算出同一个结果 —— 德育分是按
    key 存的，两边一旦不一致，界面上的"已录入"和导入进去的分就永远对不上。
    """
    sid = (student_id or "").strip()
    if sid:
        return f"{KEY_PREFIX_SID}{sid}"
    return KEY_PREFIX_NAME + (student or "").strip()


def _is_blank(value: Any) -> bool:
    """空单元格判据。``None``、``""``、``NaN`` 都算空（NaN 自己不等于自己）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return value != value
    return False


def _cell_text(value: Any) -> str:
    """单元格转成用于**匹配**的文本。

    ★ 学号是命门：Excel 里的学号是数字，pandas 读出来是 ``2023001.0``，
      直接 ``str()`` 会变成 ``"2023001.0"``，和界面上的 ``sid:2023001``
      永远匹配不上。整数值的 float 必须去掉小数点尾巴。
    """
    if _is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def new_batch_id() -> str:
    """批次号：``20261001-153012-ab12cd``（时间 + 6 位随机，一眼能看出先后）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def default_errors_dir() -> Path:
    """错误清单默认目录：``%LOCALAPPDATA%\\SGMS\\export``。

    ★ 不用当前工作目录：打包成 exe 之后，工作目录是"用户从哪儿双击的"，
      可能是只读的 Program Files，也可能每次都变 —— 用户找不到就等于没写。
    """
    return default_root() / "export"


# ---------------------------------------------------------------------------
# 一、读表 + 认列
# ---------------------------------------------------------------------------


@dataclass
class ColumnMap:
    """一张德育分表的四列在第几列（0-based），以及表头在第几行。"""

    header_row: int
    id_col: int | None = None
    name_col: int | None = None
    class_col: int | None = None
    score_col: int | None = None
    hits: int = 0

    @property
    def missing(self) -> list[str]:
        """缺哪些**必需**列。班级列可以缺（由界面指定目标班级兜底）。"""
        out: list[str] = []
        if self.id_col is None:
            out.append("学号")
        if self.name_col is None:
            out.append("姓名")
        if self.score_col is None:
            out.append("德育分")
        return out

    def describe(self) -> str:
        """人话描述，用于预览界面回显"我认出了哪几列"。"""
        def name(col: int | None, label: str) -> str:
            return f"{label}=第{col + 1}列" if col is not None else f"{label}=没找到"
        return "，".join((
            name(self.id_col, "学号"),
            name(self.name_col, "姓名"),
            name(self.class_col, "班级"),
            name(self.score_col, "德育分"),
        ))


def _match_header(text: str, keywords: Sequence[str]) -> bool:
    if not text:
        return False
    squeezed = "".join(text.split()).lower()
    return any(kw in squeezed for kw in keywords)


def detect_columns(df: Any) -> ColumnMap:
    """在前 ``HEADER_SCAN_ROWS`` 行里找表头行，并认出众数第二多的四列。

    ★ 打分制而不是"第一行就是表头"：教务和辅导员手工做的表十有八九前面挂着
      大标题、制表日期、"填表说明"。命中关键词最多的那一行才是表头。
    ★ 同一类关键词命中多列时取**最左**那列（``max`` 的 ``hits`` 相同时保留先
      出现的），因为真正的学号列一定在"学号（备用）"这类列左边。

    Raises:
        ImportFormatError: 一行都没命中，或命中了但缺必需列。
    """
    limit = min(int(df.shape[0]), HEADER_SCAN_ROWS)
    best_row = -1
    best_score = 0
    best_cols: dict[str, list[int]] = {}

    for r in range(limit):
        row = df.iloc[r]
        found: dict[str, list[int]] = {"id": [], "name": [], "class": [], "score": []}
        for c in range(len(row)):
            text = normalize_header(row.iloc[c])
            if _match_header(text, ID_HEADER_KEYWORDS):
                found["id"].append(c)
            if _match_header(text, NAME_HEADER_KEYWORDS):
                found["name"].append(c)
            if _match_header(text, CLASS_HEADER_KEYWORDS):
                found["class"].append(c)
            if _match_header(text, SCORE_HEADER_KEYWORDS):
                found["score"].append(c)
        score = sum(1 for v in found.values() if v)
        if score > best_score:
            best_score = score
            best_row = r
            best_cols = found

    if best_row < 0 or best_score < HEADER_MIN_HITS:
        raise ImportFormatError(
            "认不出表头。这张表至少要有一列「学号」和一列「德育分」"
            "（姓名列也要有）。请把表头和数据放在前几行里，"
            "别把标题、说明文字混在列中间。"
        )

    def first(key: str) -> int | None:
        cols = best_cols.get(key) or []
        return cols[0] if cols else None

    columns = ColumnMap(
        header_row=best_row,
        id_col=first("id"),
        name_col=first("name"),
        class_col=first("class"),
        score_col=first("score"),
        hits=best_score,
    )
    if columns.missing:
        raise ImportFormatError(
            f"表头认出来了（第 {best_row + 1} 行），但缺这些列："
            f"{'、'.join(columns.missing)}。"
        )
    return columns


def read_table(path: str | Path, *, sheet_name: int | str = 0) -> Any:
    """读成 DataFrame（无表头，原样读进来）。引擎由 :mod:`core.excel_io` 按魔数选。

    Raises:
        ImportFormatError: 文件不存在、引擎缺失、或被加密/占用。
    """
    target = Path(path)
    if not target.is_file():
        raise ImportFormatError(f"找不到文件：{target}")
    try:
        return read_frame(target, header=None, sheet_name=sheet_name)
    except ExcelEngineError as exc:
        # friendly_error / sanitize_message 会把「pip install xlrd」这类
        # 指令换成辅导员看得懂的话（红线：界面不许教用户装包）。
        raise ImportFormatError(sanitize_message(friendly_error(exc, target))) from exc
    except (OSError, ValueError) as exc:
        raise ImportFormatError(f"读不了这个文件：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# 二、逐行校验
# ---------------------------------------------------------------------------


@dataclass
class RawRow:
    """一行原始数据（还没判过对错）。``row_index`` 是表内 0-based 行号。

    ``key`` 是落库前才填上去的学生标识（查过班级名单之后），
    校验阶段一律为空 —— 那时还不知道这个学号对应哪个 key。
    """

    row_index: int
    student_id: str = ""
    student: str = ""
    class_name: str = ""
    score_text: str = ""
    score: float | None = None
    key: str = ""


@dataclass
class ErrorRow:
    """一行没进去的数据。``kind`` 是 ``"失败"`` 或 ``"跳过"``。

    ★ 跳过也要写进 CSV：辅导员拿到的必须是一张"这些人我没动"的名单，
      而不是一句"部分行没导入"。
    """

    row_index: int
    kind: str
    reason: str
    student_id: str = ""
    student: str = ""
    class_name: str = ""
    score_text: str = ""

    def as_csv_row(self) -> list[str]:
        return [
            str(self.row_index + 1),          # 给人看的行号从 1 开始
            self.kind,
            self.reason,
            self.student_id,
            self.student,
            self.class_name,
            self.score_text,
        ]


CSV_HEADER = ["表内行号", "类型", "原因", "学号", "姓名", "班级", "原始分值"]


@dataclass
class ImportPlan:
    """校验完的计划：能写的、不能写的、要提醒的，全在这儿。"""

    path: Path
    columns: ColumnMap
    rows: list[RawRow] = field(default_factory=list)
    errors: list[ErrorRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rounded: list[str] = field(default_factory=list)
    blank_rows: int = 0
    total_rows: int = 0

    @property
    def failures(self) -> list[ErrorRow]:
        return [e for e in self.errors if e.kind == "失败"]

    @property
    def skipped(self) -> list[ErrorRow]:
        return [e for e in self.errors if e.kind == "跳过"]

    @property
    def classes(self) -> list[str]:
        """这一批会写到哪些班（去重且保持出现顺序）。"""
        out: list[str] = []
        for row in self.rows:
            if row.class_name not in out:
                out.append(row.class_name)
        return out


def plan_import(
    path: str | Path,
    *,
    sheet_name: int | str = 0,
    known_classes: Sequence[str] = (),
    default_class: str = "",
    frame: Any = None,
) -> ImportPlan:
    """把一张表校验成一个 :class:`ImportPlan`（**只读，不动库**）。

    Args:
        known_classes: 已经建过的班级名。空列表 = "一个班都没有"，
            这时每一行都会以"班级还没建过"失败 —— 这是对的，
            没有班级快照就没有学号名单，德育分存进去没人认领。
        default_class: 表里没有班级列时，整批写进这个班。
        frame: 已经读好的 DataFrame（测试/预览复用，省一次磁盘读）。

    Returns:
        :class:`ImportPlan`。校验失败的行进 ``errors``，**不抛异常**（不中断整批）。
    """
    target = Path(path)
    df = read_table(target, sheet_name=sheet_name) if frame is None else frame
    columns = detect_columns(df)
    known = {str(name).strip() for name in known_classes if str(name).strip()}
    plan = ImportPlan(path=target, columns=columns)

    if df.shape[0] <= columns.header_row + 1:
        plan.warnings.append("这张表只有表头，没有数据行。")
        return plan

    seen: dict[str, int] = {}
    fallback_class = (default_class or "").strip()

    for r in range(columns.header_row + 1, df.shape[0]):
        cells = df.iloc[r]

        def cell(col: int | None) -> Any:
            """取第 col 列（``None`` = 这一列不存在）。越界一律当空。"""
            if col is None or col >= len(cells):
                return None
            return cells.iloc[col]

        raw_id = _cell_text(cell(columns.id_col))
        raw_name = _cell_text(cell(columns.name_col))
        raw_class = _cell_text(cell(columns.class_col)) or fallback_class
        raw_score_text = _cell_text(cell(columns.score_col))
        plan.total_rows += 1

        if all(_is_blank(cell(c)) for c in (columns.id_col, columns.name_col,
                                            columns.class_col, columns.score_col)):
            # 整行空白（Excel 的幽灵空行）。算跳过，不算失败 —— 但也报数，
            # 因为"表里有 300 行"和"真有 297 个人"是两件事。
            plan.blank_rows += 1
            plan.errors.append(ErrorRow(r, "跳过", "整行是空的"))
            continue

        reason = ""
        if not raw_id:
            reason = "学号为空"
        elif not raw_name:
            reason = "姓名为空"
        elif not raw_class:
            reason = "这一行没有班级，文件里也没有班级列，界面上也没选目标班级"
        elif raw_class not in known:
            # ★ 这里**不写 ``if known and ...``**：一个班都没有的时候，每一行都
            #   该以"班级还没建过"失败。放过去的话，德育分会写进一个没有班级
            #   快照的班里 —— 那个班的学号名单根本不存在，界面按名单取分，
            #   这些分永远不会显示在任何人身上（数据进去了，人却看不到）。
            if known:
                reason = f"班级「{raw_class}」还没建过（先在智育分析里导入这个班的成绩）"
            else:
                reason = ("一个班都还没建过。先在智育分析里把这个班的成绩导进来，"
                          "这里才有学号名单可以对应")
        else:
            duplicate = seen.get(f"{raw_class}|{raw_id}")
            if duplicate is not None:
                reason = f"学号重复（第 {duplicate + 1} 行已经出现过）"

        value: float | None = None
        if not reason:
            if _is_blank(cell(columns.score_col)) or strip_blank_marker(
                    raw_score_text) is None:
                reason = "德育分是空的"
            else:
                value = to_float(raw_score_text)
                if value is None or value != value:      # NaN 也不等于自己
                    reason = f"德育分「{raw_score_text}」不是数字"
                elif not (MORAL_MIN <= value <= MORAL_MAX):
                    reason = (f"德育分 {value:g} 超出 {MORAL_MIN}-{MORAL_MAX}，"
                              f"写进去只会变成错的分")

        if reason:
            plan.errors.append(ErrorRow(
                row_index=r, kind="失败", reason=reason, student_id=raw_id,
                student=raw_name, class_name=raw_class, score_text=raw_score_text,
            ))
            continue

        assert value is not None
        rounded = int(round(value))          # 见下方注释：和手填那条路同一个取整
        if rounded != value:
            plan.rounded.append(raw_id or raw_name)
        seen[f"{raw_class}|{raw_id}"] = r
        plan.rows.append(RawRow(
            row_index=r, student_id=raw_id, student=raw_name,
            class_name=raw_class, score_text=raw_score_text, score=float(rounded),
        ))

    if plan.rounded:
        plan.warnings.append(
            f"有 {len(plan.rounded)} 行德育分是小数，已按整数取整"
            f"（德育分本来就是 0-100 的整数）：{'、'.join(plan.rounded[:10])}"
            + ("…" if len(plan.rounded) > 10 else "")
        )
    if plan.blank_rows:
        plan.warnings.append(f"表里有 {plan.blank_rows} 行是空的，已跳过。")
    return plan


# 取整一律用内置 round()（银行家舍入），和「保存德育分」按钮那条路
# （ui/pages/scholarship_page.py 里的 int(round(float(text)))）**完全一致**：
# 同一份数据，手填和导入必须算出同一个分数，否则"谁抄错了"永远说不清。


# ---------------------------------------------------------------------------
# 三、错误清单 CSV
# ---------------------------------------------------------------------------


def write_error_csv(errors: Sequence[ErrorRow], path: str | Path) -> Path | None:
    """把失败/跳过清单写成 CSV。**没有错误行时不建文件**（返回 ``None``）。

    ★ 编码用 ``utf-8-sig``（带 BOM）：Excel 打开无 BOM 的 UTF-8 CSV 会把
      中文全显示成乱码 —— 这份清单唯一的用途就是让人用 Excel 打开看。
    """
    if not errors:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for row in errors:
            writer.writerow(row.as_csv_row())
    return target


# ---------------------------------------------------------------------------
# 四、落库
# ---------------------------------------------------------------------------


@dataclass
class ImportSummary:
    """一批导入的结果。界面上的"成功 N 行 / 跳过 M 行 / 失败 K 行"就来自这里。"""

    batch_id: str = ""
    source: str = ""
    total: int = 0
    added: int = 0
    updated: int = 0
    failed: int = 0
    skipped: int = 0
    blank: int = 0
    classes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors_path: Path | None = None

    @property
    def succeeded(self) -> int:
        return self.added + self.updated

    def as_text(self) -> str:
        """纯文本汇总（界面直接显示）。**不写 Markdown 记号**（铁律 9）。"""
        lines = [
            f"成功 {self.succeeded} 行（新增 {self.added} / 覆盖 {self.updated}）",
            f"跳过 {self.skipped} 行" + (f"（其中空行 {self.blank} 行）" if self.blank else ""),
            f"失败 {self.failed} 行",
        ]
        if self.classes:
            lines.append("涉及班级：" + "、".join(self.classes))
        if self.errors_path is not None:
            lines.append(f"失败和跳过的清单已导出：{self.errors_path}")
        for note in self.warnings:
            lines.append("提醒：" + note)
        return "\n".join(lines)


def _roster(store: CryptoStore, username: str, class_name: str) -> dict[str, str]:
    """这个班的「学号 → 学生 key」对照表。

    ★ 为什么不直接拼 ``sid:<学号>``：班级快照里的 key 是**导入成绩时**就算好
      存下来的（见 ui/pages/analysis_page.py 的 ``"key": row.key``），
      自己再拼一遍就等于开第二个真源 —— 拼法一变，德育分全部对不上人。
      所以能查名单就查名单，拼 key 只作为名单为空时的兜底。
    """
    record = scholarship.load_class(store, username, class_name)
    if record is None:
        return {}
    out: dict[str, str] = {}
    for student in record.students:
        sid = str(student.get("student_id") or "").strip()
        key = str(student.get("key") or "").strip()
        if sid and key:
            out[sid] = key
    return out


def apply_plan(
    store: CryptoStore,
    username: str,
    plan: ImportPlan,
    *,
    batch_id: str = "",
    error_dir: str | Path | None = None,
) -> ImportSummary:
    """把计划写进库，并导出一张错误清单。

    每个班单独读改写（``load_moral`` → 合并 → ``save_moral``），因为德育分
    本来就是**一个班一个加密文件**。

    Raises:
        ImportApplyError: 一个班都写不进去（账户没了 / 盘写不了）。
    """
    batch = batch_id or new_batch_id()
    summary = ImportSummary(batch_id=batch, source=plan.path.name,
                            total=plan.total_rows, blank=plan.blank_rows)
    summary.classes = plan.classes
    summary.warnings = list(plan.warnings)

    # 学号不在该班名单里的行：**跳过，不写孤儿 key**。
    # 写了也不会有人认领（界面上按名单取分），只会让库里多出永不显示的分数。
    by_class: dict[str, list[RawRow]] = {}
    extra_errors = list(plan.errors)
    # ★ 名单按班缓存：每一行都去 load_class 一次的话，一个班几十行就是几十次
    #   解密（每次都要解 users.json 和班级文件），几十人的表能卡到几十秒。
    rosters: dict[str, dict[str, str]] = {}
    for row in plan.rows:
        if row.class_name not in rosters:
            rosters[row.class_name] = _roster(store, username, row.class_name)
        roster = rosters[row.class_name]
        if roster:
            key = roster.get(row.student_id)
            if key is None:
                extra_errors.append(ErrorRow(
                    row_index=row.row_index, kind="跳过",
                    reason=f"这个学号不在「{row.class_name}」的名单里",
                    student_id=row.student_id, student=row.student,
                    class_name=row.class_name, score_text=row.score_text,
                ))
                continue
        else:
            key = student_key(row.student_id, row.student)
        row.key = key
        by_class.setdefault(row.class_name, []).append(row)

    saved_classes = 0
    for class_name, rows in by_class.items():
        moral = scholarship.load_moral(store, username, class_name)
        for row in rows:
            if row.key in moral:
                summary.updated += 1
            else:
                summary.added += 1
            moral[row.key] = int(row.score or 0)
        try:
            scholarship.save_moral(store, username, class_name, moral)
        except Exception as exc:                    # noqa: BLE001 —— 换成人话再抛
            raise ImportApplyError(
                f"「{class_name}」的德育分写不进去：{type(exc).__name__}: {exc}"
            ) from exc
        saved_classes += 1

    summary.failed = len([e for e in extra_errors if e.kind == "失败"])
    summary.skipped = len([e for e in extra_errors if e.kind == "跳过"])
    summary.errors_path = write_error_csv(
        extra_errors, _error_path(batch, error_dir)
    )

    if saved_classes:
        record_batch(store, username, summary)
    return summary


def _error_path(batch_id: str, error_dir: str | Path | None) -> Path:
    folder = Path(error_dir) if error_dir is not None else default_errors_dir()
    return folder / f"{ERROR_CSV_PREFIX}{batch_id}.csv"


def run_import(
    path: str | Path,
    store: CryptoStore,
    username: str,
    *,
    sheet_name: int | str = 0,
    default_class: str = "",
    error_dir: str | Path | None = None,
    known_classes: Sequence[str] | None = None,
    frame: Any = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> ImportSummary:
    """一步到位：读表 → 校验 → 导出错误清单 → 落库 → 记台账。

    调用方（界面）应当把它整个丢进 :func:`ui.workers.run_async`：
    读 Excel 是磁盘大活，绝不能占 UI 线程。

    Args:
        default_class: 表里没有班级列时的目标班级。
        known_classes: 已经建过的班级。``None`` = 自己查。
        progress: ``progress(已完成, 总数, 人话)``。**在调用线程里同步调用**，
            调用方负责把它送到主线程（Qt 信号会自动排队）。
    """
    if known_classes is None:
        known_classes = [c.class_name for c in scholarship.list_classes(store, username)]
    else:
        known_classes = list(known_classes)

    report = progress or (lambda done, total, message: None)
    report(0, 3, "正在读表…")
    plan = plan_import(path, sheet_name=sheet_name, known_classes=known_classes,
                       default_class=default_class, frame=frame)
    report(1, 3, f"校验完成：{len(plan.rows)} 行可导入，"
                 f"{len(plan.failures)} 行失败，{len(plan.skipped)} 行跳过")
    report(2, 3, "正在写库…")
    summary = apply_plan(store, username, plan, error_dir=error_dir)
    report(3, 3, f"完成：新增 {summary.added} / 覆盖 {summary.updated} / "
                 f"失败 {summary.failed} / 跳过 {summary.skipped}")
    return summary


# ---------------------------------------------------------------------------
# 五、批次台账
# ---------------------------------------------------------------------------


def load_batches(store: CryptoStore, username: str) -> list[dict[str, Any]]:
    """读批次台账。文件不在或坏了 → 空列表（**不抛、也不删**）。"""
    raw = load_user_json(store, username, BATCH_FILE, {})
    items = raw.get("batches") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def record_batch(
    store: CryptoStore, username: str, summary: ImportSummary
) -> Path:
    """把这一批记进台账（最近 ``MAX_BATCH_RECORDS`` 条）。

    ★ 为什么要单独一个文件：``save_moral`` 每次只写 ``{"scores": ...}``，
      塞不进任何别的东西。台账要是硬塞进去，下一次保存德育分就会把它抹掉。
    """
    entry = {
        "batch_id": summary.batch_id,
        "at": datetime.now().isoformat(timespec="seconds"),
        "source": summary.source,
        "total": summary.total,
        "added": summary.added,
        "updated": summary.updated,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "classes": list(summary.classes),
        "errors_file": str(summary.errors_path) if summary.errors_path else "",
    }
    items = [item for item in load_batches(store, username)
             if item.get("batch_id") != summary.batch_id]
    items.append(entry)
    return save_user_json(
        store, username, BATCH_FILE, {"schema": 1, "batches": items[-MAX_BATCH_RECORDS:]}
    )
