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

from PySide6.QtCore import Qt, Signal
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.crypto_store import CryptoError, CryptoStore, WrongPasswordError
from core.database import StudentStore
from core.email_recovery import mail_configured, try_send_recovery_mail
from core.recovery import RecoveryError, enable_recovery, recovery_info
from core.session_token import clear_token
from core.user_store import ROSTER_WRITE_LOCK
from core.version import CURRENT_VERSION
from ui.anim import make_card
from ui.recovery_code_dialog import show_recovery_code
from ui.workbench_widgets import TaskDialog, button, label, review_table
from ui.workers import run_async
from ui.widgets.profile_reading import profile_scroll

__all__ = ["AccountPage"]

#: 新密码最短长度。必须和 ui/login_window.MIN_PASSWORD_LENGTH 一致 ——
#: 注册时要求 8 位、改密码却允许 4 位的话，等于给自己开了个后门。
#: 那边 import 本模块会成环（login_window ← account_page），所以这里
#: 不去 import 它，而是用一条自检把两个数字钉在一起
#: （见文件末尾的 :func:`password_length_consistent`）。
MIN_PASSWORD_LENGTH = 8

_ABOUT = (
    ("程序", "朝夕工作台"),
    # ★ v1.8 修正：这里原来硬编码 "v1.0"（charter v1.8 第五节点名的旧 bug）。
    ("版本", f"v{CURRENT_VERSION}"),
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
    archives_changed = Signal()

    def __init__(
        self,
        store: CryptoStore | None = None,
        root: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore(root)
        self._data_root = root
        #: v1.9.8：没有发信配置时，新找回码直接显示，不寄邮件。
        self._mail_ready = mail_configured(root)
        self._username = ""
        self._archive_revision = 0
        self._build()

    def set_user(self, username: str) -> None:
        self._username = username or ""
        self._user_label.setText(self._username or "未登录")
        self._refresh_recovery()
        self._archive_count.setText("")
        self._manage_archive_button.setEnabled(bool(self._username))
        self.refresh_archives()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_archives()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(14)

        title = QLabel("个人中心")
        title.setObjectName("H1")
        box.addWidget(title)

        account = make_card(shadow=False)
        account_row = QHBoxLayout(account)
        account_row.setContentsMargins(22, 18, 22, 18)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        current = QLabel("当前账号")
        current.setObjectName("ProfileMeta")
        identity.addWidget(current)
        self._user_label = QLabel("未登录")
        self._user_label.setObjectName("H2")
        self._user_label.setWordWrap(True)
        identity.addWidget(self._user_label)
        account_row.addLayout(identity, 1)

        switch = QPushButton("切换账号")
        switch.setObjectName("Ghost")
        switch.setMinimumHeight(36)
        switch.clicked.connect(self._do_switch)
        account_row.addWidget(switch)
        box.addWidget(account)

        security = make_card(shadow=False)
        security_box = QVBoxLayout(security)
        security_box.setContentsMargins(22, 16, 22, 16)
        security_box.setSpacing(12)
        security_box.addWidget(self._section("账号安全"))

        self._pwd_btn = QPushButton("修改密码")
        self._pwd_btn.clicked.connect(self._do_change_password)
        self._pwd_status = QLabel()
        security_box.addLayout(self._action_row("登录密码", self._pwd_btn, self._pwd_status))

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: #E2E8F0; border: none;")
        security_box.addWidget(divider)

        self._regen_btn = QPushButton("生成找回码")
        self._regen_btn.clicked.connect(self._do_regen)
        self._regen_status = QLabel()
        self._recovery_note = QLabel()
        self._recovery_note.setObjectName("Muted")
        self._recovery_note.setWordWrap(True)
        security_box.addLayout(self._action_row(
            "密码找回", self._regen_btn, self._regen_status, self._recovery_note,
        ))
        box.addWidget(security)

        archive = make_card(shadow=False)
        archive_row = QHBoxLayout(archive)
        archive_row.setContentsMargins(22, 16, 22, 16)
        archive_text = QVBoxLayout()
        archive_text.addWidget(self._section("归档管理"))
        self._archive_count = label("", "Muted")
        archive_text.addWidget(self._archive_count)
        archive_row.addLayout(archive_text, 1)
        self._manage_archive_button = button("管理归档", self._open_archives)
        self._manage_archive_button.setEnabled(False)
        archive_row.addWidget(self._manage_archive_button)
        box.addWidget(archive)

        about = make_card(shadow=False)
        about_row = QHBoxLayout(about)
        about_row.setContentsMargins(22, 14, 22, 14)
        about_row.addWidget(self._section("关于"))
        version = QLabel("  ·  ".join(value for _, value in _ABOUT))
        version.setObjectName("Muted")
        version.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        version.setWordWrap(True)
        about_row.addWidget(version, 1)
        box.addWidget(about)
        box.addStretch()
        outer.addWidget(profile_scroll(body))

    @staticmethod
    def _action_row(
        text: str, button: QPushButton, status: QLabel, detail: QLabel | None = None,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        content = QVBoxLayout()
        content.setSpacing(4)
        title = QLabel(text)
        title.setObjectName("H3")
        content.addWidget(title)
        if detail is not None:
            content.addWidget(detail)
        status.setObjectName("Muted")
        status.setWordWrap(True)
        status.hide()
        content.addWidget(status)
        row.addLayout(content, 1)
        button.setObjectName("Ghost")
        button.setMinimumSize(128, 36)
        row.addWidget(button)
        return row

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("H2")
        return label

    def refresh_archives(self):
        self._archive_revision += 1
        if not self._username or not self.isVisible():
            return
        username = self._username
        revision = self._archive_revision
        self._archive_count.setText("正在读取…")

        def display(text):
            if isValid(self) and revision == self._archive_revision:
                self._archive_count.setText(text)

        run_async(self, lambda: len(_archived_students(self._store, username)),
                  on_done=lambda count: display(f"已归档 {count} 名学生"),
                  on_error=lambda _: display("归档记录暂时无法读取。"))

    def _open_archives(self):
        if not self._username:
            return
        dialog = _ArchiveManagementDialog(self, self._store, self._username)
        dialog.restored.connect(self.archives_changed.emit)
        dialog.exec()
        dialog.deleteLater()
        self.refresh_archives()

    # ---------------------------------------------------------------- 状态
    def _refresh_recovery(self) -> None:
        if not self._username:
            self._recovery_note.clear()
            self._recovery_note.hide()
            self._regen_btn.setEnabled(False)
            return
        info = recovery_info(self._store, self._username)
        self._recovery_note.show()
        self._regen_btn.setEnabled(True)
        if info["enabled"]:
            if info["email"]:
                self._recovery_note.setText(f"找回邮箱 {_mask_email(info['email'])}")
            else:
                self._recovery_note.setText("找回码已设置（本机未绑定邮箱）")
            self._regen_btn.setText("重新生成找回码")
        else:
            self._recovery_note.setText("尚未设置找回码")
            self._regen_btn.setText("生成找回码")

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

        dialog = _PasswordDialog(self._username, ask_email=self._mail_ready, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.password
        email = dialog.email
        if not password or (self._mail_ready and not email):
            return

        self._regen_btn.setEnabled(False)
        self._regen_btn.setText("生成中…")
        self._set_regen("正在校验密码并生成新的找回码…")

        def _work() -> tuple[str, bool]:
            # ① 验密码 —— 顺带拿到库密钥（生成找回码必须有它）
            session = self._store.login(self._username, password)
            # ② 生成新包裹（旧码立刻作废）
            code = enable_recovery(session, email, store=self._store)
            delivered = bool(email) and try_send_recovery_mail(
                email, code, username=self._username, root=self._data_root
            )
            return code, delivered

        def _done(result: tuple[str, bool]) -> None:
            code, delivered = result
            self._regen_btn.setEnabled(True)
            self._regen_btn.setText("重新生成找回码")
            self._refresh_recovery()
            if delivered:
                self._set_regen(f"新的找回码已寄到 {_mask_email(email)}，旧的已作废。")
                QMessageBox.information(
                    self,
                    "新的找回码已寄出",
                    f"新找回码已经发到：\n{email}\n\n"
                    "旧的那串立刻作废，请把旧邮件删掉，只留新的这一封。",
                )
            else:
                delivery_notice = "邮件未能发出。" if email else ""
                self._set_regen(f"旧找回码已作废。{delivery_notice}请保存屏幕上的新找回码。")
                show_recovery_code(self, code)

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
        self._regen_status.setVisible(bool(text))
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

        username = self._username
        dialog = _ChangePasswordDialog(
            username, parent=self, can_use_recovery=recovery_info(self._store, username)["enabled"]
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.use_recovery:
            from ui.recover_dialog import RecoverDialog

            recovery = RecoverDialog(self._store, self._data_root, parent=self, username=username)
            if recovery.exec() == QDialog.DialogCode.Accepted:
                self._set_pwd_status(f"「{username}」的密码已更新。")
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
            session = self._store.login(username, old_password)
            self._store.change_password(session, old_password, new_password)
            return username

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
                "请使用新密码登录。此前保存的找回码仍可使用。\n"
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
        self._pwd_status.setVisible(bool(text))
        self._pwd_status.style().unpolish(self._pwd_status)
        self._pwd_status.style().polish(self._pwd_status)


def _archived_students(store, username):
    with ROSTER_WRITE_LOCK:
        return StudentStore(store=store, username=username).search(archived_only=True)


class _ArchiveManagementDialog(TaskDialog):
    restored = Signal()

    def __init__(self, parent, store, username):
        super().__init__(parent, "归档管理")
        self.resize(700, 500)
        self._store = store
        self._username = username
        self._rows = []
        self.body.addWidget(label("归档管理", "H2"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索姓名、学号或班级")
        self._search.setClearButtonEnabled(True)
        self.body.addWidget(self._search)
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(("姓名", "学号", "班级"))
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setDefaultSectionSize(34)
        review_table(self._table)
        self.body.addWidget(self._table, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(button("关闭", self.reject))
        self._restore = button("恢复到学生名单", self._restore_selected, "Primary")
        self._restore.setEnabled(False)
        bottom.addWidget(self._restore)
        self.body.addLayout(bottom)
        self._search.textChanged.connect(self._render)
        self._table.itemSelectionChanged.connect(self._update_restore)
        self.execute(lambda: _archived_students(store, username), self._show_rows, "正在读取归档…",
                     controls=(self._search, self._table))

    def _show_rows(self, rows):
        self._rows = rows
        self._render()

    def _render(self):
        needle = self._search.text().strip().casefold()
        rows = [(row["name"], row["student_id"].removeprefix("sid:"), row.get("class_name") or "")
                for row in self._rows]
        shown = [row for row in rows if any(needle in value.casefold() for value in row)]
        self._table.clearSelection()
        self._table.setRowCount(len(shown))
        for position, row in enumerate(shown):
            for column, value in enumerate(row):
                self._table.setItem(position, column, QTableWidgetItem(value or "—"))
        self._update_restore()
        self.status.setText(f"显示 {len(shown)} / {len(rows)} 人" if rows else "暂无归档学生。")

    def _update_restore(self):
        self._restore.setEnabled(bool(self._table.selectedItems()) and not self._busy)

    def _restore_selected(self):
        if not self._table.selectedItems():
            return
        sid = self._table.item(self._table.currentRow(), 1).text()

        def restore():
            with ROSTER_WRITE_LOCK:
                students = StudentStore(store=self._store, username=self._username)
                row = students.get(sid)
                if row is None:
                    raise ValueError("该学生已不存在，请关闭窗口后重新打开。")
                if row.get("archived"):
                    students.restore(sid)
            return _archived_students(self._store, self._username)

        def done(rows):
            self._show_rows(rows)
            self.status.setText("已恢复到学生名单。")
            self.restored.emit()

        self.execute(restore, done, "正在恢复…", controls=(self._search, self._table))

class _ChangePasswordDialog(QDialog):
    """改密码的小框：旧密码 / 新密码 / 确认新密码。

    密码只在内存里待一会儿：不落盘、不打日志、不进异常信息。
    """

    def __init__(self, username: str, parent: QWidget | None = None,
                 *, can_use_recovery: bool = False) -> None:
        super().__init__(parent)
        self.use_recovery = False
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
        if can_use_recovery:
            recovery = QPushButton("用找回码修改")
            recovery.setObjectName("Ghost")
            recovery.clicked.connect(self._choose_recovery)
            buttons.addButton(recovery, QDialogButtonBox.ButtonRole.ActionRole)
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

    def _choose_recovery(self) -> None:
        self.use_recovery = True
        self.accept()

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
    """要密码（能发信时再要一个邮箱）的小框。

    密码只在内存里待一会儿，不落盘、不打日志。
    """

    def __init__(
        self,
        username: str,
        *,
        ask_email: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.password = ""
        self.email = ""
        self._ask_email = bool(ask_email)
        self.setWindowTitle("重新生成找回码")
        self.setModal(True)
        self.setMinimumWidth(460)

        box = QVBoxLayout(self)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(10)

        title = QLabel(f"确认是本人：{username}")
        title.setObjectName("H2")
        box.addWidget(title)

        if self._ask_email:
            note_text = "新找回码发送成功后，旧找回码失效。"
        else:
            note_text = (
                "本机没有配置发信服务：新找回码生成后会直接显示在屏幕上，请当场保存。"
                "旧的立刻失效。"
            )
        note = QLabel(note_text)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        box.addWidget(note)

        box.addWidget(QLabel("密码"))
        self._pass = QLineEdit()
        self._pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass.setMinimumHeight(36)
        box.addWidget(self._pass)

        #: 邮箱框只在能真发信时才需要用；本地模式下不创建、不显示。
        self._email = QLineEdit()
        if self._ask_email:
            box.addWidget(QLabel("接收找回码的邮箱"))
            self._email.setPlaceholderText("you@163.com")
            self._email.setMinimumHeight(36)
            box.addWidget(self._email)

        buttons = QDialogButtonBox()
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        ok = QPushButton("生成并寄出" if self._ask_email else "生成找回码")
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
        email = ""
        if self._ask_email:
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
