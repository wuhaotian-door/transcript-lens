"""输入本地保存或邮件中的 24 位找回码，重新包裹同一把库密钥。"""

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
from core.session_token import clear_token
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
                 parent: QWidget | None = None, *, username: str = "") -> None:
        super().__init__(parent)
        self._store = store
        self._data_root = root if root is not None else store.root
        self._busy = False
        self.username = ""

        self.setWindowTitle("找回密码")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build()
        self._refresh_accounts(username)

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(12)

        title = QLabel("用找回码重设密码")
        title.setObjectName("H1")
        box.addWidget(title)

        self._hint = QLabel("输入你保存的找回码。")
        self._hint.setObjectName("Notice")
        self._hint.setWordWrap(True)
        box.addWidget(self._hint)

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
    def _refresh_accounts(self, username: str = "") -> None:
        try:
            index = self._store.load_users()
        except CryptoError as exc:
            self._status.setText(f"读不到账户列表：{exc}")
            return
        names = [record.username for record in index if not username or record.username == username]
        self._account.clear()
        self._account.addItems(names)
        self._account.setEnabled(not username)
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
            self._hint.setText("输入你保存的找回码。")
            return
        info = recovery_info(self._store, name)
        if info["enabled"]:
            if info["email"]:
                self._email_label.setText(_mask_email(info["email"]))
                self._hint.setText(
                    "可从「成绩单透镜」找回码邮件复制找回码，也可以输入你之前保存的找回码。"
                )
            else:
                self._email_label.setText("（本机未绑定邮箱）")
                self._hint.setText("请使用注册或重新生成时保存的 24 位找回码。")
            self._status.setText("")
        else:
            self._email_label.setText("（没有绑定找回码）")
            self._hint.setText("可用旧密码登录后，在个人中心生成找回码。")
            self._status.setText(
                f"「{name}」没有可用的找回码 —— 账户是旧版本建的，"
                "或者注册时没有完成。没有它，密码忘了就打不开了。"
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
            QMessageBox.warning(self, "还没填找回码", "请输入你保存的 24 位找回码。")
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
            try:
                clear_token(self._data_root)
            except CryptoError:
                pass
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
