"""红标 / 黄标预警判定（从旧 ``ui/result_page.py`` 原样搬过来，逻辑一字未改）。

它**不是**算术 —— 它只决定"这一行要不要标红"，绝不参与加权。
及格线 60 分只影响标色，不影响任何均分。

分级：

* **红** = 必须当场处理：挂科 / 缺考 / 数据不可信（行非法、锚点对不上）
* **黄** = 数据可疑，谈之前先核对：待人工确认、分数异常被排除、均分偏低
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from core.calculator import CalculationResult

__all__ = [
    "DEFAULT_RULES",
    "LEVEL_DANGER",
    "LEVEL_WARNING",
    "PASS_SCORE",
    "FlagRules",
    "StudentFlag",
    "build_flags",
]

#: 分数及格线。低于它 = 挂科（只影响"标不标红"，不影响加权算法）
PASS_SCORE = 60.0

#: 预警级别
LEVEL_DANGER = "danger"
LEVEL_WARNING = "warning"


@dataclass
class FlagRules:
    """哪些情况算预警。分两级：红 = 必须当场处理；黄 = 数据可疑，谈之前先核对。"""

    #: 有挂科（0 < 分数 < 及格线）→ 红
    fail: bool = True
    #: 有缺考（数字 0 分计入）→ 红
    absent: bool = True
    #: 该行被锚点校验标超标 / 行本身非法（数据不可信）→ 红
    bad_data: bool = True
    #: 有课程待人工确认（类别或学分没抠出来）→ 黄
    needs_review: bool = True
    #: 有分数超出 0~100 被排除 → 黄
    invalid_score: bool = True
    #: 及格线，同时用于"均分低于它算黄标"
    pass_score: float = PASS_SCORE
    #: 均分低于它 → 黄（设 0 关闭这条）
    average_below: float = 60.0


DEFAULT_RULES = FlagRules()


@dataclass
class StudentFlag:
    """一个学生的预警汇总。"""

    student: str
    level: str = ""                     # "" / "danger" / "warning"
    reasons: list[str] = field(default_factory=list)
    failed_courses: list[str] = field(default_factory=list)
    absent_courses: list[str] = field(default_factory=list)
    reviewed_courses: list[str] = field(default_factory=list)
    invalid_courses: list[str] = field(default_factory=list)
    data_problem: str = ""

    @property
    def is_flagged(self) -> bool:
        return bool(self.level)

    @property
    def summary(self) -> str:
        """一行摘要，给表格末列用。"""
        return "；".join(self.reasons)

    @property
    def label(self) -> str:
        """表格里那一格显示的短标签。"""
        if self.level == LEVEL_DANGER:
            return "红标"
        if self.level == LEVEL_WARNING:
            return "黄标"
        return ""


def build_flags(
    results: Sequence[CalculationResult],
    *,
    rules: FlagRules = DEFAULT_RULES,
    invalid_rows: Sequence[Any] = (),
    anchor_exceeded: Sequence[Any] = (),
) -> dict[str, StudentFlag]:
    """按 :class:`FlagRules` 给每个学生算预警，返回 ``学生名 → StudentFlag``。

    Args:
        results: 某口径下的整班计算结果。
        invalid_rows: ``parse_result.invalid_rows``（行本身非法，不参与计算）。
        anchor_exceeded: ``anchor_report.exceeded``（锚点超标行）。
    """
    invalid_names = {getattr(row, "student", "") for row in invalid_rows}
    anchor_names = {getattr(check, "student", "") for check in anchor_exceeded}

    flags: dict[str, StudentFlag] = {}
    for result in results:
        flag = StudentFlag(student=result.student)

        failed = [
            course.name
            for course in result.course_scores
            if course.counted and course.score is not None
            and 0 < course.score < rules.pass_score
        ]
        absent = [
            course.name
            for course in result.course_scores
            if course.counted and course.score == 0
        ]
        pending = [
            course.name
            for course in result.course_scores
            if course.category.value == "unknown"
            or any("人工确认" in warning for warning in course.warnings)
        ]

        if rules.fail and failed:
            flag.failed_courses = failed
            flag.reasons.append(f"挂科 {len(failed)} 门：{'、'.join(failed)}")
        if rules.absent and absent:
            flag.absent_courses = absent
            flag.reasons.append(f"缺考 {len(absent)} 门：{'、'.join(absent)}")

        data_problems: list[str] = []
        if result.student in invalid_names:
            data_problems.append("所在行未通过校验")
        if result.student in anchor_names:
            data_problems.append("「所得学分」锚点对不上")
        if data_problems:
            flag.data_problem = "；".join(data_problems)

        if flag.failed_courses or flag.absent_courses or (rules.bad_data and data_problems):
            flag.level = LEVEL_DANGER
        else:
            if rules.needs_review and pending:
                flag.reviewed_courses = pending
                flag.reasons.append(f"待人工确认 {len(pending)} 门：{'、'.join(pending)}")
            if rules.invalid_score and result.invalid_scores:
                flag.invalid_courses = list(result.invalid_scores)
                flag.reasons.append(f"分数异常被排除：{'、'.join(result.invalid_scores)}")
            average = result.weighted_average
            if (
                rules.average_below > 0
                and average is not None
                and average < rules.average_below
            ):
                flag.reasons.append(f"均分低于 {rules.average_below:g}")
            if flag.reasons:
                flag.level = LEVEL_WARNING

        # 数据问题说明放最后，便于用户先看学业问题
        if flag.data_problem:
            flag.reasons.append(f"数据问题：{flag.data_problem}")

        flags[result.student] = flag
    return flags
