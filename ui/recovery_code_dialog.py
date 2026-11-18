"""找回码展示窗 —— **本地注册模式**下唯一一次能看到它的地方（v1.9.8 新增）。

为什么要有这个窗
----------------

注册时生成的找回码是"第二把钥匙"：它是**唯一**能解开被密码包住的库密钥的东西。
邮箱模式里它躺在邮件里；本地模式（安装包没带 ``email.json``）里没有邮件可看，
所以必须在注册成功的那一刻**当面交给用户**，而且要把"只显示这一次"讲清楚。

★ 三条硬约束
------------

1. **必须能复制**：24 位分 6 组的码，让用户手抄是灾难。
2. **必须显式确认**：不勾"我已保存"就不让关 —— 关掉就真的没有了。
3. **不落盘、不写日志**：这串码只在内存里待这一次（和邮件正文同一条规矩）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["RecoveryCodeDialog", "show_recovery_code"]


class RecoveryCodeDialog(QDialog):
    """展示找回码并强制确认。``exec()`` 返回 ``Accepted`` 表示用户确认保存过。"""

    def __init__(
        self,
        code: str,
        *,
        emailed_to: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._code = str(code or "")
        self._emailed_to = (emailed_to or "").strip()
        self._confirmed = False

        self.setWindowTitle("请保存你的找回码")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 22, 26, 20)
        box.setSpacing(12)

        title = QLabel("这是你的「找回码」")
        title.setObjectName("H1")
        box.addWidget(title)

        if self._emailed_to:
            lead = QLabel(f"找回码已经寄到 {self._emailed_to}，这里再显示一次。")
        else:
            lead = QLabel(
                "本机没有配置发信服务，所以找回码只显示在这里一次。"
                "关掉这个窗口就再也看不到了。"
            )
        lead.setObjectName("Warn")
        lead.setWordWrap(True)
        box.addWidget(lead)

        self._display = QLineEdit(self._code)
        self._display.setReadOnly(True)
        self._display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._display.setMinimumHeight(52)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed.setPointSize(max(fixed.pointSize() + 3, 12))
        fixed.setBold(True)
        self._display.setFont(fixed)
        self._display.setCursorPosition(0)
        box.addWidget(self._display)

        copy_row = QHBoxLayout()
        self._copy_btn = QPushButton("复制找回码")
        self._copy_btn.setMinimumHeight(34)
        self._copy_btn.clicked.connect(self._copy)
        copy_row.addWidget(self._copy_btn)
        copy_row.addStretch(1)
        box.addLayout(copy_row)

        note = QLabel(
            "忘记密码时，只有这串码能打开你的成绩库（它包住了库密钥）。\n"
            "请把它存到密码管理器、加密笔记，或者抄在纸上放好；"
            "不要发到聊天群里。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        box.addWidget(note)

        self._saved = QCheckBox("我已把找回码保存到安全的地方")
        self._saved.toggled.connect(self._on_saved_toggled)
        box.addWidget(self._saved)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._ok = QPushButton("我已保存，继续")
        self._ok.setObjectName("Primary")
        self._ok.setMinimumHeight(38)
        self._ok.setMinimumWidth(150)
        self._ok.setEnabled(False)
        self._ok.clicked.connect(self._confirm)
        buttons.addWidget(self._ok)
        box.addLayout(buttons)

    # ---------------------------------------------------------------- 动作
    def _copy(self) -> None:
        QApplication.clipboard().setText(self._code)
        self._copy_btn.setText("已复制")
        self._copy_btn.setEnabled(False)

    def _on_saved_toggled(self, checked: bool) -> None:
        self._ok.setEnabled(bool(checked))

    def _confirm(self) -> None:
        if not self._saved.isChecked():
            return
        self._confirmed = True
        self.accept()

    # ---------------------------------------------------------------- 关窗守卫
    def reject(self) -> None:  # noqa: D401 - Qt 回调
        """Esc / 关窗按钮：没确认就再问一次，别让用户误关。"""
        if self._confirmed or self._confirm_discard():
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 回调
        if self._confirmed or self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(
            self,
            "还没保存找回码",
            "这串找回码只显示这一次，关掉以后就再也拿不到了。\n\n"
            "确定要丢弃它吗？（以后忘记密码，成绩库就打不开了）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes


def show_recovery_code(
    parent: QWidget | None,
    code: str,
    *,
    emailed_to: str = "",
) -> bool:
    """弹出找回码窗，返回用户是否确认保存过。"""
    dialog = RecoveryCodeDialog(code, emailed_to=emailed_to, parent=parent)
    return dialog.exec() == QDialog.DialogCode.Accepted
