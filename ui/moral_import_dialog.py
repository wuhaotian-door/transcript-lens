"""批量导入德育分 —— 选完文件之后的那一步：**先看 5 行，再决定导不导**。

为什么要有这一步
----------------

批量导入是"一次动一个班几十号人的分数"，写错了要么覆盖别人录好的分，
要么给不存在的学号写一堆永远显示不出来的孤儿数据。所以顺序是：

1. 读表 + 校验（:func:`core.import_workers.plan_import`，**只读不写库**）；
2. 把**前 5 行**和"能导几行 / 失败几行 / 跳过几行"摆给用户看；
3. 用户点头，才 :func:`core.import_workers.apply_plan` 落库。

★ 这一层只做"看和确认"，不碰加密库、不发网络请求。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.import_workers import ImportPlan

__all__ = ["PREVIEW_ROWS", "PREVIEW_COLUMNS", "MoralImportPreviewDialog"]

#: 预览显示几行（用户要求：前 5 行）
PREVIEW_ROWS = 5

#: 预览表的列
PREVIEW_COLUMNS: tuple[str, ...] = ("表内行号", "学号", "姓名", "班级", "德育分")


class MoralImportPreviewDialog(QDialog):
    """德育分导入预览。

    Args:
        plan: :func:`core.import_workers.plan_import` 的结果（只读快照）。
        parent: 父窗口。

    Signals:
        None —— 用 :meth:`confirm` 拿 bool，不用连接信号。
    """

    def __init__(self, plan: ImportPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plan = plan
        self.setWindowTitle("批量导入德育分（预览）")
        self.setModal(True)
        self.resize(720, 460)
        self._build()

    # ------------------------------------------------------------------ 对外
    @classmethod
    def confirm(cls, plan: ImportPlan, parent: QWidget | None = None) -> bool:
        """弹预览；用户点了「开始导入」返回 ``True``。"""
        dialog = cls(plan, parent)
        return dialog.exec() == QDialog.DialogCode.Accepted

    # ------------------------------------------------------------------ 内部
    @property
    def plan(self) -> ImportPlan:
        return self._plan

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)

        outer.addWidget(self._label(self._headline(), bold=True))
        outer.addWidget(self._label(self._source_line()))
        outer.addWidget(self._label(self._column_line()))

        table = self._build_table()
        outer.addWidget(table, 1)

        if self._plan.rows:
            outer.addWidget(
                self._label(
                    f"上面只显示前 {PREVIEW_ROWS} 行，这一份一共 "
                    f"{len(self._plan.rows)} 行可以导入。"
                )
            )
        else:
            outer.addWidget(
                self._label(
                    "★ 这一份一行都导不进去：点「开始导入」只会导出一张错误清单"
                    "（用来改表），不会往库里写任何分数。"
                )
            )

        for note in self._plan.warnings[:4]:
            outer.addWidget(self._label("提醒：" + str(note)))

        buttons = QDialogButtonBox()
        buttons.addButton("开始导入", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _headline(self) -> str:
        plan = self._plan
        return (
            f"可以导入 {len(plan.rows)} 行　·　"
            f"失败 {len(plan.failures)} 行　·　"
            f"跳过 {len(plan.skipped)} 行"
        )

    def _source_line(self) -> str:
        plan = self._plan
        text = f"文件：{plan.path.name}　表内共 {plan.total_rows} 行"
        if plan.classes:
            text += "　涉及班级：" + "、".join(plan.classes)
        else:
            text += "　（表里没有班级列，一行都落不下去）"
        return text

    def _column_line(self) -> str:
        columns = self._plan.columns
        if columns is None:
            return "识别到的列：（没识别出表头）"
        return "识别到的列：" + columns.describe()

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, len(PREVIEW_COLUMNS))
        table.setHorizontalHeaderLabels(list(PREVIEW_COLUMNS))
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setShowGrid(False)

        rows = self._plan.rows[:PREVIEW_ROWS]
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            score = "" if row.score is None else _score_text(row.score)
            values = (
                str(row.row_index),
                row.student_id,
                row.student,
                row.class_name,
                score,
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                # ★ 别用 int() 转 —— Qt 的 Flag 枚举在 PySide6 里 int() 会抛
                #   TypeError（AGENTS.md 4g 记过一次）。直接把 Flag 交回去。
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter
                    | (Qt.AlignmentFlag.AlignRight if col in (0, 4)
                       else Qt.AlignmentFlag.AlignLeft)
                )
                table.setItem(index, col, item)

        header = table.horizontalHeader()
        for col in range(len(PREVIEW_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(120)
        return table

    @staticmethod
    def _label(text: str, *, bold: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName("H3" if bold else "Muted")
        label.setWordWrap(True)
        return label


def _score_text(value: Any) -> str:
    """分值写成"85"而不是"85.0"（跟 :func:`core.import_workers._cell_text` 一个口径）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"
