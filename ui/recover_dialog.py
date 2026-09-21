"""「忘记密码？」对话框 —— 用注册时寄到邮箱的找回码重设密码。

流程::

    账号  [ 张老师 ▾ ]
    邮箱  zh***@163.com        ← 从本地记录读出来，只显示掩码
    找回码 [ SHFS-7BTW-… ]      ← 从邮件里复制粘贴
    新密码 [ ●●●● ]
    确认   [ ●●●● ]
    [取消]  [重设密码]

★ 这里**没有**"发送验证码"按钮，这是故意的
------------------------------------------

邮箱在这条流程里**不参与验证**，它只是找回码的**存放处**。

原因还是那条：库密钥只在本地、只被密码包裹。忘密码那天它已经解不开了，
"现在发一封邮件"发不出任何有用的东西 —— 新发的码没有被用来包裹过任何密钥。

能解开库密钥的，只有**注册时**生成、并已经用它包好密钥的那一串找回码。
所以这个对话框要的是"邮件里那一串"，不是"刚发的新码"。

如果用户把那封邮件删了 —— 那确实没办法，界面会如实说明。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.crypto_store import CryptoError, CryptoStore
from core.recovery import (
    RECOVERY_CODE_LENGTH,
    RecoveryError,
    recovery_info,
    reset_password_recovery,
)
#: ★ import 方向：本模块依赖 login_window 的常量，反过来 login_window
#: 只在 `_do_forgot_password()` 里**延迟 import** 本模块 —— 单向依赖，不成环。
from ui.login_window import MIN_PASSWORD_LENGTH
from ui.workers import run_async

__all__ = ["RecoverDialog"]


def _mask_email(address: str) -> str:
    """``zhangsan@163.com`` → ``zh***@163.com``（界面上只露一点）。"""
    text = (address or "").strip()
    if "@" not in text:
        return "（未绑定）"
    local, _, domain = text.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}***@{domain}"


class RecoverDialog(QDialog):
    """用找回码重设密码。

    Signals: 无 —— 成功时直接 ``accept()``，调用方读 :attr:`username`。
    """

    def __init__(self, store: CryptoStore, root: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._data_root = root
        self._busy = False
        self.username = ""

        self.setWindowTitle("找回密码")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build()
        self._refresh_accounts()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(12)

        title = QLabel("用找回码重设密码")
        title.setObjectName("H1")
        box.addWidget(title)

        hint = QLabel(
            "找回码在你注册成功时收到的那封邮件里"
            "（主题：【成绩单透镜】密码找回码）。\n"
            "把它整串复制过来即可，横杠带不带都行。"
        )
        hint.setObjectName("Notice")
        hint.setWordWrap(True)
        box.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        self._account = QComboBox()
        self._account.setEditable(False)
        self._account.setMinimumHeight(36)
        self._account.currentTextChanged.connect(self._on_account_changed)
        form.addRow(QLabel("账号"), self._account)

        self._email_label = QLabel("—")
        self._email_label.setObjectName("Muted")
        form.addRow(QLabel("找回邮箱"), self._email_label)

        self._code = QLineEdit()
        self._code.setPlaceholderText(
            f"{RECOVERY_CODE_LENGTH} 位，形如 SHFS-7BTW-ECYK-…"
        )
        self._code.setMinimumHeight(36)
        form.addRow(QLabel("找回码"), self._code)

        self._new_pass = QLineEdit()
        self._new_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_pass.setMinimumHeight(36)
        form.addRow(QLabel("新密码"), self._new_pass)

        self._new_pass2 = QLineEdit()
        self._new_pass2.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_pass2.setMinimumHeight(36)
        self._new_pass2.returnPressed.connect(self._do_reset)
        form.addRow(QLabel("确认新密码"), self._new_pass2)
        box.addLayout(form)

        self._status = QLabel("")
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        box.addWidget(self._status)

        buttons = QDialogButtonBox()
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)

        self._ok = QPushButton("重设密码")
        self._ok.setObjectName("Primary")
        self._ok.setDefault(True)
        self._ok.clicked.connect(self._do_reset)
        buttons.addButton(self._ok, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addWidget(buttons)

    # ---------------------------------------------------------------- 数据
    def _refresh_accounts(self) -> None:
        try:
            index = self._store.load_users()
        except CryptoError as exc:
            self._status.setText(f"读不到账户列表：{exc}")
            return
        names = [record.username for record in index]
        self._account.clear()
        self._account.addItems(names)
        if not names:
            self._status.setText(
                "这台电脑上还没有账户。如果文件在、但账户不见了，"
                "请先确认数据目录没有被移动过。"
            )
        else:
            self._on_account_changed(names[0])

    def _on_account_changed(self, name: str) -> None:
        if not name:
            self._email_label.setText("—")
            return
        info = recovery_info(self._store, name)
        if info["enabled"]:
            self._email_label.setText(_mask_email(info["email"]))
            self._status.setText("")
        else:
            self._email_label.setText("（没有绑定找回码）")
            self._status.setText(
                f"「{name}」没有可用的找回码 —— 注册时那封邮件没寄出去，"
                "或者账户是旧版本建的。没有它，密码忘了就打不开了。"
            )

    # ---------------------------------------------------------------- 动作
    def _do_reset(self) -> None:
        if self._busy:
            return
        name = self._account.currentText().strip()
        code = self._code.text().strip()
        new_pass = self._new_pass.text()
        again = self._new_pass2.text()

        if not name:
            QMessageBox.warning(self, "还没选账号", "请选择一个账号。")
            return
        if not code:
            QMessageBox.warning(self, "还没填找回码", "请把邮件里那一串复制过来。")
            return
        if len(new_pass) < MIN_PASSWORD_LENGTH:
            QMessageBox.warning(self, "密码太短", f"密码至少 {MIN_PASSWORD_LENGTH} 位。")
            return
        if new_pass != again:
            QMessageBox.warning(self, "两次密码不一致", "请重新输入确认密码。")
            return

        self._busy = True
        self._ok.setEnabled(False)
        self._set_status("正在校验找回码（PBKDF2 派生中，约 0.3 秒）…")

        def _work() -> str:
            reset_password_recovery(self._store, name, code, new_pass)
            return name

        def _done(result: str) -> None:
            self._busy = False
            self.username = result
            self.accept()

        def _failed(exc: BaseException) -> None:
            self._busy = False
            self._ok.setEnabled(True)
            message = str(exc) if isinstance(exc, RecoveryError) else f"{type(exc).__name__}: {exc}"
            self._set_status(f"重设失败：{message}")
            QMessageBox.critical(self, "重设失败", message)

        run_async(self, _work, on_done=_done, on_error=_failed)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setObjectName("Danger" if error else "Muted")
        self._status.setText(text)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
