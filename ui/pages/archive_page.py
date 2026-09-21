"""📈 成长档案页 —— 占位骨架（Spec 七）。

Spec 七要求这一页本阶段只放 ``QLabel("开发中")``，并**预留 ``build(parent)``
函数签名**，供后续把 :mod:`core.archival` 的纵向档案接上来。

★ 这里刻意把"以后要接什么"写清楚，而不是只放两个字：这个模块
  （:mod:`core.archival`，724 行）已经写好了学期记录、趋势 Δ、定型/可救分区、
  学期排名，**只是还没有页面用它**。下一阶段这一页是纯 UI 活。
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ui.anim import make_card

__all__ = ["ArchivePage", "build"]

_PLANNED = (
    "计划中的内容（core.archival 已经实现，只差界面）：",
    "· 按学期串起每个学生的成绩时间轴",
    "· 趋势 Δ：最新学期均分 − 最早学期均分",
    "· 定型 / 可救分区，剩余可冲学期数",
    "· 学期排名与同专业对比",
    "· 导入日志（哪次从哪个文件导进来的）",
)


class ArchivePage(QWidget):
    """成长档案（占位）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(12)

        card = make_card()
        inner = QVBoxLayout(card)
        inner.setContentsMargins(24, 22, 24, 22)
        inner.setSpacing(10)

        title = QLabel("📈  成长档案")
        title.setObjectName("H1")
        inner.addWidget(title)

        todo = QLabel("开发中")
        todo.setObjectName("Placeholder")
        inner.addWidget(todo)

        inner.addSpacing(6)
        for index, line in enumerate(_PLANNED):
            note = QLabel(line)
            note.setObjectName("H3" if index == 0 else "Muted")
            note.setWordWrap(True)
            inner.addWidget(note)

        inner.addStretch(1)
        box.addWidget(card, 1)


def build(parent: QWidget | None = None) -> QWidget:
    """预留的构建入口（Spec 七：``build(parent)`` 函数签名）。

    现在只是把 :class:`ArchivePage` 包一层，将来要换成"按档案数据动态搭界面"
    时，调用方不用改。
    """
    return ArchivePage(parent)
