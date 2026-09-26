"""智育分析页 —— **本次唯一接真逻辑的页面**。

链路（严格照 Spec，一步都不省）::

    选文件 → pandas 读原始表 → core.validator.parse_and_validate
                                （= core.parser 三级提取 + 锚点校验）
           → 红线 3 人工回显确认（不可跳过）
           → core.calculator.calculate_many 三口径
           → core.archival.build_archive 归档（供侧写页用）
           → 表格显示 + 红标预警

**calculator / parser / validator / archival 一个字符都没改**，
它们返回什么这里就显示什么；本页只做格式化，不做任何二次算术。

★ 口径筛选（三个 Toggle）
------------------------

``必修`` / ``必修+限选`` / ``全部`` —— 正好就是 :class:`CalculationScope` 的三档，
不新增、不改口径定义（Spec 第 61 / 246 行：默认必修+限选计入，**选修默认剔除**）。

切换筛选只做三件事：

1. ``showColumn`` / ``hideColumn`` 把口径列显隐对齐到当前选择；
2. 红标**跟着当前口径重算** —— 因为分母变了，"挂科/缺考"的判定基础就变了；
3. 把重算后的名单再推给侧写页一次（它名单上的红点也按口径判）。

★ 表头为什么三档共用一句「加权均分」
----------------------------------

同一时刻只有一档列可见（另外两档 ``hideColumn``），表头再区分"口径①/②/③"
就是重复信息 —— 上面那三个按钮已经亮着当前是哪一档。三条信息各归各位：

* **表头**　　→ 这一列是什么数据（恒定 :data:`SCOPE_HEADER`）
* **按钮 / 状态行** → 现在算的是哪一档
* **表下明细行**　→ 三档各是多少，逐档写全名（证据链在这里，一个数都没少）

★ 为什么行序是唯一的身份
------------------------

表格里一行 = ``parsed.valid_rows`` 里的第 i 行，而 ``core.calculator.calculate_all``
就是按 ``valid_rows`` 的顺序逐行算的，所以 ``results[scope][i]`` 一定属于第 i 行。

**不能用姓名去查。** 同名学生（历届都有）会让 ``{姓名: 结果}`` 这种字典
互相覆盖 —— 两行显示同一份数字、同一份意愿。学号才是 Spec 第 2 章指定的
纵向钥匙；没有学号时退回姓名，并且界面要**明说同名风险**。

红标判定见 :mod:`ui.flags`（只决定标不标色，不参与加权）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.archival import (
    SEMESTER_ORDER,
    TOTAL_TERMS,
    Archive,
    archive_key,
    build_archive,
)
from core.calculator import CalculationScope, calculate_many
from core.crypto_store import CryptoError, CryptoStore
from core.parser import Category
from core.scholarship import ClassRecord, save_class, validate_class_name
from core.user_store import load_user_json, save_user_json
from core.validator import describe_alerts, parse_and_validate
from ui.flags import LEVEL_DANGER, LEVEL_WARNING, build_flags_by_index
from ui.intentions import student_key
from ui.review_dialog import confirm_courses

__all__ = [
    "ACTION_COLUMN",
    "COLUMN_TITLES",
    "FILTER_LABELS",
    "FILTER_ORDER",
    "FIRST_SCOPE_COLUMN",
    "FLAG_COLUMN",
    "ID_COLUMN",
    "NAME_COLUMN",
    "SCOPE_COLUMNS",
    "SCOPE_HEADER",
    "SEMESTER_TERMS",
    "AnalysisPage",
    "scope_breakdown",
]

#: 表格里的三档口径列（顺序即列顺序）。
#:
#: 第二个元素是**口径全名**，只用在表下方那行明细里
#: （``口径① 含全部＝85.05…　|　口径② 仅必修+限选＝…``）。
#: 它**不进表头** —— 表头统一叫 :data:`SCOPE_HEADER`，原因见那里。
SCOPE_COLUMNS: tuple[tuple[str, str, CalculationScope], ...] = (
    ("all", "口径① 含全部", CalculationScope.ALL),
    ("req_lim", "口径② 仅必修+限选", CalculationScope.REQUIRED_LIMITED),
    ("req", "口径③ 仅必修", CalculationScope.REQUIRED_ONLY),
)

#: 三档口径列在**表头**里统一显示这一行字。
#:
#: ★ 为什么不写「口径① 含全部」这种带区分的表头
#:
#: 因为**同一时刻只有一档列是可见的**（``_apply_scope`` 把另外两档
#: ``hideColumn`` 掉）。表头再写"口径②"就是重复信息 —— 上面那三个筛选按钮
#: 已经亮着当前是哪一档了。用户看到的就是一句"加权均分"，干净。
#:
#: ★ 分工（改这里之前先读）：
#:
#: * **表头** = 这一列是什么数据 → :data:`SCOPE_HEADER`，恒定，不随筛选变；
#: * **筛选按钮 + 状态行** = 现在算的是哪一档 → ``FILTER_LABELS`` / ``_update_status``；
#: * **表下明细行** = 三档各是多少（逐档列全名）→ :data:`SCOPE_COLUMNS` 的第二项。
#:
#: 三条各司其职，所以证据链一点没丢：三个数字仍然同时可见、各有全名。
SCOPE_HEADER = "加权均分"

#: 完整列标题：**姓名 + 学号 + 三口径 + 红标原因 + 操作**。
#:
#: ★ 必须从 SCOPE_COLUMNS **推出来**，绝不能手写。
#:   踩过的坑：加「操作」列时手写了一份表头元组，漏掉一个口径列 ——
#:   表头 5 个、数据 6 列，整体错位一格，结果「侧写」按钮正好压在
#:   「红标原因」的单元格上（表头写着"红标原因"的那一列显示的其实是口径③）。
#:   自动推导之后，两边再也不可能漂移。
COLUMN_TITLES: tuple[str, ...] = (
    ("姓名", "学号")
    + (SCOPE_HEADER,) * len(SCOPE_COLUMNS)
    + ("红标原因", "操作")
)

#: 列下标（同样推导，不写魔法数字）
NAME_COLUMN = 0
ID_COLUMN = 1
FIRST_SCOPE_COLUMN = 2
FLAG_COLUMN = len(COLUMN_TITLES) - 2
ACTION_COLUMN = len(COLUMN_TITLES) - 1

#: 口径筛选按钮的顺序：**由窄到宽**（必修 ⊂ 必修+限选 ⊂ 全部），最好理解
FILTER_ORDER: tuple[CalculationScope, ...] = (
    CalculationScope.REQUIRED_ONLY,
    CalculationScope.REQUIRED_LIMITED,
    CalculationScope.ALL,
)

#: 筛选按钮上的字。和 :data:`SCOPE_COLUMNS` 里的口径名是同一件事，只是短一点。
FILTER_LABELS: dict[CalculationScope, str] = {
    CalculationScope.REQUIRED_ONLY: "必修",
    CalculationScope.REQUIRED_LIMITED: "必修+限选",
    CalculationScope.ALL: "全部",
}

#: 默认筛选 = Spec 的默认计算口径（必修+限选计入，选修剔除）
DEFAULT_FILTER = CalculationScope.REQUIRED_LIMITED

#: 课程类别的中文名。**从 :class:`core.parser.Category` 取**，界面逐字对上。
#:
#: ★ charter 第一节把必修 / 限选 / 选修定成**三个完全独立的类别**，
#:   所以界面上也必须能一个字一个字地看出来谁被算进去了 ——
#:   这正是上一版文档把"选修"说成"归限选"时没人发现的原因：
#:   按钮上写着"全部"，没人知道"全部"到底含不含选修。
CATEGORY_LABELS: dict[Category, str] = {
    Category.REQUIRED: "必修",
    Category.LIMITED: "限选",
    Category.ELECTIVE: "选修",
    Category.UNKNOWN: "未标类别",
}

#: 口径列 / 筛选按钮旁边常驻的那句话（charter 一节的核心纠正）。
#:
#: ★ **不要在这里写 Markdown 记号**（`**加粗**` 之类）—— QLabel 不渲染 Markdown，
#:   用户看到的就是两个星号本身。第一版写成"**绝不并进限选**"，
#:   截图里原样显示成 `**绝不并进限选**`（截图才发现，静态断言查不出来）。
CALC_SCOPE_NOTE = (
    "必修、限选、选修是三个独立类别：口径① 三类全算，口径② 只算必修＋限选，"
    "口径③ 只算必修。选修默认剔除，绝不并进限选。"
)


def scope_breakdown(scope: CalculationScope) -> str:
    """把某一档口径"到底算了哪几类课"写成一句人话。

    ★ 真源是 :attr:`core.calculator.CalculationScope.categories`。这里**只做翻译**，
      不自己判断哪一类算不算数 —— 口径表一旦被口头复述第二遍，
      就会出现"选修归入限选"那种错误，而且界面上看不出来。
    """
    allowed = scope.categories
    order = (Category.REQUIRED, Category.LIMITED, Category.ELECTIVE)
    text = "＋".join(CATEGORY_LABELS[cat] for cat in order if cat in allowed)
    missing = [CATEGORY_LABELS[cat] for cat in order if cat not in allowed]
    if missing:
        text += f"（{'、'.join(missing)}不计入）"
    return text

#: 「操作」列宽度。「侧写」按钮 + 左右各 6px 容器边距，留点余量。
_ACTION_COLUMN_WIDTH = 96

#: 行高。必须 ≥ 容器上下边距(12) + 按钮最小高(26)，否则按钮会被压扁。
_ROW_HEIGHT = 44

#: 操作列里那个小按钮的最小高度
_ROW_BUTTON_HEIGHT = 28

#: 导入时可以选的学期标签（**大一上 → 大四下**，本科 8 个学期）。
#:
#: ★ **一处真源**：直接取 :data:`core.archival.SEMESTER_ORDER` 的前
#:   :data:`core.archival.TOTAL_TERMS` 个，不在这里另抄一份。
#:   档案按什么顺序排（``core.archival.semester_rank``）就是按那张表算的 ——
#:   抄第二份迟早会漂移，而漂移的表现是"时间轴上大二下排在大一上前头"。
#:
#: 大五（五年制）不放进下拉框：本项目的剩余学期数是按
#: ``TOTAL_TERMS = 8`` 算的，列出来只会让人以为自己读的是五年制。
SEMESTER_TERMS: tuple[str, ...] = SEMESTER_ORDER[:TOTAL_TERMS]

#: 兜底学期标签。**正常情况下用不到** —— 下拉框永远有选中项。
#: 它只服务于"绕过界面直接调 :func:`core.archival.build_archive`"的路径
#: （测试、命令行），那里确实没有学期可问。
DEFAULT_TERM = "未标注"

#: 学期标签为空时的告警（``build_archive`` 自己也会记一条，这里是界面上的那一句）
TERM_MISSING_HINT = "没有学期标签，纵向档案会把这次导入归进同一个无名学期"

#: 纵向归档在加密库里的文件名（成长档案页读它）
ARCHIVE_FILE = "archive.json.enc"

#: 红标单元格的文字颜色（红标原因那一列现在真的是红的）
_DANGER_COLOR = "#DC2626"
_WARNING_COLOR = "#B45309"


def _read_raw_frame(path: str) -> pd.DataFrame:
    """把原始表读成 DataFrame（保留原样，不做任何清洗）。

    ``with pd.ExcelFile(...)`` 是必须的：Windows 上不显式关闭句柄的话，
    Excel 还开着这个文件时会读失败（踩过）。
    """
    if path.lower().endswith(".csv"):
        return pd.read_csv(path, header=None, dtype=object)
    with pd.ExcelFile(path) as book:
        return pd.read_excel(book, header=None, dtype=object)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.6f}"


@dataclass(frozen=True)
class TableRow:
    """表格一行的身份。**行序就是身份**，姓名只是显示用的。

    ``flag`` 是**当前口径下**的预警（由 :meth:`AnalysisPage.roster` 现填）。
    侧写页左侧名单靠它在名字旁边点一个小圆点。默认 ``None`` ——
    导入那一刻还没选口径，谁都不该被标。
    """

    index: int
    student: str
    student_id: str
    flag: Any = None

    @property
    def key(self) -> str:
        """:func:`ui.intentions.student_key` 算出来的稳定标识。"""
        return student_key(self.student_id, self.student)

    @property
    def has_id(self) -> bool:
        return bool(self.student_id.strip())

    @property
    def label(self) -> str:
        """给列表/提示用的短标签：``张三（2023001）``。"""
        return f"{self.student}（{self.student_id}）" if self.has_id else self.student


class AnalysisPage(QWidget):
    """智育分析页。

    Signals:
        student_selected(dict): 用户双击某行或点该行「侧写」按钮。
            dict 里带着侧写辅导页需要的**全部**原料，它不需要重算任何东西。
        roster_changed(list): 导入完成后，把全班名单推给侧写辅导页。
            列表元素是 :class:`TableRow`。
    """

    student_selected = Signal(dict)
    roster_changed = Signal(list)
    #: 导入成功并**把这一班存进加密库**之后发出（奖学金页靠它刷新班级下拉）
    class_saved = Signal(str)

    def __init__(self, store: CryptoStore | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore()
        self._username = ""
        #: 页面状态。（这一页是有状态的：表格、结果、红标都挂在这里）
        self._parsed: Any = None
        self._rows: list[TableRow] = []
        #: ``{口径: [CalculationResult, ...]}`` —— **按行序**，下标即行号
        self._results: dict[Any, list[Any]] = {}
        #: ``{口径: [StudentFlag, ...]}`` —— 同样按行序
        self._flags: dict[Any, list[Any]] = {}
        self._archive: Any = None
        self._anchor_report: Any = None
        self._source: str = ""
        self._scope: CalculationScope = DEFAULT_FILTER
        self._build()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        head = QHBoxLayout()
        self._import_btn = QPushButton("📂  导入成绩表")
        self._import_btn.setObjectName("Primary")
        self._import_btn.setMinimumHeight(42)
        self._import_btn.setMinimumWidth(170)
        self._import_btn.clicked.connect(self._do_import)
        head.addWidget(self._import_btn)

        # --- 班级号（charter 4.1：导入必须人工输入班级号）---
        head.addSpacing(6)
        class_caption = QLabel("班级号")
        class_caption.setObjectName("H3")
        head.addWidget(class_caption)
        self._class_edit = QLineEdit()
        self._class_edit.setPlaceholderText("如 CS2023-1")
        self._class_edit.setMinimumHeight(38)
        self._class_edit.setMinimumWidth(150)
        self._class_edit.setMaxLength(32)
        self._class_edit.setToolTip(
            "字母、数字、短横线。奖学金测算按班级号分班存成绩。\n"
            "导入时没填会弹窗问你要。"
        )
        head.addWidget(self._class_edit)

        # --- 学期（纵向档案的标签；成长档案页的时间轴 / 趋势 Δ 全靠它）---
        head.addSpacing(10)
        term_caption = QLabel("学期")
        term_caption.setObjectName("H3")
        head.addWidget(term_caption)
        self._term_box = QComboBox()
        self._term_box.addItems(SEMESTER_TERMS)
        self._term_box.setMinimumHeight(38)
        self._term_box.setMinimumWidth(92)
        self._term_box.setToolTip(
            "这一份成绩属于哪个学期。\n"
            "成长档案的学期时间轴和趋势 Δ 按它排 —— "
            "同一学期导入两次会各留一份，绝不互相覆盖。"
        )
        head.addWidget(self._term_box)
        head.addStretch(1)

        # --- 口径筛选：三个 Toggle，互斥，默认「必修+限选」---
        head.addWidget(self._filter_caption())
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons: dict[CalculationScope, QPushButton] = {}
        for scope in FILTER_ORDER:
            button = QPushButton(FILTER_LABELS[scope])
            button.setObjectName("FilterToggle")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(34)
            button.setChecked(scope is DEFAULT_FILTER)
            # ★ 每一档到底算了哪几类，写在按钮自己的气泡里。
            #   按钮上的字是简写（"全部"），气泡里是展开的三类别。
            button.setToolTip(
                f"计入：{scope_breakdown(scope)}\n"
                "三档口径是 charter 一节定死的，不新增、不改定义。"
            )
            button.clicked.connect(
                lambda _=False, s=scope: self._set_scope(s)
            )
            self._filter_group.addButton(button)
            self._filter_buttons[scope] = button
            head.addWidget(button)
        root.addLayout(head)

        self._status = QLabel("还没有导入数据。")
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._table = QTableWidget(0, len(COLUMN_TITLES))
        self._table.setHorizontalHeaderLabels(list(COLUMN_TITLES))
        self._table.setAlternatingRowColors(True)          # Spec：斑马纹
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        # ★ 行高必须容得下操作列里的按钮：容器上下边距 12 + 按钮最小高 26 = 38。
        #   默认行高约 30px，会把按钮压成 12px 高、只剩半个字（踩过）。
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._table.setShowGrid(False)
        self._table.doubleClicked.connect(self._on_double_click)

        header = self._table.horizontalHeader()
        for col in (NAME_COLUMN, ID_COLUMN):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(FIRST_SCOPE_COLUMN, ACTION_COLUMN):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        # ★ 操作列必须写死宽度：ResizeToContents **不会**把 setCellWidget 放进去的
        #   按钮算进来，列会被压成表头「操作」两个字的宽度，按钮只剩半个字。
        header.setSectionResizeMode(ACTION_COLUMN, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(ACTION_COLUMN, _ACTION_COLUMN_WIDTH)
        root.addWidget(self._table, 1)

        self._detail = QLabel(
            "双击任意一行（或点该行的「侧写」）即可跳到「🎯 侧写辅导」。\n"
            + CALC_SCOPE_NOTE
        )
        self._detail.setObjectName("Muted")
        self._detail.setWordWrap(True)
        root.addWidget(self._detail)

        self._apply_scope()

    @staticmethod
    def _filter_caption() -> QLabel:
        label = QLabel("计算口径")
        label.setObjectName("H3")
        return label

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        """切账户时调用。班级是**按用户**存的，所以换人要换班级号输入框。

        ★ 内存里那份归档也要一起丢掉 —— 它是上一个用户的，
          留着会让下一个人导入时把两个人的学期并进同一份档案。
        """
        self._username = username or ""
        self._archive = None

    @property
    def store(self) -> CryptoStore:
        return self._store

    @property
    def term(self) -> str:
        """当前选中的学期标签（导入、建档、导出都用它）。

        下拉框永远有选中项，所以这里**不会**返回空串；
        ``DEFAULT_TERM`` 只是给不走界面的调用方兜底的。
        """
        text = self._term_box.currentText().strip() if self._term_box else ""
        return text or DEFAULT_TERM

    def set_term(self, term: str) -> None:
        """程序化选学期（测试与"接着上学期导"用）。认不出的标签就退回第 1 个。"""
        index = self._term_box.findText((term or "").strip())
        self._term_box.setCurrentIndex(index if index >= 0 else 0)

    @property
    def class_name(self) -> str:
        """当前输入框里的班级号（自检/奖学金页用）。"""
        return self._class_edit.text().strip()

    def set_class_name(self, name: str) -> None:
        self._class_edit.setText(name or "")

    # ---------------------------------------------------------------- 口径筛选
    def _set_scope(self, scope: CalculationScope) -> None:
        """切换口径筛选：口径列显隐 + 红标跟着重算。"""
        if scope is self._scope:
            return
        self._scope = scope
        self._apply_scope()
        if self._rows:
            self._refresh_flag_column()
            self._show_detail(self._current_index())
            self._update_status()
            # ★ 名单也要跟着重推一次：侧写页左侧的小红点是按口径判的，
            #   分母变了它就得变（见 roster() 的注释）。
            self.roster_changed.emit(self.roster())

    def _apply_scope(self) -> None:
        """按当前口径 ``showColumn`` / ``hideColumn``。

        ★ 三个口径列**始终都在**（表头和数据一一对应，见 COLUMN_TITLES 的注释），
          筛选只改**可见性**。这样列下标永远是稳的 —— 增删列会让
          "表头 ↔ 单元格"重新有机会漂移，而那正是上一版的 bug。
        """
        for offset, (_key, _title, scope) in enumerate(SCOPE_COLUMNS):
            column = FIRST_SCOPE_COLUMN + offset
            self._table.setColumnHidden(column, scope is not self._scope)
        for scope, button in self._filter_buttons.items():
            button.setChecked(scope is self._scope)

    @property
    def scope(self) -> CalculationScope:
        return self._scope

    # ---------------------------------------------------------------- 导入
    def _require_class_name(self) -> str | None:
        """拿到班级号（charter 4.1：导入**必须**有人工输入的班级号）。

        顺序：先用输入框里的；空的就弹窗问；还不行就报错返回 ``None``，
        调用方**中止导入**（绝不拿一个空班级号去存 —— 那会让奖学金页
        出现一个点进去没内容的班）。

        Returns:
            校验通过的班级号，或 ``None``（用户取消 / 填得不合法）。
        """
        typed = self._class_edit.text().strip()
        if typed:
            try:
                value = validate_class_name(typed)
            except ValueError as exc:
                QMessageBox.warning(self, "班级号不合法", str(exc))
                self._class_edit.setFocus()
                return None
            self._class_edit.setText(value)
            return value

        value, ok = QInputDialog.getText(
            self,
            "这份成绩属于哪个班",
            "班级号（字母、数字、短横线）：",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            self._set_status("已取消：没有班级号，本次未导入。", warn=True)
            return None
        try:
            name = validate_class_name(value)
        except ValueError as exc:
            QMessageBox.warning(self, "班级号不合法", str(exc))
            return None
        self._class_edit.setText(name)
        return name

    def _build_class_record(self, class_name: str, source: str) -> Any:
        """把刚算完的结果整理成一个班级快照（给奖学金测算用）。

        ★ 存的是**算好的数**（口径①的加权均分）+ 每门课的分数，
          奖学金页以后测算时**不需要重新解析 Excel** ——
          也就不会出现"两次解析结果不一致"。
        ★ 每门课的分数要留着：特等奖的「每门 ≥90」得逐门看（charter 4.4）。
        """
        students: list[dict[str, Any]] = []
        for row in self._rows:
            result = self._result_at(CalculationScope.ALL, row.index)
            if result is None:
                continue
            courses: list[dict[str, Any]] = []
            for course in getattr(result, "course_scores", ()) or ():
                courses.append(
                    {
                        "name": getattr(course, "name", ""),
                        # ★ score 可能是 None（没选这门课）—— 原样存，
                        #   奖学金那边靠 None 区分"没考"和"考了 0 分"（红线 2）
                        "score": getattr(course, "score", None),
                        "credit": getattr(course, "credit", None),
                    }
                )
            students.append(
                {
                    "student": row.student,
                    "student_id": row.student_id,
                    "key": row.key,
                    "academic": result.weighted_average,
                    "total_credit": result.total_credit,
                    "courses": courses,
                }
            )
        return ClassRecord(
            class_name=class_name,
            source=source,
            term=self.term,
            students=students,
        )

    def _do_import(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择成绩表（.xlsx / .xls / .csv）",
            "",
            "Excel 工作簿 (*.xlsx *.xls);;CSV 文件 (*.csv);;全部文件 (*)",
        )
        if not path:
            return

        self._status.setText("正在解析…")
        self._status.setObjectName("Muted")
        self._repolish(self._status)
        QWidget.repaint(self)

        try:
            raw = _read_raw_frame(path)
        except Exception as exc:  # noqa: BLE001 - 读文件的意外都要给人看
            QMessageBox.critical(
                self,
                "读不出这个文件",
                f"{exc.__class__.__name__}: {exc}\n\n"
                "请确认它是完整的 .xlsx/.xls/.csv，且没有被别的程序占用。",
            )
            self._set_status("读取失败。", error=True)
            return

        # core 的便捷入口：解析 + 锚点校验一次做完
        try:
            parsed, anchor_report = parse_and_validate(raw)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "解析出错", f"{exc.__class__.__name__}: {exc}"
            )
            self._set_status("解析失败。", error=True)
            return

        if not parsed.courses:
            QMessageBox.critical(
                self,
                "没识别出任何课程",
                "程序找不到表头行。需要有一行包含「姓名」或「学号」，"
                "其余列是课程名（最好带上学分和类别，例如“高等数学(5学分)必修”）。\n\n"
                + "\n".join(f"· {w}" for w in parsed.warnings[:6]),
            )
            self._set_status("解析失败：没有课程列。", error=True)
            return

        # ★ charter 4.1：导入**必须**有人工确认的班级号。
        #   位置就在"选完文件之后、确认课程弹窗之前"（用户要求的次序）——
        #   这样用户是在"刚选完那份表"的上下文里回答"这是哪个班"的。
        class_name = self._require_class_name()
        if class_name is None:
            return

        # ★ 红线 3：强制人工回显确认。没确认就绝不进入计算。
        confirmed = confirm_courses(
            self,
            parsed.courses,
            extra_alerts=describe_alerts(anchor_report),
            warnings=parsed.warnings,
        )
        if not confirmed:
            self._set_status(
                "已取消：没有确认课程表，本次未计算、未显示任何数字。", warn=True
            )
            return

        self._status.setText("正在计算三档口径…")
        self._repolish(self._status)
        QWidget.repaint(self)
        try:
            results = calculate_many(parsed)
        except Exception as exc:  # noqa: BLE001 - 计算异常必须暴露，不静默吞
            QMessageBox.critical(
                self, "计算过程出错", f"{exc.__class__.__name__}: {exc}"
            )
            self._set_status("计算失败。", error=True)
            return

        # 归档：侧写辅导页的"趋势 / 定型·可救"从这里来，本页算一次就够。
        #
        # ★ 必须把**已有归档**一起带上做 append。不带的话每导一次就把上一个
        #   学期整份覆盖掉 —— 成长档案永远只有一个学期、趋势 Δ 永远显示
        #   "单次导入，暂无趋势"。这和"学期标签是未标注"是同一个病的两条腿：
        #   只有把两份成绩都留住，纵向对比才成立。
        try:
            self._archive = build_archive(
                parsed,
                self.term,
                archive=self._load_archive(),
                source=_base_name(path),
            )
        except Exception:  # noqa: BLE001 - 归档失败不该拦住看数字
            self._archive = None
        else:
            # ★ 顺手把归档写进加密库 —— 否则"成长档案"页一关程序就空了。
            #   存不上不影响本页显示（成绩已经算完了）。
            self._save_archive()

        self._parsed = parsed
        self._results = results
        self._anchor_report = anchor_report
        self._source = _base_name(path)
        # ★ 名单按**行序**建立，姓名只是显示用的一个字段
        self._rows = [
            TableRow(
                index=i,
                student=row.student,
                student_id=getattr(row, "student_id", "") or "",
            )
            for i, row in enumerate(parsed.valid_rows)
        ]
        self._refresh_flags()
        self._fill_table()
        self._update_status()
        # ★ 把这一班存进加密库（奖学金测算按班级取成绩）。
        #   存不上**不拦界面** —— 成绩已经算完并显示了，存班级只是顺带的事；
        #   真失败会在状态行里明说，不静默。
        self._save_class(class_name, self._source)
        # 把名单推给侧写辅导页（它靠这个建左侧列表）
        self.roster_changed.emit(self.roster())

    def _load_archive(self) -> Archive | None:
        """从加密库读回纵向归档，作为本次导入的 append 底座。

        读不出来就返回 ``None`` —— ``build_archive`` 会把 ``None``
        当成"新建一份"，也就是"这是这个用户的第一次导入"，不是错误。

        ★ 优先用内存里那份（同一会话里连续导入两个学期时省一次解密），
          但内存那份可能是**上一个用户**的，所以 ``set_user`` 会把它清掉。
        """
        if self._archive is not None:
            return self._archive
        if not self._username:
            return None
        try:
            payload = load_user_json(self._store, self._username, ARCHIVE_FILE)
        except (CryptoError, ValueError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return Archive.from_dict(payload)
        except Exception:  # noqa: BLE001 - 坏档案不该拦住看数字
            return None

    def _save_archive(self) -> None:
        """把纵向归档写进加密库（成长档案页靠它跨重启存活）。

        ★ 归档是**便利数据**：存不上不弹窗、不中断，只在状态行里说一声。
          成绩本身已经在屏幕上了，用户不该因为归档写不进去而看不到数字。
        """
        if self._archive is None or not self._username:
            return
        try:
            payload = self._archive.as_dict()
        except Exception:  # noqa: BLE001
            return
        try:
            save_user_json(self._store, self._username, ARCHIVE_FILE, payload)
        except (CryptoError, ValueError, OSError) as exc:
            self._note_class_failure(f"归档没能存进加密库（{type(exc).__name__}）")

    def _save_class(self, class_name: str, source: str) -> None:
        """把本班成绩快照写进加密库，并广播 ``class_saved``。"""
        if not self._username:
            self._note_class_failure("还没登录，班级数据没地方存")
            return
        try:
            record = self._build_class_record(class_name, source)
            save_class(self._store, self._username, record)
        except (CryptoError, ValueError, OSError) as exc:
            self._note_class_failure(f"{type(exc).__name__}: {exc}")
            return
        self.class_saved.emit(class_name)

    def _note_class_failure(self, reason: str) -> None:
        """班级存不上时，把原因追加到状态行（**不弹窗打断**）。"""
        self._status.setText(
            self._status.text()
            + f"\n⚠ 班级成绩没能存进加密库（{reason}）—— "
            "奖学金测算那一页会看不到这个班。"
        )

    def _refresh_flags(self) -> None:
        """为核心口径算红标。**按行序**返回，同名也不会串。"""
        anchor_exceeded = getattr(self._anchor_report, "exceeded", ()) or ()
        invalid_rows = getattr(self._parsed, "invalid_rows", ()) if self._parsed else ()
        self._flags = {}
        for scope, rows in self._results.items():
            self._flags[scope] = build_flags_by_index(
                rows, invalid_rows=invalid_rows, anchor_exceeded=anchor_exceeded
            )

    # ---------------------------------------------------------------- 填表
    def _fill_table(self) -> None:
        self._table.setRowCount(len(self._rows))
        for row in self._rows:
            i = row.index

            name_cell = QTableWidgetItem(row.student)
            name_cell.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self._table.setItem(i, NAME_COLUMN, name_cell)

            id_cell = QTableWidgetItem(row.student_id if row.has_id else "—")
            id_cell.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            if not row.has_id:
                id_cell.setToolTip(
                    "表里没有这一行的学号。同名学生会共用同一份意愿，"
                    "建议在成绩表里补上学号列再导入。"
                )
            self._table.setItem(i, ID_COLUMN, id_cell)

            # 三档口径（列下标紧跟学号，由 COLUMN_TITLES 保证对齐）
            for offset, (_key, _title, scope) in enumerate(SCOPE_COLUMNS):
                result = self._result_at(scope, i)
                cell = QTableWidgetItem(_fmt(result.weighted_average) if result else "—")
                cell.setTextAlignment(
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                )
                self._table.setItem(i, FIRST_SCOPE_COLUMN + offset, cell)

            self._set_flag_cell(i)

            button = QPushButton("侧写")
            button.setObjectName("RowAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(_ROW_BUTTON_HEIGHT)
            button.clicked.connect(lambda _=False, idx=i: self._emit_student(idx))
            holder = QWidget()
            holder_box = QHBoxLayout(holder)
            holder_box.setContentsMargins(6, 6, 6, 6)
            holder_box.addWidget(button)
            self._table.setCellWidget(i, ACTION_COLUMN, holder)

        if self._rows:
            self._table.selectRow(0)
            self._show_detail(0)

    def _set_flag_cell(self, index: int) -> None:
        """写红标原因那一格。**颜色跟着级别走** —— 叫"红标"就得真是红的。"""
        flag = self._flag_at(self._scope, index)
        text = flag.summary if flag is not None else ""
        cell = QTableWidgetItem(text)
        cell.setToolTip(text)
        if flag is not None:
            if flag.level == LEVEL_DANGER:
                cell.setForeground(QColor(_DANGER_COLOR))
            elif flag.level == LEVEL_WARNING:
                cell.setForeground(QColor(_WARNING_COLOR))
        self._table.setItem(index, FLAG_COLUMN, cell)

    def _refresh_flag_column(self) -> None:
        for row in self._rows:
            self._set_flag_cell(row.index)

    # ---------------------------------------------------------------- 取值
    def _result_at(self, scope: CalculationScope, index: int) -> Any:
        rows = self._results.get(scope) or []
        return rows[index] if 0 <= index < len(rows) else None

    def _flag_at(self, scope: CalculationScope, index: int) -> Any:
        flags = self._flags.get(scope) or []
        return flags[index] if 0 <= index < len(flags) else None

    def _current_index(self) -> int:
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            return row
        return 0

    def row_of(self, key: str) -> int | None:
        """按稳定标识找行号。**同名不会串**（键含学号）。"""
        for row in self._rows:
            if row.key == key:
                return row.index
        return None

    def table_row(self, index: int) -> TableRow | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    # ---------------------------------------------------------------- 详情
    def _show_detail(self, index: int) -> None:
        """表下那一行口径明细 —— Σ学分 / Σ成绩×学分 是加权算术的证据链。"""
        row = self.table_row(index)
        if self._parsed is None or row is None:
            return
        parts: list[str] = []
        for _key, title, scope in SCOPE_COLUMNS:
            result = self._result_at(scope, index)
            if result is None:
                continue
            parts.append(
                f"{title}＝{_fmt(result.weighted_average)}"
                f"（Σ学分 {result.total_credit:g}，Σ成绩×学分 {result.weighted_sum:g}）"
            )
        flag = self._flag_at(self._scope, index)
        if flag is not None and flag.reasons:
            parts.append(f"预警（{flag.label}）：{'；'.join(flag.reasons)}")
        head = f"【{row.label}】"
        if not row.has_id:
            head += "（无学号）"
        self._detail.setText(head + "　|　".join(parts))
        self._repolish(self._detail)

    # ---------------------------------------------------------------- 选中
    def _on_double_click(self, index) -> None:
        self._emit_student(index.row())

    def _emit_student(self, index: int) -> None:
        payload = self.payload_at(index)
        if payload is not None:
            self.student_selected.emit(payload)

    def payload_at(self, index: int) -> dict[str, Any] | None:
        """把侧写辅导页需要的东西**一次性打包**，那边一个数都不用再算。

        里面放的是 core 的原始对象（``CalculationResult`` / ``StudentArchive``），
        不是重新拼的字典 —— 免得两份数字打架。

        ★ 打包的身份是 **``key``（学号优先）**，不是姓名。
        """
        row = self.table_row(index)
        if self._parsed is None or row is None:
            return None
        results = {
            scope: self._result_at(scope, index) for scope in self._results
        }
        if all(value is None for value in results.values()):
            return None
        student_archive = None
        if self._archive is not None:
            # ★ 档案字典的键是 core.archival.archive_key()（学号哈希），
            #   不是学号原文 —— 必须用它自己那把钥匙去查。
            student_archive = self._archive.students.get(
                archive_key(row.student_id, row.student)
            )
        return {
            "student": row.student,
            "student_id": row.student_id,
            "key": row.key,
            "index": row.index,
            "results": results,
            "flag": self._flag_at(self._scope, index),
            "archive": student_archive,
            "source": self._source,
        }

    def current_payload(self) -> dict[str, Any] | None:
        """当前选中的那行（侧栏切过来时用）。"""
        if not self._rows:
            return None
        return self.payload_at(self._current_index())

    def roster(self) -> list[TableRow]:
        """全班名单（侧写辅导页的左侧列表用它）。

        ★ ``flag`` 字段是**按当前口径现填**的 —— ``self._rows`` 里存的那份
          flag 永远是 ``None``（导入时还没选口径）。不这么做的话，用户在
          智育分析页把筛选从「必修+限选」切到「全部」，侧写页名单上的小红点
          会**停在旧口径的判定上** —— 分母都变了，红点却没跟着变，
          正是这个项目最忌讳的那种"界面在说谎"。
        """
        return [
            dataclasses.replace(row, flag=self._flag_at(self._scope, row.index))
            for row in self._rows
        ]

    def export_records(self) -> list[dict[str, Any]]:
        """给设置页的导出功能提供记录。

        列名按 :data:`core.exporter.ANONYMIZED_COLUMNS` 来 —— 脱敏导出会按
        白名单裁列，**姓名不在白名单里**，所以明文版能带姓名、脱敏版带不了。
        本方法只负责把已有数字摆成表，不做任何新算术。

        口径固定用「仅必修+限选」（Spec 默认口径），**不跟着筛选按钮变** ——
        导出的是一份可归档的固定口径，不是屏幕上的临时视图。
        """
        if not self._rows:
            return []
        scope = CalculationScope.REQUIRED_LIMITED
        archive_students: dict[str, Any] = {}
        if self._archive is not None:
            archive_students = dict(self._archive.students)

        records: list[dict[str, Any]] = []
        for index, row in enumerate(self._rows, 1):
            result = self._result_at(scope, index - 1)
            if result is None:
                continue
            student_archive = archive_students.get(
                archive_key(row.student_id, row.student)
            )
            records.append(
                {
                    "S编号": f"S{index:03d}",
                    "姓名": row.student,
                    "学期": self.term,
                    "口径": FILTER_LABELS[scope],
                    "加权均分": result.weighted_average,
                    "总学分": result.total_credit,
                    "挂科数": getattr(student_archive, "failed_total", 0),
                    "缺考数": getattr(student_archive, "absent_total", 0),
                    "意愿标签": "",
                }
            )
        return records

    # ---------------------------------------------------------------- 杂项
    def _update_status(self) -> None:
        danger = sum(
            1 for f in (self._flags.get(self._scope) or []) if f.level == LEVEL_DANGER
        )
        warning = sum(
            1 for f in (self._flags.get(self._scope) or []) if f.level == LEVEL_WARNING
        )
        missing_ids = sum(1 for row in self._rows if not row.has_id)
        if self._parsed is None:
            return
        parsed = self._parsed
        anchor_report = self._anchor_report
        if anchor_report is None or not anchor_report.anchors:
            anchor_text = "表内无「所得学分」锚点列"
        elif describe_alerts(anchor_report):
            anchor_text = f"锚点超标 {len(anchor_report.exceeded)} 处"
        else:
            anchor_text = "锚点校验通过"
        text = (
            f"已导入 {self._source}　·　学期「{self.term}」　·　"
            f"{len(parsed.courses)} 门课 / "
            f"{parsed.total_credit:g} 学分　·　{len(self._rows)} 名学生　·　"
            f"当前口径「{FILTER_LABELS[self._scope]}」"
            f"（{scope_breakdown(self._scope)}）　·　"
            f"红标 {danger} 人 / 黄标 {warning} 人　·　{anchor_text}"
        )
        if missing_ids:
            text += (
                f"\n⚠ 有 {missing_ids} 名学生没有学号，只能靠姓名区分 —— "
                "同名学生会用同一份意愿。建议在成绩表里补上学号列再导入。"
            )
        self._set_status(text, warn=bool(missing_ids))

    def _set_status(self, text: str, *, error: bool = False, warn: bool = False) -> None:
        self._status.setText(text)
        self._status.setObjectName("Danger" if error else ("Warn" if warn else "Muted"))
        self._repolish(self._status)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        """换了 objectName 之后必须重新应用样式表，否则颜色不跟着变。"""
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _base_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]
