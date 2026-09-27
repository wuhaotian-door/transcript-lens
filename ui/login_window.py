"""登录 / 注册窗 —— 左侧深色品牌区 + 右侧表单，同一窗口里翻页。

     +--------------------+--------------------------------------+
     |  成绩单透镜         |  登录                                 |
     |  transcript-lens   |  账号  [ 可输入 + 历史用户名下拉 ]     |
     |  v1.0              |  密码  [ ●●●● ]                       |
     |  把成绩单，变成      |  ☐ 7 天内自动登录                     |
     |  能坐下来谈的东西    |  [登录] [去注册]                      |
     +--------------------+--------------------------------------+

★ 三件必须说清楚的事

1. **账号下拉里只有用户名**，从 ``users.json`` 读；**没有密码，也没有密码提示**。
2. **「7 天内自动登录」默认不勾。** 勾上会在 AppData 写一个 ``session.enc``，
   它能让人**免密码**进程序（这正是自动登录的定义）。这种事必须是用户明确选的，
   不能默认替他选。
3. **登录成功不在这里算任何东西。** 本窗口只做身份确认，然后发 ``logged_in``
   信号；算术在智育分析页，且全在本地。

★ 找回密码：**走邮箱里的找回码**

* 注册**必须填邮箱并验证**（6 位验证码，10 分钟有效）。
* 注册成功后，程序生成一串**找回码**，本地存一份"用它包起来的库密钥"，
  并把码寄到邮箱。这封邮件是忘密码时唯一的出路。
* 登录页「忘记密码？」→ 填账号 + 找回码 + 新密码即可重设，成绩一条不丢。

⚠️ **为什么不是"发个新验证码就能重置"**：库密钥只在本地、只被密码包裹。
忘密码那天它已经解不开了，"现在发一封邮件"发不出任何有用的东西 ——
能解开它的只有注册时生成、并已用它包好密钥的那一串码。

★ 关于无边框
-----------

现在走**无边框自绘**（Spec 里没有禁止这一条的条文；早先那句"不做无边框拖拽"
是界面还是 tkinter 时的旧约束，本轮按商业质感要求改掉了）。
系统标题栏没了，所以拖拽/最小化/关闭全部由 :mod:`ui.frameless` 补上：

* 一圈留白给阴影，圆角 12px 做在卡片上；
* 顶部一条自定义标题栏（可拖拽 + 最小化 + 关闭）；
* 边缘 6px 可以拖动缩放。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.crypto_store import (
    CryptoError,
    CryptoStore,
    StoreCorruptError,
    UnknownUserError,
    WrongPasswordError,
)
from core.email_recovery import send_recovery_mail, send_verification_mail
from core.recovery import (
    EMAIL_CODE_LENGTH,
    EMAIL_CODE_TTL_MINUTES,
    EmailCodeError,
    RecoveryError,
    check_email_code,
    enable_recovery,
    issue_email_code,
    normalize_email,
)
from core.session_token import AUTO_LOGIN_DAYS, clear_token, save_token, token_info
from core.version import CURRENT_VERSION
from ui.anim import window_fade_in, window_fade_out
from ui.frameless import (
    SHADOW_MARGIN,
    TITLE_BAR_HEIGHT,
    WindowTitleBar,
    apply_frameless,
    build_root_frame,
    install_chrome,
)
from ui.workers import run_async

__all__ = ["APP_VERSION", "MIN_PASSWORD_LENGTH", "SLOGAN", "LoginWindow"]

#: 版本号（显示在左栏）。
#:
#: ★ v1.8 修正：这里原来硬编码 ``"v1.0"`` —— 程序都 1.7 了，登录窗左栏
#:   还在说自己是 1.0，而且**不报错**，只是一直说假话
#:   （charter v1.8 第五节列为必须修正的旧 bug）。
#:   现在唯一真源是 :data:`core.version.CURRENT_VERSION`，
#:   ``main._version_source_report()`` 用 AST 扫 ``ui/`` 拦"再写死一个字面量"。
APP_VERSION = f"v{CURRENT_VERSION}"

#: slogan
SLOGAN = "把成绩单，变成能坐下来谈的东西"

#: 密码最短长度（本地单机工具，不做复杂度评分）
MIN_PASSWORD_LENGTH = 8

_BRAND_WIDTH = 300


class LoginWindow(QWidget):
    """登录 / 注册窗口。

    Signals:
        logged_in(str): 身份确认成功，参数是用户名。
            主程序接住它去起主窗口 —— **本窗口不负责起主窗口**，
            这样它自己可以先淡出再让位。
    """

    logged_in = Signal(str)

    def __init__(self, store: CryptoStore | None = None, root: str | None = None) -> None:
        super().__init__()
        #: 数据根目录。``None`` = 默认 AppData/Local/SGMS；
        #: 自检时指向临时目录，避免往真实 AppData 里写东西。
        self._data_root = root
        self._store = store if store is not None else CryptoStore(root)

        self.setWindowTitle(f"成绩单透镜 v{CURRENT_VERSION}")
        # ★ 无边框 + 透明底必须在 show() 之前设好（setWindowFlag 会藏窗口）
        apply_frameless(self)
        # 尺寸是**卡片可见部分**的尺寸，四周还要多留一圈给阴影
        self.setMinimumSize(880 + 2 * SHADOW_MARGIN, 560 + 2 * SHADOW_MARGIN)
        self.resize(920 + 2 * SHADOW_MARGIN, 580 + 2 * SHADOW_MARGIN)
        self.setWindowOpacity(0.0)
        #: 防止淡出过程中被再次触发登录
        self._leaving = False

        self._build()
        install_chrome(self, self._card, corners=[self._title_bar, self._brand])
        self._refresh_usernames()
        self._hint_about_token()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        # 圆角 + 阴影的窗口容器（objectName 让 QSS 里的 #LoginCard 生效）
        self._card = build_root_frame(self, name="LoginCard")
        card_box = QVBoxLayout(self._card)
        card_box.setContentsMargins(0, 0, 0, 0)
        card_box.setSpacing(0)

        # --- 自定义标题栏（系统那圈边框没了，这些都得自己来）---
        self._title_bar = WindowTitleBar(
            "成绩单透镜 · 登录", minimizable=True, maximizable=False, closable=True
        )
        self._title_bar.minimize_requested.connect(self.showMinimized)
        self._title_bar.close_requested.connect(self.close)
        card_box.addWidget(self._title_bar)

        body = QWidget()
        body_box = QHBoxLayout(body)
        body_box.setContentsMargins(0, 0, 0, 0)
        body_box.setSpacing(0)

        self._brand = self._build_brand()
        body_box.addWidget(self._brand)

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(38, 30, 38, 30)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_login_page())
        self._stack.addWidget(self._build_register_page())
        right_box.addWidget(self._stack)
        body_box.addWidget(right, 1)
        card_box.addWidget(body, 1)

    def _build_brand(self) -> QFrame:
        brand = QFrame()
        brand.setObjectName("LoginBrand")
        brand.setFixedWidth(_BRAND_WIDTH)

        box = QVBoxLayout(brand)
        box.setContentsMargins(26, 34, 26, 30)

        title = QLabel("成绩单透镜")
        title.setObjectName("LoginBrandTitle")
        box.addWidget(title)

        sub = QLabel(f"transcript-lens　{APP_VERSION}")
        sub.setObjectName("LoginBrandSub")
        box.addWidget(sub)

        box.addSpacing(26)

        slogan = QLabel(SLOGAN)
        slogan.setObjectName("LoginSlogan")
        slogan.setWordWrap(True)
        box.addWidget(slogan)

        box.addSpacing(22)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#334155;")
        box.addWidget(line)

        box.addSpacing(16)

        for text in (
            "本地离线运行",
            "不上传、不联网",
            "加权算术全部本地完成",
            "成绩只存加密文件",
        ):
            item = QLabel(f"· {text}")
            item.setObjectName("BrandBullet")
            box.addWidget(item)

        box.addStretch(1)
        return brand

    # --- 登录页 ---
    def _build_login_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        title = QLabel("登录")
        title.setObjectName("H1")
        box.addWidget(title)

        sub = QLabel("选择本机已有的账户，输入密码。")
        sub.setObjectName("Muted")
        box.addWidget(sub)
        box.addSpacing(22)

        box.addWidget(self._label("账号"))
        self._user_box = QComboBox()
        self._user_box.setEditable(True)          # Spec：可编辑可输入
        self._user_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._user_box.setMinimumHeight(38)
        box.addWidget(self._user_box)
        box.addSpacing(14)

        box.addWidget(self._label("密码"))
        self._pass_edit = QLineEdit()
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_edit.setMinimumHeight(38)
        self._pass_edit.returnPressed.connect(self._do_login)
        box.addWidget(self._pass_edit)
        box.addSpacing(14)

        self._remember = QCheckBox(f"{AUTO_LOGIN_DAYS} 天内自动登录")
        self._remember.setChecked(False)          # ★ 默认不勾，见模块开头第 2 条
        box.addWidget(self._remember)

        note = QLabel(
            f"勾选后本机会保存一个免密码凭证（{AUTO_LOGIN_DAYS} 天有效）。\n"
            "共用电脑上请不要勾。"
        )
        note.setObjectName("Muted")
        box.addWidget(note)
        box.addSpacing(18)

        buttons = QHBoxLayout()
        login_btn = QPushButton("登  录")
        login_btn.setObjectName("Primary")
        login_btn.setMinimumHeight(40)
        login_btn.setMinimumWidth(130)
        login_btn.clicked.connect(self._do_login)
        buttons.addWidget(login_btn)

        to_register = QPushButton("去注册")
        to_register.setObjectName("Ghost")
        to_register.setMinimumHeight(40)
        to_register.setMinimumWidth(110)
        to_register.clicked.connect(lambda: self._goto_page(1))
        buttons.addWidget(to_register)
        buttons.addStretch(1)
        box.addLayout(buttons)
        box.addSpacing(16)

        forgot = QPushButton("忘记密码？")
        forgot.setObjectName("Link")
        forgot.clicked.connect(self._do_forgot_password)
        box.addWidget(forgot)

        warn = QLabel(
            "忘了密码？用注册时寄到邮箱的那串「找回码」重设，成绩不会丢。\n"
            "但找回码只有那一封邮件里有 —— 删了就真打不开了。"
        )
        warn.setObjectName("Muted")
        warn.setWordWrap(True)
        box.addWidget(warn)

        box.addStretch(1)
        self._login_status = QLabel("")
        self._login_status.setObjectName("Muted")
        self._login_status.setWordWrap(True)
        box.addWidget(self._login_status)
        return page

    # --- 注册页 ---
    def _build_register_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        title = QLabel("注册")
        title.setObjectName("H1")
        box.addWidget(title)

        sub = QLabel(
            "账户只建在这台电脑上。密码用 PBKDF2 派生，磁盘上只留随机盐和校验值。"
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        box.addWidget(sub)
        box.addSpacing(12)

        warn = QLabel(
            "⚠ 请记住这个密码，并填一个能收信的邮箱。\n"
            "注册成功后会往这个邮箱寄一串「找回码」—— 忘了密码时它是唯一的出路。"
        )
        warn.setObjectName("Warn")
        warn.setWordWrap(True)
        box.addWidget(warn)
        box.addSpacing(16)

        box.addWidget(self._label("账号"))
        self._new_user = QLineEdit()
        self._new_user.setMinimumHeight(38)
        box.addWidget(self._new_user)
        box.addSpacing(12)

        # --- 邮箱 + 发送验证码（Spec：注册必须验证邮箱真实可靠）---
        box.addWidget(self._label("邮箱"))
        mail_row = QHBoxLayout()
        mail_row.setSpacing(8)
        self._new_email = QLineEdit()
        self._new_email.setPlaceholderText("用来接收找回码，例如 you@163.com")
        self._new_email.setMinimumHeight(38)
        mail_row.addWidget(self._new_email, 1)

        self._send_code_btn = QPushButton("发送验证码")
        self._send_code_btn.setObjectName("Ghost")
        self._send_code_btn.setMinimumHeight(38)
        self._send_code_btn.setMinimumWidth(120)
        self._send_code_btn.clicked.connect(self._do_send_code)
        mail_row.addWidget(self._send_code_btn)
        box.addLayout(mail_row)
        box.addSpacing(12)

        box.addWidget(self._label("邮箱验证码"))
        self._new_code = QLineEdit()
        self._new_code.setPlaceholderText(f"{EMAIL_CODE_LENGTH} 位数字，10 分钟内有效")
        self._new_code.setMinimumHeight(38)
        box.addWidget(self._new_code)
        box.addSpacing(12)

        box.addWidget(self._label("密码"))
        self._new_pass = QLineEdit()
        self._new_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_pass.setMinimumHeight(38)
        box.addWidget(self._new_pass)
        box.addSpacing(12)

        box.addWidget(self._label("确认密码"))
        self._new_pass2 = QLineEdit()
        self._new_pass2.setEchoMode(QLineEdit.EchoMode.Password)
        self._new_pass2.setMinimumHeight(38)
        self._new_pass2.returnPressed.connect(self._do_register)
        box.addWidget(self._new_pass2)
        box.addSpacing(18)

        buttons = QHBoxLayout()
        self._register_btn = QPushButton("注  册")
        self._register_btn.setObjectName("Primary")
        self._register_btn.setMinimumHeight(40)
        self._register_btn.setMinimumWidth(130)
        self._register_btn.clicked.connect(self._do_register)
        buttons.addWidget(self._register_btn)

        back = QPushButton("返回登录")
        back.setObjectName("Ghost")
        back.setMinimumHeight(40)
        back.setMinimumWidth(110)
        back.clicked.connect(lambda: self._goto_page(0))
        buttons.addWidget(back)
        buttons.addStretch(1)
        box.addLayout(buttons)

        box.addStretch(1)
        self._register_status = QLabel(
            f"密码至少 {MIN_PASSWORD_LENGTH} 位。先填邮箱点「发送验证码」。"
        )
        self._register_status.setObjectName("Muted")
        self._register_status.setWordWrap(True)
        box.addWidget(self._register_status)
        return page

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("H3")
        return label

    def _goto_page(self, index: int) -> None:
        """翻页。**必须同步把窗口尺寸重算一遍**（注册页比登录页高）。"""
        self._stack.setCurrentIndex(index)
        self._ensure_page_fits()
        if index == 0:
            self._pass_edit.setFocus()
        else:
            self._new_user.setFocus()

    # ---------------------------------------------------------------- 状态
    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._login_status.setObjectName("Danger" if error else "Muted")
        self._login_status.setText(text)
        # 换了 objectName 要重新应用样式表，否则颜色不变
        self._login_status.style().unpolish(self._login_status)
        self._login_status.style().polish(self._login_status)

    def _refresh_usernames(self) -> None:
        """把本机已有的用户名填进下拉框（**只有用户名**）。"""
        try:
            index = self._store.load_users()
        except StoreCorruptError as exc:
            self._set_status(f"读不到账户列表：{exc}", error=True)
            return
        names = [record.username for record in index]
        self._user_box.clear()
        self._user_box.addItems(names)
        if names:
            self._user_box.setCurrentText(names[0])
        else:
            self._set_status("这台电脑上还没有账户，请先点「去注册」。")

    def _hint_about_token(self) -> None:
        """上次的自动登录要是失效了，说一句为什么又让输密码。"""
        if self._user_box.currentText():
            return
        info = token_info(self._data_root)
        if info["reason"] == "expired":
            self._set_status(
                f"上次的自动登录已过期（{AUTO_LOGIN_DAYS} 天），请重新登录。"
            )
        elif info["reason"] in {"corrupt", "bad_payload"}:
            self._set_status("自动登录凭证已失效（文件损坏），请重新登录。")

    # ---------------------------------------------------------------- 动作
    def _do_forgot_password(self) -> None:
        """「忘记密码？」→ 打开找回对话框（用邮件里的找回码重设密码）。

        ★ 这里**延迟 import** ``ui.recover_dialog``：那个模块要 import 本模块的
          ``MIN_PASSWORD_LENGTH``，写在文件头上就成环了。
        """
        from ui.recover_dialog import RecoverDialog

        dialog = RecoverDialog(self._store, self._data_root, parent=self)
        if dialog.exec() == RecoverDialog.DialogCode.Accepted and dialog.username:
            self._user_box.setCurrentText(dialog.username)
            self._pass_edit.clear()
            self._set_status(
                f"「{dialog.username}」的密码已重设，请用新密码登录。"
            )
            if not self._remember.isChecked():
                clear_token(self._data_root)

    def _do_login(self) -> None:
        if self._leaving:
            return
        name = self._user_box.currentText().strip()
        password = self._pass_edit.text()
        if not name or not password:
            QMessageBox.warning(self, "还差一点", "账号和密码都要填。")
            return

        self._set_status("正在校验密码（PBKDF2 派生中，约 0.3 秒）…")
        try:
            session = self._store.login(name, password)
        except UnknownUserError:
            QMessageBox.warning(
                self,
                "没有这个账号",
                f"本机没有账户「{name}」。如果这是第一次用，请点「去注册」。",
            )
            self._set_status("登录失败：没有这个账号。", error=True)
            return
        except WrongPasswordError:
            QMessageBox.critical(
                self,
                "密码错误",
                "密码不对，请重新输入。\n\n"
                "本程序不存密码，当前版本也没有找回途径 —— "
                "密码想不起来，成绩库就打不开了。",
            )
            self._set_status("登录失败：密码错误。", error=True)
            return
        except CryptoError as exc:
            QMessageBox.critical(self, "登录失败", str(exc))
            self._set_status(f"登录失败：{exc}", error=True)
            return

        self._handle_remember(session.username)
        self._leave(session.username)

    def _handle_remember(self, username: str) -> None:
        """记 / 不记自动登录。失败不拦登录 —— 那只是便利功能。"""
        try:
            if self._remember.isChecked():
                save_token(username, root=self._data_root)
            else:
                # 没勾就顺手清掉旧的：否则上次的残留会让人
                # "明明没勾却还是免密码进来"
                clear_token(self._data_root)
        except (CryptoError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "自动登录没能设置",
                f"{exc}\n\n这次仍然正常登录，只是下次还需要输入密码。",
            )

    def _leave(self, username: str) -> None:
        """淡出自己，然后把 ``logged_in`` 发出去。

        ★ 先淡出再让位：直接 ``hide()`` 会"啪"地一下消失，
          而 Spec 要求登录窗淡出 + 主窗淡入的连贯转场。
        """
        self._leaving = True
        self._set_status(f"欢迎回来，{username}。正在进入…")

        def _after() -> None:
            self.hide()
            self.logged_in.emit(username)

        window_fade_out(self, 220, _after)

    # ------------------------------------------------------------ 发送验证码
    def _do_send_code(self) -> None:
        """把验证码寄到用户填的邮箱。

        ★ 网络请求走后台线程（见 :mod:`ui.workers`）—— 在按钮回调里直接
          ``sendmail()`` 的话，网络不通时窗口会冻满 15 秒超时。
        """
        try:
            email = normalize_email(self._new_email.text())
        except EmailCodeError as exc:
            QMessageBox.warning(self, "邮箱不对", str(exc))
            return

        self._send_code_btn.setEnabled(False)
        self._send_code_btn.setText("发送中…")
        self._set_register_status(f"正在往 {email} 发送验证码…")

        def _work() -> str:
            code = issue_email_code(email, purpose="注册", root=self._data_root)
            send_verification_mail(
                email, code, purpose="注册", root=self._data_root
            )
            return email

        def _done(target: str) -> None:
            self._send_code_btn.setText("重新发送")
            self._send_code_btn.setEnabled(True)
            self._set_register_status(
                f"验证码已发到 {target}，{EMAIL_CODE_TTL_MINUTES} 分钟内有效。"
                "没收到就看一眼垃圾邮件。"
            )
            self._new_code.setFocus()

        def _failed(exc: BaseException) -> None:
            self._send_code_btn.setText("发送验证码")
            self._send_code_btn.setEnabled(True)
            message = str(exc)
            self._set_register_status(f"发送失败：{message}", error=True)
            QMessageBox.critical(self, "验证码没能发出", message)

        run_async(self, _work, on_done=_done, on_error=_failed)

    # ---------------------------------------------------------------- 注册
    def _do_register(self) -> None:
        name = self._new_user.text().strip()
        password = self._new_pass.text()
        again = self._new_pass2.text()
        raw_email = self._new_email.text().strip()
        code = self._new_code.text().strip()

        if not name:
            QMessageBox.warning(self, "还差一点", "请填账号。")
            return
        try:
            email = normalize_email(raw_email)
        except EmailCodeError:
            QMessageBox.warning(
                self, "要填邮箱", "注册必须填一个能收信的邮箱 —— 找回码要寄到那里。"
            )
            return
        if not code:
            QMessageBox.warning(
                self, "还没验证邮箱", "请先点「发送验证码」，再把收到的 6 位数字填进来。"
            )
            return
        if len(password) < MIN_PASSWORD_LENGTH:
            QMessageBox.warning(
                self, "密码太短", f"密码至少 {MIN_PASSWORD_LENGTH} 位。"
            )
            return
        if password != again:
            QMessageBox.warning(self, "两次密码不一致", "请重新输入确认密码。")
            return

        self._register_btn.setEnabled(False)
        self._register_btn.setText("创建中…")
        self._set_register_status("正在校验验证码…")

        def _work() -> str:
            # ① 验证码（不对就抛，账户不会建）
            check_email_code(email, code, root=self._data_root)
            # ② 建账户
            session = self._store.register(name, password)
            # ③ ★ 立刻生成找回码并寄出 —— 库密钥此刻还在手里，
            #    这是唯一能埋下"第二把钥匙"的时机。
            recovery_code = enable_recovery(session, email, store=self._store)
            send_recovery_mail(
                email, recovery_code, username=name, root=self._data_root
            )
            return email

        def _done(target: str) -> None:
            self._register_btn.setEnabled(True)
            self._register_btn.setText("注  册")
            for widget in (self._new_user, self._new_email, self._new_code,
                           self._new_pass, self._new_pass2):
                widget.clear()
            self._refresh_usernames()
            self._user_box.setCurrentText(name)
            self._pass_edit.clear()
            self._stack.setCurrentIndex(0)
            self._set_status(f"账户「{name}」已建好，请输入密码登录。")
            QMessageBox.information(
                self,
                "注册成功 —— 请保留那封邮件",
                f"账户「{name}」已建好，找回码已经寄到：\n{target}\n\n"
                "那封邮件里有你的「找回码」，忘了密码时唯一的出路就是它。\n"
                "建议现在就把它星标或加个标签，别让它躺在垃圾邮件里。",
            )

        def _failed(exc: BaseException) -> None:
            self._register_btn.setEnabled(True)
            self._register_btn.setText("注  册")
            if isinstance(exc, EmailCodeError):
                message = str(exc)
                self._set_register_status(f"邮箱验证没过：{message}", error=True)
                QMessageBox.warning(self, "邮箱验证没过", message)
                return
            message = str(exc)
            if not isinstance(exc, (CryptoError, ValueError, RecoveryError)):
                message = f"{type(exc).__name__}: {exc}"
            self._set_register_status(f"注册失败：{message}", error=True)
            QMessageBox.critical(self, "注册失败", message)

        run_async(self, _work, on_done=_done, on_error=_failed)

    def _set_register_status(self, text: str, *, error: bool = False) -> None:
        self._register_status.setObjectName("Danger" if error else "Muted")
        self._register_status.setText(text)
        self._register_status.style().unpolish(self._register_status)
        self._register_status.style().polish(self._register_status)
        self._ensure_page_fits()

    # ---------------------------------------------------------------- 尺寸
    def _ensure_page_fits(self) -> None:
        """翻页后保证窗口装得下当前页。

        ★ 教训（tkinter 版栽过一次）：登录窗是**固定尺寸**、注册页比登录页高。
          只在构造时定一次尺寸的话，切到注册页底部就会被切掉 ——
          控件树里查得到、用户看不见。这里每次翻页都按当前页的
          ``sizeHint()`` 重新量一遍，不够就把窗口撑大。

        ★ 高度里**必须算上自定义标题栏**（``TITLE_BAR_HEIGHT``）：系统标题栏
          没了之后，那 36px 是我们自己加的一行，量漏了注册页底部同样会被切掉
          —— 和无边框改造前是同一个坑，换了个来源而已。
        """
        current = self._stack.currentWidget()
        if current is None:
            return
        hint = current.sizeHint()
        margins = self.layout().contentsMargins()
        need_w = hint.width() + margins.left() + margins.right() + _BRAND_WIDTH + 40
        need_h = (
            hint.height()
            + margins.top()
            + margins.bottom()
            + TITLE_BAR_HEIGHT
            + 40
        )
        if need_w > self.width() or need_h > self.height():
            self.resize(max(need_w, self.width()), max(need_h, self.height()))

    # ---------------------------------------------------------------- 起窗
    def run(self) -> None:
        """显示并淡入。"""
        self.show()
        window_fade_in(self, 240)
