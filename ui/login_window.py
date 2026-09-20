"""登录窗：左侧品牌区 + 右侧表单，登录 / 注册在同一窗口里翻页。

布局::

    +----------------------+--------------------------------+
    |  成绩单透镜           |  账号  [ 下拉，只读 ]           |
    |  transcript-lens v1  |  密码  [ ●●●● ]                |
    |  slogan              |  ☐ 7天内自动登录    [登录]      |
    |  本地离线 · 不上传    |  还没有账户？[去注册]           |
    +----------------------+--------------------------------+

★ 三件必须说清楚的事

1. **账号下拉里只有用户名**，从 ``users.json`` 读；**没有密码，也没有密码提示**。
2. **「7 天内自动登录」默认不勾。** 勾上会在 AppData 写一个 ``session.enc``，
   它能让人**免密码**进程序（这正是自动登录的定义）。这种事必须是用户明确选的，
   不能默认替他选 —— 所以默认不勾。
3. **登录成功不在这里算任何东西。** 本窗口只做身份确认，然后把
   ``show_main(user_id)`` 叫起来。算术在智育分析页，且全在本地。

★ 找回密码的状态（2026-09 阶段）

**本阶段没有任何找回途径**，这是产品决定，不是没做完：

* 恢复码已按决定**整体删除**（``core/crypto_store.py`` 里一行不剩）。
* 邮箱找回**只预留了接口**（``core/email_recovery.py``），不会真的发信。
* 「忘记密码？」按钮**已经摆出来**，点开只会说明现状 ——
  下一阶段的邮箱流程会长在那上面。

登录页和注册页各有**一处明确提示**，把"忘了就打不开了"这句话说到用户脸上。
不能让用户以为还有后路。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import ttkbootstrap as ttkb

from core.crypto_store import (
    CryptoError,
    CryptoStore,
    StoreCorruptError,
    UnknownUserError,
    WrongPasswordError,
)
from core.session_token import AUTO_LOGIN_DAYS, clear_token, save_token, token_info
from ui.main_window import APP_NAME, THEME, show_main

__all__ = ["APP_VERSION", "LoginWindow", "run_login"]

#: 版本号（显示在左栏）
APP_VERSION = "v1.0"

#: slogan
SLOGAN = "把成绩单，变成能坐下来谈的东西"

#: 左栏宽度
_BRAND_WIDTH = 260

#: 窗口最小尺寸。实际尺寸由 :meth:`LoginWindow._fit_to_content` 按内容算 ——
#: **不要**再手写 ``geometry("820x500")``，那样加内容会被静默截断。
_MIN_WIDTH = 820
_MIN_HEIGHT = 500

_MUTED = "#6c757d"
_TITLE_FONT = ("Microsoft YaHei UI", 20, "bold")
_BRAND_FONT = ("Microsoft YaHei UI", 15, "bold")

#: 口令最短长度（和 core.crypto_store 一脉相承：本地单机，不做复杂度评分）
MIN_PASSWORD_LENGTH = 8


class LoginWindow(ttkb.Window):
    """登录 / 注册窗口。"""

    def __init__(self, store: CryptoStore | None = None, root: str | None = None) -> None:
        super().__init__(themename=THEME)
        #: 数据根目录。``None`` = 默认 AppData/Local/SGMS；
        #: 自检时指向临时目录，避免往真实 AppData 里写东西。
        #:
        #: ★ 名字**不能**叫 ``self._root``：``tk.Misc`` 自己有个 ``_root()`` 方法
        #:   （返回根 widget），拿一个 str 覆盖它之后，任何 ``tk.StringVar()``
        #:   都会在 ``master._root()`` 上炸 ``'str' object is not callable``。
        #:   这个坑第一次跑窗口就中了 —— 静态检查一点都看不出来。
        self._data_root = root
        self._store = store if store is not None else CryptoStore(root)
        #: 登录成功后的用户名（给 :meth:`run` 返回用；也便于自检）
        self.entered_user: str | None = None

        self.title(f"{APP_NAME}　—　登录")
        self.minsize(_MIN_WIDTH, _MIN_HEIGHT)
        self.resizable(False, False)

        self._build_layout()
        self._build_brand()
        self._build_login_page()
        self._build_register_page()
        self.show_login_page()
        self._refresh_usernames()

        # ★ 必须**在所有控件建完之后**再定尺寸（见 _fit_to_content 的说明）
        self._autofit_on_switch = True
        self._fit_to_content()

    def _fit_to_content(self) -> None:
        """按内容实际需要的大小定窗口尺寸。

        ★ 这里原先写死 ``self.geometry("820x500")``，而且
          ``resizable(False, False)`` 让它改不了。后来登录页加了一个
          「忘记密码？」按钮和两行说明，内容长到 **639px**，窗口却还是 500px：

          * 按钮被切在可视区之外 —— **控件树里查得到、用户看不见**；
          * 分配高度被挤成 3px（同一个按钮单独量是正常的 44px）。

        「写死尺寸 + 禁用缩放」的界面，每加一行字都得重新手量一遍，而漏量的
        后果是静默截断。改成按需计算之后，以后再加内容会自动撑开。
        """
        self.update_idletasks()
        width = max(_MIN_WIDTH, self.winfo_reqwidth())
        height = max(_MIN_HEIGHT, self.winfo_reqheight())
        self.geometry(f"{width}x{height}")

    # ------------------------------------------------------------------ 搭

    def _build_layout(self) -> None:
        self._brand = ttk.Frame(self, width=_BRAND_WIDTH)
        self._brand.pack(side="left", fill="y")
        self._brand.pack_propagate(False)     # 固定宽度

        ttk.Separator(self, orient="vertical").pack(side="left", fill="y")

        self._right = ttk.Frame(self, padding=(34, 30))
        self._right.pack(side="left", fill="both", expand=True)

    def _build_brand(self) -> None:
        brand = ttk.Frame(self._brand, padding=(22, 30))
        brand.pack(fill="both", expand=True)

        ttk.Label(brand, text="成绩单透镜", font=_BRAND_FONT).pack(anchor="w")
        ttk.Label(
            brand, text=f"transcript-lens {APP_VERSION}", foreground=_MUTED,
        ).pack(anchor="w", pady=(2, 18))

        ttk.Label(
            brand, text=SLOGAN, wraplength=_BRAND_WIDTH - 50, justify="left",
            font=("Microsoft YaHei UI", 11),
        ).pack(anchor="w")

        ttk.Separator(brand).pack(fill="x", pady=18)

        for line in (
            "本地离线运行",
            "不上传、不联网",
            "加权算术全部本地完成",
            "成绩只存加密文件",
        ):
            ttk.Label(brand, text=f"· {line}", foreground=_MUTED).pack(
                anchor="w", pady=1
            )

    # --- 登录页 ---
    def _build_login_page(self) -> None:
        page = ttk.Frame(self._right)
        self._login_page = page

        ttk.Label(page, text="登录", font=_TITLE_FONT).pack(anchor="w")
        ttk.Label(
            page, text="选择本机已有的账户，输入密码。", foreground=_MUTED,
        ).pack(anchor="w", pady=(4, 20))

        ttk.Label(page, text="账号").pack(anchor="w")
        self._user_var = tk.StringVar()
        self._user_box = ttk.Combobox(
            page, textvariable=self._user_var, state="readonly", width=34,
        )
        self._user_box.pack(anchor="w", pady=(4, 12))

        ttk.Label(page, text="密码").pack(anchor="w")
        self._pass_var = tk.StringVar()
        pass_entry = ttk.Entry(
            page, textvariable=self._pass_var, show="●", width=36,
        )
        pass_entry.pack(anchor="w", pady=(4, 12))
        pass_entry.bind("<Return>", lambda _e: self._do_login())

        self._remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            page, text=f"{AUTO_LOGIN_DAYS} 天内自动登录", variable=self._remember_var,
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            page,
            text=f"勾选后本机会保存一个免密码凭证（{AUTO_LOGIN_DAYS} 天有效）。\n"
                 "共用电脑上请不要勾。",
            foreground=_MUTED, justify="left",
        ).pack(anchor="w", pady=(2, 16))

        buttons = ttk.Frame(page)
        buttons.pack(anchor="w")
        ttkb.Button(
            buttons, text="登录", bootstyle="primary", width=12,
            command=self._do_login,
        ).pack(side="left")
        ttkb.Button(
            buttons, text="去注册", bootstyle="secondary-outline", width=12,
            command=self.show_register_page,
        ).pack(side="left", padx=(10, 0))

        # --- 找回密码：按钮先摆出来，逻辑下一阶段接邮箱 API 时再填 ---
        # 刻意把话说全：现在点它**什么也找不回**，不能让用户以为还有出路。
        ttkb.Button(
            page, text="忘记密码？", bootstyle="link",
            command=self._do_forgot_password,
        ).pack(anchor="w", pady=(14, 0))
        ttk.Label(
            page,
            text="本地不存密码，也没有任何找回途径。\n"
                 "忘了密码，这台电脑上的成绩库就打不开了。",
            foreground=_MUTED, wraplength=400, justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._login_status = ttk.Label(page, text="", foreground=_MUTED)
        self._login_status.pack(anchor="w", pady=(16, 0))

    # --- 注册页 ---
    def _build_register_page(self) -> None:
        page = ttk.Frame(self._right)
        self._register_page = page

        ttk.Label(page, text="注册", font=_TITLE_FONT).pack(anchor="w")
        ttk.Label(
            page,
            text="账户只建在这台电脑上。密码用 PBKDF2 派生，"
                 "磁盘上只留随机盐和校验值。",
            foreground=_MUTED, wraplength=420, justify="left",
        ).pack(anchor="w", pady=(4, 8))

        # ★ 唯一能提前警告用户的时机。等他把密码忘了再说就晚了。
        ttk.Label(
            page,
            text="⚠ 请记住这个密码。本地不保存它，当前版本也没有任何找回途径，"
                 "忘了就打不开成绩库了。",
            foreground="#9B1C1C", wraplength=420, justify="left",
        ).pack(anchor="w", pady=(0, 20))

        ttk.Label(page, text="账号").pack(anchor="w")
        self._new_user_var = tk.StringVar()
        ttk.Entry(page, textvariable=self._new_user_var, width=36).pack(
            anchor="w", pady=(4, 12)
        )

        ttk.Label(page, text="密码").pack(anchor="w")
        self._new_pass_var = tk.StringVar()
        ttk.Entry(page, textvariable=self._new_pass_var, show="●", width=36).pack(
            anchor="w", pady=(4, 12)
        )

        ttk.Label(page, text="确认密码").pack(anchor="w")
        self._new_pass2_var = tk.StringVar()
        entry2 = ttk.Entry(
            page, textvariable=self._new_pass2_var, show="●", width=36,
        )
        entry2.pack(anchor="w", pady=(4, 12))
        entry2.bind("<Return>", lambda _e: self._do_register())

        buttons = ttk.Frame(page)
        buttons.pack(anchor="w", pady=(6, 0))
        ttkb.Button(
            buttons, text="注册", bootstyle="primary", width=12,
            command=self._do_register,
        ).pack(side="left")
        ttkb.Button(
            buttons, text="返回登录", bootstyle="secondary-outline", width=12,
            command=self.show_login_page,
        ).pack(side="left", padx=(10, 0))

        ttk.Label(
            page,
            text=f"密码至少 {MIN_PASSWORD_LENGTH} 位。\n"
                 "本程序没有服务器，密码不保存在任何地方 —— "
                 "当前版本也没有可用的找回途径。",
            foreground=_MUTED, wraplength=420, justify="left",
        ).pack(anchor="w", pady=(16, 0))

    # ------------------------------------------------------------- 翻页

    #: 翻页时要不要跟着重算窗口大小。
    #: ``__init__`` 里建控件的过程中会翻一次页，那时窗口还没定位，
    #: 提前 geometry() 会让窗口在屏幕角落闪一下 —— 所以先关掉，
    #: 等 ``_fit_to_content()`` 在构造末尾统一算一次。
    _autofit_on_switch = False

    def show_login_page(self) -> None:
        self._register_page.pack_forget()
        self._login_page.pack(fill="both", expand=True)
        self._hint_about_token()
        self._refit_if_switching()

    def show_register_page(self) -> None:
        self._login_page.pack_forget()
        self._register_page.pack(fill="both", expand=True)
        self._refit_if_switching()

    def _refit_if_switching(self) -> None:
        """两页高度不一样，翻页后要按当前页重算窗口高度。

        登录页和注册页的控件数量不同（注册页多一个「确认密码」和一段警告），
        而窗口是 ``resizable(False, False)``。**只在构造时算一次是不够的** ——
        切到更高的那一页，底部照样会被切掉。
        """
        if self._autofit_on_switch:
            self._fit_to_content()

    def _refresh_usernames(self) -> None:
        """把本机已有的用户名填进下拉框（**只有用户名**）。"""
        try:
            index = self._store.load_users()
        except StoreCorruptError as exc:
            self._login_status.configure(text=f"读不到账户列表：{exc}", foreground="#9B1C1C")
            return
        names = [record.username for record in index]
        self._user_box.configure(values=names)
        if names:
            self._user_var.set(names[0])
        else:
            self._login_status.configure(
                text="这台电脑上还没有账户，请先点「去注册」。", foreground=_MUTED
            )

    def _hint_about_token(self) -> None:
        """如果上次的自动登录失效了，说一句为什么又让输密码。"""
        if self._user_var.get():
            return
        info = token_info(self._data_root)
        if info["reason"] == "expired":
            self._login_status.configure(
                text=f"上次的自动登录已过期（{AUTO_LOGIN_DAYS} 天），请重新登录。",
                foreground=_MUTED,
            )
        elif info["reason"] in {"corrupt", "bad_payload"}:
            self._login_status.configure(
                text="自动登录凭证已失效（文件损坏），请重新登录。", foreground=_MUTED
            )

    def _status(self, text: str, *, error: bool = False) -> None:
        self._login_status.configure(
            text=text, foreground="#9B1C1C" if error else _MUTED
        )
        self.update_idletasks()

    # ------------------------------------------------------------- 动作

    def _do_forgot_password(self) -> None:
        """「忘记密码？」——**本阶段故意什么都不做**。

        这个按钮是**提前摆出来的位置**：下一阶段接邮箱 API 之后，流程会长在这里
        （注册时把一份恢复材料寄到邮箱 → 忘密码时凭它解出库密钥 → 设新密码）。

        ★ 现在绝不能假装"已经受理"。本地不存密码、不存库密钥明文，
          本阶段**确实没有任何找回途径** —— 用户此刻最需要知道的恰恰是这个，
          而不是一句会让他在几天后才发现上当的"请稍候"。
        """
        messagebox.showinfo(
            "找回密码：尚未开放",
            "本功能还在开发中，当前版本没有任何找回密码的途径。\n\n"
            "原因：密码不保存在任何地方，成绩库的密钥只由它派生。\n"
            "本地没有、云端也没有，所以谁也「重置」不了它。\n\n"
            "后续版本会支持「用注册邮箱找回」。届时需要在注册时就绑定邮箱 ——\n"
            "事到临头再绑定是来不及的。\n\n"
            "在此之前，请务必记住密码。",
            parent=self,
        )
        self._status("找回密码功能尚未开放。")

    def _do_login(self) -> None:
        name = self._user_var.get().strip()
        password = self._pass_var.get()
        if not name or not password:
            messagebox.showwarning("还差一点", "账号和密码都要填。", parent=self)
            return

        self._status("正在校验密码（PBKDF2 派生中，约 0.3 秒）…")
        try:
            session = self._store.login(name, password)
        except UnknownUserError:
            messagebox.showwarning(
                "没有这个账号",
                f"本机没有账户「{name}」。如果这是第一次用，请点「去注册」。",
                parent=self,
            )
            self._status("登录失败：没有这个账号。", error=True)
            return
        except WrongPasswordError:
            messagebox.showerror(
                "密码错误",
                "密码不对，请重新输入。\n\n"
                "本程序不存密码，当前版本也没有找回途径 —— "
                "密码想不起来，成绩库就打不开了。",
                parent=self,
            )
            self._status("登录失败：密码错误。", error=True)
            return
        except CryptoError as exc:
            messagebox.showerror("登录失败", str(exc), parent=self)
            self._status(f"登录失败：{exc}", error=True)
            return

        self._handle_remember(session.username)

        self.entered_user = session.username
        self.destroy()
        show_main(session.username)

    def _handle_remember(self, username: str) -> None:
        """记 / 不记自动登录。失败不拦登录 —— 那只是便利功能。"""
        try:
            if self._remember_var.get():
                save_token(username, root=self._data_root)
            else:
                # 没勾就顺手清掉旧的：否则上次的残留会让人"明明没勾却还是免密码进来"
                clear_token(self._data_root)
        except (CryptoError, ValueError) as exc:
            messagebox.showwarning(
                "自动登录没能设置",
                f"{exc}\n\n这次仍然正常登录，只是下次还需要输入密码。",
                parent=self,
            )

    def _do_register(self) -> None:
        name = self._new_user_var.get().strip()
        password = self._new_pass_var.get()
        if not name:
            messagebox.showwarning("请填账号", "账号不能为空。", parent=self)
            return
        if len(password) < MIN_PASSWORD_LENGTH:
            messagebox.showwarning(
                "密码太短",
                f"至少 {MIN_PASSWORD_LENGTH} 位。这是唯一能打开加密数据的钥匙。",
                parent=self,
            )
            return
        if password != self._new_pass2_var.get():
            messagebox.showwarning("两次密码不一致", "请重新输入确认密码。", parent=self)
            return

        try:
            self._store.register(name, password)
        except CryptoError as exc:
            messagebox.showerror("注册失败", str(exc), parent=self)
            return

        self._new_pass_var.set("")
        self._new_pass2_var.set("")
        self._new_user_var.set("")
        self._refresh_usernames()
        self._user_var.set(name)
        self.show_login_page()
        self._status(f"账户「{name}」已创建，请输入密码登录。")
        messagebox.showinfo(
            "注册成功",
            f"已在本机创建账户「{name}」。\n\n"
            "磁盘上只写了随机盐和校验值，没有密码明文，也没有任何上传。\n"
            "接下来请输入密码登录。",
            parent=self,
        )

    # ------------------------------------------------------------- 入口

    def run(self) -> str | None:
        """开窗并阻塞到关闭。返回登录成功的用户名（直接关窗则返回 ``None``）。"""
        self.mainloop()
        return self.entered_user


def run_login(root: str | None = None) -> str | None:
    """开一个登录窗。"""
    return LoginWindow(root=root).run()
