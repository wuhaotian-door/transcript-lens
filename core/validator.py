"""「所得学分」锚点校验（Spec 第 6 章第 5 条）。

Spec 原文：

    校验锚点：若表里有"所得学分"列，程序算出总学分与锚点差 > 0.5 → 弹窗警告。

为什么要有这个模块：``core/parser.py`` 只能抓到"值溢出到课程列之外"这种错位。
**整行右移一格**时所有值仍落在已识别的课程列内，形状上完全合法，parser 无法判定 ——
此时「所得学分」列就是唯一的独立证据：锚点是教务系统自己算的，我们按分数重算一遍，
差得多说明我们读到的分数和教务系统的认知对不上，也就是错位了。

判定规则（刻意保持极简，不引入任何猜测性阈值）：

* 程序算出所得学分 = Σ(学分 × 1[分数 ≥ :data:`PASS_SCORE`])
* ``|锚点 - 程序算出| > 0.5`` → **标红弹窗**（这是 Spec 写死的唯一阈值）
* 锚点单元格为空 / 非数字 → 不比对、不猜，只记一笔数据缺口

本模块不做加权、不碰 UI、不联网。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.parser import (
    ANCHOR_HEADER_KEYWORDS,
    ParseResult,
    is_anchor_header,
    normalize_header,
    strip_blank_marker,
    to_float,
)

__all__ = [
    "ANCHOR_HEADER_KEYWORDS",
    "DEVIATION_THRESHOLD",
    "PASS_SCORE",
    "AnchorReport",
    "CreditAnchor",
    "StudentAnchorCheck",
    "check_credit_anchor",
    "count_credit",
    "describe_alerts",
    "find_anchor_columns",
    "is_anchor_header",
    "parse_and_validate",
]

#: Spec 第 6 章第 5 条写死的阈值：差值 > 0.5 才弹窗
DEVIATION_THRESHOLD = 0.5

#: 及格线（含）。**当前为 0.0 —— 挂科不计入会被排除的课程，但学分照算。**
#:
#: 口径来源（Owner 明确指定，2026 确认）：**挂科科目也算所得学分，成绩以第一次考试为准，
#: 补考不覆盖第一次成绩。** 这与 Spec 第 2 章「补考不覆盖原成绩」是自洽的：
#: 学分在**选课那一刻**就已归属，补考只改写成绩、不改写学分归属。
#:
#: 因此所得学分 = Σ(选了这门课的学分)，其中：
#:   * 数字 0（缺考）→ 考了但没通过，**学分照算**（它在加权里也照样进分母，红线 2）
#:   * 空 / ``-`` / ``未选`` → 没选这门课，学分不算
#:
#: 若贵校「所得学分」列实际是"及格才计"，把这里改成 ``60.0`` 即可，其余代码不用动。
PASS_SCORE = 0.0

#: 避免 22.000000000000004 这种浮点残渣导致误判
_EPS = 1e-9


def _round_credit(value: float) -> float:
    """学分保留 3 位小数，消掉浮点残渣。"""
    return round(value + _EPS, 3)


@dataclass
class CreditAnchor:
    """一个「所得学分」锚点列。"""

    col_index: int
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {"col_index": self.col_index, "label": self.label}


@dataclass
class StudentAnchorCheck:
    """一个学生的锚点比对结果。"""

    student: str
    row_index: int
    #: 教务系统自己写的「所得学分」；锚点格为空或非数字时为 None
    anchor_value: float | None
    #: 程序按分数重算的所得学分（Σ 及格课学分）
    computed_credit: float
    #: 差值 = 程序算出 - 锚点；无法比对时为 None
    deviation: float | None
    anchor_cell: Any = None
    anchor_label: str = ""
    ok: bool = True
    #: 差值超阈值 → 必须弹窗
    exceeded: bool = False
    #: 锚点不可用（空/非数字），不做判断
    anchor_missing: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "student": self.student,
            "row_index": self.row_index,
            "anchor_value": self.anchor_value,
            "computed_credit": self.computed_credit,
            "deviation": self.deviation,
            "anchor_cell": self.anchor_cell,
            "anchor_label": self.anchor_label,
            "ok": self.ok,
            "exceeded": self.exceeded,
            "anchor_missing": self.anchor_missing,
            "note": self.note,
        }


@dataclass
class AnchorReport:
    """整张表的锚点校验报告。"""

    checks: list[StudentAnchorCheck] = field(default_factory=list)
    anchors: list[CreditAnchor] = field(default_factory=list)
    threshold: float = DEVIATION_THRESHOLD
    #: 锚点列有多个（表头重复）时的提示
    warnings: list[str] = field(default_factory=list)

    @property
    def exceeded(self) -> list[StudentAnchorCheck]:
        """差值超阈值、必须弹窗的行。"""
        return [c for c in self.checks if c.exceeded]

    @property
    def missing(self) -> list[StudentAnchorCheck]:
        """锚点格为空/非数字、无法比对的行。"""
        return [c for c in self.checks if c.anchor_missing]

    @property
    def passed(self) -> list[StudentAnchorCheck]:
        return [c for c in self.checks if c.ok and not c.anchor_missing]

    @property
    def has_alert(self) -> bool:
        """是否需要弹窗（红线：差 > 0.5 必须提醒）。"""
        return bool(self.exceeded)

    @property
    def alerts(self) -> list[str]:
        """可直接塞进弹窗的文案列表。"""
        return describe_alerts(self)


def find_anchor_columns(df: pd.DataFrame | None) -> list[CreditAnchor]:
    """扫整张表，找出所有「所得学分」锚点列。

    逐格扫描而不是只看表头行，是因为有的表把锚点标题写在数据区上方或右侧。
    """
    if df is None or df.empty:
        return []
    found: dict[int, CreditAnchor] = {}
    for r in range(df.shape[0]):
        for c in range(df.shape[1]):
            if c in found:
                continue
            text = normalize_header(df.iloc[r].iloc[c])
            if text and is_anchor_header(text):
                found[c] = CreditAnchor(col_index=c, label=text)
    return [found[c] for c in sorted(found)]


def _rule_text(pass_score: float) -> str:
    """把当前口径翻译成人话，塞进告警文案里。

    口径曾从"及格才计"改成"选了就计"，当时这条写死的"Σ 及格课学分"就变成了
    误导性文案（用户会以为挂科没算）。所以这里改成按 :data:`PASS_SCORE` 动态生成，
    并由 ``test_anchor_alert_wording_matches_policy`` 钉住。
    """
    if pass_score <= 0.0:
        return "Σ 选了这门课的学分（挂科与缺考都照算）"
    return f"Σ 及格（≥{pass_score:g} 分）课程的学分"


def count_credit(
    scores: Any,
    credits: Any,
    pass_score: float = PASS_SCORE,
) -> float:
    """按分数重算「所得学分」= Σ(学分 × 1[分数 ≥ :data:`PASS_SCORE`])。

    在默认口径 :data:`PASS_SCORE` = 0.0 下，语义就是**"选了课就拿到学分，与考多少分无关"**：

    * 空 / 未选（``None``）→ 没选这门课，学分不算
    * 数字 ``0``（缺考）→ 考了没通过，但**学分照样算**（补考不改学分归属）
    * 挂科（如 58 分）→ 同上，学分照算
    * 文本型数字 ``"85.000000"`` → 强转 float 后照常判断（红线 4）

    传入 ``pass_score=60.0`` 可切换成"及格才计学分"的另一种口径。
    """
    total = 0.0
    for score, credit in zip(scores, credits):
        if score is None:
            continue
        value = to_float(score)  # 文本型数字强转（红线 4）
        if value is None:
            continue
        if value >= pass_score:
            total += float(credit)
    return _round_credit(total)


def check_credit_anchor(
    frame: pd.DataFrame,
    parse_result: ParseResult,
    threshold: float = DEVIATION_THRESHOLD,
    pass_score: float = PASS_SCORE,
) -> AnchorReport:
    """对整表跑一遍锚点校验。

    Args:
        frame: 原始 DataFrame（表头行 + 数据行，就是喂给 ``parse_dataframe`` 的那个）。
        parse_result: :func:`core.parser.parse_dataframe` 的结果。
        threshold: 差值阈值，默认 :data:`DEVIATION_THRESHOLD`（Spec 写死的 0.5）。
        pass_score: 计学分的分数线，默认 :data:`PASS_SCORE`。告警文案会跟着它变。

    Returns:
        :class:`AnchorReport`。表里没有锚点列时返回空报告（``has_alert`` 为 False）。
    """
    report = AnchorReport(threshold=threshold)

    anchors = find_anchor_columns(frame)
    report.anchors = anchors
    if not anchors:
        return report
    if len(anchors) > 1:
        report.warnings.append(
            f"发现 {len(anchors)} 个「所得学分」列（第 "
            f"{'、'.join(str(a.col_index + 1) for a in anchors)} 列），"
            f"校验只用第一个：{anchors[0].label}"
        )
    anchor = anchors[0]
    credits = [float(c.credit) for c in parse_result.courses]

    for row in parse_result.rows:
        if not row.is_valid:
            continue  # 行本来就非法，已单独标红，不再叠加一条锚点警告
        if row.row_index >= frame.shape[0]:
            continue
        cell = frame.iloc[row.row_index].iloc[anchor.col_index]
        anchor_value = _anchor_value(cell)
        computed = count_credit(row.score_list, credits, pass_score=pass_score)
        check = StudentAnchorCheck(
            student=row.student,
            row_index=row.row_index,
            anchor_value=anchor_value,
            computed_credit=computed,
            deviation=None,
            anchor_cell=cell,
            anchor_label=anchor.label,
        )

        if anchor_value is None:
            check.anchor_missing = True
            check.ok = False
            check.note = (
                f"锚点「{anchor.label}」单元格为 {cell!r}，无法比对，"
                "请人工核对该生所得学分"
            )
        else:
            deviation = _round_credit(computed - anchor_value)
            check.deviation = deviation
            if abs(deviation) > threshold + _EPS:
                check.exceeded = True
                check.ok = False
                check.note = (
                    f"程序算出所得学分 {computed:g}（{_rule_text(pass_score)}），"
                    f"与表内锚点「{anchor.label}」{anchor_value:g} 相差 "
                    f"{abs(deviation):g} 学分（> {threshold:g}），"
                    "该行可能错位，请人工核对"
                )
            else:
                check.note = f"锚点一致：程序算出 {computed:g}，表内 {anchor_value:g}"
        report.checks.append(check)

    return report


def _anchor_value(cell: Any) -> float | None:
    """锚点单元格 → float；空/未选/非数字 → ``None``（不猜）。"""
    if strip_blank_marker(cell) is None:
        return None
    return to_float(cell)


def describe_alerts(report: AnchorReport) -> list[str]:
    """把报告转成可直接弹窗的文案列表：先超标行，再数据缺口。"""
    alerts: list[str] = []
    for check in report.exceeded:
        alerts.append(f"【第 {check.row_index + 1} 行 · {check.student}】{check.note}")
    for check in report.missing:
        alerts.append(f"【第 {check.row_index + 1} 行 · {check.student}】{check.note}")
    for warning in report.warnings:
        alerts.append(f"【锚点列】{warning}")
    return alerts


def parse_and_validate(
    frame: pd.DataFrame,
    threshold: float = DEVIATION_THRESHOLD,
) -> tuple[ParseResult, AnchorReport]:
    """便捷入口：解析 + 锚点校验一次做完（UI 导入页直接调这个）。

    延迟 import parser 的 ``parse_dataframe`` 会造成循环，所以这里用模块级函数。
    """
    from core.parser import parse_dataframe

    parsed = parse_dataframe(frame)
    return parsed, check_credit_anchor(frame, parsed, threshold=threshold)
