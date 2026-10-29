"""主界面 —— 无边框窗口 + 深色侧栏 + 顶部窄条（兼自定义标题栏） + 内容区。

     ╭──────────────────────────────────────────────────────────╮  ← 圆角 12px + 阴影
     │ 成绩单    |  张老师                            📊 智育分析  — □ ✕ │
     │ 透镜      ├──────────────────────────────────────────────┤
     │           │                                              │
     │ 📊 智育   │                                              │
     │ 🎯 侧写   │           当前页面（QStackedWidget）           │
     │ 📈 档案   │                                              │
     │ 💰 奖学   │                                              │
     │ ⚙️ 设置   │                                              │
     │ 👤 个人   │                                              │
     ╰──────────────────────────────────────────────────────────╯

★ 顶栏就是自定义标题栏
----------------------

系统标题栏被 :func:`ui.frameless.apply_frameless` 拿掉了，所以"拖窗口 /
最小化 / 最大化 / 关闭"都得自己做。做法是把最上面那条顶栏直接当标题栏用
（``WindowTitleBar`` 的 ``content`` 参数塞的就是那一排用户名 + 当前页名），
而不是再加一条空条 —— 那样白占 36px 高度，还难看。

★ 顶栏**只有用户名和当前页名**：v1.8 删掉了整套激活码系统，所以原来那个
  "● 已激活"小圆点连同它的文字一起没了。注册登录之后所有功能即可用，
  界面上不该再有任何"解锁 / 激活"的字样。

★ 圆角不会裁剪子控件
--------------------

窗口容器有 12px 圆角，但 QSS 的 ``border-radius`` **不会**把子控件裁掉。
所以贴着窗口边的两块 —— 左侧栏（左上/左下）和顶栏（右上）——
在 QSS 里各自带了对应角的圆角；最大化时由 :mod:`ui.frameless` 统一取消。

★ 切页动画走 :class:`ui.anim.StackSwitcher`：淡出旧页 → setCurrentIndex →
  淡入新页，单次 ≤250ms，并带 ``_busy`` 锁防连点（Spec 四）。
★ 侧栏选中态由 QSS 的 ``QPushButton#NavButton:checked`` 负责 ——
  左侧 4px 蓝线 + 半透明浅蓝底。
★ 点侧栏「🎯 侧写辅导」**不会替你挑学生** —— 只落到名单上，看谁由你在名单里点。
"""

from __future__ import annotations

import logging
import traceback

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.crypto_store import CryptoStore
from core.version import CURRENT_VERSION
from ui.anim import StackSwitcher, window_fade_in
from ui.frameless import (
    SHADOW_MARGIN,
    WindowTitleBar,
    apply_frameless,
    build_root_frame,
    install_chrome,
    toggle_maximize,
)
from ui.intentions import load_intentions
from ui.pages import (
    AccountPage,
    AnalysisPage,
    ArchivePage,
    ProfilePage,
    ScholarshipPage,
    SettingsPage,
)

__all__ = ["APP_NAME", "MainWindow", "NAV_ITEMS", "PROFILE_NAV_KEY", "TOPBAR_HEIGHT"]

#: 本模块的日志器。补档失败**只记 warning**，绝不影响导入结果、绝不弹窗。
_log = logging.getLogger(__name__)

#: 程序名（侧栏顶部）
APP_NAME = "成绩单透镜"

#: 侧栏宽度（Spec 四：固定 190px）
SIDEBAR_WIDTH = 190

#: 顶栏（兼标题栏）高度
TOPBAR_HEIGHT = 54

#: 主窗淡入时长。
#:
#: 原来 260ms。它跟欢迎页的淡出**首尾相接**，两段加起来快半秒，
#: 而这两段传达的是同一件事（"换界面了"）。200ms 足够看清是淡入而不是闪现。
WINDOW_FADE_IN_MS = 200

#: 导航项：``(键, 图标+文字)``。顺序即侧栏顺序。
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("analysis", "📊  智育分析"),
    ("profile", "🎯  侧写辅导"),
    ("archive", "📈  成长档案"),
    ("scholarship", "💰  奖学金测算"),
    ("settings", "⚙️  设置"),
    ("account", "👤  个人中心"),
)

#: 侧写辅导那一页的键（点它只落名单，不替你挑学生）
PROFILE_NAV_KEY = "profile"

#: 侧写辅导页在 ``NAV_ITEMS`` 里的下标（从同一张表推导，不另写死数字）
PROFILE_PAGE_INDEX = next(
    position
    for position, (key, _title) in enumerate(NAV_ITEMS)
    if key == PROFILE_NAV_KEY
)

#: 默认落在哪一页（Spec 四：默认首页 = 智育分析）
DEFAULT_PAGE = 0


class MainWindow(QMainWindow):
    """主窗口。

    Signals:
        switch_account(): 用户在个人中心点了「切换账号」。
            主程序接住它去清令牌 + 起登录窗。
    """

    switch_account = Signal()

    def __init__(self, store: CryptoStore | None = None, root: str | None = None) -> None:
        super().__init__()
        self._store = store if store is not None else CryptoStore(root)
        self._data_root = root
        self._username = ""

        self.setWindowTitle(f"{APP_NAME} · transcript-lens v{CURRENT_VERSION}")
        # ★ 无边框 + 透明底必须在 show() 之前设好
        apply_frameless(self)
        # 最小/初始尺寸指**可见部分**，四周另有一圈透明边留给阴影
        self.setMinimumSize(1100 + 2 * SHADOW_MARGIN, 700 + 2 * SHADOW_MARGIN)
        self.resize(1240 + 2 * SHADOW_MARGIN, 780 + 2 * SHADOW_MARGIN)
        self.setWindowOpacity(0.0)

        self._build()
        self._wire()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        self._root = build_root_frame(central, name="WindowRoot")

        body = QHBoxLayout(self._root)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._sidebar = self._build_sidebar()
        body.addWidget(self._sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self._topbar = self._build_topbar()
        right.addWidget(self._topbar)

        self._stack = QStackedWidget()
        self._stack.setObjectName("Content")
        right.addWidget(self._stack, 1)
        body.addLayout(right, 1)

        # --- 六个页面 ---
        self.analysis_page = AnalysisPage(self._store)
        self.profile_page = ProfilePage(self._store)
        self.archive_page = ArchivePage(self._store)
        self.scholarship_page = ScholarshipPage(self._store)
        self.settings_page = SettingsPage(self._store, self._data_root)
        self.account_page = AccountPage(self._store, self._data_root)

        for page in (
            self.analysis_page,
            self.profile_page,
            self.archive_page,
            self.scholarship_page,
            self.settings_page,
            self.account_page,
        ):
            self._stack.addWidget(page)

        # --- 切页动画（淡出 → 换 index → 淡入，带防连点锁）---
        self._switcher = StackSwitcher(self._stack, self)
        self._switcher.on_switched(self._sync_nav)

        # --- 无边框的窗口补丁：边缘缩放 + 最大化时收边距 ---
        install_chrome(
            self,
            self._root,
            maximizable=True,
            corners=[self._sidebar, self._topbar],
        )

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(SIDEBAR_WIDTH)

        box = QVBoxLayout(side)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        # 品牌区
        brand = QWidget()
        brand_box = QVBoxLayout(brand)
        brand_box.setContentsMargins(18, 20, 14, 16)
        brand_box.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("BrandTitle")
        brand_box.addWidget(title)
        sub = QLabel("transcript-lens")
        sub.setObjectName("BrandSub")
        brand_box.addWidget(sub)
        box.addWidget(brand)

        # 导航
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        for index, (_key, text) in enumerate(NAV_ITEMS):
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(44)
            button.clicked.connect(lambda _=False, i=index: self._nav_to(i))
            self._nav_group.addButton(button, index)
            self._nav_buttons.append(button)
            box.addWidget(button)

        box.addStretch(1)

        foot = QLabel("本地离线 · 成绩不出这台电脑")
        foot.setObjectName("BrandSub")
        foot.setWordWrap(True)
        foot.setContentsMargins(18, 8, 14, 16)
        box.addWidget(foot)
        return side

    def _build_topbar(self) -> WindowTitleBar:
        """顶栏 = 自定义标题栏。

        左边是用户名，右边是当前页名；
        中间 ``addStretch(1)`` 顶开。
        三个窗口按钮由 :class:`ui.frameless.WindowTitleBar` 自己加。

        ★ v1.9.1 起**不再有**「AI 谈话助手」入口（Owner 决定）：全局入口删掉，
          谈话能力收进成长档案页的「AI 辅导」标签页（见
          :mod:`ui.pages.profile_page`）。:class:`ui.chat_dialog.ChatDialog`
          本身保留 —— 它的 ``open_chat`` 是 §34 的冻结接口。
        """
        strip = QWidget()
        box = QHBoxLayout(strip)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        self._user_label = QLabel("（未登录）")
        self._user_label.setObjectName("TopUser")
        box.addWidget(self._user_label)

        box.addStretch(1)

        self._page_title = QLabel("")
        self._page_title.setObjectName("TopMeta")
        box.addWidget(self._page_title)

        bar = WindowTitleBar(
            "",
            minimizable=True,
            maximizable=True,
            closable=True,
            content=strip,
            bar_height=TOPBAR_HEIGHT,
            object_name="TopBar",
        )
        bar.minimize_requested.connect(self.showMinimized)
        bar.maximize_requested.connect(lambda: toggle_maximize(self))
        bar.close_requested.connect(self.close)
        return bar

    # ---------------------------------------------------------------- 接线
    def _wire(self) -> None:
        # 智育分析页双击/点「侧写」→ 切到成绩档案页，由它选学期、再开学期侧写
        self.analysis_page.student_selected.connect(self._open_profile)
        # 名单推给侧写页（它靠这个建左侧列表）；★ v1.9.2 顺手补进学生主档
        self.analysis_page.roster_changed.connect(self._on_roster_changed)
        # 侧写页按行号取原料 —— 不复制数据，两个页面看的是同一份结果
        self.profile_page.set_payload_provider(self.analysis_page.payload_at)
        # 个人中心「切换账号」
        self.account_page.switch_account.connect(self.switch_account.emit)
        # 设置页导出用的数据来自智育分析页（不复制数据，只给个取值函数）
        self.settings_page.set_export_provider(self.analysis_page.export_records)
        # ★ 导入完一个班 → 成长档案 / 奖学金两页立刻能选到它，不用切页重进
        self.analysis_page.class_saved.connect(self._on_class_saved)
        # ★ v1.9.1：设置页删完成绩库 → 别的页面读的是**同一个库**，
        #   不刷新就会继续显示已经删掉的学生（AGENTS §18：一处真源 + 信号刷新）。
        #   ★ 这条信号原先**没有接收方**（第一波只把发射端做出来了）。
        self.settings_page.library_cleared.connect(self._on_library_cleared)

    # ---------------------------------------------------------------- 对外
    def set_user(self, username: str) -> None:
        """设置当前用户，并把六个页面各自的用户相关状态刷新一遍。"""
        self._username = username or ""
        self._user_label.setText(self._username or "（未登录）")
        self.analysis_page.set_user(self._username)
        self.profile_page.set_user(self._username)
        self.archive_page.set_user(self._username)
        self.scholarship_page.set_user(self._username)
        self.settings_page.set_user(self._username)
        self.account_page.set_user(self._username)

    def _on_class_saved(self, class_name: str) -> None:
        """刚导入了一个班 —— 让奖学金页和成长档案页立刻能看到新数据。

        ★ 这里**只是刷新**，不替用户选中、也不自动跳页。
          用户刚导完成绩，多半还想继续看智育分析。
        ★ 刷新失败**只打日志、不弹窗**：数据已经存进去了，
          某一页没刷新出来是显示问题，不该打断用户看成绩。
        """
        try:
            self.scholarship_page.reload_classes(select=class_name)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        try:
            self.archive_page.reload()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _on_library_cleared(self, grade_rows: int, profile_rows: int, files: int) -> None:
        """设置页「删除成绩库」之后的广播（接 ``SettingsPage.library_cleared``）。

        ★ v1.9.1 接的线：这条信号原先**没有接收方** —— 删完之后成长档案页还在
          列着已经删掉的学生（名单是智育分析页推进内存的，见 §18）。
        ★ 刷新失败**只打日志、不弹窗**：删除本身已经成功了，某一页没刷新出来
          是显示问题，不该打断用户。
        ★ 一律 ``getattr`` 探一下再调：各页方法名不一样，缺一个不该让整条
          刷新链断在半路上。

        Args:
            grade_rows: 删掉了几条成绩记录。
            profile_rows: 删掉了几份侧写。
            files: 删掉了几个文件。
        """
        # ★ v1.9.2：档案页是两级视图（详情页 / 学期侧写页）。清库后优先用
        #   ``reset_view()`` 把**两级都复位**，否则用户切回详情页还能看到上一个
        #   学生的学期侧写。组件还没落地时退回 ``show_no_data()``。
        profile_reset = getattr(self.profile_page, "reset_view", None)
        if not callable(profile_reset):
            profile_reset = getattr(self.profile_page, "show_no_data", None)
        if callable(profile_reset):
            try:
                profile_reset()
            except Exception:  # noqa: BLE001 - 刷新失败只打日志（§18）
                traceback.print_exc()

        for target, method in (
            # 这两个是"重新从库里读一遍"，清库之后读出来自然是空的
            (self.archive_page, "reload"),
            (self.scholarship_page, "reload_classes"),
        ):
            refresh = getattr(target, method, None)
            if not callable(refresh):
                continue
            try:
                refresh()
            except Exception:  # noqa: BLE001 - 刷新失败只打日志（§18）
                traceback.print_exc()
        print(
            f"[MainWindow] 成绩库已清空（成绩 {grade_rows} 条 / 侧写 {profile_rows} 份 / "
            f"文件 {files} 个），相关页面已重新读库。"
        )

    def goto(self, index: int) -> None:
        """切到第 ``index`` 页（带动画）。"""
        if not self._switcher.switch(index):
            # 被 _busy 锁吃掉或本来就在这页 —— 把选中态拨回去，别让界面说谎
            self._sync_nav(self._stack.currentIndex())

    def show_and_fade_in(self, username: str) -> None:
        """显示窗口 + 淡入。登录窗淡出之后由主程序调这个。

        ★ 窗口可能**已经被 `AppFlow._prewarm_main` 提前 show 过**（透明度 0）。
          那种情况下这里的 ``show()`` 是空操作，只剩一次 opacity 0→1 ——
          这正是预热想要的结果，不用特判。
        """
        self.set_user(username)
        self._stack.setCurrentIndex(DEFAULT_PAGE)
        self._sync_nav(DEFAULT_PAGE)
        self.show()
        window_fade_in(self, WINDOW_FADE_IN_MS)

    # ---------------------------------------------------------------- 内部
    def _nav_to(self, index: int) -> None:
        """侧栏点击。

        ★ 「🎯 侧写辅导」只把焦点交给**名单**，**不替你挑学生** ——
          以前点侧栏会落到上一次那一名学生身上，用户看到的是别人的数据。
        """
        self.goto(index)
        if 0 <= index < len(NAV_ITEMS) and NAV_ITEMS[index][0] == PROFILE_NAV_KEY:
            self.profile_page.focus_list()

    def _open_profile(self, payload: dict) -> None:
        """从智育分析页跳成绩档案页 —— 这一次是**明确指定了人**，就该选中他。

        ★ v1.9.2：档案页变成两级视图（详情页 / 学期侧写页）。导入页的
          「侧写」按钮只把 payload 交过来，**选哪个学期由用户在档案页里选**
          （``ProfilePage.ask_semester_and_open``）—— 这里不再自己弹任何侧写窗口。

        ★ 顺序：先把页切过去 → 再落学生 → 最后**延后一拍**弹学期选择。
          切页动画还没落位就弹模态框，会把动画卡在半路（AGENTS §11 / §4k）。
        """
        self._nav_to(PROFILE_PAGE_INDEX)
        self.profile_page.show_student(payload)
        ask = getattr(self.profile_page, "ask_semester_and_open", None)
        if callable(ask):
            QTimer.singleShot(0, lambda: self._ask_semester_and_open(ask, payload))

    def _ask_semester_and_open(self, ask, payload: dict) -> None:
        """``ProfilePage.ask_semester_and_open`` 的延后调用。

        ★ 用 ``getattr`` 探测过才排这一拍（组件可能还没落地）；
          这里再包一层 try/except —— 弹窗出错**只打日志**，不许冒到事件循环里。
        """
        try:
            ask(payload)
        except Exception:  # noqa: BLE001 - 弹窗失败只打日志
            traceback.print_exc()

    def _on_roster_changed(self, rows: list) -> None:
        """导入完成后把全班名单推给侧写页，并**顺手补进学生主档**。

        ★ v1.9.2：``students.json.enc`` 是学生主档，唯一写入口是
          :class:`core.database.StudentStore`；导入时在这里自动补档，
          **不另立第二份名单**（AGENTS §37）。
        """
        self.profile_page.set_roster(rows)
        self._upsert_student_roster(rows)

    def _upsert_student_roster(self, rows: list) -> None:
        """把名单里每个**有学号**的学生补进学生主档。

        ★ 失败只记 ``logging.warning``：成绩已经存进去了，主档漏一个人是
          显示问题，不许影响导入结果、更不许弹窗（AGENTS §18 同源）。
        """
        try:
            from core.database import StudentStore  # noqa: PLC0415 - 晚绑定，组件可能还没落地
        except Exception:  # noqa: BLE001 - 还没落地就跳过补档，界面照常
            _log.debug("StudentStore 还没落地，这次跳过学生主档补档")
            return
        try:
            store = StudentStore()
        except Exception:  # noqa: BLE001 - 打不开主档也只记日志
            _log.warning("学生主档打不开，这次跳过补档", exc_info=True)
            return
        try:
            class_name = str(self.analysis_page.class_name or "").strip()
        except Exception:  # noqa: BLE001 - 班级号读不到就留空，不许断链
            class_name = ""
        for row in rows or ():
            student_id = str(getattr(row, "student_id", "") or "").strip()
            if not student_id:
                continue
            name = str(getattr(row, "student", "") or "").strip()
            try:
                store.upsert_import(student_id, name, class_name)
            except Exception:  # noqa: BLE001 - 单个学生失败不中断整批
                _log.warning("学生主档补档失败：%s", student_id, exc_info=True)

    def _sync_nav(self, index: int) -> None:
        """把侧栏选中态和顶部标题对齐到当前页。"""
        for position, button in enumerate(self._nav_buttons):
            button.setChecked(position == index)
        if 0 <= index < len(NAV_ITEMS):
            self._page_title.setText(NAV_ITEMS[index][1].replace("  ", " "))

    # ---------------------------------------------------------------- 自检用
    @property
    def nav_count(self) -> int:
        return len(self._nav_buttons)

    @property
    def page_count(self) -> int:
        return self._stack.count()

    @property
    def current_index(self) -> int:
        return self._stack.currentIndex()

    @property
    def switching(self) -> bool:
        return self._switcher.busy

    @property
    def root_frame(self):
        """圆角容器（截图 / 命中判定自检用）。"""
        return self._root

    def intentions_of_current_user(self) -> dict[str, str]:
        """给自检用：当前用户已登记的发展意愿。"""
        return load_intentions(self._store, self._username)
