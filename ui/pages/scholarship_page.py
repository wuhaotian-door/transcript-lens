"""💰 奖学金测算页 —— **纯壳，零逻辑**（Spec 七）。

本页**不 import calculator、不 import parser**，一个数字都不算。
所有输入框都是 ``setEnabled(False)`` 的灰控件，明确告诉用户"这里现在不能用"。

为什么连"尝试算一下"都不做：综测分的换算规则（德育/智育/文体各占多少、
各学院怎么折算）Spec 里没有定，**没定的规则不能自己发明** —— 算出来一个
看起来像模像样的数字，比不算危险得多。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.anim import make_card

__all__ = ["ScholarshipPage"]

#: 将来要填的字段（现在一律灰掉）
DISABLED_FIELDS: tuple[tuple[str, str], ...] = (
    ("综测分", "—"),
    ("专业排名", "—"),
    ("一等奖线", "—"),
    ("二等奖线", "—"),
    ("三等奖线", "—"),
)


class ScholarshipPage(QWidget):
    """奖学金测算（占位）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(12)

        head = QLabel("🎓  奖学金测算（即将支持）")
        head.setObjectName("H1")
        box.addWidget(head)

        sub = QLabel("下面这些字段是将来要填的，本版本一律不可编辑。")
        sub.setObjectName("Muted")
        box.addWidget(sub)

        card = make_card()
        inner = QVBoxLayout(card)
        inner.setContentsMargins(24, 20, 24, 20)
        inner.setSpacing(14)

        section = QLabel("参数（未启用）")
        section.setObjectName("H3")
        inner.addWidget(section)

        form = QFormLayout()
        form.setSpacing(10)
        self._fields: list[QLineEdit] = []
        for label_text, placeholder in DISABLED_FIELDS:
            edit = QLineEdit(placeholder)
            edit.setEnabled(False)          # ★ Spec 七：灰掉的输入框
            edit.setMinimumHeight(36)
            edit.setMinimumWidth(220)
            self._fields.append(edit)
            form.addRow(QLabel(label_text), edit)
        inner.addLayout(form)

        inner.addSpacing(6)
        note = QLabel(
            "当前版本仅做智育加权，综测/德育换算将在后续版本开放"
        )
        note.setObjectName("Notice")
        note.setWordWrap(True)
        inner.addWidget(note)

        button = QPushButton("开始测算")
        button.setObjectName("Primary")
        button.setMinimumHeight(40)
        button.setEnabled(False)            # 同上一律不可用
        inner.addWidget(button)

        inner.addStretch(1)
        box.addWidget(card, 1)

    # ------------------------------------------------------------------
    @property
    def field_count(self) -> int:
        """给自检用：这一页到底有几个灰控件。"""
        return len(self._fields)

    @property
    def all_disabled(self) -> bool:
        """给自检用：是不是全部都不可编辑。"""
        return all(not field.isEnabled() for field in self._fields)
