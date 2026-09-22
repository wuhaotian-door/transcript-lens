"""登录成功后的欢迎页 —— 打招呼，停 1.5 秒，淡出，让位给主界面。

时序（全部走 QTimer / QPropertyAnimation，**一处 time.sleep 都没有**）::

    opacity 0 → 1   (260ms)
    停留 1500ms     (QTimer.singleShot)
    opacity 1 → 0   (260ms)
    finished 信号   → 主程序淡入主窗口

★ 为什么要多这一页
------------------

登录窗淡出之后直接"啪"地跳出主界面，人会有点懵：刚输完密码，还没反应过来
就到了另一个界面。中间垫一句带时间的问候，是让人喘一口气，也是确认
"进来的是我，账号没选错"。

★ 时段划分（按系统本地时间）

======  ==========
时间    问候
======  ==========
05:00–11:59  早上好
12:00–17:59  下午好
18:00–04:59  晚上好
======  ==========

判定逻辑抽成了纯函数 :func:`greeting_for`，因为它有边界（4:59 / 5:00 / 11:59 /
12:00 / 17:59 / 18:00 六个点），必须能离线单测 —— 靠改系统时间来测不现实。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ui.anim import window_fade
from ui.frameless import SHADOW_MARGIN, SHADOW_BLUR, apply_frameless
from ui.anim import apply_shadow

__all__ = [
    "FADE_MS",
    "HOLD_MS",
    "WelcomeWindow",
    "greeting_for",
    "greeting_text",
]

#: 淡入 / 淡出各用多久
FADE_MS = 260

#: 完全显示后停留多久（需求：1.5 秒）
HOLD_MS = 1500

_CARD_W = 520
_CARD_H = 280

#: 星期几（datetime.weekday() 返回 0=周一）
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def greeting_for(hour: int) -> str:
    """按小时给问候语。**纯函数**，边界可直接单测。

    >>> [greeting_for(h) for h in (0, 4, 5, 11, 12, 17, 18, 23)]
    ['晚上好', '晚上好', '早上好', '早上好', '下午好', '下午好', '晚上好', '晚上好']
    """
    if 5 <= hour < 12:
        return "早上好"
    if 12 <= hour < 18:
        return "下午好"
    return "晚上好"


def greeting_text(username: str, now: datetime | None = None) -> str:
    """``{时段}，{用户名}``。"""
    moment = now or datetime.now()
    return f"{greeting_for(moment.hour)}，{username}"


def date_text(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return (
        f"{moment.year} 年 {moment.month} 月 {moment.day} 日"
        f"　{_WEEKDAYS[moment.weekday()]}"
    )


class WelcomeWindow(QWidget):
    """欢迎页。

    Signals:
        finished(): 整套时序跑完（或 :meth:`skip_to_end` 被调用）。
    """

    finished = Signal()

    def __init__(
        self,
        username: str,
        *,
        hold_ms: int = HOLD_MS,
        fade_ms: int = FADE_MS,
        now: datetime | None = None,
    ) -> None:
        super().__init__()
        self._username = username or ""
        self._hold_ms = hold_ms
        self._fade_ms = fade_ms
        self._done = False

        apply_frameless(self)
        self.setWindowTitle("成绩单透镜")
        self.resize(_CARD_W + 2 * SHADOW_MARGIN, _CARD_H + 2 * SHADOW_MARGIN)
        self.setWindowOpacity(0.0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )
        card = QFrame()
        card.setObjectName("WelcomeCard")
        card.setFrameShape(QFrame.Shape.NoFrame)
        apply_shadow(card, blur=SHADOW_BLUR + 9, dy=4, alpha=60)
        outer.addWidget(card)

        box = QVBoxLayout(card)
        box.setContentsMargins(42, 40, 42, 34)
        box.setSpacing(0)

        moment = now or datetime.now()
        self._greeting = QLabel(greeting_text(self._username, moment))
        self._greeting.setObjectName("WelcomeGreeting")
        self._greeting.setWordWrap(True)
        box.addWidget(self._greeting)

        box.addSpacing(10)

        self._date = QLabel(date_text(moment))
        self._date.setObjectName("WelcomeDate")
        box.addWidget(self._date)

        box.addStretch(1)

        hint = QLabel("正在进入成绩单透镜…")
        hint.setObjectName("WelcomeHint")
        box.addWidget(hint)

    # ------------------------------------------------------------- 对外
    @property
    def greeting(self) -> str:
        """自检用：屏幕上那行问候语。"""
        return self._greeting.text()

    def greeting_hour(self) -> int:
        return datetime.now().hour

    def center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:  # pragma: no cover - 无显示器时
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.center().y() - self.height() // 2,
        )

    def run(self) -> None:
        """淡入 → 停留 → 淡出 → ``finished``。"""
        self.center_on_screen()
        self.show()
        window_fade(self, 0.0, 1.0, self._fade_ms, self._after_in)

    def _after_in(self) -> None:
        QTimer.singleShot(self._hold_ms, self._fade_out)

    def _fade_out(self) -> None:
        window_fade(self, self.windowOpacity(), 0.0, self._fade_ms, self._emit_finished)

    def _emit_finished(self) -> None:
        if self._done:  # 防重入：finished 只该发一次
            return
        self._done = True
        self.close()
        self.finished.emit()

    def skip_to_end(self) -> None:
        """跳过动画直接发 ``finished``（自检/截图脚本用，不进产品路径）。"""
        self._emit_finished()
