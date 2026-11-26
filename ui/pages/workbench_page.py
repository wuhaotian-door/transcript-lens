"""当前账户的在册情况、成绩覆盖和学校规章。"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from html import escape
from random import choice

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont, QFontInfo, QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSizePolicy,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from core.archival import semester_rank
from core.database import GradeHistoryDB, StudentStore
from core.knowledge import KnowledgeLibrary
from core.student_import import BATCHES
from core.user_store import load_user_json
from ui.widgets.profile_reading import profile_scroll
from ui.widgets.workbench_ring import WorkbenchRingChart
from ui.workbench_widgets import button, label
from ui.workers import run_async


SEASONAL_LINES = (
    ("春风又绿江南岸", "等闲识得东风面", "春色满园关不住"),
    ("小荷才露尖尖角", "映日荷花别样红", "接天莲叶无穷碧"),
    ("晴空一鹤排云上", "霜叶红于二月花", "停车坐爱枫林晚"),
    ("晚来天欲雪", "墙角数枝梅", "千山鸟飞绝"),
)


def _greeting(hour: int) -> str:
    for upper, text in ((5, "夜深了"), (11, "早上好"), (18, "欢迎回来"), (23, "晚上好")):
        if hour < upper:
            return text
    return "夜深了"


class _GreetingLine(QLabel):
    def __init__(self):
        super().__init__()
        self.setObjectName("WorkbenchHeading")
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        verse_font = QFont()
        verse_font.setFamilies(["FangSong", "仿宋", "STFangsong", "SimSun"])
        verse_font.setPixelSize(25)
        self.setFont(verse_font)
        self._month_font = QFont(verse_font)
        self._month_font.setPixelSize(22)
        self._salutation_font = QFont(verse_font)
        self._salutation_font.setFamilies(["KaiTi", "楷体", "STKaiti", *verse_font.families()])
        self._salutation_font.setPixelSize(28)
        self.set_greeting("欢迎")

    def set_greeting(self, verse, month="", username="", salutation=""):
        self._greeting = verse, month, " ".join(username.split()), salutation
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        verse, month, username, salutation = self._greeting
        if not month:
            self.setText(escape(verse))
            self.setToolTip("")
            return
        prefix, separator = f"{verse} ～ ", " · "
        metrics = QFontMetrics(self.font())
        fixed_width = (metrics.horizontalAdvance(prefix + separator + "，")
                       + QFontMetrics(self._month_font).horizontalAdvance(month)
                       + QFontMetrics(self._salutation_font).horizontalAdvance(salutation))
        name = metrics.elidedText(username, Qt.TextElideMode.ElideRight,
                                  max(0, self.contentsRect().width() - fixed_width))
        family = escape(QFontInfo(self._salutation_font).family())
        self.setText(
            f'{escape(prefix)}<span style="font-size:{self._month_font.pixelSize()}px">{escape(month)}</span>'
            f'{escape(separator + name)}，<span style="font-family:\'{family}\'; font-size:{self._salutation_font.pixelSize()}px">'
            f'{escape(salutation)}</span>'
        )
        full = f"{prefix}{month}{separator}{username}，{salutation}"
        self.setToolTip(full if name != username else "")


@dataclass(frozen=True)
class Overview:
    students: int
    archived: int
    classes: tuple[tuple[str, int], ...]
    grades: int
    term_counts: tuple[tuple[str, int], ...]
    regulations: tuple[tuple[str, str], ...]
    last_import: str

    @property
    def terms(self) -> int:
        return len(self.term_counts)


def read_overview(store, username: str) -> Overview:
    rows = StudentStore(store=store, username=username).rows()
    active = [row for row in rows if not row.get("archived")]
    classes = Counter(row.get("class_name") or "" for row in active)
    grades = GradeHistoryDB(store=store, username=username).rows()
    terms = Counter(row["semester"] for row in grades if row.get("semester"))
    batches = load_user_json(store, username, BATCHES, {}, strict=True).get("batches", ())
    documents = KnowledgeLibrary(store, username).list()
    return Overview(
        len(active), len(rows) - len(active), tuple(sorted(classes.items(), key=lambda pair: (-pair[1], pair[0]))),
        len(grades), tuple(sorted(terms.items(), key=lambda pair: (semester_rank(pair[0]), pair[0]))),
        tuple((str(doc.get("title") or "未命名"), " · ".join(str(doc.get(key) or "") for key in ("school", "year")).strip(" ·"))
              for doc in reversed(documents)),
        batches[-1].get("at", "").replace("T", " ")[:16] if batches else "",
    )


class WorkbenchPage(QWidget):
    grades_requested = Signal()
    archive_requested = Signal()
    import_requested = Signal()
    knowledge_requested = Signal()
    models_requested = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self._store = store
        self._username = ""
        self._generation = 0
        surface = QWidget()
        surface.setObjectName("WorkbenchPage")
        page = QVBoxLayout(surface)
        page.setContentsMargins(24, 20, 24, 20)
        page.setSpacing(15)

        masthead = QFrame()
        masthead.setObjectName("WorkbenchMasthead")
        hero = QHBoxLayout(masthead)
        hero.setContentsMargins(22, 17, 22, 17)
        self._greeting_line = _GreetingLine()
        hero.addWidget(self._greeting_line, 1)
        hero.addWidget(button("导入学生资料", self.import_requested.emit, "WorkbenchPrimary"))
        page.addWidget(masthead)
        self._last_verse = ""
        self._greeting_timer = QTimer(self, interval=180_000)
        self._greeting_timer.timeout.connect(self._update_greeting)

        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        self._metrics = []
        for title in ("在册学生", "在册班级", "成绩记录", "已录学期"):
            card = QFrame()
            card.setObjectName("WorkbenchStrip")
            column = QVBoxLayout(card)
            column.setContentsMargins(14, 10, 14, 10)
            column.setSpacing(3)
            column.addWidget(label(title, "WorkbenchCaption"))
            value = label("—", "WorkbenchValue")
            value.setAccessibleName(title)
            column.addWidget(value)
            self._metrics.append(value)
            metrics.addWidget(card, 1)
        page.addLayout(metrics)

        grid = QGridLayout()
        grid.setSpacing(15)
        classes, class_layout = self._panel("班级分布")
        self._class_chart = QVBoxLayout()
        self._class_chart.setSpacing(6)
        class_layout.addLayout(self._class_chart)
        self._population = label("", "WorkbenchCaption")
        self._population.setWordWrap(True)
        class_layout.addWidget(self._population)
        class_layout.addStretch()
        class_layout.addWidget(button("打开成长档案", self.archive_requested.emit), alignment=Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(classes, 0, 0)

        terms, term_layout = self._panel("各学期成绩记录")
        self._term_chart = QVBoxLayout()
        self._term_chart.setSpacing(6)
        term_layout.addLayout(self._term_chart)
        term_layout.addStretch()
        term_layout.addWidget(button("打开成绩透析", self.grades_requested.emit), alignment=Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(terms, 1, 0)

        rules, rules_layout = self._panel("学校规章")
        self._rules_count = label("", "WorkbenchCaption")
        self._rules_count.setWordWrap(True)
        rules_layout.addWidget(self._rules_count)
        self._rules = QListWidget()
        self._rules.setObjectName("WorkbenchRules")
        self._rules.setMinimumHeight(170)
        self._rules.setWordWrap(True)
        self._rules.setMouseTracking(True)
        rules_layout.addWidget(self._rules, 1)
        rule_actions = QHBoxLayout()
        rule_actions.addWidget(button("管理学校规章", self.knowledge_requested.emit))
        rule_actions.addStretch()
        rules_layout.addLayout(rule_actions)
        grid.addWidget(rules, 0, 1, 2, 1)
        grid.setColumnStretch(0, 6)
        grid.setColumnStretch(1, 5)
        page.addLayout(grid)

        footer = QHBoxLayout()
        self._recent = label("", "WorkbenchCaption")
        self._recent.setWordWrap(True)
        footer.addWidget(self._recent, 1)
        footer.addWidget(button("模型中心", self.models_requested.emit))
        page.addLayout(footer)
        page.addStretch()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(profile_scroll(surface))
        self._present_empty("登录后查看")

    @staticmethod
    def _panel(title):
        frame = QFrame()
        frame.setObjectName("WorkbenchStrip")
        box = QVBoxLayout(frame)
        box.setContentsMargins(15, 12, 15, 12)
        box.setSpacing(9)
        box.addWidget(label(title, "H2"))
        return frame, box

    def set_user(self, username: str) -> None:
        self._generation += 1
        self._username = username
        self._update_greeting()
        self._present_empty("正在读取…" if username else "登录后查看")
        if self.isVisible():
            self.refresh()

    def _update_greeting(self):
        if not self._username:
            self._greeting_line.set_greeting("欢迎")
            self._last_verse = ""
            return
        now = datetime.now()
        season = (now.month % 12) // 3
        verses = SEASONAL_LINES[(season - 1) % 4]
        self._last_verse = choice([line for line in verses if line != self._last_verse])
        self._greeting_line.set_greeting(
            self._last_verse, f"现在{now.month}月", self._username, _greeting(now.hour)
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._update_greeting()
        self._greeting_timer.start()
        self.refresh()

    def hideEvent(self, event):
        self._greeting_timer.stop()
        super().hideEvent(event)

    def refresh(self):
        if not self._username or not self.isVisible():
            return
        username = self._username
        self._generation += 1
        generation = self._generation

        def show(result):
            if isValid(self) and generation == self._generation:
                self._render(result)

        def failed(_):
            if isValid(self) and generation == self._generation:
                self._present_empty("读取未完成。再次进入工作台可重试。")

        run_async(self, lambda: read_overview(self._store, username), on_done=show, on_error=failed)

    @staticmethod
    def _draw_ring(layout, rows, unit, empty):
        while layout.count():
            widget = layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        layout.addWidget(WorkbenchRingChart(rows, unit) if rows else label(empty, "WorkbenchCaption"))

    def _draw_charts(self, classes, terms, empty_class, empty_terms):
        self._draw_ring(self._class_chart, classes, "人", empty_class)
        self._draw_ring(self._term_chart, terms, "条", empty_terms)

    def _present_empty(self, message):
        self._draw_charts((), (), message, message)
        self._population.hide()
        self._rules_count.setText(message)
        self._rules.hide()
        self._rules.clear()
        self._recent.clear()
        for item in self._metrics:
            item.setText("—")

    def _render(self, result: Overview):
        class_count = sum(bool(name) for name, _ in result.classes)
        for widget, value in zip(self._metrics, (result.students, class_count, result.grades, result.terms)):
            widget.setText(f"{value:,}")

        display_classes = [(name or "未分班", count) for name, count in result.classes[:5]]
        if len(result.classes) > 5:
            display_classes.append((f"其余 {len(result.classes) - 5} 组", sum(count for _, count in result.classes[5:])))
        display_terms = list(result.term_counts[-5:])
        if result.terms > 5:
            display_terms.insert(0, ("更早学期", sum(count for _, count in result.term_counts[:-5])))
        self._draw_charts(display_classes, display_terms, "暂无在册学生。", "尚未导入成绩。")
        self._population.setText(f"另有 {result.archived:,} 人已归档" if result.archived else "")
        self._population.setVisible(bool(result.archived))
        self._rules_count.setText(f"已收录 {len(result.regulations)} 份" if result.regulations else "可导入学校规章，供本地查询。")
        self._rules.setVisible(bool(result.regulations))
        self._rules.clear()
        for title, scope in result.regulations:
            item = QListWidgetItem(f"{title}\n{scope}" if scope else title, self._rules)
            item.setToolTip(f"{title} · {scope}" if scope else title)
        self._recent.setText(f"最近资料导入 · {result.last_import}" if result.last_import else "")
