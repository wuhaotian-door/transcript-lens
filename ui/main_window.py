"""主界面 —— 深色侧栏 + 顶部窄条 + QStackedWidget 内容区。

     +--------+--------------------------------------------------+
     | 成绩单  |  张老师            ● 已激活                       |  ← 顶部窄条
     | 透镜    +--------------------------------------------------+
     |        |                                                  |
     | 📊 智育 |                                                  |
     | 🎯 侧写 |              当前页面（QStackedWidget）           |
     | 📈 档案 |                                                  |
     | 💰 奖学 |                                                  |
     | ⚙️ 设置 |                                                  |
     | 👤 个人 |                                                  |
     +--------+--------------------------------------------------+

★ 切页动画走 :class:`ui.anim.StackSwitcher`：淡出旧页 → setCurrentIndex →
  淡入新页，单次 ≤250ms，并带 ``_busy`` 锁防连点（Spec 四）。
★ 侧栏选中态由 QSS 的 ``QPushButton#NavButton:checked`` 负责 ——
  左侧 4px 蓝线 + 半透明浅蓝底。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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

from core.activation import load_state
from core.crypto_store import CryptoStore
from ui.anim import StackSwitcher, window_fade_in
from ui.intentions import load_intentions
from ui.pages import (
    AccountPage,
    AnalysisPage,
    ArchivePage,
    ProfilePage,
    ScholarshipPage,
    SettingsPage,
)

__all__ = ["APP_NAME", "MainWindow", "NAV_ITEMS"]

#: 程序名（侧栏顶部）
APP_NAME = "成绩单透镜"

#: 侧栏宽度（Spec 四：固定 190px）
SIDEBAR_WIDTH = 190

#: 导航项：``(键, 图标+文字, 页面类名)``。顺序即侧栏顺序。
NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("analysis", "📊  智育分析"),
    ("profile", "🎯  侧写辅导"),
    ("archive", "📈  成长档案"),
    ("scholarship", "💰  奖学金测算"),
    ("settings", "⚙️  设置"),
    ("account", "👤  个人中心"),
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

        self.setWindowTitle(f"{APP_NAME} · transcript-lens")
        self.setMinimumSize(1100, 700)
        self.resize(1240, 780)
        self.setWindowOpacity(0.0)

        self._build()
        self._wire()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        self._stack = QStackedWidget()
        self._stack.setObjectName("Content")
        body.addWidget(self._stack, 1)
        outer.addLayout(body, 1)

        # --- 六个页面 ---
        self.analysis_page = AnalysisPage()
        self.profile_page = ProfilePage(self._store)
        self.archive_page = ArchivePage()
        self.scholarship_page = ScholarshipPage()
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
            button.clicked.connect(lambda _=False, i=index: self.goto(i))
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

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(54)

        box = QHBoxLayout(bar)
        box.setContentsMargins(22, 0, 22, 0)
        box.setSpacing(10)

        self._user_label = QLabel("（未登录）")
        self._user_label.setObjectName("TopUser")
        box.addWidget(self._user_label)

        box.addWidget(self._sep())
        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        box.addWidget(self._dot)

        self._act_label = QLabel("激活状态未知")
        self._act_label.setObjectName("TopMeta")
        box.addWidget(self._act_label)

        box.addStretch(1)

        self._page_title = QLabel("")
        self._page_title.setObjectName("TopMeta")
        box.addWidget(self._page_title)
        return bar

    @staticmethod
    def _sep() -> QLabel:
        line = QLabel("|")
        line.setObjectName("TopMeta")
        return line

    # ---------------------------------------------------------------- 接线
    def _wire(self) -> None:
        # 智育分析页双击/点「侧写」→ 带着学生数据跳到侧写辅导
        self.analysis_page.student_selected.connect(self._open_profile)
        # 个人中心「切换账号」
        self.account_page.switch_account.connect(self.switch_account.emit)
        # 设置页导出用的数据来自智育分析页（不复制数据，只给个取值函数）
        self.settings_page.set_export_provider(self.analysis_page.export_records)

    # ---------------------------------------------------------------- 对外
    def set_user(self, username: str) -> None:
        """设置当前用户，并把六个页面各自的用户相关状态刷新一遍。"""
        self._username = username or ""
        self._user_label.setText(self._username or "（未登录）")
        self.profile_page.set_user(self._username)
        self.settings_page.set_user(self._username)
        self.account_page.set_user(self._username)
        self._refresh_activation()

    def goto(self, index: int) -> None:
        """切到第 ``index`` 页（带动画）。"""
        if not self._switcher.switch(index):
            # 被 _busy 锁吃掉或本来就在这页 —— 把选中态拨回去，别让界面说谎
            self._sync_nav(self._stack.currentIndex())

    def show_and_fade_in(self, username: str) -> None:
        """显示窗口 + 淡入。登录窗淡出之后由主程序调这个。"""
        self.set_user(username)
        self._stack.setCurrentIndex(DEFAULT_PAGE)
        self._sync_nav(DEFAULT_PAGE)
        self.show()
        window_fade_in(self, 260)

    # ---------------------------------------------------------------- 内部
    def _open_profile(self, payload: dict) -> None:
        """从智育分析页跳侧写辅导。"""
        self.profile_page.show_student(payload)
        index = next(i for i, (key, _t) in enumerate(NAV_ITEMS) if key == "profile")
        self._switcher.switch(index)

    def _sync_nav(self, index: int) -> None:
        """把侧栏选中态和顶部标题对齐到当前页。"""
        for position, button in enumerate(self._nav_buttons):
            button.setChecked(position == index)
        if 0 <= index < len(NAV_ITEMS):
            self._page_title.setText(NAV_ITEMS[index][1].replace("  ", " "))

    def _refresh_activation(self) -> None:
        """顶部那个小圆点：绿=已激活，灰=未激活（Spec 四）。"""
        try:
            state = load_state(self._data_root)
        except Exception:  # noqa: BLE001 - 读不到就当未激活，不拦界面
            state = None
        activated = bool(getattr(state, "activated", False))
        color = "#22C55E" if activated else "#CBD5E1"
        self._dot.setStyleSheet(f"background:{color}; border-radius:5px;")
        if activated:
            self._act_label.setText(
                f"已激活（版本 {state.active_version}，{state.activated_year} 年）"
            )
        else:
            self._act_label.setText("未激活")

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

    def intentions_of_current_user(self) -> dict[str, str]:
        """给自检用：当前用户已登记的发展意愿。"""
        return load_intentions(self._store, self._username)
