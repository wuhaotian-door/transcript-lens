"""🎯 侧写辅导页 —— 本次的核心新增。

★ 文件名说明：Spec 五/六 把这一页定名为 ``profile_page.py``（"侧写"），
  所以**个人中心**那一页在 :mod:`ui.pages.account_page`，别走错。

三层结构（Spec 六）::

    第一层 · 学业肖像卡片     均分 / 趋势 / 已定型短板 / 可抢救项
    第二层 · 发展意愿         QButtonGroup + 6 个 QRadioButton → 落加密盘
    第三层 · 辅导提纲         本地规则拼三段文字（只读）+ 一键复制

★ 关于"不重新算"
----------------

Spec 说「数据来源：student dict 里已有的字段，不重新算」。本页严格遵守：
均分、趋势、定型/可救分区全部来自 :mod:`core.archival` 的
``trend()`` / ``partition()`` / ``merge_records()`` —— 那些是 core 里**已经有**的函数，
不是本页新写的算术。

唯一的例外是「提至 85 可 +0.X」这一句：它天生是个假设运算，不算是算不出来的。
处理办法是**复用 core 自己的** ``core.calculator.weighted_average`` ——
把某一门课的分数换掉之后，把整串课程重新喂给它。这样"假设分"和"实际分"
出自同一份实现，**绝不会出现两个数字打架**。本页自己一行加减乘除都没写。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.archival import merge_records, partition, trend
from core.calculator import CalculationScope, weighted_average
from core.crypto_store import CryptoError, CryptoStore
from ui.anim import make_card
from ui.intentions import INTENTION_OPTIONS, load_intentions, save_intention

__all__ = ["ProfilePage", "RESCUE_TARGET"]

#: "提一提"的目标分（Spec 六：附一句"提至85可+0.X"）
RESCUE_TARGET = 85.0

#: 趋势判定的死区：|Δ| 小于这个值就当"平稳"
TREND_EPS = 0.5

#: 可抢救项最多列几门（列表太长反而看不清重点）
MAX_RESCUE = 5


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


class ProfilePage(QWidget):
    """侧写辅导页。"""

    def __init__(self, store: CryptoStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore()
        self._username = ""
        self._payload: dict[str, Any] | None = None
        self._intentions: dict[str, str] = {}
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        """切账户时调用（登录、切换账号后）。会重新读这个人的意愿。"""
        self._username = username or ""
        self._intentions = (
            load_intentions(self._store, self._username) if self._username else {}
        )
        if self._payload is not None:
            self.show_student(self._payload)

    def show_student(self, payload: dict[str, Any]) -> None:
        """接住智育分析页传来的学生包。"""
        self._payload = payload
        self._empty.hide()
        self._body.show()
        self._render()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(0)

        # --- 没选学生时的占位 ---
        self._empty = QLabel(
            "请先在「📊 智育分析」导入成绩表，然后双击一名学生\n"
            "（或点该行的「侧写」按钮）"
        )
        self._empty.setObjectName("Placeholder")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._empty, 1)

        # --- 有学生时的主体 ---
        self._body = QWidget()
        body_box = QVBoxLayout(self._body)
        body_box.setContentsMargins(0, 0, 0, 0)
        body_box.setSpacing(14)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._inner_box = QVBoxLayout(inner)
        self._inner_box.setContentsMargins(0, 0, 6, 0)
        self._inner_box.setSpacing(14)

        self._inner_box.addWidget(self._build_portrait())
        self._inner_box.addWidget(self._build_intention())
        self._inner_box.addWidget(self._build_outline())
        self._inner_box.addStretch(1)

        scroll.setWidget(inner)
        body_box.addWidget(scroll, 1)
        root.addWidget(self._body, 1)
        self._body.hide()

    # --- 第一层：学业肖像 ---
    def _build_portrait(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 18, 20, 18)
        box.setSpacing(10)

        top = QHBoxLayout()
        self._portrait_title = QLabel("学业肖像")
        self._portrait_title.setObjectName("H2")
        top.addWidget(self._portrait_title)
        top.addSpacing(12)

        self._avg_label = QLabel("—")
        self._avg_label.setObjectName("H1")
        top.addWidget(self._avg_label)

        self._trend_tag = QLabel("—")
        self._trend_tag.setObjectName("TagGray")
        top.addWidget(self._trend_tag)
        top.addStretch(1)
        box.addLayout(top)

        self._avg_sub = QLabel("")
        self._avg_sub.setObjectName("Muted")
        self._avg_sub.setWordWrap(True)
        box.addWidget(self._avg_sub)

        box.addWidget(self._section("已定型短板", "补考不改分（红线 9），不必再纠结"))
        self._locked_box = QVBoxLayout()
        self._locked_box.setSpacing(4)
        box.addLayout(self._locked_box)

        box.addWidget(self._section("可抢救项", "分不高、但提一提能拉均分"))
        self._rescue_box = QVBoxLayout()
        self._rescue_box.setSpacing(4)
        box.addLayout(self._rescue_box)
        return card

    def _section(self, title: str, hint: str) -> QWidget:
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("H3")
        box.addWidget(label)
        note = QLabel(hint)
        note.setObjectName("Muted")
        box.addWidget(note)
        box.addStretch(1)
        return holder

    # --- 第二层：发展意愿 ---
    def _build_intention(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(8)

        title = QLabel("发展意愿")
        title.setObjectName("H2")
        box.addWidget(title)

        note = QLabel("选完立即存进本地加密文件；下次打开这名学生还在。")
        note.setObjectName("Muted")
        box.addWidget(note)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._intent_group = QButtonGroup(self)
        self._intent_group.setExclusive(True)
        self._intent_buttons: dict[str, QRadioButton] = {}
        for option in INTENTION_OPTIONS:
            radio = QRadioButton(option)
            radio.toggled.connect(
                lambda checked, name=option: self._on_intention(name) if checked else None
            )
            self._intent_group.addButton(radio)
            self._intent_buttons[option] = radio
            row.addWidget(radio)
        row.addStretch(1)
        box.addLayout(row)

        self._intent_status = QLabel("")
        self._intent_status.setObjectName("Muted")
        box.addWidget(self._intent_status)
        return card

    # --- 第三层：辅导提纲 ---
    def _build_outline(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("辅导提纲")
        title.setObjectName("H2")
        head.addWidget(title)
        note = QLabel("本地规则拼出来的，不联网、不调 AI")
        note.setObjectName("Muted")
        head.addWidget(note)
        head.addStretch(1)

        self._copy_btn = QPushButton("📋  一键复制")
        self._copy_btn.setObjectName("Ghost")
        self._copy_btn.setMinimumHeight(36)
        self._copy_btn.clicked.connect(self._do_copy)
        head.addWidget(self._copy_btn)
        box.addLayout(head)

        self._outline = QTextEdit()
        self._outline.setObjectName("Outline")
        self._outline.setReadOnly(True)
        self._outline.setMinimumHeight(210)
        self._outline.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        box.addWidget(self._outline)
        return card

    # ---------------------------------------------------------------- 渲染
    def _render(self) -> None:
        payload = self._payload
        if payload is None:
            return
        name = payload["student"]
        results = payload.get("results") or {}
        archive = payload.get("archive")

        default = results.get(CalculationScope.REQUIRED_LIMITED)
        overall = results.get(CalculationScope.ALL)

        self._portrait_title.setText(f"{name} · 学业肖像")
        self._avg_label.setText(_fmt(default.weighted_average if default else None))

        # --- 趋势（core.archival.trend）---
        trend_info = trend(archive) if archive is not None else None
        delta = trend_info.get("delta") if trend_info else None
        if delta is None:
            self._trend_tag.setText("→ 单次导入，暂无趋势")
            self._trend_tag.setObjectName("TagGray")
            terms_text = "只导入了 1 个学期"
        elif delta > TREND_EPS:
            self._trend_tag.setText(f"↑ 进步 {delta:+.2f}")
            self._trend_tag.setObjectName("TagGreen")
            terms_text = _terms_text(trend_info)
        elif delta < -TREND_EPS:
            self._trend_tag.setText(f"↓ 下滑 {delta:+.2f}")
            self._trend_tag.setObjectName("TagOrange")
            terms_text = _terms_text(trend_info)
        else:
            self._trend_tag.setText(f"→ 平稳 {delta:+.2f}")
            self._trend_tag.setObjectName("TagGray")
            terms_text = _terms_text(trend_info)
        self._repolish(self._trend_tag)

        parts = [f"仅必修+限选均分 {_fmt(default.weighted_average if default else None)}"]
        if overall is not None:
            parts.append(f"全口径 {_fmt(overall.weighted_average)}")
        parts.append(terms_text)
        self._avg_sub.setText("　·　".join(parts))

        # --- 已定型短板 / 可抢救项（全部来自 core.archival）---
        self._clear_layout(self._locked_box)
        self._clear_layout(self._rescue_box)

        part = partition(archive) if archive is not None else None
        locked = list(part.get("locked", [])) if part else []
        absent = list(part.get("absent", [])) if part else []

        if locked:
            for entry in locked:
                credit = _credit_of(archive, entry.get("term", ""), entry.get("course", ""))
                text = f"· {entry.get('course', '?')}"
                if credit is not None:
                    text += f"　{credit:g} 学分"
                text += f"　（{entry.get('term', '')} 已定型）"
                self._locked_box.addWidget(self._tagged(text, "TagGray"))
        else:
            self._locked_box.addWidget(self._tagged("· 没有已定型的挂科课", "TagGray"))

        for entry in absent:
            self._locked_box.addWidget(
                self._tagged(
                    f"· {entry.get('course', '?')}　缺考（还能安排补考，"
                    f"{entry.get('term', '')}）",
                    "TagOrange",
                )
            )

        rescues = self._compute_rescues(payload)
        if rescues:
            for course_name, credit, score, gain in rescues:
                self._rescue_box.addWidget(
                    self._tagged(
                        f"· {course_name}　{credit:g} 学分　当前 {score:g} 分　→　"
                        f"提至 {RESCUE_TARGET:g} 可 {gain:+.3f}",
                        "TagOrange",
                    )
                )
        else:
            self._rescue_box.addWidget(
                self._tagged("· 没有明显可抢救的课（分数都已达标或学分太小）", "TagGray")
            )

        # --- 意愿回填 ---
        current = self._intentions.get(name, "")
        for option, radio in self._intent_buttons.items():
            radio.blockSignals(True)
            radio.setChecked(option == current)
            radio.blockSignals(False)
        self._intent_status.setText(
            f"当前意愿：{current}" if current else "还没有选意愿。"
        )

        # --- 提纲 ---
        self._outline.setPlainText(self._build_outline_text(payload, rescues))

    def _compute_rescues(self, payload: dict[str, Any]) -> list[tuple[str, float, float, float]]:
        """算出"提至 85 能拉多少"。

        ★ 算术全部由 ``core.calculator.weighted_average`` 做 —— 本函数里
          **一行加减乘除都没有**。做法是把某一门课的分数换掉，再把整串课程
          原样喂回 core，看它给出的均分差多少。
        """
        archive = payload.get("archive")
        base = None
        if archive is not None:
            base = merge_records(archive)
        else:
            base = payload.get("results", {}).get(CalculationScope.REQUIRED_LIMITED)
        if base is None or base.weighted_average is None:
            return []

        courses = list(base.counted_courses)
        if not courses:
            return []

        out: list[tuple[str, float, float, float]] = []
        for course in courses:
            if course.score is None or course.score >= RESCUE_TARGET:
                continue
            hypothetical = [
                dataclasses.replace(item, score=RESCUE_TARGET) if item is course else item
                for item in courses
            ]
            new_average = weighted_average(hypothetical)
            if new_average is None:
                continue
            gain = new_average - base.weighted_average
            if gain <= 0:
                continue
            out.append((course.name, float(course.credit), float(course.score), gain))

        out.sort(key=lambda row: row[3], reverse=True)
        return out[:MAX_RESCUE]

    # ---------------------------------------------------------------- 提纲
    def _build_outline_text(
        self, payload: dict[str, Any], rescues: list[tuple[str, float, float, float]]
    ) -> str:
        """三段式提纲（Spec 七）。

        **纯字符串拼接**：不联网、不调 AI、不做算术 —— 数字全部来自上面
        已经算好的结果。
        """
        name = payload["student"]
        results = payload.get("results") or {}
        archive = payload.get("archive")
        default = results.get(CalculationScope.REQUIRED_LIMITED)
        overall = results.get(CalculationScope.ALL)
        flag = payload.get("flag")

        avg = default.weighted_average if default is not None else None
        all_avg = overall.weighted_average if overall is not None else None
        trend_info = trend(archive) if archive is not None else None
        delta = trend_info.get("delta") if trend_info else None
        part = partition(archive) if archive is not None else None
        locked = list(part.get("locked", [])) if part else []
        absent = list(part.get("absent", [])) if part else []
        intention = self._intentions.get(name, "")

        # --- 一、事实陈述：念数 ---
        fact: list[str] = [f"{name}　仅必修+限选均分 {_fmt(avg)}"]
        if all_avg is not None:
            fact.append(f"全口径 {_fmt(all_avg)}")
        if delta is not None:
            if delta > TREND_EPS:
                fact.append(f"趋势较首次导入上升 {delta:+.2f}")
            elif delta < -TREND_EPS:
                fact.append(f"趋势较首次导入下滑 {delta:+.2f}")
            else:
                fact.append(f"趋势平稳（{delta:+.2f}）")
        else:
            fact.append("目前只有一个学期的数据，趋势还判断不了")
        if flag is not None and flag.reasons:
            fact.append(f"预警：{'；'.join(flag.reasons)}")
        if locked:
            names = "、".join(entry.get("course", "?") for entry in locked)
            fact.append(f"已定型（补考不改分）的课：{names}")
        if absent:
            names = "、".join(entry.get("course", "?") for entry in absent)
            fact.append(f"缺考待安排：{names}")
        paragraph1 = "【事实】" + "。".join(fact) + "。"

        # --- 二、行动建议：抓大放小 ---
        act: list[str] = []
        if rescues:
            top = rescues[0]
            act.append(
                f"先抓 {top[0]}（{top[1]:g} 学分，当前 {top[2]:g} 分）："
                f"提到 {RESCUE_TARGET:g} 能把均分拉 {top[3]:+.3f}，"
                f"这是当前性价比最高的一门"
            )
            if len(rescues) > 1:
                rest = "、".join(
                    f"{row[0]}（{row[3]:+.3f}）" for row in rescues[1:4]
                )
                act.append(f"其次可以看：{rest}")
            act.append("学分大的课优先 —— 同样提 5 分，5 学分的课顶 1 学分的课五倍")
        else:
            act.append("眼下没有明显需要抢救的课，重心可以放在保持上")
        if locked:
            act.append("已经定型的那几门不必再纠结，补考不覆盖第一次成绩，谈它只会消耗情绪")
        if absent:
            act.append("缺考的课尽快安排补考，那是唯一还能改写的分数")
        remaining = part.get("remaining_terms") if part else None
        if remaining:
            act.append(f"按学期算后面还剩约 {remaining} 个学期可以冲")
        paragraph2 = "【行动】" + "。".join(act) + "。"

        # --- 三、对标意愿 ---
        if not intention:
            paragraph3 = (
                "【对标意愿】还没有登记发展意愿 —— 在上面「发展意愿」里选一个，"
                "这一段会自动换成对应的对标建议。"
            )
        else:
            paragraph3 = f"【对标意愿={intention}】" + _intention_advice(intention, avg, rescues)

        return "\n\n".join((paragraph1, paragraph2, paragraph3))

    # ---------------------------------------------------------------- 动作
    def _on_intention(self, option: str) -> None:
        payload = self._payload
        if payload is None:
            return
        name = payload["student"]
        if not self._username:
            self._intent_status.setText("还没有登录用户，意愿没地方存。")
            return
        try:
            path = save_intention(self._store, self._username, name, option)
        except (CryptoError, ValueError) as exc:
            QMessageBox.warning(self, "意愿没能保存", str(exc))
            self._intent_status.setText(f"保存失败：{exc}")
            return

        self._intentions[name] = option
        self._intent_status.setText(f"已保存：{name} → {option}（{path.name}）")
        self._render()

    def _do_copy(self) -> None:
        text = self._outline.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "还没有内容", "先选一名学生。")
            return
        QApplication.clipboard().setText(text)
        self._copy_btn.setText("✅  已复制")
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1600, lambda: self._copy_btn.setText("📋  一键复制"))

    # ---------------------------------------------------------------- 杂项
    @staticmethod
    def _tagged(text: str, tag: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(tag)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


# ---------------------------------------------------------------------------
# 纯字符串的辅助函数
# ---------------------------------------------------------------------------


def _terms_text(trend_info: dict[str, Any]) -> str:
    count = int(trend_info.get("term_count") or 0)
    first = trend_info.get("first_term") or ""
    last = trend_info.get("last_term") or ""
    if count >= 2 and first and last and first != last:
        return f"{first} → {last}（{count} 个学期）"
    return f"{count} 个学期"


def _credit_of(archive: Any, term: str, course: str) -> float | None:
    """从档案里翻出某门课的学分（只为显示，不参与任何算术）。"""
    if archive is None:
        return None
    for record in getattr(archive, "records", []):
        if record.term != term:
            continue
        for snapshot in record.courses:
            if snapshot.name == course:
                return float(snapshot.credit)
    return None


def _intention_advice(
    intention: str, average: float | None, rescues: list[tuple[str, float, float, float]]
) -> str:
    """按意愿给一句对标建议。**纯文案，不含任何计算。**"""
    gap = ""
    if average is not None:
        gap = f"当前 {average:.2f} 分"

    if intention == "保研":
        body = (
            f"保研看的是核心课均分，通常要 80 以上。{gap}。"
            "先说结论：哪几门高学分的必修课还能动，就先动它们；"
            "保研名额是排位赛，不是及格赛，要盯的是相对位置而不是绝对分数。"
        )
        if rescues:
            body += f"眼下最值得动的是 {rescues[0][0]}。"
        body += "若本学期结束仍没有起色，建议早点做两手准备，别把宝全压在一条路上。"
    elif intention == "考研":
        body = (
            f"考研更看专业课和数学的底子，{gap}，但初试才是主战场。"
            "本科成绩的作用主要在复试材料和推免资格上 —— "
            "所以策略是：专业课别掉队，同时把时间往初试科目上让。"
        )
        body += "如果前面已经挂过科，务必确认补考和清挂记录都对得上，别卡在资格审查上。"
    elif intention == "就业":
        body = (
            f"就业最要紧的是顺利毕业，{gap}。"
            "先把毕业学分和必修课兜住 —— 挂科会直接影响毕业证和报到时间，"
            "这个代价远比均分低几分大。"
        )
        body += "余下的精力建议放到实习和项目上，那部分对求职的边际收益更高。"
    elif intention == "考公":
        body = (
            f"考公主要看笔试面试，{gap}，本科成绩通常只在政审和个别岗位的资格里出现。"
            "所以底线是：别挂科、别延毕，把学位证和毕业证稳稳拿到手。"
        )
        body += "行测申论是长期功夫，建议从大三就开始每天固定投入，别等到公告出来才动。"
    elif intention == "留学":
        body = (
            f"留学看的是均分和绩点，{gap}，而且每一门都算，"
            "包括选修 —— 这和国内保研只看核心课不一样。"
        )
        body += "所以哪怕是 1 学分的课也别放弃，能提就提；同时尽早准备语言考试，别卡在时间上。"
    else:
        body = (
            f"暂未确定也没关系，{gap}。"
            "这个阶段最划算的做法是把选择权留住："
            "均分别掉、别挂科、英语别断 —— 这三件事对哪条路都有用。"
        )
        body += "等方向清楚了再针对性发力，比现在盲目投入更有效率。"
    return body
