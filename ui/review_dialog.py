"""人工回显确认弹窗 —— **红线 3，不可跳过**。

Spec 第 230 行把「学分提取必须人工回显确认」列为红线之一；第 176 行更直接：

    人工回显弹窗是强制环节，不能 skip。

所以这个对话框**故意没有**「跳过」「稍后」「直接计算」这类按钮。想要数字，
只有一条路：确认课程表。窗口的关闭按钮等同于「返回修正」，返回 ``False``。

上一版是 tkinter 实现的，这一版换成 QDialog，**判定逻辑一个字没改**：
有问题的行标红、按钮只有两个、关闭即拒绝。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.parser import Category

__all__ = ["confirm_courses"]

#: 有问题的行：浅红底 + 深红字
_WARN_BG = QColor("#FDE8E8")
_WARN_FG = QColor("#9B1C1C")

_HEADERS = ("#", "课程名", "类别", "学分", "学分来源", "类别来源")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class ReviewDialog(QDialog):
    """课程表回显确认。"""

    def __init__(
        self,
        courses: Sequence[Any],
        *,
        title: str = "请核对提取结果",
        extra_alerts: Iterable[str] = (),
        warnings: Iterable[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(860, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        head = QLabel(
            "程序按三级规则从表头和独立权重行里抠出了下面这些课程。\n"
            "这一步是强制的：确认之后才会开始任何加权计算。"
        )
        head.setObjectName("H3")
        root.addWidget(head)

        risky = [c for c in courses if self._looks_risky(c)]
        if risky:
            note = QLabel(
                f"⚠ 有 {len(risky)} 门课的学分或类别没提取准（下表标红），"
                "请重点核对。"
            )
            note.setObjectName("Warn")
            note.setWordWrap(True)
            root.addWidget(note)

        table = QTableWidget(len(courses), len(_HEADERS), self)
        table.setHorizontalHeaderLabels(_HEADERS)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)

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

        header = table.horizontalHeader()
        for col in range(len(_HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        root.addWidget(table, 1)

        alerts = [str(line) for line in extra_alerts if str(line).strip()]
        if alerts:
            box = QLabel("锚点校验：\n" + "\n".join(f"· {line}" for line in alerts))
            box.setObjectName("Warn")
            box.setWordWrap(True)
            root.addWidget(box)

        notes = [str(line) for line in warnings if str(line).strip()]
        if notes:
            box = QLabel("解析提示：\n" + "\n".join(f"· {line}" for line in notes[:6]))
            box.setObjectName("Notice")
            box.setWordWrap(True)
            root.addWidget(box)

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
    warnings: Iterable[str] = (),
) -> bool:
    """弹窗让人核对。**返回 True 才允许进入计算。**

    这是红线 3 的唯一入口 —— 调用方拿到 ``False`` 就必须停下来，
    不显示任何数字。
    """
    dialog = ReviewDialog(
        courses,
        title=title,
        extra_alerts=extra_alerts,
        warnings=warnings,
        parent=parent,
    )
    return dialog.exec() == QDialog.DialogCode.Accepted
