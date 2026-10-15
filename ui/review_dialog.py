"""人工回显确认弹窗 —— **红线 3，不可跳过**。

Spec 第 230 行把「学分提取必须人工回显确认」列为红线之一；第 176 行更直接：

    人工回显弹窗是强制环节，不能 skip。

所以这个对话框**故意没有**「跳过」「稍后」「直接计算」这类按钮。想要数字，
只有一条路：确认课程表。窗口的关闭按钮等同于「返回修正」，返回 ``False``。

上一版是 tkinter 实现的，这一版换成 QDialog，**判定逻辑一个字没改**：
有问题的行标红、按钮只有两个、关闭即拒绝。

v1.9.1（问题 7）只动**布局**：主体从"表在上、提示在下"改成 ``QSplitter``
左右两栏（左 = 课程表预览，右 = 提示 / 锚点校验），中间那条抓手可拖动，
默认 70% / 30%；右栏内容多时自己滚，**绝不把表格挤扁**。
判定逻辑、文案、按钮语义**一个字没改**。

★ 记住分隔位置用的是 ``QSettings(..., IniFormat)`` + **显式文件路径**
（``%LOCALAPPDATA%\\SGMS\\ui_state.ini``）—— Windows 上 ``QSettings()`` 默认
``NativeFormat`` 会写 **HKCU 注册表**，而本项目的铁律是"不写注册表"。
显式指定 ini 路径之后，落盘的就只是一个普通文本文件，注册表一个字节都不动。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.parser import Category

__all__ = ["MIN_COLUMN_WIDTH", "ReviewDialog", "SPLITTER_KEY", "confirm_courses", "state_path"]

#: 有问题的行：浅红底 + 深红字
_WARN_BG = QColor("#FDE8E8")
_WARN_FG = QColor("#9B1C1C")

_HEADERS = ("#", "课程名", "类别", "学分", "学分来源", "类别来源")

#: ★ 列宽下限（像素）。30 门课的宽表也不许被压到看不清 —— 交互式表头 +
#: ``minimumSectionSize`` 双保险，用户自己拖也拖不到这个数以下。
MIN_COLUMN_WIDTH = 72

#: 每列的初始宽度，逐项都不小于 :data:`MIN_COLUMN_WIDTH`
_COLUMN_WIDTHS = (72, 260, 96, 84, 150, 150)

#: 初始窗口尺寸下限（任务书：``resize(1000, 700)``）
_DEFAULT_SIZE = (1000, 700)

#: 默认比例 预览 70% / 提示 30%（``setSizes`` 的数是相对权重，Qt 会按可用宽度摊开）
_DEFAULT_SIZES = (700, 300)

#: 两侧各自的宽度下限：拖到底也不许把任何一栏收成 0
_MIN_PANE_WIDTHS = (280, 200)

#: QSettings 的键名（任务书指定，别改）
SPLITTER_KEY = "grade_import/splitter_state"

#: 状态文件名字（放在数据根目录里，见模块文档：绝不进注册表）
_STATE_FILE_NAME = "ui_state.ini"

#: 覆盖状态文件路径的环境变量（测试 / 绿色版用；正常用户不设）
STATE_ENV = "TL_UI_STATE"

#: 抓手样式。选择器带 objectName，**只作用于本对话框的分隔条**，不污染全局 QSS。
_HANDLE_QSS = """
QSplitter#ReviewSplit::handle:horizontal {
    background: #CBD5E1;
    border-radius: 3px;
    margin: 2px 1px;
}
QSplitter#ReviewSplit::handle:horizontal:hover,
QSplitter#ReviewSplit::handle:horizontal:pressed {
    background: #64748B;
}
"""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def state_path() -> Path:
    """分隔位置状态文件的路径：``<数据根>/ui_state.ini``。

    数据根走 :func:`core.crypto_store.default_root`（``%LOCALAPPDATA%\\SGMS``）
    —— **唯一真源**，不在这里另抄一份。环境变量 :data:`STATE_ENV` 可以覆盖
    它（测试用；正常用户不会设）。只返回路径，**不创建**。
    """
    override = os.environ.get(STATE_ENV)
    if override:
        return Path(override)
    try:
        from core.crypto_store import default_root

        root: Path = Path(default_root())
    except Exception:  # noqa: BLE001 - 拿不到数据根也不能让对话框开不出来
        root = Path.home() / "AppData" / "Local" / "SGMS"
    return root / _STATE_FILE_NAME


def _settings() -> QSettings:
    """显式 ``IniFormat`` + 显式文件路径的 QSettings。

    ★ 为什么不写 ``QSettings()``：Windows 上默认格式是 ``NativeFormat``，
    那一份会落到 ``HKEY_CURRENT_USER\\Software\\...`` 注册表里 ——
    本项目铁律"不写注册表"，所以这里两个参数都给死。
    """
    return QSettings(str(state_path()), QSettings.Format.IniFormat)


def _initial_size(parent: QWidget | None) -> QSize:
    """初始尺寸：**至少** :data:`_DEFAULT_SIZE`，屏幕够大就按屏幕比例放大。

    父窗口已定尺寸时也一样 —— 这个弹窗装的是整张课程表 + 提示区，
    比父窗口小反而看不清。
    """
    width, height = _DEFAULT_SIZE
    screen = None
    try:
        if parent is not None:
            screen = parent.screen()
    except Exception:  # noqa: BLE001 - 拿不到屏幕就退回默认尺寸
        screen = None
    if screen is None:
        try:
            screen = QGuiApplication.primaryScreen()
        except Exception:  # noqa: BLE001
            screen = None
    if screen is not None:
        try:
            avail = screen.availableGeometry()
            width = max(width, int(avail.width() * 0.7))
            height = max(height, int(avail.height() * 0.7))
            # 屏幕比下限还小就**不缩**（任务书要求"至少 1000x700"）
            if avail.width() >= _DEFAULT_SIZE[0]:
                width = min(width, avail.width())
            if avail.height() >= _DEFAULT_SIZE[1]:
                height = min(height, avail.height())
        except Exception:  # noqa: BLE001
            pass
    return QSize(width, height)


class ReviewDialog(QDialog):
    """课程表回显确认。"""

    def __init__(
        self,
        courses: Sequence[Any],
        *,
        title: str = "请核对提取结果",
        extra_alerts: Iterable[str] = (),
        score_alerts: Iterable[str] = (),
        warnings: Iterable[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(_initial_size(parent))

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        head = QLabel(
            "程序按三级规则从表头和独立权重行里抠出了下面这些课程。\n"
            "这一步是强制的：确认之后才会开始任何加权计算。"
        )
        head.setObjectName("H3")
        root.addWidget(head)

        # ★ 主体：左 = 预览表，右 = 提示 / 锚点校验。中间的抓手可以拖。
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setObjectName("ReviewSplit")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        splitter.setStyleSheet(_HANDLE_QSS)

        table = self._build_table(courses)
        self.table = table
        splitter.addWidget(table)

        hints = self._build_hint_area(courses, extra_alerts, score_alerts, warnings)
        self.hint_scroll = hints
        splitter.addWidget(hints)

        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes(list(_DEFAULT_SIZES))
        self.splitter = splitter
        root.addWidget(splitter, 1)

        # 拖动即时生效（QSplitter 自己保证），顺手把位置记下来（尽力而为）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.remember_split)
        splitter.splitterMoved.connect(lambda *_: self._save_timer.start())

        self._restore_split()

        footer = QHBoxLayout()
        footer.addWidget(
            QLabel("确认无误才会计算；关掉本窗口等于「返回修正」。")
        )
        footer.addStretch(1)

        back = QPushButton("返回修正")
        back.setObjectName("Ghost")
        back.clicked.connect(self.reject)
        footer.addWidget(back)

        ok = QPushButton("确认无误，开始计算")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        footer.addWidget(ok)

        root.addLayout(footer)

        self.buttons = (back, ok)

    # -- 预览表 ---------------------------------------------------------

    def _build_table(self, courses: Sequence[Any]) -> QTableWidget:
        """课程表：表头常驻、行号常驻、两个方向都能滚、列宽有下限。"""
        table = QTableWidget(len(courses), len(_HEADERS), self)
        table.setHorizontalHeaderLabels(_HEADERS)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        # 30 门课以上时两个方向都要能滚（按像素滚，手感更顺）
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setMinimumWidth(_MIN_PANE_WIDTHS[0])

        # ★ 行号常驻可见（表头行 + 左侧行号列）
        vheader = table.verticalHeader()
        vheader.setVisible(True)
        vheader.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vheader.setDefaultSectionSize(26)

        for row, course in enumerate(courses):
            values = (
                str(row + 1),
                _text(getattr(course, "name", "")),
                _category_text(getattr(course, "category", "")),
                _text(getattr(course, "credit", "")),
                _text(getattr(course, "credit_source", "")) or "—",
                _text(getattr(course, "category_source", "")) or "—",
            )
            flagged = self._looks_risky(course)
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if flagged:
                    item.setBackground(QBrush(_WARN_BG))
                    item.setForeground(QBrush(_WARN_FG))
                if col in (0, 3):
                    item.setTextAlignment(
                        int(Qt.AlignmentFlag.AlignCenter)
                    )
                table.setItem(row, col, item)

        # ★ 交互式表头（用户可自己调列宽）+ 最小列宽：任何路径都不许把列压没。
        header = table.horizontalHeader()
        header.setVisible(True)
        header.setSectionsMovable(False)
        header.setHighlightSections(False)
        header.setMinimumSectionSize(MIN_COLUMN_WIDTH)
        for col in range(len(_HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in enumerate(_COLUMN_WIDTHS):
            table.setColumnWidth(col, max(MIN_COLUMN_WIDTH, width))
        return table

    # -- 提示 / 锚点校验区 ------------------------------------------------

    def _build_hint_area(
        self,
        courses: Sequence[Any],
        extra_alerts: Iterable[str],
        score_alerts: Iterable[str],
        warnings: Iterable[str],
    ) -> QScrollArea:
        """右栏：自己带 ``QScrollArea``，内容再多也只在这一栏里滚。"""
        pane = QWidget(self)
        pane.setObjectName("ReviewHints")
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        heading = QLabel("提示与校验")
        heading.setObjectName("H3")
        box.addWidget(heading)

        risky = [c for c in courses if self._looks_risky(c)]
        if risky:
            note = QLabel(
                f"⚠ 有 {len(risky)} 门课的学分或类别没提取准（下表标红），"
                "请重点核对。"
            )
            note.setObjectName("Warn")
            note.setWordWrap(True)
            box.addWidget(note)

        alerts = [str(line) for line in extra_alerts if str(line).strip()]
        if alerts:
            widget = QLabel("锚点校验：\n" + "\n".join(f"· {line}" for line in alerts))
            widget.setObjectName("Warn")
            widget.setWordWrap(True)
            box.addWidget(widget)

        # ★ 分数超出 0~100 的格子：必须单独说，不能混进上面那个"锚点校验"框
        #   ——那个标题写的就不是这件事。见 analysis_page.out_of_range_scores。
        score_notes = [str(line) for line in score_alerts if str(line).strip()]
        if score_notes:
            widget = QLabel(
                "⚠ 成绩超出 0~100（请核对原始表）：\n"
                + "\n".join(f"· {line}" for line in score_notes)
            )
            widget.setObjectName("Warn")
            widget.setWordWrap(True)
            box.addWidget(widget)

        notes = [str(line) for line in warnings if str(line).strip()]
        if notes:
            widget = QLabel("解析提示：\n" + "\n".join(f"· {line}" for line in notes[:6]))
            widget.setObjectName("Notice")
            widget.setWordWrap(True)
            box.addWidget(widget)

        if not (risky or alerts or score_notes or notes):
            quiet = QLabel("这一份表没有需要额外注意的提示。")
            quiet.setObjectName("Notice")
            quiet.setWordWrap(True)
            box.addWidget(quiet)

        box.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setObjectName("ReviewHintScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumWidth(_MIN_PANE_WIDTHS[1])
        scroll.setWidget(pane)
        return scroll

    # -- 记住上次的分隔位置（尽力而为） -----------------------------------

    def _restore_split(self) -> None:
        """读回上次的分隔位置。**任何失败都静默忽略**，退回默认 70/30。"""
        try:
            blob = _settings().value(SPLITTER_KEY)
        except Exception:  # noqa: BLE001 - 读不到就当没记过
            return
        if not blob:
            return
        try:
            if not isinstance(blob, QByteArray):
                blob = QByteArray(blob)
            if not self.splitter.restoreState(blob):
                return
            sizes = self.splitter.sizes()
            total = sum(sizes)
            # 坏值（0 宽 / 明显畸形比例）一律退回默认
            if total <= 0 or min(sizes) <= 0 or not 0.2 <= sizes[0] / total <= 0.95:
                raise ValueError("坏比例")
        except Exception:  # noqa: BLE001 - 损坏的 ini 不该让对话框开不出来
            self.splitter.setSizes(list(_DEFAULT_SIZES))

    def remember_split(self) -> None:
        """把当前分隔位置写进 ini。写不进去**静默忽略**。"""
        try:
            settings = _settings()
            settings.setValue(SPLITTER_KEY, self.splitter.saveState())
            settings.sync()
        except Exception:  # noqa: BLE001 - 记不住位置不影响功能
            pass

    def done(self, result: int) -> None:
        """关窗前把最后的位置记下来（返回值语义一个字没改）。"""
        try:
            self._save_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        self.remember_split()
        super().done(result)

    @staticmethod
    def _looks_risky(course: Any) -> bool:
        """这门课是不是"提取得可疑"。

        判据与上一版一致，**不新增规则**：

        * 解析器自己说 ``needs_review``（= 有 warnings）
        * 学分 <= 0（没抠出来）
        * 类别是 :data:`core.parser.Category.UNKNOWN`（没抠出来）
        """
        if bool(getattr(course, "needs_review", False)):
            return True
        credit = getattr(course, "credit", None)
        if credit is None or float(credit) <= 0:
            return True
        return getattr(course, "category", None) is Category.UNKNOWN


def _category_text(category: Any) -> str:
    """``Category`` 枚举或字符串都能转成给人看的字。

    ``Category.UNKNOWN`` 的 value 是 ``"unknown"``（英文），直接显示不好看，
    这里翻成「未知」；它同时也是下面标红的依据。
    """
    if category is None:
        return ""
    if category is Category.UNKNOWN:
        return "未知"
    return str(getattr(category, "value", category))


def confirm_courses(
    parent: QWidget | None,
    courses: Sequence[Any],
    *,
    title: str = "请核对提取结果",
    extra_alerts: Iterable[str] = (),
    score_alerts: Iterable[str] = (),
    warnings: Iterable[str] = (),
) -> bool:
    """弹窗让人核对。**返回 True 才允许进入计算。**

    这是红线 3 的唯一入口 —— 调用方拿到 ``False`` 就必须停下来，
    不显示任何数字。

    Args:
        score_alerts: 原始分数超出 0~100 的格子（见
            :func:`ui.pages.analysis_page.out_of_range_scores`）。
            单独一栏显示，不混进锚点校验。
    """
    dialog = ReviewDialog(
        courses,
        title=title,
        extra_alerts=extra_alerts,
        score_alerts=score_alerts,
        warnings=warnings,
        parent=parent,
    )
    return dialog.exec() == QDialog.DialogCode.Accepted
