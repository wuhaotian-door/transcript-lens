"""📈 成长档案页 —— 把 :mod:`core.archival` 的纵向数据画出来。

    ┌ 学生名单 ────┬ 【张三（2023001）】累计均分 84.64  ↑进步 +1.90 ──────┐
    │ 🔍 搜索      │ ┌ 学期时间轴（QScrollArea）──────────────────────┐ │
    │ 张三         │ │ ● 大二上   82.74   8 门 / 22 学分              │ │
    │ 李四         │ │ │                                             │ │
    │ 王五         │ │ ● 大二下   84.64   8 门 / 23 学分  挂科 1 门   │ │
    │              │ └───────────────────────────────────────────────┘ │
    │              │ 已定型短板（补考不改分）· 可抢救项 · 剩余可冲学期    │
    └──────────────┴────────────────────────────────────────────────────┘

★ 本页**一行算术都没有**（charter 铁律 1 / v1.3 红线 1）。

  累计均分、每学期均分、趋势 Δ、定型/可救分区、剩余学期数，全部来自
  :mod:`core.archival` 的 ``cumulative_average`` / ``term_average`` /
  ``trend()`` / ``partition()`` —— 那些是 core 里**已经有**的函数。
  本页只做排版。

★ 数据从**加密库**读（``archive.json.enc``，导入成绩时顺手写进去的），
  所以关掉程序再打开，档案还在。见
  :data:`ui.pages.analysis_page.ARCHIVE_FILE`。

★ 身份用**学号**（charter 铁律 4）：列表条目的键是
  ``sid:<学号>``，没学号才退回 ``name:<姓名>`` 并且界面明说同名风险。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.archival import Archive, partition, semester_rank, trend
from core.calculator import CalculationScope
from core.crypto_store import CryptoError, CryptoStore
from core.user_store import load_user_json
from ui.anim import make_card
from ui.pages.analysis_page import ARCHIVE_FILE
from ui.pages.profile_page import (
    ITEM_HEIGHT,
    LIST_WIDTH,
    _ROLE_HAS_ID,
    _ROLE_ID,
    _ROLE_INDEX,
    _ROLE_LEVEL,
    _ROLE_NAME,
    _ROLE_SEED,
    StudentItemDelegate,
    _avatar_color,
)

__all__ = ["ARCHIVE_SCOPE", "ArchivePage", "build"]

#: 档案用哪一档口径展示。
#:
#: charter / v1.3 的默认口径是「必修+限选」（Spec 第 61 行）。
#: 档案页**不跟智育分析页的筛选按钮联动** —— 它是纵向的、可归档的固定口径，
#: 跟着屏幕上的临时视图变会让"上学期看是 84、这学期看是 86"这种事发生。
ARCHIVE_SCOPE = CalculationScope.REQUIRED_LIMITED

#: 趋势死区：|Δ| 小于它就算"平稳"，不标颜色（避免 0.01 分也号称进步）
TREND_EPS = 0.5

_UP_COLOR = "#15803D"
_DOWN_COLOR = "#C2410C"
_FLAT_COLOR = "#475569"


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


class ArchivePage(QWidget):
    """成长档案页：左名单 + 右时间轴 / 趋势 / 定型分区。"""

    def __init__(self, store: CryptoStore | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore()
        self._username = ""
        self._archive: Archive | None = None
        self._current: Any = None
        self._key_by_row: dict[int, str] = {}
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        self._username = username or ""
        self.reload()

    def set_archive(self, archive: Archive | None) -> None:
        """直接塞一份现成的归档（智育分析页刚导入完时用，省一次解密）。"""
        self._archive = archive
        self._fill_list()

    def reload(self) -> None:
        """从加密库读回纵向归档。

        读不出来就是"还没有档案"，界面给一句明确指引，
        **不报错** —— 没导入过不是错误。
        """
        self._archive = None
        if self._username:
            payload = load_user_json(self._store, self._username, ARCHIVE_FILE)
            if isinstance(payload, dict):
                try:
                    self._archive = Archive.from_dict(payload)
                except Exception:  # noqa: BLE001 - 坏档案不该拦住整页
                    self._archive = None
        self._fill_list()

    @property
    def student_count(self) -> int:
        return self._list.count()

    def all_terms(self) -> list[str]:
        """整份档案里出现过的学期标签（去重、按学期顺序排）。

        ★ ``Archive`` 本身**没有** ``terms`` 属性 —— 学期是挂在每个
          ``StudentArchive`` 上的（``archive.students[i].terms``）。
          这里按排序权重合并去重，不是简单 set（否则"大二下"会排到"大一上"前面）。
        """
        if self._archive is None:
            return []
        seen: dict[str, int] = {}
        for student in self._archive.students.values():
            for term in student.terms:
                seen.setdefault(term, semester_rank(term))
        return [t for t, _rank in sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))]

    @property
    def visible_count(self) -> int:
        return sum(1 for i in range(self._list.count())
                   if not self._list.item(i).isHidden())

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        root.addWidget(self._build_list_pane())
        root.addWidget(self._build_detail_pane(), 1)

    def _build_list_pane(self) -> QFrame:
        card = make_card()
        card.setFixedWidth(LIST_WIDTH)
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        head = QWidget()
        head_box = QHBoxLayout(head)
        head_box.setContentsMargins(14, 13, 12, 10)
        head_box.setSpacing(8)
        title = QLabel("学生名单")
        title.setObjectName("H2")
        head_box.addWidget(title)
        head_box.addStretch(1)
        self._count_pill = QLabel("0")
        self._count_pill.setObjectName("CountPill")
        head_box.addWidget(self._count_pill)
        box.addWidget(head)

        holder = QWidget()
        holder_box = QVBoxLayout(holder)
        holder_box.setContentsMargins(12, 0, 12, 10)
        self._search = QLineEdit()
        self._search.setObjectName("ListSearch")
        self._search.setPlaceholderText("搜索姓名或学号")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(32)
        self._search.textChanged.connect(self._on_search)
        holder_box.addWidget(self._search)
        box.addWidget(holder)

        self._list = QListWidget()
        self._list.setObjectName("StudentList")
        self._list.setUniformItemSizes(True)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setItemDelegate(StudentItemDelegate(self._list))
        self._list.itemClicked.connect(self._on_item_clicked)
        box.addWidget(self._list, 1)

        self._note = QLabel("")
        self._note.setObjectName("ListNote")
        self._note.setWordWrap(True)
        self._note.setContentsMargins(14, 8, 12, 10)
        box.addWidget(self._note)
        return card

    def _build_detail_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self._empty = QLabel(
            "👈  在左边点一名学生，这里显示他的学期时间轴和趋势。\n\n"
            "档案来自「📊 智育分析」导入的成绩表 —— 导入时会自动存进加密库，\n"
            "所以关掉程序再打开，档案还在。"
        )
        self._empty.setObjectName("Placeholder")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        box.addWidget(self._empty, 1)

        self._body = QWidget()
        body_box = QVBoxLayout(self._body)
        body_box.setContentsMargins(0, 0, 0, 0)
        body_box.setSpacing(14)

        body_box.addWidget(self._build_portrait_card())
        body_box.addWidget(self._build_timeline_card(), 1)
        body_box.addWidget(self._build_partition_card())
        box.addWidget(self._body, 1)

        self._body.hide()
        return pane

    def _build_portrait_card(self) -> QFrame:
        card = make_card()
        box = QHBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(12)

        self._who = QLabel("—")
        self._who.setObjectName("H2")
        box.addWidget(self._who)

        self._cumulative = QLabel("—")
        self._cumulative.setObjectName("H1")
        box.addWidget(self._cumulative)

        self._cum_sub = QLabel("")
        self._cum_sub.setObjectName("Muted")
        box.addWidget(self._cum_sub)

        box.addStretch(1)

        self._delta_tag = QLabel("—")
        self._delta_tag.setObjectName("TagGray")
        box.addWidget(self._delta_tag)
        return card

    def _build_timeline_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("学期时间轴")
        title.setObjectName("H2")
        head.addWidget(title)
        self._timeline_hint = QLabel("")
        self._timeline_hint.setObjectName("Muted")
        head.addWidget(self._timeline_hint)
        head.addStretch(1)
        box.addLayout(head)

        self._timeline_scroll = QScrollArea()
        self._timeline_scroll.setWidgetResizable(True)
        self._timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._timeline_box = QVBoxLayout(inner)
        self._timeline_box.setContentsMargins(2, 2, 6, 2)
        self._timeline_box.setSpacing(8)
        self._timeline_box.addStretch(1)
        self._timeline_scroll.setWidget(inner)
        self._timeline_scroll.setMinimumHeight(160)
        self._timeline_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        box.addWidget(self._timeline_scroll, 1)
        return card

    def _build_partition_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(8)

        title = QLabel("定型 / 可救分区")
        title.setObjectName("H2")
        box.addWidget(title)

        self._locked = QLabel("—")
        self._locked.setObjectName("Muted")
        self._locked.setWordWrap(True)
        box.addWidget(self._locked)

        self._rescue = QLabel("—")
        self._rescue.setObjectName("Muted")
        self._rescue.setWordWrap(True)
        box.addWidget(self._rescue)

        self._remaining = QLabel("—")
        self._remaining.setObjectName("Notice")
        self._remaining.setWordWrap(True)
        box.addWidget(self._remaining)
        return card

    # ---------------------------------------------------------------- 名单
    def _fill_list(self) -> None:
        blocked = self._list.blockSignals(True)
        self._list.clear()
        self._key_by_row = {}
        students = list(self._archive.students.values()) if self._archive else []
        students.sort(key=lambda s: (s.student, s.student_id))
        for row, student in enumerate(students):
            label = (f"{student.student}（{student.student_id}）"
                     if student.student_id else student.student)
            item = QListWidgetItem(label)
            item.setData(_ROLE_INDEX, row)
            item.setData(_ROLE_NAME, student.student)
            item.setData(_ROLE_ID, student.student_id)
            item.setData(_ROLE_HAS_ID, bool(student.student_id))
            # 档案页的圆点按"挂科总数"判，跟智育分析的口径筛选无关 ——
            # 这一页是纵向的固定口径（见 ARCHIVE_SCOPE 的注释）
            level = "danger" if student.failed_total else ""
            item.setData(_ROLE_LEVEL, level)
            item.setData(_ROLE_SEED, student.student_id or student.student)
            item.setSizeHint(QSize(0, ITEM_HEIGHT))
            item.setToolTip(
                f"学号 {student.student_id}" if student.student_id
                else "⚠ 表里没有学号，同名学生会共用同一份意愿"
            )
            self._list.addItem(item)
            self._key_by_row[row] = student.key
        self._list.blockSignals(blocked)

        count = len(students)
        self._count_pill.setText(str(count))
        if count == 0:
            self._note.setText("还没有档案。先在「📊 智育分析」导入成绩表。")
            self._body.hide()
            self._empty.show()
            self._empty.setText(
                "还没有可看的档案。\n\n"
                "去「📊 智育分析」导入一份成绩表 —— 导入时会顺手把纵向档案\n"
                "存进本机的加密库，之后关掉程序再打开，这里就还在。"
            )
        else:
            terms = len(self.all_terms())
            self._note.setText(f"共 {count} 人　·　{terms} 个学期标签")
            if self._current is None:
                self._empty.setText(
                    "👈  在左边点一名学生，这里显示他的学期时间轴和趋势。"
                )
        self._update_note_filter()

    def _update_note_filter(self) -> None:
        total = self._list.count()
        if total == 0:
            return
        shown = self.visible_count
        if shown != total:
            terms = len(self.all_terms())
            self._note.setText(
                f"筛出 {shown} / {total} 人　·　{terms} 个学期标签"
            )

    def _on_search(self, text: str) -> None:
        keyword = (text or "").strip()
        for i in range(self._list.count()):
            self._list.item(i).setHidden(True)
        if keyword:
            for item in self._list.findItems(keyword, Qt.MatchFlag.MatchContains):
                item.setHidden(False)
        else:
            for i in range(self._list.count()):
                self._list.item(i).setHidden(False)
        self._update_note_filter()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        row = item.data(_ROLE_INDEX)
        students = list(self._archive.students.values()) if self._archive else []
        students.sort(key=lambda s: (s.student, s.student_id))
        if not isinstance(row, int) or not (0 <= row < len(students)):
            return
        self.show_student(students[row])

    # ---------------------------------------------------------------- 渲染
    def show_student(self, student: Any) -> None:
        """渲染一名学生的档案。``student`` 是 ``core.archival.StudentArchive``。"""
        if student is None:
            return
        self._current = student
        self._empty.hide()
        self._body.show()

        label = (f"{student.student}（{student.student_id}）"
                 if student.student_id else student.student)
        self._who.setText(label)
        self._cumulative.setText(_fmt(student.cumulative_average))
        self._cum_sub.setText(
            f"{len(student.terms)} 个学期　·　{len(student.records)} 条记录　·　"
            f"挂科 {student.failed_total} 门　·　缺考 {student.absent_total} 门"
        )

        info = trend(student, ARCHIVE_SCOPE)
        self._render_delta(info)
        self._render_timeline(student, info)
        self._render_partition(student)

    def _render_delta(self, info: dict[str, Any]) -> None:
        delta = info.get("delta")
        if delta is None:
            self._delta_tag.setText("单次导入，暂无趋势")
            self._delta_tag.setObjectName("TagGray")
            self._delta_tag.setStyleSheet("")
            self._repolish(self._delta_tag)
            return
        if delta > TREND_EPS:
            text, color, obj = f"↑ 进步 +{delta:.2f}", _UP_COLOR, "TagGreen"
        elif delta < -TREND_EPS:
            text, color, obj = f"↓ 下滑 {delta:.2f}", _DOWN_COLOR, "TagOrange"
        else:
            text, color, obj = f"→ 平稳 {delta:+.2f}", _FLAT_COLOR, "TagGray"
        self._delta_tag.setText(text)
        self._delta_tag.setObjectName(obj)
        # 下滑用橙色（用户要求：↑进步绿 / ↓下滑橙）
        self._delta_tag.setStyleSheet(
            f"color:{color};" if obj == "TagGray" else ""
        )
        self._repolish(self._delta_tag)

    def _render_timeline(self, student: Any, info: dict[str, Any]) -> None:
        """重画时间轴。

        ★ 先清空再重建 —— 而且**必须**把旧的 deleteLater 掉，
          否则切学生时旧节点会叠在新节点下面（越点越乱）。
        """
        while self._timeline_box.count() > 1:      # 末尾那个 stretch 留着
            item = self._timeline_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        series = info.get("series") or []
        if not series:
            hint = QLabel("这个学生还没有可显示的学期记录。")
            hint.setObjectName("Muted")
            self._timeline_box.insertWidget(0, hint)
            self._timeline_hint.setText("")
            return

        self._timeline_hint.setText(
            f"{info.get('first_term') or '—'} → {info.get('last_term') or '—'}"
        )
        averages = [p["average"] for p in series if p["average"] is not None]
        lo = min(averages) if averages else 0.0
        hi = max(averages) if averages else 100.0

        for index, point in enumerate(series):
            self._timeline_box.insertWidget(
                index,
                self._timeline_row(
                    point,
                    first=(index == 0),
                    last=(index == len(series) - 1),
                    lo=lo, hi=hi,
                    records=self._records_of(student, point["term"]),
                ),
            )

    @staticmethod
    def _records_of(student: Any, term: str) -> list[Any]:
        return [r for r in student.records if r.term == term]

    def _timeline_row(self, point: dict[str, Any], *, first: bool, last: bool,
                      lo: float, hi: float, records: list[Any]) -> QWidget:
        """时间轴上的一个节点：左边一个圆点 + 竖线，右边是那一学期的明细。"""
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        # --- 刻度（圆点 + 上下连线）---
        rail = QWidget()
        rail.setFixedWidth(18)
        rail_box = QVBoxLayout(rail)
        rail_box.setContentsMargins(0, 0, 0, 0)
        rail_box.setSpacing(0)

        top = QFrame()
        top.setFixedWidth(2)
        top.setStyleSheet(
            "background: transparent;" if first else "background: #CBD5E1;"
        )
        top.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        rail_box.addWidget(top, 1, Qt.AlignmentFlag.AlignHCenter)

        dot = QFrame()
        dot.setFixedSize(11, 11)
        dot.setStyleSheet(
            f"background: {_avatar_color(point['term'])}; border-radius: 5px;"
        )
        rail_box.addWidget(dot, 0, Qt.AlignmentFlag.AlignHCenter)

        bottom = QFrame()
        bottom.setFixedWidth(2)
        bottom.setStyleSheet(
            "background: transparent;" if last else "background: #CBD5E1;"
        )
        bottom.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        rail_box.addWidget(bottom, 1, Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(rail)

        # --- 内容 ---
        card = QFrame()
        card.setObjectName("TimelineRow")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        inner = QHBoxLayout(card)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(10)

        term = QLabel(str(point["term"]))
        term.setObjectName("H3")
        term.setMinimumWidth(96)
        inner.addWidget(term)

        average = point["average"]
        avg = QLabel(_fmt(average))
        avg.setObjectName("H2")
        inner.addWidget(avg)

        # 一条按分数量出来的横条 —— 让"哪学期掉了"一眼可见，
        # 不用去读数字。宽度是**纯展示**，不参与任何计算。
        if average is not None and hi > lo:
            bar = QFrame()
            bar.setFixedHeight(6)
            bar.setMinimumWidth(40)
            bar.setMaximumWidth(220)
            ratio = (float(average) - lo) / (hi - lo) if hi > lo else 1.0
            bar.setFixedWidth(int(40 + ratio * 180))
            color = _UP_COLOR if float(average) >= (lo + hi) / 2 else "#94A3B8"
            bar.setStyleSheet(f"background:{color}; border-radius:3px;")
            inner.addWidget(bar)

        inner.addStretch(1)

        credits = sum(getattr(r, "total_credit", 0.0) or 0.0 for r in records)
        courses = sum(len(getattr(r, "courses", ()) or ()) for r in records)
        meta = QLabel(f"{courses} 门 / {credits:g} 学分")
        meta.setObjectName("Muted")
        inner.addWidget(meta)

        failed = [name for r in records for name in (r.failed_courses or ())]
        absent = [name for r in records for name in (r.absent_courses or ())]
        if failed:
            tag = QLabel(f"挂科 {len(failed)} 门")
            tag.setObjectName("TagRed")
            inner.addWidget(tag)
        if absent:
            tag = QLabel(f"缺考 {len(absent)} 门")
            tag.setObjectName("TagOrange")
            inner.addWidget(tag)

        box.addWidget(card, 1)
        return holder

    def _render_partition(self, student: Any) -> None:
        part = partition(student, ARCHIVE_SCOPE)
        locked = part.get("locked") or []
        absent = part.get("absent") or []
        remaining = part.get("remaining_terms", 0)
        elapsed = part.get("terms_elapsed", 0)

        if locked:
            shown = "、".join(f"{item['term']} {item['course']}" for item in locked[:6])
            more = f" 等 {len(locked)} 门" if len(locked) > 6 else ""
            self._locked.setText(
                f"已定型（补考不改分，红线 9，不必再纠结）：{shown}{more}"
            )
        else:
            self._locked.setText("已定型短板：没有 —— 目前没有需要放下的挂科。")

        if absent:
            shown = "、".join(f"{item['term']} {item['course']}" for item in absent[:6])
            self._rescue.setText(f"缺考（还能安排）：{shown}")
        else:
            self._rescue.setText(
                "可抢救项：没有缺考记录。具体哪几门能拉分，看「🎯 侧写辅导」的可抢救项。"
            )

        if not remaining:
            self._remaining.setText(
                "剩余可冲学期：0 —— 已经走到最后一个学期，重点应转向稳妥收尾。"
            )
        elif not elapsed:
            # 学期标签认不出来（导入用的是默认的"未标注"），别硬说"走到第 0 个学期"
            self._remaining.setText(
                "剩余可冲学期：无法判断 —— 导入时没有学期信息（标签是"
                f"「{self._timeline_hint.text() or '未标注'}」）。"
                "带上学期标签再导入，这里就能算出还剩几个学期可冲。"
            )
        else:
            self._remaining.setText(
                f"剩余可冲学期：{remaining} 个"
                f"（按日历走到第 {elapsed} 个学期，共 8 个）。"
            )

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def build(parent: QWidget | None = None) -> QWidget:
    """预留的构建入口（Spec 七：``build(parent)`` 函数签名）。"""
    return ArchivePage(None, parent)
