"""学期侧写页：只围绕**一个学期**看成绩、画像，并生成这一段学期的 AI 侧写。

页面契约（主窗接线用）::

    page = SemesterSideProfile()
    page.set_session(session)          # 可 None（未登录）
    page.set_student({"student_id": "...", "name": "..."})
    page.set_records(grade_history_rows)
    page.set_term("大二下")
    page.set_moral(88)
    page.reload()
    page.backRequested.connect(...)     # 点「← 返回档案」

★ 数据从哪来（AGENTS §18）
--------------------------
本页**不重新解析 Excel、不重新算加权**：成绩行由调用方（档案页 / 主窗）
从 ``grade_history`` 读好传进来；这里只做筛选、计数和显示。学期均分是
**算术平均**，标签写死"本学期算术平均"，不要和加权均分混为一谈。

★ 空 ≠ 0（红线 2）
------------------
空成绩在表格里显示「—」，不显示 0；平均分、学分合计都**不把它算进分母**。
缺考是数字 0，算考了，因此 0 分算挂科、也计入平均分。

★ AI 侧写
---------
* 只在本机推理（``core.ai.inference``），不走任何云端 API；
* 引擎 / 模型不可用时给中文提示，**绝不静默失败**；
* 推理放在 ``QThread`` 里，子线程**不碰任何控件**，结果通过信号回主线程；
* 提示词只注入**本学期**的成绩与德育分（``profile_shared.build_side_text_prompt``
  加一段本学期限定语）。

★ 保存
------
优先用 ``ProfileStore.save_semester_side_text(student_id, term, text)``；
老版本没有这个方法时，退回 ``save_side_text(student_id, "[学期]\\n" + text)``。
本页**不修改** ``core/database.py``，只做 ``getattr`` 探测。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.ai.inference import (
    InferenceUnavailable,
    LocalModelEngine,
    engine_available,
    friendly_error,
    list_local_models,
)
from core.database import ProfileStore
from core.gpa import DEFAULT_SCHEME, resolve_scheme, scheme_label
from ui.anim import make_card
from ui.intentions import student_key
from ui.pages import profile_shared
from ui.widgets.profile_reading import ProfileDocument, profile_scroll
from ui.widgets.model_picker import CORE_MISSING_HINT, ModelPicker

logger = logging.getLogger(__name__)

#: 表格三列（顺序 = ``_fill_table`` 里写单元格的顺序，测试也按它断言）。
COLUMNS = ("课程", "学分", "成绩")

#: 这些写法在导入时都表示"没选 / 没考"，一律当空白，绝不能当 0。
_BLANK_MARKERS = {"", "-", "/", "—", "–", "未选", "无", "N/A", "n/a", "None"}

#: 行高下限（AGENTS §4c：行高必须容得下单元格内容，不能只剩半个字）。
ROW_HEIGHT = 34
MIN_ROW_HEIGHT = 30

#: 别的学期留下的 ``[大二下]`` 前缀（回退保存格式）。
_TERM_PREFIX = re.compile(r"^\[[^\]\n]{1,40}\]\r?\n")


def _clean_text(value: Any) -> str:
    """把 ``None`` / 空白 / 数字统一成去掉首尾空白的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _score_value(raw: Any) -> float | None:
    """把一行的原始成绩转成数字；空 / ``-`` / 未选 → ``None``。

    ★ 这里**不返回 0**：``None`` 与 ``0`` 在整条链路上必须分得开
      （``0`` 是缺考，进分母；``None`` 是没选，不进分母）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text in _BLANK_MARKERS:
            return None
        raw = text
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _credit_value(raw: Any) -> float | None:
    """学分数值；空 / ``-`` → ``None``（空 ≠ 0）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text in _BLANK_MARKERS:
            return None
        raw = text
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _score_text(raw: Any) -> str:
    """成绩单元格文字：能转数字就走 ``format_score``，否则原样显示；空 →「—」。"""
    value = _score_value(raw)
    if value is not None:
        return profile_shared.format_score(value)
    if raw is None:
        return "—"
    text = str(raw).strip()
    if not text or text in _BLANK_MARKERS:
        return "—"
    return text


def _credit_text(raw: Any) -> str:
    """学分单元格文字：空 →「—」，不是 0。"""
    text = profile_shared.format_credit(raw)
    return text or "—"


class _SemesterSideWorker(QThread):
    """后台推理线程：逐块把 ``engine.stream`` 的文本发回主线程。

    ★ 子线程**绝不碰控件**（AGENTS §5b / §21a）：只发 Signal，由主线程的
      ``_on_chunk`` 追加到 ``QTextEdit``。测试可以把 ``_start_worker`` 换成
      同步执行 ``worker.run()``，信号在同一个线程里直接派发。
    """

    chunk = Signal(str)
    failed = Signal(str)
    done = Signal()

    def __init__(self, engine: Any, prompt: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._prompt = prompt

    def run(self) -> None:  # noqa: D401 - QThread 的约定入口
        try:
            loader = getattr(self._engine, "load", None)
            if callable(loader):
                loader()
            for piece in self._engine.stream(self._prompt):
                if piece:
                    self.chunk.emit(str(piece))
        except InferenceUnavailable as exc:
            self.failed.emit(str(exc) or "本地推理引擎暂时不可用。")
        except Exception as exc:  # noqa: BLE001 - 任何推理异常都要变成中文提示
            logger.exception("学期侧写生成失败")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


class SemesterSideProfile(QWidget):
    """一个学期的侧写页（只读成绩表 + 学业画像 + AI 侧写）。"""

    #: 点「← 返回档案」时发出（主窗接到后切回档案详情页）。
    backRequested = Signal()
    model_settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: Any = None
        self._username = ""
        self._student: dict[str, Any] = {}
        self._records: list[dict[str, Any]] = []
        self._term = ""
        self._moral: Any = None
        self._store: Any = None
        self._profile_store_injected: Any = None

        self._side_worker: _SemesterSideWorker | None = None
        self._ai_error = ""
        self._text_context: tuple[str, str] | None = None
        self._preferred_model = ""

        self._build_ui()
        self._refresh_header()
        self.reload()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        self._back_button = QToolButton(self)
        self._back_button.setObjectName("BackLink")
        self._back_button.setText("← 返回档案")
        self._back_button.setAutoRaise(True)
        self._back_button.setCursor(Qt.PointingHandCursor)
        self._back_button.clicked.connect(self.backRequested.emit)
        root.addWidget(self._back_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self._title_label = QLabel(self)
        self._title_label.setObjectName("H1")
        self._title_label.setWordWrap(True)
        root.addWidget(self._title_label)

        self._student_label = QLabel(self)
        self._student_label.setObjectName("ProfileMeta")
        self._student_label.setWordWrap(True)
        root.addWidget(self._student_label)

        content = QWidget()
        self._body_layout = QVBoxLayout(content)
        self._body_layout.setContentsMargins(0, 0, 8, 4)
        self._body_layout.setSpacing(16)
        self._body_layout.addWidget(self._build_portrait_panel())

        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setObjectName("semesterSplitter")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(12)
        self._splitter.addWidget(self._build_courses_panel())
        self._splitter.addWidget(self._build_side_panel())
        self._splitter.setSizes([440, 560])
        self._body_layout.addWidget(self._splitter, 1)

        self._scroll = profile_scroll(content)
        root.addWidget(self._scroll, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._arrange_panels()

    def _arrange_panels(self) -> None:
        if not hasattr(self, "_splitter"):
            return
        orientation = Qt.Orientation.Horizontal if self.width() >= 880 else Qt.Orientation.Vertical
        if self._splitter.orientation() != orientation:
            self._splitter.setOrientation(orientation)
            if orientation == Qt.Orientation.Horizontal:
                self._splitter.setSizes([440, 560])
            else:
                self._splitter.setSizes([350, 420])
        columns = 4 if self.width() >= 880 else 3 if self.width() >= 620 else 2
        if columns != self._metric_columns:
            self._metric_columns = columns
            for position, label in enumerate(self._metric_labels):
                self._metrics.removeWidget(label)
                self._metrics.addWidget(label, position // columns, position % columns)
            for column in range(4):
                self._metrics.setColumnStretch(column, int(column < columns))

    def _build_courses_panel(self) -> QWidget:
        panel = make_card(shadow=False, name="ProfilePanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(14, 14, 14, 14)
        box.setSpacing(10)
        title = QLabel("课程成绩", panel)
        title.setObjectName("H2")
        box.addWidget(title)

        self._empty_hint = QLabel("本学期暂无成绩记录", panel)
        self._empty_hint.setObjectName("emptyHint")
        self._empty_hint.setWordWrap(True)
        box.addWidget(self._empty_hint)

        self._table = QTableWidget(0, len(COLUMNS), panel)
        self._table.setObjectName("courseTable")
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setMinimumHeight(250)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 185)
        header.setMinimumSectionSize(48)
        self._table.setColumnWidth(1, 58)
        self._table.setColumnWidth(2, 58)

        vheader = self._table.verticalHeader()
        vheader.setVisible(False)
        vheader.setSectionResizeMode(QHeaderView.Fixed)
        vheader.setMinimumSectionSize(MIN_ROW_HEIGHT)
        vheader.setDefaultSectionSize(ROW_HEIGHT)

        box.addWidget(self._table, 1)
        return panel

    def _build_portrait_panel(self) -> QWidget:
        portrait = make_card(shadow=False, name="ProfilePortrait")
        box = QVBoxLayout(portrait)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        title = QLabel("学业画像", portrait)
        title.setObjectName("H2")
        box.addWidget(title)
        self._metrics = QGridLayout()
        self._metric_labels: list[QLabel] = []
        self._metric_columns = 2
        self._metrics.setSpacing(8)
        box.addLayout(self._metrics)
        self._stat_average = self._add_metric(0)
        self._stat_courses = self._add_metric(1)
        self._stat_fails = self._add_metric(2)
        self._stat_credits = self._add_metric(3)
        self._stat_moral = self._add_metric(4)
        self._stat_note = QLabel("空成绩不计入平均分和学分合计，0 分按缺考计入。", portrait)
        self._stat_note.setObjectName("ProfileMeta")
        self._stat_note.setWordWrap(True)
        box.addWidget(self._stat_note)
        return portrait

    def _add_metric(self, position: int, text: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("ProfileMetric")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setMinimumHeight(40)
        self._metric_labels.append(label)
        self._metrics.addWidget(label, position // self._metric_columns, position % self._metric_columns)
        return label

    def _build_side_panel(self) -> QWidget:
        side = make_card(shadow=False, name="ProfilePanel")
        side_box = QVBoxLayout(side)
        side_box.setContentsMargins(14, 14, 14, 14)
        side_box.setSpacing(10)
        title = QLabel("AI 侧写", side)
        title.setObjectName("H2")
        side_box.addWidget(title)
        self._ai_status = QLabel("尚未生成", side)
        self._ai_status.setObjectName("ProfileMeta")
        self._ai_status.setWordWrap(True)

        self._ai_text = ProfileDocument(side)
        self._ai_text.setReadOnly(True)
        self._ai_text.setPlaceholderText("生成的学期侧写将在这里显示。")
        self._ai_text.setMinimumHeight(235)
        side_box.addWidget(self._ai_text, 1)
        side_box.addWidget(self._ai_status)
        self._runtime_hint = QLabel(CORE_MISSING_HINT, side)
        self._runtime_hint.setObjectName("ProfileMeta")
        self._runtime_hint.setWordWrap(True)
        side_box.addWidget(self._runtime_hint)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._generate_button = QPushButton("生成学期侧写", side)
        self._generate_button.setObjectName("Ghost")
        self._generate_button.clicked.connect(self._start_generation)
        actions.addWidget(self._generate_button)
        self._save_button = QPushButton("保存", side)
        self._save_button.setObjectName("Primary")
        self._save_button.clicked.connect(self._save_side_text)
        actions.addWidget(self._save_button)
        self._generate_button.setMinimumHeight(34)
        self._save_button.setMinimumHeight(34)
        self._ai_actions = actions

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._model_picker = ModelPicker(side)
        self._model_picker.model_selected.connect(self._choose_model)
        self._model_picker.library_requested.connect(self._on_model_center_clicked)
        tools.addWidget(self._model_picker)
        tools.addStretch(1)
        self._install_core_btn = QPushButton("下载推理核心", side)
        self._install_core_btn.setObjectName("Primary")
        self._install_core_btn.setMinimumHeight(34)
        self._install_core_btn.clicked.connect(self._on_setup_core_clicked)
        tools.addWidget(self._install_core_btn)
        side_box.addLayout(tools)
        side_box.addLayout(actions)
        return side

    # -------------------------------------------------------------- setter / 刷新
    def set_session(self, session: Any) -> None:
        """接住主窗的登录 session；``None`` 表示未登录（侧写存不进加密库）。"""
        username = ""
        if isinstance(session, str):
            username = session.strip()
        elif isinstance(session, dict):
            username = _clean_text(session.get("username"))
        elif session is not None:
            username = _clean_text(getattr(session, "username", ""))
        if username != self._username:
            self._username = username
            self._store = None
            self._profile_store_injected = None
        self._session = session
        self._refresh_header()

    def set_student(self, student: dict[str, Any] | None) -> None:
        """设置学生：``{'student_id', 'name', 'class_name', ...}``。"""
        self._student = dict(student or {})
        self.reload()

    def set_records(self, records: list[dict[str, Any]] | None) -> None:
        """设置该生的成绩历史行（``grade_history`` 的原始行）。"""
        self._records = [row for row in (records or []) if isinstance(row, dict)]
        self.reload()

    def set_term(self, term: str) -> None:
        """设置要看的学期标签，如 ``"大二下"``。"""
        self._term = _clean_text(term)
        self.reload()

    def set_moral(self, moral: Any) -> None:
        """设置德育分；``None`` 表示没录（显示「未录入」，不是 0）。"""
        self._moral = moral
        self.reload()

    def set_profile_store(self, store: Any) -> None:
        """注入 ``ProfileStore``（测试用；生产代码不调用）。"""
        self._profile_store_injected = store
        self._store = store

    # --------------------------------------------------------------- 数据
    def _student_name(self) -> str:
        return _clean_text(
            self._student.get("name")
            or self._student.get("student_name")
            or self._student.get("student")
        )

    def _student_id(self) -> str:
        raw = _clean_text(
            self._student.get("student_id")
            or self._student.get("sid")
            or self._student.get("id")
        )
        return raw[4:] if raw.startswith("sid:") else raw

    def _class_name(self) -> str:
        return _clean_text(
            self._student.get("class_name")
            or self._student.get("class")
            or self._student.get("classname")
        )

    def _term_rows(self) -> list[dict[str, Any]]:
        """只留下 ``term == self._term`` 的行；没设学期时一行都不显示。"""
        if not self._term:
            return []
        rows: list[dict[str, Any]] = []
        from ui.pages.profile_shared import is_unselected_elective
        for row in self._records:
            term = _clean_text(row.get("term") or row.get("semester"))
            if term == self._term and not is_unselected_elective(row.get("category"), row.get("score")):
                rows.append(row)
        return rows

    # --------------------------------------------------------------- 刷新
    def reload(self) -> None:
        """按当前 setter 设好的数据重画整页（不重新读库、不重新算加权）。"""
        self._refresh_header()
        rows = self._term_rows()
        self._fill_table(rows)
        self._refresh_portrait(rows)
        self._refresh_ai_text()
        self._refresh_ai_controls()

    def _refresh_header(self) -> None:
        self._title_label.setText(f"学期侧写 · {self._term or '未指定学期'}")
        name = self._student_name()
        sid = self._student_id()
        if name and sid:
            line = f"{name} · {sid}"
        elif name or sid:
            line = name or sid
        else:
            line = "未选择学生"
        self._student_label.setText(line)

    def _fill_table(self, rows: list[dict[str, Any]]) -> None:
        self._table.setRowCount(0)
        self._table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            course = _clean_text(row.get("course") or row.get("course_name")) or "（课程名缺失）"
            values = (
                course,
                _credit_text(row.get("credit")),
                _score_text(row.get("score")),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignLeft | Qt.AlignVCenter
                    if column == 0
                    else Qt.AlignCenter
                )
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setToolTip(course)
                self._table.setItem(index, column, item)
            self._table.setRowHeight(index, ROW_HEIGHT)
        self._empty_hint.setVisible(not rows)

    def _refresh_portrait(self, rows: list[dict[str, Any]]) -> None:
        scores: list[float] = []
        credits: list[float] = []
        has_credit = False
        for row in rows:
            score = _score_value(row.get("score"))
            if score is None:
                continue
            scores.append(score)
            credit = _credit_value(row.get("credit"))
            if credit is not None:
                has_credit = True
                credits.append(credit)

        course_text = f"本学期课程数：{len(rows)}"
        if len(scores) != len(rows):
            course_text += f"（有成绩 {len(scores)} 门）"
        self._stat_courses.setText(course_text)

        if scores:
            average = sum(scores) / len(scores)
            self._stat_average.setText(f"本学期算术平均：{average:.2f}")
        else:
            self._stat_average.setText("本学期算术平均：—")

        fails = sum(1 for score in scores if score < 60)
        self._stat_fails.setText(f"挂科数：{fails}")
        if self._stat_fails.property("attention") != bool(fails):
            self._stat_fails.setProperty("attention", bool(fails))
            self._stat_fails.style().unpolish(self._stat_fails)
            self._stat_fails.style().polish(self._stat_fails)

        if has_credit:
            self._stat_credits.setText(f"学分合计：{profile_shared.format_credit(sum(credits))}")
        else:
            self._stat_credits.setText("学分合计：—")

        self._stat_moral.setText(f"德育分：{profile_shared.moral_text(self._moral)}")

    # --------------------------------------------------------------- AI 文本
    def _refresh_ai_text(self) -> None:
        """换学生 / 换学期时清空旧文本，并读回这个学期存过的侧写。"""
        if self._side_worker is not None:
            return
        context = (self._student_id(), self._term)
        if context == self._text_context:
            return
        self._text_context = context
        self._ai_error = ""
        self._ai_text.clear()
        self._ai_status.setText("尚未生成")
        self._load_saved_side_text()

    def _store_id(self) -> str:
        """持久化用的学号：``ProfileStore`` 的行键（去掉 ``sid:`` 前缀）。"""
        return self._student_id()

    def _profile_store(self) -> Any:
        if self._profile_store_injected is not None:
            return self._profile_store_injected
        if self._store is not None:
            return self._store
        if not self._username:
            return None
        try:
            self._store = ProfileStore(username=self._username)
        except Exception:  # noqa: BLE001 - 侧写是便利数据，坏掉不该拦住看成绩
            logger.exception("初始化 ProfileStore 失败")
            return None
        return self._store

    def _load_saved_side_text(self) -> None:
        """读回这个学期的侧写；读不到就保持空白，不报错。"""
        if not self._term or not self._student_id():
            return
        store = self._profile_store()
        if store is None:
            return
        term = self._term
        text = ""
        try:
            reader = getattr(store, "semester_side_text", None)
            if callable(reader):
                text = str(reader(self._store_id(), term) or "")
            else:
                raw = str(getattr(store, "side_text", lambda *_: "")(self._store_id()) or "")
                text = self._extract_term_text(raw, term)
        except Exception:  # noqa: BLE001 - 读侧写失败不影响成绩显示
            logger.exception("读取学期侧写失败")
            return
        if text.strip():
            self._ai_text.setPlainText(text)
            self._ai_status.setText("已读取这个学期存过的侧写。")

    @staticmethod
    def _extract_term_text(raw: str, term: str) -> str:
        """从回退格式 ``[学期]\\n正文`` 里取出当前学期；别的学期一律不显示。"""
        prefix = f"[{term}]\n"
        if raw.startswith(prefix):
            return raw[len(prefix):]
        match = _TERM_PREFIX.match(raw)
        if match:
            return ""          # 是**别的学期**的侧写，不要串到这一学期
        return raw             # 老数据没有学期前缀，当作通用侧写

    def _side_prompt(self, rows: list[dict[str, Any]]) -> str:
        """只注入本学期成绩与德育分的提示词。"""
        term = self._term or "本学期"
        qualifier = (
            f"任务：只写 {term} 这一个学期的侧写。下面只提供这个学期的成绩与德育分，"
            "不要引用其它学期，也不要把不同学期混在一起。"
            "请分三段写：本学期表现、优势与短板、下学期建议。\n\n"
        )
        key = student_key(self._student_id(), self._student_name())
        return qualifier + profile_shared.build_side_text_prompt(
            self._student_name(),
            self._student_id(),
            key,
            rows,
            self._moral,
        )

    # --------------------------------------------------------------- 生成
    def _start_generation(self) -> None:
        """「用 AI 生成本学期侧写」：检查引擎 → 选模型 → 后台流式生成。"""
        if self._side_worker is not None:
            return

        try:
            ready, reason = engine_available()
        except Exception as exc:  # noqa: BLE001 - 引擎探测失败也要给中文原因
            logger.exception("探测本地推理引擎失败")
            QMessageBox.information(self, "本地推理引擎未就绪", f"探测失败：{friendly_error(exc)}")
            return
        if not ready:
            self._refresh_ai_controls()
            QMessageBox.information(
                self, "本地推理引擎未就绪", reason or CORE_MISSING_HINT
            )
            return

        try:
            models = [model for model in list_local_models()
                      if bool(getattr(model, "usable", False))]
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取本地模型列表失败")
            QMessageBox.information(
                self, "AI 侧写", f"读取本地模型列表失败：{friendly_error(exc)}"
            )
            return
        if not models:
            self._refresh_ai_controls()
            QMessageBox.information(self, "AI 侧写", "请先在模型中心下载模型。")
            return

        model_path = str(getattr(self._selected_model(models), "path", "") or "")
        if not model_path:
            QMessageBox.information(self, "AI 侧写", "模型文件缺失，请先在模型中心重新下载模型。")
            return

        rows = self._term_rows()
        if not rows:
            QMessageBox.information(self, "AI 侧写", "本学期还没有成绩记录，先导入成绩再生成侧写。")
            return

        try:
            engine = LocalModelEngine(model_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("加载本地模型失败")
            QMessageBox.information(
                self, "AI 侧写", f"加载本地模型失败：{friendly_error(exc)}"
            )
            return

        prompt = self._side_prompt(rows)
        self._ai_text.clear()
        self._ai_error = ""
        self._ai_status.setText("正在生成本学期侧写……")
        self._set_busy(True)

        worker = _SemesterSideWorker(engine, prompt, self)
        self._side_worker = worker
        worker.chunk.connect(lambda text, w=worker: self._on_chunk(w, text))
        worker.failed.connect(lambda message, w=worker: self._on_failed(w, message))
        worker.done.connect(lambda w=worker: self._on_done(w))
        try:
            self._start_worker(worker)
        except Exception as exc:  # noqa: BLE001 - 线程起不来也要恢复按钮
            logger.exception("启动侧写线程失败")
            self._side_worker = None
            self._set_busy(False)
            self._ai_status.setText("生成失败")
            QMessageBox.information(
                self, "AI 侧写", f"启动本地推理失败：{friendly_error(exc)}"
            )

    @staticmethod
    def _start_worker(worker: _SemesterSideWorker) -> None:
        """线程启动点（测试把它换成同步 ``worker.run()``）。"""
        worker.start()

    def _on_chunk(self, worker: _SemesterSideWorker, text: str) -> None:
        if worker is not self._side_worker:
            return
        cursor = self._ai_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._ai_text.setTextCursor(cursor)
        self._ai_text.insertPlainText(text)

    def _on_failed(self, worker: _SemesterSideWorker, message: str) -> None:
        if worker is not self._side_worker:
            return
        self._ai_error = message or "本地推理引擎生成失败，请稍后再试。"

    def _on_done(self, worker: _SemesterSideWorker) -> None:
        if worker is not self._side_worker:
            return
        self._side_worker = None
        self._set_busy(False)
        if self._ai_error:
            message = self._ai_error
            self._ai_error = ""
            self._ai_status.setText("生成失败")
            QMessageBox.information(self, "AI 侧写", message)
            return
        if not self._ai_text.toPlainText().strip():
            self._ai_status.setText("没有生成内容")
            QMessageBox.information(self, "AI 侧写", "这次没有生成任何内容，可以再试一次。")
            return
        self._ai_status.setText("已生成本学期侧写。")

    def _set_busy(self, busy: bool) -> None:
        self._generate_button.setEnabled(not busy)
        self._save_button.setEnabled(not busy)
        self._model_picker.setEnabled(not busy)

    # --------------------------------------------------------------- 保存
    def _save_side_text(self) -> None:
        """把当前文本按学期存进 ``ProfileStore``。"""
        text = self._ai_text.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "保存侧写", "还没有可保存的内容，请先生成本学期侧写。")
            return
        student_id = self._store_id()
        if not student_id:
            QMessageBox.information(self, "保存侧写", "缺少学号，无法保存侧写。")
            return
        if not self._term:
            QMessageBox.information(self, "保存侧写", "缺少学期，无法保存侧写。")
            return
        store = self._profile_store()
        if store is None:
            QMessageBox.information(self, "保存侧写", "侧写暂时存不进加密库（没登录？）")
            return

        saver = getattr(store, "save_semester_side_text", None)
        try:
            if callable(saver):
                saver(student_id, self._term, text)
            else:
                store.save_side_text(student_id, f"[{self._term}]\n{text}")
        except Exception:  # noqa: BLE001 - 失败要明确告诉用户，正文留在框里
            logger.exception("保存学期侧写失败（%s / %s）", student_id, self._term)
            self._ai_status.setText("保存失败")
            QMessageBox.information(self, "保存侧写", "保存失败，侧写正文还留在框里，可以稍后再试。")
            return
        self._ai_status.setText("已保存到本地加密文件。")

    def _selected_model(self, models):
        return next((m for m in models if getattr(m, "name", "") == self._preferred_model), models[0] if models else None)

    def _choose_model(self, model) -> None:
        self._preferred_model = getattr(model, "name", "")
        self._refresh_ai_controls()

    def _refresh_ai_controls(self) -> None:
        try:
            ready, _ = engine_available()
            models = [m for m in list_local_models() if getattr(m, "usable", True)] if ready else []
        except Exception:
            ready, models = False, []
        self._runtime_hint.setText(CORE_MISSING_HINT if not ready else "请在模型中心下载模型。")
        self._runtime_hint.setVisible(not ready or not models)
        self._install_core_btn.setVisible(not ready)
        self._model_picker.setVisible(ready)
        self._model_picker.set_models(models, self._selected_model(models))
        self._generate_button.setVisible(ready)
        self._save_button.setVisible(ready)
        self._ai_status.setVisible(ready)
        if hasattr(self, "_talk_button"):
            grounded = getattr(self, "workbench_vault", None) is not None
            self._talk_button.setVisible(ready or grounded)
            self._talk_button.setEnabled(bool(models) or grounded)

    def _on_setup_core_clicked(self) -> None:
        try:
            from ui.widgets.backend_setup import open_backend_setup
            open_backend_setup(self)
        except Exception:
            logger.exception("打开推理核心安装向导失败")
        self._refresh_ai_controls()

    def _on_model_center_clicked(self) -> None:
        if callable(getattr(self.window(), "_go_model_center", None)):
            self.model_settings_requested.emit()
            return
        try:
            from ui.model_center_dialog import open_model_center
            open_model_center(self)
        except Exception:
            logger.exception("打开模型中心失败")
        self._refresh_ai_controls()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_ai_controls()


# ---------------------------------------------------------------------------
# 档案页 page1 的接线适配层
# ---------------------------------------------------------------------------
class SemesterSideProfilePage(SemesterSideProfile):
    """档案页第二级视图的适配层。

    档案页按任务书契约调用 :meth:`set_context`，本类把它翻译成父类的
    ``set_session / set_student / set_records / set_term``，并补两件事：

    * 侧写正文允许辅导员自己改字再保存（父类默认只读）；
    * 「和 AI 谈谈这个学期」按钮 —— 系统提示词只注入本学期数据。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        self._gpa_scheme = resolve_scheme(DEFAULT_SCHEME)
        self.workbench_vault = None
        super().__init__(parent)
        self._on_back: Any = None
        # 允许改字再保存（父类默认只读，任务书要求可编辑）
        self._ai_text.setReadOnly(False)
        self._talk_button = QPushButton("和 AI 谈谈这个学期", self)
        self._talk_button.setObjectName("Ghost")
        self._talk_button.setMinimumHeight(34)
        self._talk_button.setMaximumWidth(220)
        self._talk_button.clicked.connect(self._talk_to_ai)
        self._ai_actions.addStretch()
        self._ai_actions.addWidget(self._talk_button)
        self.backRequested.connect(self._on_back_clicked)

    def set_gpa_scheme(self, scheme: Any) -> None:
        self._gpa_scheme = resolve_scheme(scheme)
        self._refresh_gpa(self._term_rows())

    # -- 契约入口 ---------------------------------------------------------
    def set_context(
        self,
        store: Any,
        username: str,
        student_id: str,
        student_name: str,
        semester: str,
        *,
        on_back: Any = None,
    ) -> None:
        """绑定一个学生的某个学期，并立刻重画整页。"""
        self._on_back = on_back
        self.set_session({"username": username})
        profile_store = None
        try:
            profile_store = ProfileStore(store=store, username=username)
        except Exception:  # noqa: BLE001 - 主档/侧写读不出来也不许拦住看成绩
            logger.exception("初始化 ProfileStore 失败")
        if profile_store is not None:
            self.set_profile_store(profile_store)
        # 谈话窗口（ChatDialog）要从 parent 上取真正的 CryptoStore
        self._store = store
        self.set_student(
            {
                "student_id": student_id,
                "name": student_name,
            }
        )
        self.set_records(self._collect_rows(store, username, student_id, student_name))
        self.set_term(semester)
        self.reload()
        self._title_label.setText(
            f"{student_name or '（未命名）'} · {semester or '未指定学期'} 学业侧写"
        )
        self._student_label.setText(f"学号 {student_id}" if student_id else "未填写学号")
        self._scroll.verticalScrollBar().setValue(0)

    def current_semester(self) -> str:
        return self._term

    def current_student_id(self) -> str:
        return self._student_id()

    # -- 数据 -------------------------------------------------------------
    def _collect_rows(
        self,
        store: Any,
        username: str,
        student_id: str,
        student_name: str,
    ) -> list[dict[str, Any]]:
        """把该生所有学期的成绩行摊平成父类要的形态（term/course/score/credit）。"""
        try:
            from ui.pages.profile_shared import student_semester_rows
        except Exception:  # noqa: BLE001
            logger.exception("加载成绩读取工具失败")
            return []
        key = student_key(student_id, student_name)
        try:
            grouped = student_semester_rows(store, username, student_id, key)
        except Exception:  # noqa: BLE001 - 读不到就当没有成绩
            logger.exception("读取学期成绩失败")
            return []
        rows: list[dict[str, Any]] = []
        for term, items in grouped.items():
            for row in items:
                course = str(row.get("course") or row.get("course_name") or "").strip()
                rows.append(
                    {
                        "student_id": student_id,
                        "semester": term,
                        "term": term,
                        "course_name": course,
                        "course": course,
                        "score": row.get("score"),
                        "credit": row.get("credit"),
                        # ★ 类别要带下来：加权均分（口径②）只有认得类别才计得进去
                        "category": row.get("category"),
                    }
                )
        return rows

    # -- 谈话 -------------------------------------------------------------
    def _talk_to_ai(self) -> None:
        """打开谈话窗口：系统提示词只注入本学期成绩。"""
        if self.workbench_vault is not None:
            from ui.academic_context import AcademicContext
            from ui.grounded_chat_dialog import GroundedChatDialog

            rows = self._term_rows()
            context = AcademicContext(self._side_prompt(rows), ()) if rows else None
            dialog = GroundedChatDialog(self, self.workbench_vault, self._student_id(), context,
                                        self.model_settings_requested.emit)
            dialog.exec()
            return
        ready, _ = engine_available()
        if not ready:
            self._refresh_ai_controls()
            return
        if not self._term_rows():
            self._ai_status.setText("本学期还没有成绩记录，先导入成绩再谈。")
            return
        try:
            from ui.chat_dialog import open_chat
        except Exception as exc:  # noqa: BLE001
            logger.exception("加载谈话窗口失败")
            self._ai_status.setText(f"谈话窗口打不开：{friendly_error(exc)}")
            return
        try:
            open_chat(
                parent=self,
                system_prompt=self._side_prompt(self._term_rows()),
                prefer_model=self._preferred_model or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("打开谈话窗口失败")
            self._ai_status.setText(f"谈话窗口打不开：{friendly_error(exc)}")

    # -- 返回 -------------------------------------------------------------
    def _on_back_clicked(self) -> None:
        if callable(self._on_back):
            try:
                self._on_back()
            except Exception:  # noqa: BLE001
                logger.exception("学期侧写页返回回调失败")

    # -- 任务书 §3 的加厚：挂科列 / 口径② 说明 / 加权、绩点、趋势、短板、可提升 ---

    def _ensure_extras(self) -> None:
        """一次性补出任务书要求的列与画像行（父类保持原样，只在子类加）。"""
        if getattr(self, "_extras_ready", False):
            return
        self._extras_ready = True
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["课程", "学分", "成绩", "状态"])
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(3, 58)

        self._course_note = QLabel(
            "加权均分按必修和限选计算。空白成绩不等于 0 分。",
            self,
        )
        self._course_note.setObjectName("courseNote")
        self._course_note.setWordWrap(True)
        holder = self._table.parentWidget()
        if holder is not None and holder.layout() is not None:
            holder.layout().addWidget(self._course_note)

        portrait = self._stat_note.parentWidget()
        box = portrait.layout() if portrait is not None else None
        if box is None:
            return
        index = max(0, box.indexOf(self._stat_note))
        self._extra_weighted = self._add_metric(5, "本学期加权均分（口径②）：—")
        self._extra_gpa = self._add_metric(6)
        self._extra_gpa.setTextFormat(Qt.TextFormat.PlainText)
        self._extra_trend = QLabel("趋势：—", portrait)
        self._extra_weak = QLabel("短板科目：—", portrait)
        self._extra_improve = QLabel("可提升科目：—", portrait)
        for label in (self._extra_weak, self._extra_improve):
            label.setToolTip("短板与可提升仅供辅导参考。")
        for position, widget in enumerate(
            (
                self._extra_trend,
                self._extra_weak,
                self._extra_improve,
            )
        ):
            widget.setWordWrap(True)
            widget.setObjectName("ProfileMeta")
            box.insertWidget(index + position, widget)

    def _fill_table(self, rows: list[dict[str, Any]]) -> None:  # noqa: D401 - 覆写
        """父类只画三列；这里再加一列「是否挂科」，其余完全沿用父类。"""
        self._ensure_extras()
        super()._fill_table(rows)
        for index, row in enumerate(rows):
            score = _score_value(row.get("score"))
            if score is None:
                text = "—"
            elif profile_shared.score_is_failed(score):
                text = "挂科"
            else:
                text = "通过"
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if score is not None:
                item.setForeground(QColor('#9A471C' if text == '挂科' else '#0F766E'))
            self._table.setItem(index, 3, item)

    def _refresh_gpa(self, rows: list[dict[str, Any]]) -> None:
        gpa = profile_shared.rows_gpa(rows, self._gpa_scheme)
        precision = self._gpa_scheme["round"]
        value = "—" if gpa is None else f"{gpa:.{precision}f}"
        self._extra_gpa.setText(f"GPA {value}\n{scheme_label(self._gpa_scheme)}")

    def _refresh_portrait(self, rows: list[dict[str, Any]]) -> None:
        """父类画出算术平均等；这里补加权、绩点、趋势、短板、可提升。"""
        super()._refresh_portrait(rows)
        self._ensure_extras()
        if not hasattr(self, "_extra_weighted"):
            return

        weighted = profile_shared.rows_weighted_mean(rows)
        self._extra_weighted.setText(
            "本学期加权均分（口径②）："
            + (f"{weighted:.2f}" if weighted is not None else "—")
        )

        self._refresh_gpa(rows)

        average = profile_shared.rows_arithmetic_mean(rows)
        previous = self._previous_term_average()
        if previous is None or average is None:
            self._extra_trend.setText("趋势：—（没有上一学期可对比）")
        else:
            arrow = profile_shared.trend_arrow_text(average - previous)
            self._extra_trend.setText(
                f"趋势：{arrow} 比上一学期 {average - previous:+.2f}"
            )

        scored = [
            (str(row.get("course") or row.get("course_name") or "").strip(),
             _score_value(row.get("score")))
            for row in rows
        ]
        scored = [(name, score) for name, score in scored if name and score is not None]
        weak = sorted(scored, key=lambda pair: pair[1])[:3]
        self._extra_weak.setText(
            "短板科目："
            + ("、".join(f"{name}（{score:g}）" for name, score in weak) or "—")
        )
        improve = [
            f"{name}（{score:g}）"
            for name, score in scored
            if average is not None
            and score < average
            and not profile_shared.score_is_failed(score)
        ]
        self._extra_improve.setText(
            "可提升科目：" + ("、".join(improve) if improve else "—")
        )

    def _previous_term_average(self) -> float | None:
        """上一个**有成绩**学期的算术平均（没有就返回 None，不写 0）。"""
        from core.archival import SEMESTER_ORDER

        rows_by_term: dict[str, list[dict[str, Any]]] = {}
        for row in self._records:
            term = _clean_text(row.get("term") or row.get("semester"))
            if term:
                rows_by_term.setdefault(term, []).append(row)
        order = [term for term in SEMESTER_ORDER if term in rows_by_term]
        for term in rows_by_term:
            if term not in order:
                order.append(term)
        if self._term not in order:
            return None
        index = order.index(self._term)
        if index == 0:
            return None
        return profile_shared.rows_arithmetic_mean(rows_by_term[order[index - 1]])
