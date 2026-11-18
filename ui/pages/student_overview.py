"""成长档案的跨学期个人专属侧写。"""

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui.anim import make_card
from ui.pages.profile_shared import (
    course_rows_from_archive, group_grades_by_term, history_rows_for,
    rows_gpa, rows_weighted_mean, score_is_failed,
)
from ui.widgets.profile_reading import profile_scroll


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


class StudentOverviewPage(QWidget):
    back_requested = Signal()
    semester_requested = Signal(str)
    model_settings_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store = None
        self._username = ""
        self._summary = ""
        self._terms: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 4)
        layout.setSpacing(14)
        header = QHBoxLayout()
        back = QPushButton("‹ 返回档案")
        back.setObjectName("Ghost")
        back.setMinimumHeight(34)
        back.clicked.connect(self.back_requested.emit)
        header.addWidget(back)
        header.addStretch()
        self._talk = QPushButton("与 AI 讨论")
        self._talk.setObjectName("Ghost")
        self._talk.setMinimumHeight(34)
        self._talk.clicked.connect(self._open_chat)
        header.addWidget(self._talk)
        layout.addLayout(header)

        title = QLabel("个人专属侧写")
        title.setObjectName("H1")
        layout.addWidget(title)
        self._identity = QLabel("")
        self._identity.setObjectName("ProfileMeta")
        self._identity.setWordWrap(True)
        layout.addWidget(self._identity)

        summary = make_card(shadow=False, name="ProfilePortrait")
        metrics = QHBoxLayout(summary)
        metrics.setContentsMargins(16, 14, 16, 14)
        self._metrics = []
        for caption in ("已导入", "累计加权均分", "累计 GPA", "最近变化"):
            column = QVBoxLayout()
            label = QLabel(caption)
            label.setObjectName("ProfileMeta")
            value = QLabel("—")
            value.setObjectName("H2")
            value.setWordWrap(True)
            column.addWidget(label)
            column.addWidget(value)
            metrics.addLayout(column, 1)
            self._metrics.append(value)
        self._metrics[1].setToolTip("按必修和限选课程的学分加权。")
        layout.addWidget(summary)

        panel = make_card(shadow=False, name="ProfilePanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        section = QLabel("各学期")
        section.setObjectName("H2")
        box.addWidget(section)
        self._empty = QLabel("暂无成绩记录。")
        self._empty.setObjectName("Muted")
        box.addWidget(self._empty)
        self._table = QTableWidget(0, 5)
        self._table.setObjectName("courseTable")
        self._table.setHorizontalHeaderLabels(("学期", "有成绩课程", "加权均分", "GPA", "挂科"))
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().hide()
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.cellDoubleClicked.connect(self._open_term)
        self._table.itemSelectionChanged.connect(self._sync_selection)
        box.addWidget(self._table, 1)
        self._semester = QPushButton("查看所选学期")
        self._semester.setObjectName("Primary")
        self._semester.setMinimumHeight(34)
        self._semester.clicked.connect(lambda: self._open_term(self._table.currentRow()))
        box.addWidget(self._semester, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(panel, 1)
        outer.addWidget(profile_scroll(body))

    def set_context(self, store, username: str, student, class_name: str = "") -> None:
        self._store, self._username = store, username
        identity = [student.student, student.student_id, class_name]
        self._identity.setText("  ·  ".join(part for part in identity if part))
        rows = course_rows_from_archive(student)
        if rows:
            effective = replace(student, records=student.calculation_records)
            overall = course_rows_from_archive(effective)
        else:
            rows = history_rows_for(store, username, student.student_id)
            overall = rows
        grouped = group_grades_by_term(rows)
        table = []
        context = []
        for term, courses in grouped:
            count = sum(row["score"] is not None for row in courses)
            weighted = rows_weighted_mean(courses)
            gpa = rows_gpa(courses)
            failed = sum(score_is_failed(row["score"]) for row in courses)
            label = term or "未标注学期"
            table.append((term, label, count, weighted, gpa, failed))
            context.append(f"{label}：有成绩 {count} 门，加权均分 {_number(weighted)}，"
                           f"GPA {_number(gpa)}，挂科 {failed} 门。")

        # 完整学年与对应的分学期归档同在时，累计和变化用既有归档口径去重。
        effective_terms = {row.get("term") or row.get("semester") or "" for row in overall}
        averages = [mean for term, _, _, mean, _, _ in table
                    if term in effective_terms and mean is not None]
        delta = f"{averages[-1] - averages[-2]:+.2f}" if len(averages) > 1 else "—"
        for label, value in zip(self._metrics, (
            f"{len(table)} 个学期", _number(rows_weighted_mean(overall)),
            _number(rows_gpa(overall)), delta,
        )):
            label.setText(value)

        self._terms = [term for term, *_ in table]
        self._table.clearSelection()
        self._table.setRowCount(len(table))
        for index, (_, term, count, weighted, gpa, failed) in enumerate(table):
            for col, value in enumerate((term, str(count), _number(weighted), _number(gpa), str(failed))):
                cell = QTableWidgetItem(value)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(index, col, cell)
        self._empty.setVisible(not table)
        self._table.setVisible(bool(table))
        self._semester.setVisible(bool(table))
        self._sync_selection()
        self._summary = "\n".join(context)
        self._talk.setEnabled(bool(context))

    def _sync_selection(self) -> None:
        row = self._table.currentRow()
        self._semester.setEnabled(0 <= row < len(self._terms) and bool(self._terms[row])
                                  and bool(self._table.selectedItems()))

    def _open_term(self, row: int, _column: int = 0) -> None:
        if 0 <= row < len(self._terms) and self._terms[row]:
            self.semester_requested.emit(self._terms[row])

    def _open_chat(self) -> None:
        from ui.chat_dialog import open_chat

        open_chat(self, system_prompt=(
            "你是辅导员的学业谈话助手。以下是同一位学生所有已导入学期的汇总。"
            "结合各学期变化与需要关注的问题回答，缺失的数据不得编造。\n" + self._summary
        ))
