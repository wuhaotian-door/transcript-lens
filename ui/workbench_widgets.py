"""共用的学生资料操作控件。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from core.crypto_store import CryptoError
from ui.workers import run_async


def label(text: str = "", role: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    return widget


def button(text, callback, role="Ghost") -> QPushButton:
    widget = QPushButton(text)
    widget.setObjectName(role)
    widget.setMinimumHeight(34)
    widget.clicked.connect(callback)
    return widget


def fields(parent, title, items):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(490, dialog.sizeHint().height())
    form = QFormLayout(dialog)
    inputs = {}
    for key, name, initial in items:
        widget = QLineEdit(initial)
        widget.setMaxLength(250)
        inputs[key] = widget
        form.addRow(name, widget)
    actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    actions.accepted.connect(dialog.accept)
    actions.rejected.connect(dialog.reject)
    form.addRow(actions)
    if not dialog.exec():
        return None
    return {key: widget.text().strip() for key, widget in inputs.items()}


def unlock(parent, vault, *, force=False) -> bool:
    if vault is None or not vault.username:
        return False
    if vault.unlocked and not force:
        return True
    password, ok = QInputDialog.getText(parent, "解锁学生资料", "输入当前账户登录密码", QLineEdit.EchoMode.Password)
    if not ok:
        return False
    try:
        vault.unlock(password)
    except (CryptoError, OSError) as exc:
        QMessageBox.warning(parent, "解锁未完成", str(exc))
        return False
    return True


class TaskDialog(QDialog):
    """同一对话框只运行一项任务，避免操作对象或账户中途变化。"""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._busy = False
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(22, 20, 22, 20)
        self.body.setSpacing(12)
        self.status = label("", "Muted")
        self.status.setWordWrap(True)

    def execute(self, work, done, message="正在处理…"):
        if self._busy:
            return
        self._busy = True
        self.status.setText(message)
        buttons = [b for b in self.findChildren(QPushButton) if b.isEnabled()]
        for item in buttons:
            item.setEnabled(False)

        def release():
            self._busy = False
            for item in buttons:
                item.setEnabled(True)

        def completed(result):
            release()
            self.status.clear()
            done(result)

        def failed(error):
            release()
            self.status.setText(f"未完成。{error}")

        run_async(self, work, on_done=completed, on_error=failed)

    def reject(self):
        if not self._busy:
            super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
        else:
            super().closeEvent(event)
