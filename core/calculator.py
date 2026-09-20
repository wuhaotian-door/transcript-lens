"""单表加权计算器（Spec 第 2 章业务规则表 · 第 9 章红线）。

四件事，全部是**本地确定性算术**，不调任何大模型 API（红线 1）：

1. 单表加权：``Σ(成绩×学分) / Σ学分``
2. 三口径切换：含全部 / 仅必修+限选（默认） / 仅必修
3. 空跳过、0 计入（红线 2）
4. 同名课取成绩高那次，并取**那一次的学分**；不叠加、不平均（Spec 第 2 章）

红线提醒：**空 ≠ 0**。空 / ``-`` / ``/`` / ``未选`` = 没选这门课 → 学分不进分母；
数字 ``0`` = 缺考 → 学分进分母、分子记 0。

本模块不读文件、不碰 UI、不联网、不做加密。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.parser import UNKNOWN, Category, Course, ExamRow, ParseResult, to_float

__all__ = [
    "DEFAULT_SCOPE",
    "UNKNOWN",
    "CalculationResult",
    "CalculationScope",
    "CourseScore",
    "DedupReport",
    "ScopeDecision",
    "calculate_all",
    "calculate_many",
    "calculate_student",
    "dedupe_courses",
    "filter_by_scope",
    "merge_semesters",
    "scope_credits",
    "weighted_average",
]

#: 允许的合法分数区间，超出即视为异常并排除出计算
MIN_SCORE = 0.0
MAX_SCORE = 100.0


def _is_valid_score(score: float | None) -> bool:
    """分数是否合法：非 None 且在 ``[MIN_SCORE, MAX_SCORE]`` 内。"""
    return score is not None and MIN_SCORE <= score <= MAX_SCORE


class CalculationScope(str, Enum):
    """三档计算口径（Spec：UI 提供三档切换，默认必修+限选）。"""

    ALL = "含全部"
    REQUIRED_LIMITED = "仅必修+限选"
    REQUIRED_ONLY = "仅必修"

    @property
    def categories(self) -> frozenset[Category]:
        """该口径允许计入的课程类别集合。"""
        if self is CalculationScope.ALL:
            return frozenset(
                {Category.REQUIRED, Category.LIMITED, Category.ELECTIVE, Category.UNKNOWN}
            )
        if self is CalculationScope.REQUIRED_LIMITED:
            return frozenset({Category.REQUIRED, Category.LIMITED})
        return frozenset({Category.REQUIRED})


#: 默认计算口径：仅必修+限选计入（Spec 第 2 章规则表、第 10 章）
DEFAULT_SCOPE = CalculationScope.REQUIRED_LIMITED


@dataclass
class CourseScore:
    """一门课在某口径下的一次记分（已做完同名取高）。"""

    name: str
    category: Category
    credit: float
    #: 与 parser 同一语义：``None`` = 空/未选（跳过），``0.0`` = 缺考（计入）
    score: float | None
    #: 是否参与加权 =（在口径内）且（选了这门课）且（分数合法）
    counted: bool = False
    #: 原始列序号；跨表合并时是 ``(sheet_index, col_index)``
    col_index: Any = None
    #: >1 表示这门课在原始输入里出现了多次，已按"取高"合并
    source_count: int = 1
    warnings: list[str] = field(default_factory=list)

    @property
    def selected(self) -> bool:
        """是否选了这门课（空 = 未选 → 不选，红线 2）。"""
        return self.score is not None

    @property
    def contribution(self) -> float:
        """对分子的贡献：``成绩 × 学分``；未计入则为 0。"""
        return (
            self.score * self.credit
            if self.counted and self.score is not None
            else 0.0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "credit": self.credit,
            "score": self.score,
            "counted": self.counted,
            "col_index": self.col_index,
            "source_count": self.source_count,
            "warnings": list(self.warnings),
        }


@dataclass
class ScopeDecision:
    """某门课被口径排除的原因（回显与谈话提纲都要用）。"""

    name: str
    category: Category
    credit: float
    reason: str


@dataclass
class CalculationResult:
    """一次加权计算的全部结果与证据链。"""

    student: str = ""
    scope: CalculationScope = DEFAULT_SCOPE
    #: 权重和 = Σ学分（只算计入口径且选了的课）
    total_credit: float = 0.0
    #: 加权和 = Σ(成绩×学分)
    weighted_sum: float = 0.0
    #: 计入口径的课程（含未选的，便于回显）；顺序稳定
    course_scores: list[CourseScore] = field(default_factory=list)
    #: 因口径被排除的课（选修默认剔除）
    excluded_by_scope: list[ScopeDecision] = field(default_factory=list)
    #: 空/未选被跳过的课
    skipped_blank: list[str] = field(default_factory=list)
    #: 数字 0（缺考）计入的课 —— 必须出现在这里，红线 2
    zero_scores: list[str] = field(default_factory=list)
    #: 同名取高发生过的课
    deduped: list[str] = field(default_factory=list)
    #: 类别未识别、非"含全部"口径下被排除的课
    unknown_categories: list[str] = field(default_factory=list)
    #: 分数非法的课
    invalid_scores: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def counted_courses(self) -> list[CourseScore]:
        """真正进了分子分母的课。"""
        return [c for c in self.course_scores if c.counted]

    @property
    def weighted_average(self) -> float | None:
        """加权均分 ``Σ(成绩×学分)/Σ学分``；没有可计算的课则返回 ``None``。"""
        if self.total_credit <= 0.0:
            return None
        return self.weighted_sum / self.total_credit

    #: 别名，方便 UI 直接取用
    average = weighted_average

    def as_dict(self) -> dict[str, Any]:
        return {
            "student": self.student,
            "scope": self.scope.value,
            "total_credit": self.total_credit,
            "weighted_sum": self.weighted_sum,
            "weighted_average": self.weighted_average,
            "course_scores": [c.as_dict() for c in self.course_scores],
            "excluded_by_scope": [
                {
                    "name": d.name,
                    "category": d.category.value,
                    "credit": d.credit,
                    "reason": d.reason,
                }
                for d in self.excluded_by_scope
            ],
            "skipped_blank": list(self.skipped_blank),
            "zero_scores": list(self.zero_scores),
            "deduped": list(self.deduped),
            "unknown_categories": list(self.unknown_categories),
            "invalid_scores": list(self.invalid_scores),
            "warnings": list(self.warnings),
        }


@dataclass
class DedupReport:
    """同名取高的完整记录。"""

    course_scores: list[CourseScore] = field(default_factory=list)
    #: 发生过取高的课名
    deduped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 第 4 条：同名取高
# ---------------------------------------------------------------------------


def _norm_score(score: Any) -> float | None:
    """分数归一化：接受 ``CourseScore`` / 数字 / 文本型数字（红线 4）。"""
    if isinstance(score, CourseScore):
        return score.score
    if score is None:
        return None
    return to_float(score)


def _as_course_score(item: Any) -> CourseScore:
    """把 ``CourseScore`` 或 ``(Course, score)`` 统一成 :class:`CourseScore`。"""
    if isinstance(item, CourseScore):
        return item
    course, raw_score = item
    return CourseScore(
        name=course.name,
        category=course.category,
        credit=float(course.credit),
        score=_norm_score(raw_score),
        col_index=course.col_index,
    )


def _better(left: CourseScore, right: CourseScore) -> CourseScore:
    """两个同名校次取"成绩高那次"；一方没成绩时取有成绩的那个。

    Spec 第 2 章：同名课程出现两次（非补考）取成绩高那次，**对应那次的学分参与加权**；
    不叠加、不平均。补考不覆盖原成绩（红线 9），所以"取高"只适用于非补考的同名重复。
    """
    if left.score is None and right.score is None:
        return left
    if left.score is None:
        return right
    if right.score is None:
        return left
    if right.score > left.score:
        return right
    if right.score < left.score:
        return left
    # 同分时学分高者优先，保证结果确定；学分也相同则保持先出现的
    return right if right.credit > left.credit else left


def dedupe_courses(courses: Iterable[Any]) -> DedupReport:
    """同名课取成绩高那次，并保留**那一次的学分**。

    ★ Owner 已确认的业务含义（Spec 第 2 章规则表的落地口径）：

    同一门课在两个学期各出现一次、分数不同，这是**刷分**（为了提均分主动重修），
    不是补考 —— 因为**补考成绩根本不会出现在成绩单表格里**，
    表格上留下的永远是第一次考试成绩。所以：

    * 能看到的"同名两次"只可能是刷分 → **取高那次**
    * 取高时，**学分也用那一次的学分**（不叠加、不平均）
    * 看不到的补考 → 不需要也不应该由本程序去"覆盖"什么（红线 9），
      它压根没进过我们的数据

    接受两种输入，方便单表计算与跨表合并复用：

    * 已构造好的 :class:`CourseScore` 序列
    * ``(Course, score)`` 二元组序列

    跨学期合并（Spec 第 2 章"学期合并公式"）时，把两个表的课程拼成一个序列丢进来即可，
    合并后的 ``weighted_sum / total_credit`` 就是总加权 —— **绝不**对两个学期的均分再取平均
    （红线 8）。
    """
    report = DedupReport()
    winners: dict[str, CourseScore] = {}
    order: list[str] = []

    for item in courses:
        entry = _as_course_score(item)
        existing = winners.get(entry.name)
        if existing is None:
            winners[entry.name] = entry
            order.append(entry.name)
            continue

        best = _better(existing, entry)
        best.source_count = existing.source_count + entry.source_count
        winners[entry.name] = best

    for name in order:
        entry = winners[name]
        report.course_scores.append(entry)
        if entry.source_count > 1:
            report.deduped.append(name)
            if entry.score is not None:
                report.warnings.append(
                    f"课程「{name}」出现 {entry.source_count} 次，已按取高规则取 "
                    f"{entry.score:g} 分（{entry.credit:g} 学分）参与加权，不叠加、不平均"
                )
            else:
                report.warnings.append(
                    f"课程「{name}」出现 {entry.source_count} 次但都没有成绩，按未选处理"
                )

    return report


# ---------------------------------------------------------------------------
# 第 3 条：口径过滤
# ---------------------------------------------------------------------------


def scope_credits(scope: CalculationScope, courses: Iterable[Course]) -> float:
    """某口径下这些课的学分合计（只按类别筛，不判断空值）。"""
    allowed = scope.categories
    return sum(float(c.credit) for c in courses if c.category in allowed)


def filter_by_scope(
    courses: Iterable[CourseScore], scope: CalculationScope
) -> tuple[list[CourseScore], list[CourseScore], list[ScopeDecision]]:
    """按口径三分：计入的 / 排除的 / 因类别未识别被排除的。"""
    allowed = scope.categories
    included: list[CourseScore] = []
    excluded: list[CourseScore] = []
    decisions: list[ScopeDecision] = []

    for entry in courses:
        if entry.category is Category.UNKNOWN:
            if scope is CalculationScope.ALL:
                # "含全部"口径下才计入 unknown，且会带警告提示人工确认
                included.append(entry)
                continue
            decisions.append(
                ScopeDecision(
                    name=entry.name,
                    category=entry.category,
                    credit=entry.credit,
                    reason=f"类别未识别（{UNKNOWN}），默认口径不猜测其归属，已排除；"
                    "请在导入页人工确认类别",
                )
            )
            excluded.append(entry)
            continue

        if entry.category in allowed:
            included.append(entry)
        else:
            excluded.append(entry)
            decisions.append(
                ScopeDecision(
                    name=entry.name,
                    category=entry.category,
                    credit=entry.credit,
                    reason=f"类别「{entry.category.value}」不在口径「{scope.value}」内",
                )
            )

    return included, excluded, decisions


# ---------------------------------------------------------------------------
# 第 1 条：单表加权
# ---------------------------------------------------------------------------


def weighted_average(courses: Iterable[Any]) -> float | None:
    """``Σ(成绩×学分) / Σ学分``；没有可计算的课则返回 ``None``（不是 0，也不报错）。

    语义（红线 2）：

    * 空/未选 → 该课学分**不进分母**
    * 数字 0 → 该课学分进分母，分子记 0
    * 非法分数（超出 :data:`MIN_SCORE`~:data:`MAX_SCORE`）→ 与 :func:`calculate_student`
      一致，整门课排除，不进分子也不进分母
    """
    report = dedupe_courses(courses)
    total_credit = 0.0
    weighted_sum = 0.0
    for entry in report.course_scores:
        if not _is_valid_score(entry.score):
            continue
        assert entry.score is not None  # _is_valid_score 已保证
        total_credit += entry.credit
        weighted_sum += entry.score * entry.credit
    if total_credit <= 0.0:
        return None
    return weighted_sum / total_credit


def calculate_student(
    row: ExamRow,
    courses: Sequence[Course] | None = None,
    scope: CalculationScope = DEFAULT_SCOPE,
    parse_result: ParseResult | None = None,
) -> CalculationResult:
    """算一个学生的单表加权。

    Args:
        row: parser 产出的 :class:`~core.parser.ExamRow`。
        courses: 与 ``row.score_list`` **同序**的课程列表；省略时从 ``parse_result.courses`` 取。
        scope: 三档口径，默认"仅必修+限选"。
        parse_result: 省略 ``courses`` 时用它取课程列表。

    Returns:
        :class:`CalculationResult`。非法行（``row.is_valid`` 为 False）整体排除并给出警告，
        不做"带病计算"。
    """
    if courses is None:
        if parse_result is None:
            raise ValueError("calculate_student 需要 courses 或 parse_result 之一")
        courses = parse_result.courses

    result = CalculationResult(student=row.student, scope=scope)

    if not row.is_valid:
        result.warnings.append(
            f"学生「{row.student}」所在行未通过校验"
            f"（{'；'.join(row.warnings) or '未知原因'}），已整体排除，不参与加权"
        )
        result.course_scores = [
            CourseScore(
                name=course.name,
                category=course.category,
                credit=float(course.credit),
                score=None,
                col_index=course.col_index,
            )
            for course in courses
        ]
        return result

    raw_entries = [
        (course, row.score_list[index] if index < len(row.score_list) else None)
        for index, course in enumerate(courses)
    ]

    # 第 4 条：同名取高（取那一次的学分）
    dedup = dedupe_courses(raw_entries)
    result.deduped = list(dedup.deduped)
    result.warnings.extend(dedup.warnings)

    # 第 3 条：口径过滤
    included, _excluded, decisions = filter_by_scope(dedup.course_scores, scope)
    result.excluded_by_scope = decisions

    for entry in included:
        if entry.score is None:
            # 空/未选 → 跳过，学分不进分母（红线 2）
            entry.counted = False
            result.skipped_blank.append(entry.name)
        elif not _is_valid_score(entry.score):
            entry.counted = False
            entry.warnings.append(
                f"分数 {entry.score:g} 超出 {MIN_SCORE:g}~{MAX_SCORE:g} 合法区间，已排除出计算"
            )
            result.invalid_scores.append(entry.name)
        else:
            entry.counted = True
            result.total_credit += entry.credit
            result.weighted_sum += entry.score * entry.credit
            if entry.score == 0.0:
                # 数字 0 = 缺考，必须计入（红线 2）
                result.zero_scores.append(entry.name)

        if entry.category is Category.UNKNOWN:
            result.unknown_categories.append(entry.name)

        result.course_scores.append(entry)

    if not included:
        result.warnings.append(
            f"学生「{row.student}」在口径「{scope.value}」下没有任何课程可计算"
        )
    elif result.total_credit <= 0.0:
        result.warnings.append(
            f"学生「{row.student}」在口径「{scope.value}」下总学分为 0"
            "（所选课程全为空），无法给出均分"
        )
    if result.invalid_scores:
        result.warnings.append(
            f"学生「{row.student}」有非法分数被排除：{'、'.join(result.invalid_scores)}"
        )
    if result.unknown_categories:
        result.warnings.append(
            f"学生「{row.student}」有类别未识别课程被计入："
            f"{'、'.join(result.unknown_categories)}，仅在口径"
            f"「{CalculationScope.ALL.value}」下计入，请人工确认类别"
        )
    return result


def calculate_all(
    parse_result: ParseResult,
    scope: CalculationScope = DEFAULT_SCOPE,
) -> list[CalculationResult]:
    """整班计算（只看有效行，与 UI 全班列表一致）。"""
    return [
        calculate_student(row, parse_result=parse_result, scope=scope)
        for row in parse_result.valid_rows
    ]


def calculate_many(
    parse_result: ParseResult,
    scopes: Iterable[CalculationScope] = (
        CalculationScope.REQUIRED_LIMITED,
        CalculationScope.ALL,
        CalculationScope.REQUIRED_ONLY,
    ),
) -> dict[CalculationScope, list[CalculationResult]]:
    """一次算完三档口径，供 UI 三档切换直接取用（避免重复解析）。"""
    return {scope: calculate_all(parse_result, scope) for scope in scopes}


# ---------------------------------------------------------------------------
# 跨学期合并（红线 8 的公式锚点；真正的 append 属 core/archival.py）
# ---------------------------------------------------------------------------


def merge_semesters(*results: CalculationResult) -> tuple[float, float, float | None]:
    """学期合并：``(Σ上成绩×上学分 + Σ下成绩×下学分) / (Σ上学分 + Σ下学分)``。

    **红线 8**：禁止对两个学期的均分再取平均。本函数只做公式，学期 append、纵向累计、
    趋势 Δ、定型/可救分区属于 ``core/archival.py``（后续阶段）。
    """
    total_credit = sum(r.total_credit for r in results)
    weighted_sum = sum(r.weighted_sum for r in results)
    if total_credit <= 0.0:
        return total_credit, weighted_sum, None
    return total_credit, weighted_sum, weighted_sum / total_credit
