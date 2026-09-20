"""主窗口：左侧固定 180px 侧边栏 + 右侧内容区。

结构
----
``MainWindow`` 是 :class:`ttkbootstrap.Window`（``themename="cosmo"``），
最小尺寸 1000×650。

* **侧边栏** 固定 180px（``pack_propagate(False)`` 撑住宽度），竖排导航按钮
* **内容区** 只有一个 Frame；切页 = 先把旧的 ``pack_forget()``，再 pack 新的
* 默认首页 = 智育分析

六个页面各自是 ``ui/page_*.py`` 里的一个 ``build_xxx_page(parent_frame)`` 函数，
本模块只负责"把它们挂进来 + 切来切去"，不含任何业务逻辑。

★ 侧边栏底部的「退出登录」不在你的页面清单里，是我加的，理由见
:meth:`MainWindow._do_logout` 的说明 —— 没有它，勾了自动登录就再也取消不掉。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

import ttkbootstrap as ttkb

from ui.page_academic import build_academic_page
from ui.page_scholarship import build_scholarship_page
from ui.page_stubs import (
    build_archive_page,
    build_outline_page,
    build_profile_page,
    build_settings_page,
)

__all__ = ["MainWindow", "NAV_ITEMS", "SIDEBAR_WIDTH", "THEME", "show_main"]

#: ttkbootstrap 主题名
THEME = "cosmo"

#: 侧边栏固定宽度（像素）
SIDEBAR_WIDTH = 180

#: 窗口标题
APP_NAME = "成绩单透镜 transcript-lens"

#: 导航项：``(key, 显示名, 页面构造函数)`` —— 顺序即侧边栏顺序
NAV_ITEMS: tuple[tuple[str, str, Callable[[tk.Misc], ttk.Frame]], ...] = (
    ("academic", "智育分析", build_academic_page),
    ("archive", "档案", build_archive_page),
    ("scholarship", "奖学金测算", build_scholarship_page),
    ("outline", "谈话提纲", build_outline_page),
    ("settings", "设置", build_settings_page),
    ("profile", "个人中心", build_profile_page),
)

#: 默认首页
DEFAULT_PAGE = "academic"

_SELECTED_STYLE = "secondary-outline"
_NORMAL_STYLE = "light"
_MUTED = "#6c757d"


class MainWindow(ttkb.Window):
    """主窗口。"""

    def __init__(self, user_id: str = "") -> None:
        super().__init__(themename=THEME)
        self.user_id = user_id or ""

        self.title(f"{APP_NAME}　—　{self.user_id}" if self.user_id else APP_NAME)
        self.minsize(1000, 650)
        self.geometry("1180x720")

        self._pages: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[str, ttkb.Button] = {}
        self._current: str = ""

        self._build_sidebar()
        self._build_content()
        self.show_page(DEFAULT_PAGE)

    # ------------------------------------------------------------------ 搭

    def _build_sidebar(self) -> None:
        sidebar = ttk.Frame(self, width=SIDEBAR_WIDTH)
        sidebar.pack(side="left", fill="y")
        # ★ 固定宽度：不加这一句，Frame 会被里面的按钮撑大，
        #   width=180 就成了"建议值"而不是"固定值"。
        sidebar.pack_propagate(False)
        self._sidebar = sidebar

        brand = ttk.Frame(sidebar)
        brand.pack(fill="x", padx=14, pady=(18, 16))
        ttk.Label(brand, text="成绩单透镜",
                  font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(brand, text="transcript-lens", foreground=_MUTED).pack(anchor="w")

        # 底部先占位（pack side=bottom 要在导航按钮之前，否则会被挤没）
        footer = ttk.Frame(sidebar)
        footer.pack(side="bottom", fill="x", padx=12, pady=12)
        ttk.Label(
            footer,
            text=f"当前账户：{self.user_id or '—'}",
            foreground=_MUTED, wraplength=SIDEBAR_WIDTH - 28, justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ttkb.Button(
            footer, text="退出登录", bootstyle="danger-outline",
            command=self._do_logout, width=16,
        ).pack(fill="x")

        nav = ttk.Frame(sidebar)
        nav.pack(side="top", fill="x", padx=10)

        for key, label, _builder in NAV_ITEMS:
            button = ttkb.Button(
                nav, text=label, bootstyle=_NORMAL_STYLE, width=16,
                command=lambda k=key: self.show_page(k),
            )
            button.pack(fill="x", pady=3)
            self._nav_buttons[key] = button

    def _build_content(self) -> None:
        self._content = ttk.Frame(self)
        self._content.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------- 切页

    def show_page(self, key: str) -> bool:
        """切到某一页：先 forget 旧的，再 pack 新的。"""
        if key == self._current:
            return True

        builders = {item[0]: item[2] for item in NAV_ITEMS}
        if key not in builders:
            return False

        old = self._pages.get(self._current)
        if old is not None:
            old.pack_forget()

        page = self._pages.get(key)
        if page is None:
            page = builders[key](self._content)
            self._pages[key] = page
        page.pack(fill="both", expand=True)

        self._current = key
        self._sync_nav()
        return True

    def _sync_nav(self) -> None:
        """选中态换 bootstyle —— 这一页用 Outline 样式高亮。"""
        for key, button in self._nav_buttons.items():
            button.configure(
                bootstyle=_SELECTED_STYLE if key == self._current else _NORMAL_STYLE
            )

    # ------------------------------------------------------------- 退出

    def _do_logout(self) -> None:
        """清掉「7 天内自动登录」，回到登录窗。

        ★ 为什么这个按钮必须存在（它不在你给的页面清单里）：

        ``session.enc`` 是**免密码进入**的凭证。如果界面上没有清除入口，
        用户勾了一次自动登录之后，除非自己去翻 AppData 删文件，否则**永远
        取消不掉** —— 那是个真坑，不是洁癖。

        它只删令牌，**不删任何成绩数据、不删账户、不删激活状态**。
        """
        from core.crypto_store import CryptoError
        from core.session_token import clear_token

        if not messagebox.askyesno(
            "退出登录",
            "将清除「7 天内自动登录」，下次启动需要重新输入密码。\n\n"
            "不会删除任何成绩数据、账户或激活状态。",
            parent=self,
        ):
            return

        try:
            clear_token()
        except CryptoError as exc:
            messagebox.showerror("清除失败", str(exc), parent=self)
            return

        self.destroy()
        show_login()


def show_main(user_id: str = "") -> None:
    """打开主窗口并阻塞到它关闭。登录窗和 ``main.py`` 都调这个。"""
    MainWindow(user_id).mainloop()


def show_login() -> None:
    """回到登录窗（退出登录后用）。函数内 import，避免两个窗口互相 import。"""
    from ui.login_window import LoginWindow

    LoginWindow().run()
