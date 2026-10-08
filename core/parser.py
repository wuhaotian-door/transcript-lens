"""成绩单表头三级提取器（Spec 第 6 章 · 导入解析铁律）。

设计原则（对应 Spec 第 9 章红线）：

* 红线 2：**空 ≠ 0**。空 / ``-`` / ``/`` / ``未选`` / 纯空格 = 未选，记为 ``None``（跳过）；
  数字 ``0`` = 缺考，原样记为 ``0.0``（必须计入加权）。两者绝不混同。
* 红线 3：本模块**只负责提取与回显**，不自动开算。学分/类别提取结果连同
  ``credit_source`` / ``category_source`` 一起返回，供 UI 强制回显人工确认。
* 红线 4：文本型成绩 ``"85.000000"`` 强转 ``float``；转不了的单元格记入 ``bad_cells``，
  整行 ``is_valid=False``。
* 红线 10：有内容却缺了课程成绩列（行短了/错位）→ 整行 ``is_valid=False``、
  ``has_column_mismatch=True``，**绝不静默吞掉该行**。

**v1.9 表格干扰过滤**（Owner 实测的三类干扰）：

教务系统导出的表里混着三类"不是学生成绩"的东西，以前它们会被当成
课程列和"学生"算进加权分母 —— 分母凭空变大，均分被稀释，而且**没有任何提示**：

1. **统计列**：不及格门数 / 平均分 / 总分 / 平均学分绩 / 平均学分绩点 / 排名 …
   （见 :data:`STAT_HEADER_KEYWORDS`）→ 排除，记进 ``ParseResult.stat_columns``。
2. **末尾汇总行**：``37人  470  2485 …`` / ``不及格  18  6  0``
   （见 :data:`SUMMARY_ROW_KEYWORDS`）→ 跳过，记进 ``ParseResult.skipped_rows``。
3. **顶部大标题**：跨列合并的"…学生成绩汇总表"（读出来只有一个非空单元格）
   → 不当表头行，见 :func:`_row_is_title_like`。

三条都**留痕**：跳过的行、排除的列都写进 ``warnings`` / ``stat_columns`` /
``skipped_rows``，绝不静默吞（红线 10 的精神）。

本模块不做加权、不做合并、不碰 UI、不碰网络。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd

__all__ = [
    "UNKNOWN",
    "Category",
    "Course",
    "ExamRow",
    "ParseResult",
    "clean_course_name",
    "extract_category",
    "extract_credit",
    "find_header_rows",
    "is_anchor_header",
    "is_meta_header",
    "is_stat_header",
    "normalize_header",
    "parse_dataframe",
    "parse_dataframe_to_courses",
    "parse_excel",
    "strip_blank_marker",
    "to_float",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 类别识别失败时的标记（Spec 第 6 章：都失败则标红进人工弹窗）
UNKNOWN = "unknown"

#: 表头关键词 → 类别。命中次数取多数，保证确定性。
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "必修": ("必修", "学位课", "学位必修", "专业必修", "公共必修"),
    "限选": ("限选", "限选课", "专业限选", "限定选修", "专业选修"),
    "选修": ("选修", "任选", "公选", "通识", "校选", "任意选修", "公共选修"),
}

#: 未选课标记（Spec 第 2 章规则表）。语义 = "没选这门课"，整门课跳过。
#: 注意：``"0"`` 不在此列 —— 数字 0 是缺考，必须计入。
BLANK_MARKERS: frozenset[str] = frozenset(
    {
        "",
        "-",
        "--",
        "—",
        "——",
        "/",
        "\\",
        "未选",
        "未选课",
        "未修",
        "未修读",
        "未修课",
        "免修",
        "无",
        "n/a",
        "na",
        "null",
        "none",
        "nan",
    }
)

#: 认定为"学生身份列"的表头关键词，用于定位主表头行。
SUBJECT_HEADER_KEYWORDS: tuple[str, ...] = (
    "姓名",
    "学号",
    "学生",
    "名字",
    "考生",
    "student",
    "name",
)

#: 学生身份列的表头关键词，这些列永远不当课程列。
#: 含 ``学分`` —— 独立权重行的姓名列位置常写成 "学分"/"学时"。
META_HEADER_KEYWORDS: frozenset[str] = frozenset(
    {
        "序号",
        "编号",
        "学号",
        "姓名",
        "学生",
        "学生姓名",
        "班级",
        "专业",
        "学院",
        "年级",
        "备注",
        "学分",
        "学分 ",
        "学时",
        "课程",
        "课程名称",
        "科目",
    }
)

#: 「学号」列表头关键词。学号是纵向累计的钥匙（Spec 第 2 章：按"学号哈希 + 学期标签"append），
#: 必须单独识别，不能和姓名列混为一谈。
STUDENT_ID_HEADER_KEYWORDS: tuple[str, ...] = (
    "学号",
    "学生学号",
    "考生号",
    "studentid",
    "student_id",
    "sid",
)

#: 「姓名」列表头关键词。
STUDENT_NAME_HEADER_KEYWORDS: tuple[str, ...] = (
    "姓名",
    "学生姓名",
    "名字",
    "学生",
    "name",
)

#: 「所得学分」锚点列表头关键词（Spec 第 6 章第 5 条：校验锚点）。
#: 命中这些词的列是**校验锚点**，不是课程列，更不是身份列 —— parser 只负责认出来，
#: 真正的逐行比对在 :mod:`core.validator`。
ANCHOR_HEADER_KEYWORDS: tuple[str, ...] = (
    "所得学分",
    "已得学分",
    "已获学分",
    "获得学分",
    "已修学分",
    "实得学分",
    "取得学分",
    "总学分",
    "学分合计",
    "学分总计",
    "合计学分",
    "学分小计",
)

#: ★ v1.9 新增：**统计列**的表头关键词。命中这些词的列是"算出来的汇总"，
#: 不是学生考的分数 —— 绝不能当课程列。
#:
#: 为什么必须单独列一张表：``parse_dataframe`` 原来的判据是"不是身份列、
#: 不是锚点列 → 就是课程列"。教务系统导出的成绩表恰恰会在课程列右边挂一长串
#: 统计列（不及格门数 / 平均分 / 总分 / 平均学分绩点 / 排名），于是每一列都被
#: 当成一门课算进加权分母 —— 分母凭空变大，加权均分被稀释，而且没有任何提示。
#:
#: ★ 判据只看**字符串**表头（``normalize_header`` 对非字符串返回空串），
#:   所以数据区里的数字不会误命中。
STAT_HEADER_KEYWORDS: tuple[str, ...] = (
    "不及格门数",
    "不及格科目",
    "不及格课程",
    "不及格数",
    "不及格",
    "平均学分绩点",
    "平均学分绩",
    "平均绩点",
    "平均分",
    "平均成绩",
    "绩点",
    "gpa",
    "总分",
    "总成绩",
    "总评",
    "合计",
    "小计",
    "排名",
    "名次",
    "rank",
)

#: ★ v1.9 新增：**末尾统计/汇总行**里出现的词。这类行整行都不是学生：
#: ``37人  470  2485 …`` / ``不及格  18  6  0``。
SUMMARY_ROW_KEYWORDS: tuple[str, ...] = (
    "不及格",
    "合计",
    "小计",
    "总计",
    "汇总",
    "平均",
    "人数",
    "门数",
    "统计",
)

#: ``37人`` / ``37 人`` 这种"人数"单元格。
_SUMMARY_COUNT_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*人\s*$")

#: 教师列那种"按行堆叠"的污染写法：``90 张三`` / ``91：李四``。
#: 只拦"纯数字 + 分隔符 + 姓名"这一种形态，不能因为表头带空格就误杀正常课程名。
_GRADE_LIST_CELL_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*[\s\u3000:：]\s*\S+\s*$")

#: 表头最多向下扩展几行（三级提取：表头行 / 权重行 / 类别行）。
MAX_HEADER_ROWS = 3

#: 允许的学分上限，超过视为"这一格不是学分"。
MAX_CREDIT = 30.0

_CREDIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"学分\D{0,4}?(\d+(?:\.\d+)?)"),  # 高等数学(5学分) / 学分: 5
    re.compile(r"(\d+(?:\.\d+)?)\s*学分"),        # 高等数学 5学分 / 5学分
    re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*[)）]"),  # 高等数学(5)
)

_CATEGORY_TOKEN_RE = re.compile(
    "|".join(re.escape(kw) for kws in CATEGORY_KEYWORDS.values() for kw in kws)
    + r"|unknown|未知"
)

_TRAILING_NOISE_RE = re.compile(r"[\s\u3000\-—_/\\·、,，;；:：()（）\[\]【】]+$")


def _fmt_number(value: float) -> str:
    """``5.0`` → ``'5'``，``2.5`` → ``'2.5'``（仅用于回显文案，不影响精度）。"""
    return str(int(value)) if value == int(value) else str(value)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


class Category(str, Enum):
    """课程类别。``UNKNOWN`` 用于提取失败，必须走人工确认。"""

    REQUIRED = "必修"
    LIMITED = "限选"
    ELECTIVE = "选修"
    UNKNOWN = UNKNOWN


@dataclass
class Course:
    """一列课程（= 一列表头）的结构化结果。"""

    name: str
    category: Category
    credit: float
    col_index: int
    #: 学分/类别是从哪儿抠出来的，回显弹窗要显示给辅导员看。
    credit_source: str = ""
    category_source: str = ""
    #: 提取失败原因，非空即表示需要人工确认。
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """是否必须人工确认（学分或类别任一没抠出来）。"""
        return bool(self.warnings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "credit": self.credit,
            "col_index": self.col_index,
            "credit_source": self.credit_source,
            "category_source": self.category_source,
            "warnings": list(self.warnings),
        }


@dataclass
class ExamRow:
    """一行学生成绩。``score_list`` 与 ``ParseResult.courses`` 一一对应。"""

    row_index: int
    #: 姓名列，或行内第一个非空单元格（两者都没有则 ``"<第N行>"``）。
    student: str
    #: 学号列的原文。纵向累计的钥匙（Spec 第 2 章：按"学号哈希 + 学期标签"append）。
    #: 表里没有学号列时为 ``""``。
    student_id: str = ""
    #: 课程名 → 分数；``None`` = 空/未选（跳过），``0.0`` = 缺考（计入）。
    #: 注意：这是**原始提取结果，未做同名取高**；取高在 core/calculator.py。
    scores: dict[str, float | None] = field(default_factory=dict)
    #: 与 ``ParseResult.courses`` 同序的分数，便于按列对齐。
    score_list: list[float | None] = field(default_factory=list)
    is_valid: bool = True
    #: 有内容却缺课程成绩列 → 行错位（红线 10）。
    has_column_mismatch: bool = False
    #: 同一门课在这行里出现了多次非空分数（非补考），需要取高处理。
    duplicate_course_names: list[str] = field(default_factory=list)
    #: 强转 float 失败的单元格 ``(列序号, 原文)``（红线 4）。
    bad_cells: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts_for_gpa(self) -> bool:
        """是否允许参与加权。无效行一律不参与（不静默、也不带病计算）。"""
        return self.is_valid

    @property
    def student_key(self) -> str:
        """纵向累计用的稳定标识：优先学号，没有学号才退回姓名。

        姓名做 key 有同名风险，:meth:`ParseResult.missing_student_id` 会提示这件事。
        """
        return self.student_id or self.student

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "student": self.student,
            "student_id": self.student_id,
            "scores": dict(self.scores),
            "score_list": list(self.score_list),
            "is_valid": self.is_valid,
            "has_column_mismatch": self.has_column_mismatch,
            "duplicate_course_names": list(self.duplicate_course_names),
            "bad_cells": list(self.bad_cells),
            "warnings": list(self.warnings),
        }


@dataclass
class ParseResult:
    """``parse_dataframe`` 的完整返回值 —— UI 回显弹窗直接吃这个对象。"""

    courses: list[Course] = field(default_factory=list)
    rows: list[ExamRow] = field(default_factory=list)
    #: 参与表头合并的行（0-based）。``[]`` = 没找到表头。
    header_rows: list[int] = field(default_factory=list)
    data_start_row: int | None = None
    #: 被识别为学生身份列、排除在课程之外的列序号。
    meta_columns: list[int] = field(default_factory=list)
    #: 被识别为「所得学分」校验锚点的列序号（Spec 第 6 章第 5 条）。
    #: 这不是课程列，也不该进加权；逐行比对请用 :func:`core.validator.check_credit_anchor`。
    anchor_columns: list[int] = field(default_factory=list)
    #: 被识别为「学号」的列序号。学号是纵向累计的钥匙。
    id_columns: list[int] = field(default_factory=list)
    #: 被识别为「姓名」的列序号。取姓名时优先用它，避免把学号当姓名。
    name_columns: list[int] = field(default_factory=list)
    #: ★ v1.9：被识别为「统计列」的列序号（不及格门数 / 平均分 / 总分 /
    #: 平均学分绩点 / 排名 …）。这些列的数值是**算出来的汇总**，不是学生成绩，
    #: 一律不进加权分母。回显弹窗要把这件事说给辅导员听。
    stat_columns: list[int] = field(default_factory=list)
    #: ★ v1.9：被跳过的行 ``(行序号 0-based, 原因)``。统计/汇总行、
    #: 缺学生标识的行。**跳过的行必须留痕**（红线 10 的精神：绝不静默吞行）。
    skipped_rows: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_credit_anchor(self) -> bool:
        """表里是否有「所得学分」锚点列。"""
        return bool(self.anchor_columns)

    @property
    def missing_student_id(self) -> list[str]:
        """没有学号、只能靠姓名做纵向 key 的学生。

        姓名做 key 有同名风险，纵向累计前必须让辅导员知道这件事。
        """
        return [row.student for row in self.rows if not row.student_id]

    @property
    def valid_rows(self) -> list[ExamRow]:
        return [r for r in self.rows if r.is_valid]

    @property
    def invalid_rows(self) -> list[ExamRow]:
        return [r for r in self.rows if not r.is_valid]

    @property
    def courses_needing_review(self) -> list[Course]:
        """学分或类别没抠出来、红线 3 要求必须人工确认的课程。"""
        return [c for c in self.courses if c.needs_review]

    @property
    def total_credit(self) -> float:
        return sum(c.credit for c in self.courses)

    @property
    def needs_manual_review(self) -> bool:
        """是否需要弹人工回显窗（Spec 第 6 章第 2 条：强制环节，不能 skip）。"""
        return (
            self.data_start_row is None
            or not self.courses
            or bool(self.courses_needing_review)
            or bool(self.invalid_rows)
            or any(r.duplicate_course_names for r in self.rows)
            or bool(self.warnings)
        )


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def normalize_header(value: Any) -> str:
    """表头单元格 → 单行紧凑字符串；非字符串类型一律返回 ``""``。"""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def strip_blank_marker(value: Any) -> Any:
    """未选标记 → ``None``。数字 ``0`` 与 ``0.0`` 原样返回（红线 2）。"""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in BLANK_MARKERS:
            return None
        return text
    return value


def to_float(value: Any) -> float | None:
    """强转 float（红线 4）。空/未选 → ``None``；``0`` → ``0.0``；转不了 → ``None``。

    调用方需先用 :func:`strip_blank_marker` 区分"未选"与"非法"，否则无法判断
    返回的 ``None`` 到底是未选还是解析失败。
    """
    value = strip_blank_marker(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "")
        if text.endswith("%"):
            text = text[:-1]
        if not text:
            return None
        try:
            return float(text)  # "85.000000" → 85.0
        except ValueError:
            return None
    return None


def extract_credit(text: str) -> float | None:
    """从表头文本里正则抠学分。``'高等数学(5学分)'`` → ``5.0``，抠不到 → ``None``。"""
    if not text:
        return None
    for pattern in _CREDIT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:  # pragma: no cover - 正则已保证是数字
                continue
    return None


def extract_category(text: str) -> Category:
    """从表头/类别行文本里识别课程类别；识别不出 → :attr:`Category.UNKNOWN`。"""
    if not text:
        return Category.UNKNOWN
    hits: dict[Category, int] = {}
    for match in _CATEGORY_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token in ("unknown", "未知"):
            continue
        for category_name, keywords in CATEGORY_KEYWORDS.items():
            if token in keywords:
                category = Category(category_name)
                hits[category] = hits.get(category, 0) + 1
                break
    if not hits:
        return Category.UNKNOWN
    # 命中次数相同时按 必修 > 限选 > 选修 取先出现的，保证确定性。
    order = {Category.REQUIRED: 0, Category.LIMITED: 1, Category.ELECTIVE: 2}
    return max(hits.items(), key=lambda kv: (kv[1], -order[kv[0]]))[0]


def clean_course_name(text: str) -> str:
    """表头 → 纯课程名。剥掉 ``(5学分)``、``必修``、``限选`` 等噪音。"""
    if not text:
        return ""

    # 0) 教师列那种"90 张三"的按行堆叠写法：**原始表头**命中"数字+分隔+姓名"即视为非课程名。
    #    必须在剥离噪音之前判断，否则自己补进去的分隔符会误杀正常表头。
    if _GRADE_LIST_CELL_RE.match(text):
        return ""

    name = text

    # 1) 括号里的元信息：(5学分) / （必修） / 【限选】
    name = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", name)

    # 2) "学分" 前后粘连的标记：高等数学5学分必修 / 高等数学学分5
    name = re.sub(r"\d+(?:\.\d+)?\s*学分", "", name)
    name = re.sub(r"学分\s*\d+(?:\.\d+)?", "", name)
    name = name.replace("学分", "")

    # 3) 零散的类别词
    name = _CATEGORY_TOKEN_RE.sub("", name)

    # 4) 收尾噪音
    name = _TRAILING_NOISE_RE.sub("", name.strip())
    name = re.sub(r"\s+", " ", name).strip()

    # 5) 剥掉残留的前后分隔符与孤立数字：'学分:2必修' → ':2' → ''
    name = re.sub(r"^[\s\u3000\-—_/\\·、,，;；:：()（）\[\]【】]+", "", name)
    name = re.sub(r"^[\s\u3000\-—_/\\·、,，;；:：]+", "", name)
    name = re.sub(r"^[0-9]+(?:\.[0-9]+)?[\s\u3000:：]*$", "", name)
    name = _TRAILING_NOISE_RE.sub("", name.strip())

    if not name or len(name) > 40:
        return ""
    return name


# ---------------------------------------------------------------------------
# 表头定位
# ---------------------------------------------------------------------------


def is_anchor_header(value: Any) -> bool:
    """该表头是否是「所得学分」类校验锚点列（Spec 第 6 章第 5 条）。

    锚点列既不是课程列、也不是学生身份列，必须单独识别出来 —— 否则
    ``find_header_rows`` 会因为它含"学分"二字而用不上，或把它当课程列算进加权。
    """
    text = normalize_header(value)
    return bool(text) and any(kw in text for kw in ANCHOR_HEADER_KEYWORDS)


def is_stat_header(value: Any) -> bool:
    """该表头是否是「统计列」（v1.9）。

    统计列 = 教务系统在课程列右边挂的那串**算出来的汇总**：
    不及格门数 / 平均分 / 总分 / 平均学分绩 / 平均学分绩点 / 排名 …

    它们既不是学生身份列，也不是学生考的分数。以前判据是"不是身份列、
    不是锚点列 → 就是课程列"，于是这些列的数值被当成课程分数算进了加权。

    ★ 匹配前把**所有空白都挤掉**（含全角空格和换行）：教务导出的表头里
    「平 均 分」「平均\n分」这两种写法都真见过，而 `normalize_header` 只去
    首尾空白。挤空白对课程名是安全的 —— 课程名里不会出现上面那串关键词。
    """
    text = normalize_header(value).lower()
    if not text:
        return False
    squeezed = "".join(text.split())
    return any(kw in squeezed for kw in STAT_HEADER_KEYWORDS)


def is_student_id_header(value: Any) -> bool:
    """该表头是否是「学号」列（纵向累计的钥匙）。"""
    text = normalize_header(value).lower()
    if not text:
        return False
    return any(kw in text for kw in STUDENT_ID_HEADER_KEYWORDS)


def is_student_name_header(value: Any) -> bool:
    """该表头是否是「姓名」列。"""
    text = normalize_header(value).lower()
    if not text:
        return False
    return any(kw in text for kw in STUDENT_NAME_HEADER_KEYWORDS)


def is_meta_header(value: Any) -> bool:
    """该表头是否永远是学生身份列（姓名/学号/班级/备注…）。"""
    text = normalize_header(value)
    if not text:
        return False
    return _is_subject_header(text) or text in META_HEADER_KEYWORDS


def _is_subject_header(value: Any) -> bool:
    """该单元格是否像"学生身份列"的表头（姓名/学号/学生…）。"""
    text = normalize_header(value).lower()
    return bool(text) and any(kw in text for kw in SUBJECT_HEADER_KEYWORDS)


def _row_is_empty(df: pd.DataFrame, row_index: int) -> bool:
    """整行是否没有任何有效内容。"""
    return not any(
        strip_blank_marker(df.iloc[row_index].iloc[c]) is not None
        for c in range(df.shape[1])
    )


def _looks_like_real_data_row(
    df: pd.DataFrame, row_index: int, meta_columns: list[int]
) -> bool:
    """该行是否是一条真实学生成绩行（据此终止表头块扩展）。

    判据：学生姓名列有值且**不是身份列表头关键词**（``姓名``/``学号``/``学分``…）。
    纯数字权重行的姓名列位置写的是 "学分"，因此不会被误判为数据行。
    """
    row = df.iloc[row_index]
    for c in meta_columns:
        text = normalize_header(row.iloc[c])
        if text and not _is_subject_header(text) and text not in META_HEADER_KEYWORDS:
            return True
    return False


def _row_non_empty_count(df: pd.DataFrame, row_index: int) -> int:
    """整行有几个非空单元格。"""
    return sum(
        1
        for c in range(df.shape[1])
        if strip_blank_marker(df.iloc[row_index].iloc[c]) is not None
    )


def _row_is_title_like(df: pd.DataFrame, row_index: int) -> bool:
    """这一行是不是**顶部大标题**（v1.9 干扰之三）。

    判据：整行**只有一个**非空单元格。

    为什么这么判：教务导出的表最上面常有一行跨列合并的大标题
    （"2024-2025 学年第一学期学生成绩汇总表"）。合并单元格被 openpyxl /
    xlrd 读出来时只有最左边那格有值、其余是空 —— "只有一个非空格"
    就是它的指纹。而**真正的表头行不可能只有一个非空格**：它至少要写出
    「姓名」+ 学号或至少一门课。

    ★ 不这么做的话，标题里的"学生"二字会命中
      :data:`SUBJECT_HEADER_KEYWORDS`，标题行就被当成表头行，白白吃掉
      :data:`MAX_HEADER_ROWS` 里的一格，真正的表头行反而被挤掉。
    """
    return _row_non_empty_count(df, row_index) == 1


def _summary_text_reason(text: str) -> str:
    """单元格文本像不像"统计/汇总"？像则返回人话原因，否则返回空串。

    ``37人`` → "人数统计"；``不及格`` / ``合计`` / ``平均分`` → 含汇总词。
    """
    if not text:
        return ""
    if _SUMMARY_COUNT_RE.match(text):
        return f"「{text}」是人数统计"
    for keyword in SUMMARY_ROW_KEYWORDS:
        if keyword in text:
            return f"「{text}」含汇总词「{keyword}」"
    return ""


def find_header_rows(
    df: pd.DataFrame,
) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
    """定位表头行、学生身份列、锚点列、学号列与姓名列。

    Returns:
        ``(header_rows, meta_columns, anchor_columns, id_columns, name_columns)``。
        ``header_rows`` 最多 :data:`MAX_HEADER_ROWS` 行，按从上到下排列；
        找不到表头时返回五个空列表。
    """
    if df is None or df.empty:
        return [], [], [], [], []

    if df.ndim != 2 or df.shape[1] < 2 or df.shape[0] < 1:
        return [], [], [], [], []

    n_rows, n_cols = df.shape
    for r in range(n_rows):
        # ★ v1.9：顶部跨列合并的大标题不是表头行（干扰之三）。
        #   不跳过的话，标题里只要有"学生"二字就会被当成身份列表头，
        #   真正的表头行反而被 MAX_HEADER_ROWS 挤掉。
        if _row_is_title_like(df, r):
            continue

        raw = [normalize_header(df.iloc[r].iloc[c]) for c in range(n_cols)]

        # 锚点列（所得学分）自己占一列，但绝不当课程列
        anchor_columns: list[int] = []
        for c, text in enumerate(raw):
            if text and is_anchor_header(text):
                anchor_columns.append(c)

        # 学号列要单独记下来：它是纵向累计的钥匙，不能被当成普通身份列丢掉
        id_columns: list[int] = [
            c
            for c, text in enumerate(raw)
            if c not in anchor_columns and is_student_id_header(text)
        ]

        # 姓名列也要单独记：有学号列时，不能按"第一个非空身份列"去取姓名，
        # 否则学号会被当成姓名（踩过一次）。
        name_columns: list[int] = [
            c
            for c, text in enumerate(raw)
            if c not in anchor_columns
            and c not in id_columns
            and is_student_name_header(text)
        ]

        meta_columns: list[int] = []
        for c, text in enumerate(raw):
            if c in anchor_columns:
                continue
            if is_meta_header(text):
                meta_columns.append(c)

        if not meta_columns:
            continue

        columns = [
            c for c in range(n_cols) if c not in meta_columns and c not in anchor_columns
        ]
        header_rows = [r]
        for r2 in range(r + 1, min(r + MAX_HEADER_ROWS, n_rows)):
            if _row_is_empty(df, r2):
                break
            # 姓名列出现学生名 → 表头块到此为止，这是第一条数据
            if _looks_like_real_data_row(df, r2, meta_columns):
                break
            # 课程列一个非空值都没有 → 也不是表头延伸行
            # 注意用 strip_blank_marker 而非 normalize_header：权重行里放的是**数字**。
            if not any(
                strip_blank_marker(df.iloc[r2].iloc[c]) is not None for c in columns
            ):
                break
            header_rows.append(r2)
        return (
            header_rows,
            sorted(meta_columns),
            sorted(anchor_columns),
            sorted(id_columns),
            sorted(name_columns),
        )

    return [], [], [], [], []


# ---------------------------------------------------------------------------
# 课程列提取（三级兜底）
# ---------------------------------------------------------------------------


def _row_is_weight_row_evidence(
    df: pd.DataFrame, row_index: int, columns: list[int], exclude: int
) -> bool:
    """判断表头下方某行是否是"纯权重行"：只有裸数字，且其它列都是类别词或空。

    用于把 ``学分 | 2 | 5 | 3`` 这种独立权重行认出来（红线 3 的二级兜底）。
    """
    values: list[tuple[int, Any]] = []
    for c in columns:
        if c == exclude:
            continue
        raw = df.iloc[row_index].iloc[c]
        if normalize_header(raw):
            values.append((c, raw))
        elif strip_blank_marker(raw) is not None:
            values.append((c, raw))

    if not values:
        return False
    for _, raw in values:
        category = extract_category(normalize_header(raw))
        if category is not Category.UNKNOWN:
            continue  # 类别词，正常
        if "学分" in normalize_header(raw):
            continue  # 行首的"学分"文字标签，正常
        number = to_float(raw)
        if number is None or number < 0 or number > MAX_CREDIT:
            return False  # 出现了既不是类别也不是学分数值的东西
    return True


def _extract_course_values(
    df: pd.DataFrame,
    columns: list[int],
    header_rows: list[int],
    meta_columns: list[int],
) -> tuple[list[Course], list[str]]:
    """对每个课程列做三级提取：表头行 → 独立权重行 → 类别行 → unknown。"""
    del meta_columns  # 课程列已由调用方筛好，这里不再需要
    main_row = header_rows[0]
    extension_rows = header_rows[1:]
    warnings: list[str] = []
    courses: list[Course] = []

    for col in columns:
        warnings_for_col: list[str] = []

        # ---- 课程名：取第一个能洗出课程名的表头行 ----
        name = ""
        for r in header_rows:
            name = clean_course_name(normalize_header(df.iloc[r].iloc[col]))
            if name:
                break

        # ---- 一级：正则抠学分 / 类别（表头行自带） ----
        header_text = normalize_header(df.iloc[main_row].iloc[col])
        credit = extract_credit(header_text)
        credit_source = f"header:regex:{_fmt_number(credit)}" if credit is not None else ""
        category = extract_category(header_text)
        category_source = (
            f"header:{category.value}" if category is not Category.UNKNOWN else ""
        )

        # ---- 二级：表头下方的独立权重行 ----
        # 2a) 该格自带"学分"字样或括号数字 → 直接抠
        if credit is None:
            for r in extension_rows:
                direct = extract_credit(normalize_header(df.iloc[r].iloc[col]))
                if direct is not None:
                    credit, credit_source = direct, f"weight_row:{r}"
                    break
        # 2b) 该格是裸数字，且整行只有裸数字 + 类别词 → 这一格就是学分
        if credit is None:
            for r in extension_rows:
                direct = to_float(df.iloc[r].iloc[col])
                if direct is None or direct < 0 or direct > MAX_CREDIT:
                    continue
                if _row_is_weight_row_evidence(df, r, columns, exclude=col):
                    credit, credit_source = direct, f"weight_row:{r}"
                    break

        # ---- 三级：独立类别行 ----
        if category is Category.UNKNOWN:
            for r in extension_rows:
                found = extract_category(normalize_header(df.iloc[r].iloc[col]))
                if found is not Category.UNKNOWN:
                    category, category_source = found, f"category_row:{r}"
                    break

        # ---- 兜底：标 unknown + 待人工确认 ----
        if credit is None:
            credit = 0.0
            credit_source = ""
            warnings_for_col.append(
                f"第 {col + 1} 列「{name or '未命名'}」学分提取失败，已按 0 学分暂存，必须人工确认"
            )
        if category is Category.UNKNOWN:
            warnings_for_col.append(
                f"第 {col + 1} 列「{name or '未命名'}」类别未识别（{UNKNOWN}），必须人工确认"
            )
        if not name:
            name = f"<未命名第{col + 1}列>"
            warnings_for_col.append(f"第 {col + 1} 列课程名提取失败，必须人工确认")

        courses.append(
            Course(
                name=name,
                category=category,
                credit=credit,
                col_index=col,
                credit_source=credit_source,
                category_source=category_source,
                warnings=warnings_for_col,
            )
        )

    return courses, warnings


def _merge_duplicate_course_columns(courses: list[Course]) -> tuple[list[Course], list[str]]:
    """同名课程列只保留首列（取高由 core/calculator.py 负责），并提示人工确认。"""
    warnings: list[str] = []
    seen: dict[str, Course] = {}
    kept: list[Course] = []
    for course in courses:
        if course.name in seen:
            first = seen[course.name]
            warnings.append(
                f"课程「{course.name}」在第 {first.col_index + 1} 列和第 {course.col_index + 1} 列"
                "重复出现，只取首列参与计算，请在回显弹窗确认是否应取高"
            )
            first.warnings.append(f"与第 {course.col_index + 1} 列同名，需人工确认取高")
            continue
        seen[course.name] = course
        kept.append(course)
    return kept, warnings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _id_text(value: Any) -> str:
    """学号单元格 → 字符串。

    学号常被 Excel 读成数字（``20230001.0``），这里去掉尾部 ``.0``，
    避免同一个学号在两次导入里变成两种 key。
    """
    if value is None or value is pd.NaT:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        return str(int(value)) if value == int(value) else str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def parse_dataframe(df: pd.DataFrame) -> ParseResult:
    """解析成绩单 DataFrame → :class:`ParseResult`（供 UI 强制回显确认）。

    不做任何加权计算，也不修改入参 ``df``。
    """
    result = ParseResult()
    if df is None or df.empty:
        result.warnings.append("传入的表格为空，无法解析")
        return result

    header_rows, meta_columns, anchor_columns, id_columns, name_columns = find_header_rows(df)
    if not header_rows:
        result.warnings.append("未找到表头行（需要包含「姓名」或「学号」列）")
        return result

    result.header_rows = header_rows
    result.meta_columns = meta_columns
    result.anchor_columns = anchor_columns
    result.id_columns = id_columns
    result.name_columns = name_columns
    data_start = header_rows[-1] + 1
    result.data_start_row = data_start

    # ---- v1.9 干扰之一：统计列绝不是课程列 --------------------------------
    # 判据落在**表头行**上：只要任一表头行的这一格命中 is_stat_header，
    # 整列就是统计列。身份列/锚点列优先（它们不参与统计判定）。
    stat_columns = sorted(
        {
            c
            for c in range(df.shape[1])
            if c not in meta_columns
            and c not in anchor_columns
            and any(is_stat_header(df.iloc[r].iloc[c]) for r in header_rows)
        }
    )
    result.stat_columns = stat_columns
    for c in stat_columns:
        label = ""
        for r in header_rows:
            label = label or normalize_header(df.iloc[r].iloc[c])
        result.warnings.append(
            f"第 {c + 1} 列「{label or '未命名'}」是统计列（不及格门数/平均分/排名这类"
            "算出来的汇总），已排除，不计入加权分母"
        )

    columns = [
        c
        for c in range(df.shape[1])
        if c not in meta_columns and c not in anchor_columns and c not in stat_columns
    ]
    courses, course_warnings = _extract_course_values(
        df, columns, header_rows, meta_columns
    )
    courses, duplicate_warnings = _merge_duplicate_course_columns(courses)
    result.courses = courses
    result.warnings.extend(course_warnings)
    result.warnings.extend(duplicate_warnings)
    for course in courses:
        result.warnings.extend(course.warnings)

    if not courses:
        result.warnings.append("未识别出任何课程列")
        return result

    duplicate_names = {c.name for c in courses if any("同名" in w for w in c.warnings)}
    score_columns = {c.col_index for c in courses}

    for r in range(data_start, df.shape[0]):
        row = df.iloc[r]

        # 整行全空 → 不是学生行，跳过
        if not any(
            strip_blank_marker(row.iloc[c]) is not None for c in range(df.shape[1])
        ):
            continue

        # ---- v1.9 干扰之二：末尾统计/汇总行不是学生 ----------------------
        # 身份列（姓名/学号/序号/班级…）里写着「37人」「不及格」「平均分」
        # 这类汇总词 → 整行跳过。先判这一条，免得它又被下面的
        # "第一个非空单元格"兜底成一个人名，然后带着一堆分数进加权。
        summary_reason = ""
        for c in sorted(set(name_columns) | set(id_columns) | set(meta_columns)):
            summary_reason = _summary_text_reason(normalize_header(row.iloc[c]))
            if summary_reason:
                break

        # 姓名：优先取「姓名」列；没有姓名列时才退回"第一个非空身份列"
        student = ""
        for c in name_columns:
            text = normalize_header(row.iloc[c])
            if text:
                student = text
                break
        if not student:
            for c in meta_columns:
                if c in id_columns:
                    continue          # 绝不用学号充当姓名
                text = normalize_header(row.iloc[c])
                if text:
                    student = text
                    break

        # 学号：纵向累计的钥匙。学号列里的数字/长串原样保留为字符串。
        student_id = ""
        for c in id_columns:
            text = _id_text(row.iloc[c])
            if text:
                student_id = text
                break

        # 有效学生行必须有学生标识（学号或姓名）—— 干扰之二的后半条：
        # 尾巴上的纯汇总数值行（``470  2485 …``）既没有姓名也没有学号，
        # 以前会被"第一个非空单元格"兜底成一个人名。
        # ★ 只在表里**确实有**学号/姓名列时才这么判：没有身份列的窄表
        #   （``课程 | 学分 | 成绩``）本来就取不到标识，不能把它的行全判死。
        if (
            not summary_reason
            and not student
            and not student_id
            and (id_columns or name_columns)
        ):
            summary_reason = "没有学号也没有姓名（纯汇总数值行）"

        if summary_reason:
            result.skipped_rows.append((r, summary_reason))
            continue

        if not student:
            student = next(
                (
                    normalize_header(row.iloc[c])
                    for c in range(df.shape[1])
                    if normalize_header(row.iloc[c])
                ),
                "",
            )
        if not student:
            student = f"<第{r + 1}行>"

        exam = ExamRow(row_index=r, student=student, student_id=student_id, scores={})
        bad_cells: list[tuple[int, str]] = []

        for course in courses:
            raw = row.iloc[course.col_index]
            stripped = strip_blank_marker(raw)

            if stripped is None:
                score: float | None = None  # 空/未选 → 跳过（红线 2）
            else:
                score = to_float(stripped)
                if score is None:
                    bad_cells.append((course.col_index, str(raw)))  # 标黄（红线 4）
            exam.score_list.append(score)
            exam.scores[course.name] = score

        if bad_cells:
            exam.is_valid = False
            exam.bad_cells = bad_cells
            detail = "、".join(f"第 {col + 1} 列「{text}」" for col, text in bad_cells)
            exam.warnings.append(f"分数无法转为数字：{detail}，该行不参与计算")

        # 列数一致性：整行没有任何有效成绩，却出现无法解析的单元格
        # 或溢出到非课程列的内容 → 判定整行错位（红线 10），绝不静默吞行。
        if all(s is None for s in exam.score_list):
            leftovers = [
                strip_blank_marker(row.iloc[c])
                for c in range(df.shape[1])
                if c not in meta_columns
                and c not in score_columns
                and c not in anchor_columns
                # ★ v1.9：统计列已被排除出课程列，这里也必须一起排除 ——
                #   否则统计列的汇总数值会掉进 leftovers，把**每一行**都
                #   误报成"错位"（红线 10 的告警被稀释成噪音）。
                and c not in stat_columns
            ]
            leftovers = [v for v in leftovers if v is not None]
            if leftovers or bad_cells:
                exam.is_valid = False
                exam.has_column_mismatch = True
                evidence = leftovers + [text for _, text in bad_cells]
                exam.warnings.append(
                    f"第 {r + 1} 行可能错位：整行没有任何有效成绩，却发现 {evidence!r}，"
                    "整行不参与计算，请人工核对"
                )

        for course in courses:
            if course.name in duplicate_names and exam.scores.get(course.name) is not None:
                exam.duplicate_course_names.append(course.name)
        if exam.duplicate_course_names:
            names = "、".join(dict.fromkeys(exam.duplicate_course_names))
            exam.warnings.append(f"课程 {names} 在本行重复记分，取高由计算器处理")

        result.rows.append(exam)

    # ★ v1.9：跳过的行必须留痕。辅导员要能看见"程序替我扔掉了哪几行、
    #   为什么扔" —— 静默吞行是红线 10 明令禁止的事。
    if result.skipped_rows:
        rows_text = "、".join(str(r + 1) for r, _ in result.skipped_rows)
        reasons_text = "；".join(sorted({reason for _, reason in result.skipped_rows}))
        result.warnings.append(
            f"已跳过 {len(result.skipped_rows)} 行统计/汇总行（不是学生成绩）："
            f"第 {rows_text} 行 —— {reasons_text}"
        )

    if not result.rows:
        result.warnings.append("表头下没有找到任何学生行")
    elif not result.valid_rows:
        result.warnings.append("没有一行通过校验，全部需要人工核对")

    return result


def parse_dataframe_to_courses(df: pd.DataFrame) -> list[Course]:
    """便捷入口：只要课程列表（Spec 第 6 章要求的那份"回显数据结构"）。"""
    return parse_dataframe(df).courses


def parse_excel(path: str, sheet_name: Any = 0, **read_excel_kwargs: Any) -> ParseResult:
    """读 xlsx/csv 后调用 :func:`parse_dataframe`。

    只负责读取，不做任何加权。测试里读的是 ``tests/fixtures/fake_grades.xlsx``。
    """
    if str(path).lower().endswith(".csv"):
        df = pd.read_csv(path, header=None, **read_excel_kwargs)  # pragma: no cover
    else:
        df = pd.read_excel(path, sheet_name=sheet_name, header=None, **read_excel_kwargs)
    return parse_dataframe(df)
