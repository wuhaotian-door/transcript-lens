"""成长档案的跨学期个人专属侧写。"""

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.gpa import DEFAULT_SCHEME, credit_of, resolve_scheme, scheme_label
from ui.academic_context import AcademicContext
from ui.anim import make_card
from ui.pages.profile_shared import (
    course_rows_from_archive, format_credit, format_score, group_grades_by_term, history_rows_for,
    rows_gpa, rows_weighted_mean, score_is_failed,
)
from ui.widgets.profile_reading import profile_scroll


def _number(value: float | None, precision: int = 2) -> str:
    return "—" if value is None else f"{value:.{precision}f}"


class StudentOverviewPage(QWidget):
    back_requested = Signal()
    semester_requested = Signal(str)
    model_settings_requested = Signal()
    interview_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store = None
        self._username = ""
        self.vault = None
        self._sid = ""
        self._academic_context: AcademicContext | None = None
        self._terms: list[str] = []
        self._gpa_scheme = resolve_scheme(DEFAULT_SCHEME)
        self._grouped = []
        self._overall = []

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
        interviews = QPushButton("谈话记录")
        interviews.setObjectName("Ghost")
        interviews.setMinimumHeight(34)
        interviews.clicked.connect(self.interview_requested.emit)
        header.addWidget(interviews)
        self._talk = QPushButton("全学期 AI 分析")
        self._talk.setObjectName("Primary")
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

        review = make_card(shadow=False, name="ProfilePanel")
        review_box = QVBoxLayout(review)
        review_box.setContentsMargins(16, 14, 16, 14)
        review_title = QLabel("四学期总评")
        review_title.setObjectName("H2")
        review_box.addWidget(review_title)
        self._four_terms = QLabel()
        self._four_terms.setTextFormat(Qt.TextFormat.PlainText)
        self._four_terms.setWordWrap(True)
        review_box.addWidget(self._four_terms)
        layout.addWidget(review)

        panel = make_card(shadow=False, name="ProfilePanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        section = QLabel("各学期")
        section.setObjectName("H2")
        box.addWidget(section)
        self._scheme_caption = QLabel()
        self._scheme_caption.setObjectName("ProfileMeta")
        self._scheme_caption.setWordWrap(True)
        self._scheme_caption.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self._scheme_caption)
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

    def set_gpa_scheme(self, scheme) -> None:
        self._gpa_scheme = resolve_scheme(scheme)
        self._render_summary(keep_selection=True)

    def set_context(self, store, username: str, student, class_name: str = "") -> None:
        self._store, self._username = store, username
        self._sid = student.student_id
        identity = [student.student, student.student_id, class_name]
        self._identity.setText("  ·  ".join(part for part in identity if part))
        rows = course_rows_from_archive(student)
        if rows:
            effective = replace(student, records=student.calculation_records)
            overall = course_rows_from_archive(effective)
        else:
            rows = history_rows_for(store, username, student.student_id)
            overall = rows
        self._grouped = group_grades_by_term(rows)
        self._overall = overall
        self._render_summary()

    def _render_summary(self, *, keep_selection: bool = False) -> None:
        selected_row = self._table.currentRow() if keep_selection and self._table.selectedItems() else -1
        scheme_name = scheme_label(self._gpa_scheme)
        self._scheme_caption.setText(f"GPA 方案 · {scheme_name}")
        self._metrics[2].setToolTip(scheme_name)
        precision = self._gpa_scheme["round"]
        table = []
        context = []
        details = []
        for term, courses in self._grouped:
            count = sum(row["score"] is not None for row in courses)
            weighted = rows_weighted_mean(courses)
            gpa = rows_gpa(courses, self._gpa_scheme)
            failed = sum(score_is_failed(row["score"]) for row in courses)
            label = term or "未标注学期"
            table.append((term, label, count, weighted, gpa, failed))
            context.append(f"{label}：有成绩 {count} 门，加权均分 {_number(weighted)}，"
                           f"GPA {_number(gpa, precision)}，挂科 {failed} 门。")
            lines = []
            for row in sorted(courses, key=lambda item: not score_is_failed(item["score"])):
                course = " ".join(row["course"].split())
                score = format_score(row["score"]) if row["score"] is not None else "无成绩"
                credit = format_credit(row["credit"])
                kind = row["category"] or "类别未录入"
                lines.append((course, f"{label}｜{course}｜{kind}｜学分 {credit}｜成绩 {score}"))
            details.append((label, tuple(lines)))

        # 完整学年与对应的分学期归档同在时，累计和变化用既有归档口径去重。
        effective_terms = {row.get("term") or row.get("semester") or "" for row in self._overall}
        averages = [mean for term, _, _, mean, _, _ in table
                    if term in effective_terms and mean is not None]
        delta = f"{averages[-1] - averages[-2]:+.2f}" if len(averages) > 1 else "—"
        for label, value in zip(self._metrics, (
            f"{len(table)} 个学期", _number(rows_weighted_mean(self._overall)),
            _number(rows_gpa(self._overall, self._gpa_scheme), precision), delta,
        )):
            label.setText(value)

        self._terms = [term for term, *_ in table]
        self._table.clearSelection()
        self._table.setRowCount(len(table))
        for index, (_, term, count, weighted, gpa, failed) in enumerate(table):
            for col, value in enumerate((term, str(count), _number(weighted), _number(gpa, precision), str(failed))):
                cell = QTableWidgetItem(value)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(index, col, cell)
        if 0 <= selected_row < len(table):
            self._table.selectRow(selected_row)
        self._empty.setVisible(not table)
        self._table.setVisible(bool(table))
        self._semester.setVisible(bool(table))
        self._sync_selection()
        four = self._summarize_four_terms(table, effective_terms, precision)
        self._four_terms.setText(four)
        self._academic_context = AcademicContext(
            "\n".join([f"GPA 方案：{scheme_name}。加权均分采用必修与限选。",
                       "各学期统计如下。完整学年与分学期记录可能重叠，累计范围以总评为准。",
                       *context, f"四学期总评：{four}"]),
            tuple(details),
        ) if context else None
        self._talk.setEnabled(bool(context) or self.vault is not None)

    def _summarize_four_terms(self, table, effective_terms, precision: int) -> str:
        recent = [row for row in table if row[0] in effective_terms and row[2] > 0][-4:]
        if not recent:
            return "暂无可用于总评的成绩。"
        keys = {row[0] for row in recent}
        rows = [row for row in self._overall
                if (row.get("term") or row.get("semester") or "") in keys and row.get("score") is not None]
        means = [row[3] for row in recent if row[3] is not None]
        change = f"{means[-1] - means[0]:+.2f} 分" if len(means) > 1 else "—"
        span = "、".join(row[1] for row in recent)
        credits = sum(credit_of(row.get("credit")) or 0 for row in rows)
        failed = sum(score_is_failed(row.get("score")) for row in rows)
        return (
            f"纳入 {len(recent)}/4 个有成绩学期：{span}\n"
            f"有成绩 {len(rows)} 门 · 学分合计 {credits:g} · 挂科 {failed} 门\n"
            f"加权均分 {_number(rows_weighted_mean(rows))} · "
            f"GPA {_number(rows_gpa(rows, self._gpa_scheme), precision)} · 首尾均分变化 {change}"
        )

    def _sync_selection(self) -> None:
        row = self._table.currentRow()
        self._semester.setEnabled(0 <= row < len(self._terms) and bool(self._terms[row])
                                  and bool(self._table.selectedItems()))

    def _open_term(self, row: int, _column: int = 0) -> None:
        if 0 <= row < len(self._terms) and self._terms[row]:
            self.semester_requested.emit(self._terms[row])

    def _open_chat(self) -> None:
        if self.vault is not None:
            from ui.grounded_chat_dialog import GroundedChatDialog

            dialog = GroundedChatDialog(self, self.vault, self._sid, self._academic_context,
                                        self.model_settings_requested.emit)
            dialog.question.setText("请结合全部已导入学期给出总评和下一阶段建议。")
            dialog.exec()
            return
        from ui.chat_dialog import ChatDialog

        if self._academic_context is None:
            return
        dialog = ChatDialog(
            self, academic_context=self._academic_context,
            system_prompt="你是辅导员的学业分析助手。结合全学期成绩解释表现、风险和改进建议。"
                          "区分缺考零分与未录入，资料缺失时如实说明。",
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.input_edit.setPlainText("请根据全部已导入学期，给出综合总评、优势、风险和下一阶段建议。")
