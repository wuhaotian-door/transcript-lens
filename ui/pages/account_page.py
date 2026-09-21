"""👤 个人中心页（Spec 七）。

当前账号名 + 找回邮箱 + [重新生成找回码] + [切换账号] + [修改密码]占位 + 关于信息。

★ 改名说明：Spec 六 把「侧写辅导」定名为 ``profile_page.py``，
  所以"个人中心"落在本模块，别走错。

★ 为什么「重新生成找回码」要再问一次密码
----------------------------------------

生成找回码 = 用一把新的钥匙把**库密钥**重新包一遍 —— 所以手里必须有库密钥。
而库密钥只有输过密码才拿得到（自动登录的令牌里刻意不放它，见
``core/session_token.py``）。所以这一步会弹一个小框让你输密码，
输对了才发新码。

这不是多此一举：它顺带挡住了"别人趁你没锁屏，偷偷把找回码换成他自己的邮箱"。

★ 「修改密码」仍是占位：它同样需要库密钥，而且要先验旧密码。
  下一阶段连同这个一起做。
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
from ui.anim import make_card
from ui.workers import run_async

__all__ = ["AccountPage"]

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

        change = QPushButton("修改密码")
        change.setObjectName("Ghost")
        change.setMinimumHeight(38)
        change.clicked.connect(self._do_change_password)
        row.addWidget(change)

        row.addStretch(1)
        acc_box.addLayout(row)
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
        QMessageBox.information(
            self,
            "修改密码：占位",
            "本功能还没做，原因不是懒：\n\n"
            "改密码需要「库密钥」才能重新包裹它，而库密钥只有输过密码的那次登录"
            "才拿得到 —— 自动登录的令牌里刻意不放密钥"
            "（放了就等于把成绩库的钥匙放进一个人人可解的文件）。\n\n"
            "下一阶段会连同「改密码时校验旧密码」一起做。",
        )


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
