"""谈话提纲三段式（Spec 第 7 章 · 本地规则模板）。

Spec 原文的样子：

    【事实】累计均分 XX，大二下高数 58（已定型/5 学分，补过已清挂），
            大三上英语 68→82（进步）
    【行动】本学期可抓：大物 72/3 学分提到 85 能拉回 0.1；已挂的不必再纠结
    【对标意愿=保研】核心课均分需 80+，当前差 2.3 分，剩余高学分课还有 3 门可冲；
            若本学期无起色建议早做两手准备

**这个模块完全不联网、不调任何大模型**（Spec 红线 1 与第 10 章"不做云"）。
云端适配器（``ai/cloud_adapter.py``）是后续阶段的事，且它只能拿到脱敏后的
编号 + 均分 + 学分 + 分数区间 + 意愿标签。

口径约定（与别处保持一致）：

* 空 = 未选（跳过）；数字 0 = 缺考（计入）—— 红线 2
* 补考不覆盖第一次成绩，已挂的课分数已定型 —— 红线 9
* 累计均分用总加权，不对各学期均分再平均 —— 红线 8
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from core.archival import StudentArchive, partition, term_average, trend
from core.calculator import CalculationScope

__all__ = [
    "ASPIRATION_TAGS",
    "ASPIRATION_TARGETS",
    "Outline",
    "build_outline",
    "estimate_gain",
    "render_plain_text",
]

#: 意愿标签（Spec 第 2 章：辅导员在 UI 手动勾选）
ASPIRATION_TAGS: tuple[str, ...] = ("保研", "考研", "就业", "考公", "留学", "未定")

#: ★ 各意愿的"核心课均分目标线"。**这是产品口径，Spec 没写死，按学校实际改这里。**
#: 值含义：``(目标均分, 一句话说明)``；``None`` 表示该意愿不看均分。
ASPIRATION_TARGETS: dict[str, tuple[float | None, str]] = {
    "保研": (80.0, "核心课均分需 80+"),
    "考研": (75.0, "初试靠单科，但复试看本科成绩，建议 75+"),
    "留学": (80.0, "多数院校要求均分 80+（GPA 3.0 起）"),
    "就业": (None, "就业主要看项目与实习，均分过线即可"),
    "考公": (None, "考公看笔试面试，均分不设硬线"),
    "未定": (None, "方向未定，建议先把均分守在及格线以上"),
}


@dataclass
class Outline:
    """三段式谈话提纲。"""

    student: str
    aspiration: str = "未定"
    #: 【事实】
    facts: list[str] = field(default_factory=list)
    #: 【行动】
    actions: list[str] = field(default_factory=list)
    #: 【对标意愿】
    benchmark: list[str] = field(default_factory=list)
    #: 需要辅导员自己核实/补充的提示
    caveats: list[str] = field(default_factory=list)
    #: 用来渲染的原始数字，便于 UI 做高亮
    numbers: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.facts or self.actions or self.benchmark)

    def as_dict(self) -> dict[str, Any]:
        return {
            "student": self.student,
            "aspiration": self.aspiration,
            "facts": list(self.facts),
            "actions": list(self.actions),
            "benchmark": list(self.benchmark),
            "caveats": list(self.caveats),
            "numbers": dict(self.numbers),
        }


def estimate_gain(
    current_score: float,
    credit: float,
    target_score: float,
    total_credit: float,
) -> float:
    """把这门课从 ``current_score`` 提到 ``target_score``，累计均分能拉回多少。

    ``Δ = (target − current) × credit / total_credit``

    这是 Spec 第 7 章"大物 72/3 学分提到 85 能拉回 0.1"背后的算式。
    ``total_credit`` 是**累计**学分（含所有已修课），不是单学期。
    """
    if total_credit <= 0:
        return 0.0
    return (target_score - current_score) * credit / total_credit


def reachable_average(
    earned_credit: float,
    earned_sum: float,
    remaining_credit: float,
    *,
    best_score: float = 100.0,
) -> float | None:
    """**还剩的课全部考到最高分**时的累计均分，即"理论上限"。

    ``(已得加权和 + 剩余学分 × best_score) / (已得学分 + 剩余学分)``

    这是判断一个目标"还够不够得着"的正确方式。比"还需 N 门课"靠谱得多 ——
    后者会给出学生根本修不到的课程数（例：还差 16.9 分却算出"还需 41 门课"）。

    ``remaining_credit`` 未知时（没有培养方案总学分）返回 ``None``，调用方据此降级措辞。
    """
    if remaining_credit <= 0:
        return None
    total = earned_credit + remaining_credit
    if total <= 0:
        return None
    return (earned_sum + remaining_credit * best_score) / total


def build_outline(
    archive: StudentArchive,
    aspiration: str = "未定",
    *,
    scope: CalculationScope = CalculationScope.REQUIRED_LIMITED,
    improvement_target: float = 10.0,
    top_n: int = 3,
) -> Outline:
    """按三段式生成谈话提纲。**纯本地规则，不联网。**

    Args:
        archive: 该学生的纵向档案。
        aspiration: 意愿标签（:data:`ASPIRATION_TAGS` 之一）。
        scope: 用哪个口径谈（默认必修+限选，与结果页默认一致）。
        improvement_target: "还能冲"的课程按"提 X 分"估算，默认 +10 分。
        top_n: 【行动】里最多列几门课。
    """
    outline = Outline(student=archive.student, aspiration=aspiration)
    if aspiration not in ASPIRATION_TARGETS:
        outline.caveats.append(
            f"意愿标签「{aspiration}」不在预设里（{'/'.join(ASPIRATION_TAGS)}），"
            "对标建议需要人工判断"
        )

    cumulative = archive.cumulative_average
    total_credit = archive.total_credit
    terms = archive.terms
    info = partition(archive, scope)
    trend_info = trend(archive, scope)

    # ---------------- 【事实】 ----------------
    if cumulative is None:
        outline.facts.append("档案里还没有可计算的成绩（所有课都是空/未选）。")
    else:
        outline.facts.append(
            f"累计均分 {cumulative:.1f}（{len(terms)} 个学期，累计 {total_credit:g} 学分）"
        )

    # 已定型的挂科：Spec 第 7 章写法"大二下高数 58（已定型/5 学分，补过已清挂）"
    for item in info["locked"]:
        outline.facts.append(
            f"{item['term']} {item['course']} 已挂科（分数已定型，补考不覆盖第一次成绩，"
            "不必再纠结）"
        )
    for item in info["absent"]:
        outline.facts.append(f"{item['term']} {item['course']} 缺考（0 分已计入均分）")

    # 进步/退步（Spec：大三上英语 68→82（进步））
    series = [
        (point["term"], point["average"])
        for point in trend_info["series"]
        if point["average"] is not None
    ]
    if len(series) >= 2:
        (first_term, first_avg), (last_term, last_avg) = series[0], series[-1]
        delta = last_avg - first_avg
        word = "进步" if delta > 0.5 else ("退步" if delta < -0.5 else "基本持平")
        outline.facts.append(
            f"{first_term} {first_avg:.1f} → {last_term} {last_avg:.1f}"
            f"（{word} {delta:+.1f}）"
        )

    # ---------------- 【行动】 ----------------
    # 可冲的课分两类，绝不混为一谈：
    #   * 缺考（0 分）→ 只能"赶紧安排补考"，把它说成"提到 X 分"是误导
    #   * 考过但分数还能涨 → 按"提 improvement_target 分"估算能拉回多少均分
    candidates: list[tuple[float, str, float, float, str]] = []
    retake_needed: list[tuple[str, str, float]] = []
    allowed_categories = {c.value for c in scope.categories}
    for record in archive.sorted_records:
        for course in record.courses:
            if course.score is None:
                continue
            if course.category not in allowed_categories:
                continue  # 口径外（例如选修）不列入可冲，避免和默认口径打架
            if course.score == 0.0:
                retake_needed.append((record.term, course.name, course.credit))
                continue
            target = min(100.0, course.score + improvement_target)
            if target <= course.score:
                continue
            gain = estimate_gain(
                course.score, course.credit, target, total_credit or 1.0
            )
            candidates.append((gain, course.name, course.score, course.credit, record.term))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    if retake_needed:
        detail = "、".join(f"{term} {name}（{credit:g} 学分）"
                          for term, name, credit in retake_needed)
        outline.actions.append(
            f"缺考 {len(retake_needed)} 门要马上确认能否补考：{detail}；"
            "补考出来的分数会直接改写累计均分（学分已经在分母里了）"
        )
    if candidates and cumulative is not None:
        for gain, name, score, credit, term in candidates[:top_n]:
            outline.actions.append(
                f"{term} {name} {score:g}/{credit:g} 学分提到 "
                f"{min(100.0, score + improvement_target):g} 能拉回 {gain:.2f}"
            )
    elif cumulative is not None and not retake_needed:
        outline.actions.append("没有明显可冲的课（要么都到高分段了，要么都没选）。")

    if info["locked"]:
        outline.actions.append(
            f"已挂的 {len(info['locked'])} 门不必再纠结（分数已定型），"
            "把精力放在还能改的课上"
        )
    if info["remaining_terms"]:
        outline.actions.append(
            f"按日历还剩 {info['remaining_terms']} 个学期（当前最新学期："
            f"{info['latest_term'] or '未标注'}）"
        )
    else:
        outline.actions.append("已到大四下学期，基本没有可改的空间了，重点转向两手准备")

    # ---------------- 【对标意愿】 ----------------
    target, note = ASPIRATION_TARGETS.get(aspiration, (None, ""))
    if target is None:
        outline.benchmark.append(f"【对标意愿={aspiration}】{note}")
    elif cumulative is None:
        outline.benchmark.append(
            f"【对标意愿={aspiration}】{note}；但当前给不出累计均分，无法算差距"
        )
    else:
        gap = target - cumulative
        if gap <= 0:
            outline.benchmark.append(
                f"【对标意愿={aspiration}】{note}，当前 {cumulative:.1f} 已达标"
                f"（高出 {abs(gap):.1f} 分），保持即可"
            )
        else:
            outline.benchmark.append(
                _gap_sentence(
                    aspiration, note, cumulative, gap, archive, info, improvement_target
                )
            )

    # ---------------- 【需要人工核实】 ----------------
    if archive.failed_total and not info["locked"]:
        outline.caveats.append("档案里记到过挂科，但没定位到具体课程，请人工核对")
    if not archive.student_id:
        outline.caveats.append("这名学生没有学号，纵向累计靠姓名匹配，同名学生有串联风险")
    if len(archive.records) > len(terms):
        outline.caveats.append("有学期被导入过多次，时间轴上可能重复，请核对")
    if cumulative is not None and info["locked"]:
        outline.caveats.append(
            "挂科课的分数仍计入累计均分（补考不覆盖第一次成绩），"
            "谈话时注意区分「已定型」与「还能改」"
        )

    outline.numbers = {
        "cumulative_average": cumulative,
        "total_credit": total_credit,
        "term_count": len(terms),
        "terms": list(terms),
        "target": target,
        "gap": (target - cumulative) if (target is not None and cumulative is not None) else None,
        "locked_count": len(info["locked"]),
        "absent_count": len(info["absent"]),
        "remaining_terms": info["remaining_terms"],
        "delta": trend_info["delta"],
    }
    return outline


def _gap_sentence(
    aspiration: str,
    note: str,
    cumulative: float,
    gap: float,
    archive: StudentArchive,
    info: dict[str, Any],
    improvement: float,
) -> str:
    """写【对标意愿】那一条。**必须说实话**：目标够不着就直说够不着。

    曾经的写法是"按每门提 10 分估算，还需约 N 门课"，会算出"还需 41 门课"
    这种学生根本修不到的数字 —— 那是给假承诺。现在按三步走：

    1. 估剩余学分（用**该生自己的**平均每学期学分，而不是拍脑袋的 20）
    2. 算理论上限 :func:`reachable_average`（剩余课全部满分）
    3. 上限够不到 → 明说"即使全部满分也差 X 分"，建议两手准备
       上限够得到 → 给"剩余课需要平均考到多少分"，并且只有 ≤100 才说得出口
    """
    prefix = f"【对标意愿={aspiration}】{note}，当前 {cumulative:.1f} 差 {gap:.1f} 分"
    remaining_terms = int(info.get("remaining_terms") or 0)
    if remaining_terms <= 0:
        return prefix + "，且已无剩余学期，来不及了，建议直接转向两手准备"

    per_term = estimate_credits_per_term(archive)
    remaining_credit = per_term * remaining_terms
    target = cumulative + gap
    ceiling = reachable_average(
        archive.total_credit, archive.weighted_sum, remaining_credit
    )
    basis = (
        f"剩余 {remaining_terms} 个学期约 {remaining_credit:g} 学分"
        f"（按该生已修 {per_term:.1f} 学分/学期推算，真实值需核对培养方案）"
    )
    if ceiling is None or ceiling < target:
        shown = f"{ceiling:.1f}" if ceiling is not None else "未知"
        return (
            f"{prefix}；{basis}，即使这些课**全部拿满分**，累计也只有 {shown} 分，"
            "**够不到目标**。建议尽快把重心转到两手准备"
            "（就业/考研/留学里选一条主线）"
        )

    # 上限够得到：反推剩余课需要平均考多少分
    #   目标总加权和 = (已得学分 + 剩余学分) × 目标均分
    #   剩余课需平均分 = (目标总加权和 − 已得加权和) / 剩余学分
    required_avg = (
        target * (archive.total_credit + remaining_credit) - archive.weighted_sum
    ) / remaining_credit
    if required_avg > 100.0:  # 理论上限够得到但需要超 100 分 → 实际不可能
        return (
            f"{prefix}；{basis}，需要平均考到 {required_avg:.1f} 分，"
            "**这不可能**。建议尽快把重心转到两手准备"
        )
    return (
        f"{prefix}；{basis}，剩余课需要平均考到 {required_avg:.1f} 分才能追上，"
        f"比每门提 {improvement:g} 分的常规目标高，建议把精力集中在高学分课上"
    )


def estimate_credits_per_term(archive: StudentArchive, default: float = 15.0) -> float:
    """按**该生自己的**历史推算平均每学期修多少学分。

    比拍脑袋写死 20 靠谱：一个每学期只修 10 学分的学生，不该被按 20 学分去估算
    未来能修多少。没有任何历史时退回 ``default``。
    """
    terms = len(archive.terms)
    if terms <= 0 or archive.total_credit <= 0:
        return default
    return archive.total_credit / terms


def render_plain_text(outline: Outline) -> str:
    """把提纲渲染成 Spec 第 7 章那种纯文本，便于复制到谈话记录里。"""
    lines = [f"【谈话提纲】{outline.student}　意愿：{outline.aspiration}", ""]

    def section(title: str, items: Sequence[str]) -> None:
        if not items:
            return
        lines.append(title)
        lines.extend(f"　　{text}" for text in items)
        lines.append("")

    section("【事实】", outline.facts)
    section("【行动】", outline.actions)
    section("【对标意愿】", outline.benchmark)
    section("【需要人工核实】", outline.caveats)
    return "\n".join(lines).rstrip() + "\n"
