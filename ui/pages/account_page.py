"""👤 个人中心页（Spec 七）。

当前账号名 + 找回邮箱 + [重新生成找回码] + [切换账号] + [修改密码] + 关于信息。

★ 改名说明：Spec 六 把「侧写辅导」定名为 ``profile_page.py``，
  所以"个人中心"落在本模块，别走错。

★ 为什么「重新生成找回码」要再问一次密码
----------------------------------------

生成找回码 = 用一把新的钥匙把**库密钥**重新包一遍 —— 所以手里必须有库密钥。
而库密钥只有输过密码才拿得到（自动登录的令牌里刻意不放它，见
``core/session_token.py``）。所以这一步会弹一个小框让你输密码，
输对了才发新码。

这不是多此一举：它顺带挡住了"别人趁你没锁屏，偷偷把找回码换成他自己的邮箱"。

★ 「修改密码」怎么做的
--------------------

改密码**不是**把整个成绩库重新加密一遍 —— 库密钥不随密码变，只是换一把
钥匙去包它（``CryptoStore.change_password`` 就是干这个的）。所以：

* 快；也不存在"改一半失败、半个库是新钥匙半个库是旧钥匙"的风险；
* **找回码不受影响** —— 它包的是同一把库密钥；
* 但**已经泄漏出去的那把库密钥不会失效**（这个取舍写在 ``crypto_store`` 开头）。

改完顺手把「7 天自动登录」的令牌清掉：改密码的动机通常就是"把别人挡在外面"，
而令牌是免密码入口，留着它等于门没锁。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.crypto_store import CryptoError, CryptoStore, WrongPasswordError
from core.email_recovery import send_recovery_mail
from core.recovery import RecoveryError, enable_recovery, recovery_info
from core.session_token import clear_token
from ui.anim import make_card
from ui.workers import run_async

__all__ = ["AccountPage"]

#: 新密码最短长度。必须和 ui/login_window.MIN_PASSWORD_LENGTH 一致 ——
#: 注册时要求 8 位、改密码却允许 4 位的话，等于给自己开了个后门。
#: 那边 import 本模块会成环（login_window ← account_page），所以这里
#: 不去 import 它，而是用一条自检把两个数字钉在一起
#: （见文件末尾的 :func:`password_length_consistent`）。
MIN_PASSWORD_LENGTH = 8

_ABOUT = (
    ("程序", "成绩单透镜 · transcript-lens"),
    ("版本", "v1.0"),
    ("定位", "辅导员本地单机工具，成绩不出这台电脑"),
    ("算术", "全部在本地确定性完成，绝不调用大模型做数字"),
    ("网络", "只有找回密码会联网（发一封邮件），成绩永不出网"),
    ("落盘", "明文成绩任何时候都不落盘，磁盘上只有 .enc 密文"),
)


def _mask_email(address: str) -> str:
    text = (address or "").strip()
    if "@" not in text:
        return "（未绑定）"
    local, _, domain = text.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}***@{domain}"


class AccountPage(QWidget):
    """个人中心。

    Signals:
        switch_account(): 用户点了「切换账号」，主窗口负责回到登录窗。
    """

    switch_account = Signal()

    def __init__(
        self,
        store: CryptoStore | None = None,
        root: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore(root)
        self._data_root = root
        self._username = ""
        self._build()

    def set_user(self, username: str) -> None:
        self._username = username or ""
        self._user_label.setText(self._username or "（还没有登录）")
        self._refresh_recovery()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(14)

        title = QLabel("👤  个人中心")
        title.setObjectName("H1")
        box.addWidget(title)

        # --- 账户 ---
        acc = make_card()
        acc_box = QVBoxLayout(acc)
        acc_box.setContentsMargins(24, 20, 24, 20)
        acc_box.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(self._section("当前账号"))
        self._user_label = QLabel("（还没有登录）")
        self._user_label.setObjectName("H2")
        head.addWidget(self._user_label)
        head.addStretch(1)
        acc_box.addLayout(head)

        note = QLabel(
            "账户只建在这台电脑上。密码用 PBKDF2 派生，磁盘上只留随机盐和校验值。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        acc_box.addWidget(note)

        row = QHBoxLayout()

        switch = QPushButton("切换账号")
        switch.setObjectName("Ghost")
        switch.setMinimumHeight(38)
        switch.clicked.connect(self._do_switch)
        row.addWidget(switch)

        self._pwd_btn = QPushButton("修改密码")
        self._pwd_btn.setObjectName("Ghost")
        self._pwd_btn.setMinimumHeight(38)
        self._pwd_btn.clicked.connect(self._do_change_password)
        row.addWidget(self._pwd_btn)

        row.addStretch(1)
        acc_box.addLayout(row)

        self._pwd_status = QLabel("")
        self._pwd_status.setObjectName("Muted")
        self._pwd_status.setWordWrap(True)
        acc_box.addWidget(self._pwd_status)
        box.addWidget(acc)

        # --- 找回密码 ---
        rec = make_card()
        rec_box = QVBoxLayout(rec)
        rec_box.setContentsMargins(24, 20, 24, 20)
        rec_box.setSpacing(10)
        rec_box.addWidget(self._section("找回密码"))

        self._recovery_note = QLabel("")
        self._recovery_note.setObjectName("Muted")
        self._recovery_note.setWordWrap(True)
        rec_box.addWidget(self._recovery_note)

        rec_row = QHBoxLayout()
        self._regen_btn = QPushButton("重新生成找回码")
        self._regen_btn.setObjectName("Ghost")
        self._regen_btn.setMinimumHeight(38)
        self._regen_btn.clicked.connect(self._do_regen)
        rec_row.addWidget(self._regen_btn)
        rec_row.addStretch(1)
        rec_box.addLayout(rec_row)

        self._regen_status = QLabel("")
        self._regen_status.setObjectName("Muted")
        self._regen_status.setWordWrap(True)
        rec_box.addWidget(self._regen_status)
        box.addWidget(rec)

        # --- 关于 ---
        about = make_card()
        about_box = QVBoxLayout(about)
        about_box.setContentsMargins(24, 20, 24, 20)
        about_box.setSpacing(8)
        about_box.addWidget(self._section("关于"))

        for key, value in _ABOUT:
            line = QHBoxLayout()
            name = QLabel(key)
            name.setObjectName("H3")
            name.setFixedWidth(60)
            line.addWidget(name)
            text = QLabel(value)
            text.setObjectName("Muted")
            text.setWordWrap(True)
            line.addWidget(text, 1)
            about_box.addLayout(line)

        box.addWidget(about)
        box.addStretch(1)

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("H2")
        return label

    # ---------------------------------------------------------------- 状态
    def _refresh_recovery(self) -> None:
        if not self._username:
            self._recovery_note.setText("还没有登录。")
            self._regen_btn.setEnabled(False)
            return
        info = recovery_info(self._store, self._username)
        self._regen_btn.setEnabled(True)
        if info["enabled"]:
            self._recovery_note.setText(
                f"找回邮箱：{_mask_email(info['email'])}\n"
                "忘了密码时，用注册（或上次重新生成）时寄到该邮箱的那串「找回码」"
                "重设密码，成绩不会丢。\n"
                "重新生成会作废旧的那串 —— 新邮件发出去之后，旧邮件就没用了。"
            )
        else:
            self._recovery_note.setText(
                "⚠ 这个账户还没有找回码，忘了密码就打不开了。\n"
                "点下面的按钮生成一串并寄到邮箱（需要输一次密码）。"
            )

    # ---------------------------------------------------------------- 动作
    def _do_switch(self) -> None:
        answer = QMessageBox.question(
            self,
            "切换账号",
            "当前账号会退出登录（成绩库和账户都不受影响）。\n\n要继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.switch_account.emit()

    def _do_regen(self) -> None:
        if not self._username:
            QMessageBox.warning(self, "还没有登录", "请先登录。")
            return

        dialog = _PasswordDialog(self._username, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.password
        email = dialog.email
        if not password or not email:
            return

        self._regen_btn.setEnabled(False)
        self._regen_btn.setText("生成中…")
        self._set_regen("正在校验密码并生成新的找回码…")

        def _work() -> str:
            # ① 验密码 —— 顺带拿到库密钥（生成找回码必须有它）
            session = self._store.login(self._username, password)
            # ② 生成新包裹（旧码立刻作废）
            code = enable_recovery(session, email, store=self._store)
            # ③ 寄出
            send_recovery_mail(email, code, username=self._username, root=self._data_root)
            return email

        def _done(target: str) -> None:
            self._regen_btn.setEnabled(True)
            self._regen_btn.setText("重新生成找回码")
            self._refresh_recovery()
            self._set_regen(f"新的找回码已寄到 {_mask_email(target)}，旧的已作废。")
            QMessageBox.information(
                self,
                "新的找回码已寄出",
                f"新找回码已经发到：\n{target}\n\n"
                "旧的那串立刻作废，请把旧邮件删掉，只留新的这一封。",
            )

        def _failed(exc: BaseException) -> None:
            self._regen_btn.setEnabled(True)
            self._regen_btn.setText("重新生成找回码")
            if isinstance(exc, WrongPasswordError):
                message = "密码不对，找回码没有改动。"
            elif isinstance(exc, (CryptoError, RecoveryError, ValueError)):
                message = str(exc)
            else:
                message = f"{type(exc).__name__}: {exc}"
            self._set_regen(f"失败：{message}", error=True)
            QMessageBox.critical(self, "没能生成", message)

        run_async(self, _work, on_done=_done, on_error=_failed)

    def _set_regen(self, text: str, *, error: bool = False) -> None:
        self._regen_status.setObjectName("Danger" if error else "Muted")
        self._regen_status.setText(text)
        self._regen_status.style().unpolish(self._regen_status)
        self._regen_status.style().polish(self._regen_status)

    def _do_change_password(self) -> None:
        """改密码 = 换一把钥匙重新包住库密钥（不重加密成绩库）。

        ★ 走后台线程：``change_password`` 里面有两次 PBKDF2（验旧 + 派生新），
          主线程直接跑会让窗口僵住半秒多。
        """
        if not self._username:
            QMessageBox.warning(self, "还没有登录", "请先登录。")
            return

        dialog = _ChangePasswordDialog(self._username, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        old_password = dialog.old_password
        new_password = dialog.new_password
        if not old_password or not new_password:
            return

        self._pwd_btn.setEnabled(False)
        self._pwd_btn.setText("修改中…")
        self._set_pwd_status("正在校验旧密码并重新包裹库密钥…")

        def _work() -> str:
            # login 解出库密钥（也就验了旧密码），change_password 再换新的包裹
            session = self._store.login(self._username, old_password)
            self._store.change_password(session, old_password, new_password)
            return self._username

        def _done(name: str) -> None:
            self._pwd_btn.setEnabled(True)
            self._pwd_btn.setText("修改密码")
            # ★ 改密码通常就是为了把别人挡在外面；自动登录令牌是免密码入口，
            #   留着它等于门没锁。顺手清掉（不影响当前这次登录）。
            token_note = ""
            try:
                cleared = clear_token(self._data_root)
                if cleared:
                    token_note = "「自动登录」凭证已一并清除，下次启动需要重新输密码。"
            except Exception:  # noqa: BLE001 - 清不掉也要能继续用
                token_note = ""
            self._set_pwd_status(f"「{name}」的密码已更新。{token_note}")
            QMessageBox.information(
                self,
                "密码已修改",
                f"账户「{name}」的密码已经改好。\n\n"
                "· 成绩库没有被重新加密，一条数据都没动。\n"
                "· 邮箱里的**找回码依然有效** —— 它包的是同一把库密钥。\n"
                + (f"· {token_note}\n" if token_note else ""),
            )

        def _failed(exc: BaseException) -> None:
            self._pwd_btn.setEnabled(True)
            self._pwd_btn.setText("修改密码")
            if isinstance(exc, WrongPasswordError):
                message = "旧密码不对，密码没有改动。"
            elif isinstance(exc, (CryptoError, ValueError)):
                message = str(exc)
            else:
                message = f"{type(exc).__name__}: {exc}"
            self._set_pwd_status(f"失败：{message}", error=True)
            QMessageBox.critical(self, "没能修改", message)

        run_async(self, _work, on_done=_done, on_error=_failed)

    def _set_pwd_status(self, text: str, *, error: bool = False) -> None:
        self._pwd_status.setObjectName("Danger" if error else "Muted")
        self._pwd_status.setText(text)
        self._pwd_status.style().unpolish(self._pwd_status)
        self._pwd_status.style().polish(self._pwd_status)


class _ChangePasswordDialog(QDialog):
    """改密码的小框：旧密码 / 新密码 / 确认新密码。

    密码只在内存里待一会儿：不落盘、不打日志、不进异常信息。
    """

    def __init__(self, username: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.old_password = ""
        self.new_password = ""
        self.setWindowTitle("修改密码")
        self.setModal(True)
        self.setMinimumWidth(460)

        box = QVBoxLayout(self)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(10)

        title = QLabel(f"修改「{username}」的密码")
        title.setObjectName("H2")
        box.addWidget(title)

        note = QLabel(
            "先输旧密码确认是本人。成绩库不会被重新加密，"
            "邮箱里的找回码也依然有效。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        box.addWidget(note)

        box.addWidget(QLabel("旧密码"))
        self._old = QLineEdit()
        self._old.setEchoMode(QLineEdit.EchoMode.Password)
        self._old.setMinimumHeight(36)
        box.addWidget(self._old)

        box.addWidget(QLabel("新密码"))
        self._new = QLineEdit()
        self._new.setEchoMode(QLineEdit.EchoMode.Password)
        self._new.setPlaceholderText(f"至少 {MIN_PASSWORD_LENGTH} 位")
        self._new.setMinimumHeight(36)
        box.addWidget(self._new)

        box.addWidget(QLabel("确认新密码"))
        self._new2 = QLineEdit()
        self._new2.setEchoMode(QLineEdit.EchoMode.Password)
        self._new2.setMinimumHeight(36)
        self._new2.returnPressed.connect(self._accept)
        box.addWidget(self._new2)

        buttons = QDialogButtonBox()
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        ok = QPushButton("确认修改")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        buttons.addButton(ok, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addWidget(buttons)

    def _accept(self) -> None:
        old = self._old.text()
        new = self._new.text()
        again = self._new2.text()
        if not old:
            QMessageBox.warning(self, "还差一点", "请输入旧密码。")
            return
        if len(new) < MIN_PASSWORD_LENGTH:
            QMessageBox.warning(
                self, "新密码太短", f"新密码至少 {MIN_PASSWORD_LENGTH} 位。"
            )
            return
        if new != again:
            QMessageBox.warning(self, "两次不一致", "两次输入的新密码不一样。")
            return
        if new == old:
            QMessageBox.warning(self, "没有变化", "新密码和旧密码一样，等于没改。")
            return
        self.old_password = old
        self.new_password = new
        self.accept()


class _PasswordDialog(QDialog):
    """要密码 + 找回邮箱的小框。密码只在内存里待一会儿，不落盘、不打日志。"""

    def __init__(self, username: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.password = ""
        self.email = ""
        self.setWindowTitle("重新生成找回码")
        self.setModal(True)
        self.setMinimumWidth(460)

        box = QVBoxLayout(self)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(10)

        title = QLabel(f"确认是本人：{username}")
        title.setObjectName("H2")
        box.addWidget(title)

        note = QLabel(
            "生成找回码要用到库密钥，所以需要你输一次密码。\n"
            "新找回码会寄到下面这个邮箱，旧的那串立刻作废。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        box.addWidget(note)

        box.addWidget(QLabel("密码"))
        self._pass = QLineEdit()
        self._pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass.setMinimumHeight(36)
        box.addWidget(self._pass)

        box.addWidget(QLabel("接收找回码的邮箱"))
        self._email = QLineEdit()
        self._email.setPlaceholderText("you@163.com")
        self._email.setMinimumHeight(36)
        box.addWidget(self._email)

        buttons = QDialogButtonBox()
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        ok = QPushButton("生成并寄出")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        buttons.addButton(ok, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addWidget(buttons)

    def _accept(self) -> None:
        from core.recovery import EmailCodeError, normalize_email

        if not self._pass.text():
            QMessageBox.warning(self, "还差一点", "请输入密码。")
            return
        try:
            email = normalize_email(self._email.text())
        except EmailCodeError as exc:
            QMessageBox.warning(self, "邮箱不对", str(exc))
            return
        self.password = self._pass.text()
        self.email = email
        self.accept()


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def password_length_consistent() -> bool:
    """本页的密码长度下限和登录/注册页是不是同一个数。

    ★ 为什么不直接 ``from ui.login_window import MIN_PASSWORD_LENGTH``：
      ``ui.login_window`` 会（延迟地）import 本模块所在的页面树，写在文件头上
      会成环。所以这里在**函数里**延迟 import，只给自检用。
      两个数字一旦漂开（注册要 8 位、改密码只要 4 位），就是给自己开后门。
    """
    from ui.login_window import MIN_PASSWORD_LENGTH as OTHER

    return OTHER == MIN_PASSWORD_LENGTH
