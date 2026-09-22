"""登录成功后的欢迎页 —— 打招呼，停 0.9 秒，淡出，让位给主界面。

时序（全部走 QTimer / QPropertyAnimation，**一处 time.sleep 都没有**）::

    opacity 0 → 1   (180ms)
    停留 900ms      (QTimer.singleShot)
    opacity 1 → 0   (180ms)
    finished 信号   → 主程序淡入主窗口

★ 时序是量出来的，不是拍的
--------------------------

「登录成功 → 用户真的看见主界面」原来要 2020ms，是整条启动链上最大的一块。
问候语只有一行、日期只有一行，扫一眼就够了，1500ms 是纯粹站着等。
现在 180 + 900 + 180 = 1260ms。

★ 主窗口在这 900ms 里**已经建好了**（见 ``main.AppFlow`` 的预热），
  所以这条线跑完就能直接淡入，不用再等一次原生窗口创建。

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

import time
from datetime import datetime
from typing import Callable

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
FADE_MS = 180

#: 完全显示后停留多久。
#:
#: 原来 1500ms —— 实测「登录 → 看见主界面」这一段因此多花 600ms。
#: 这一页要传达的信息只有一句问候 + 一个日期，人扫一眼就够了；
#: 900ms 读得完，又不至于变成一段站着等的过场。
#:
#: ★ 主窗的构造（实测约 600ms）**藏在这 900ms 里面**，不是加在它后面
#:   —— 见 :meth:`WelcomeWindow._after_in`。
HOLD_MS = 900

#: 停留时间的下限。预热再慢也要让人看清是谁进来了。
_MIN_HOLD_MS = 400

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
        on_shown: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._username = username or ""
        self._hold_ms = hold_ms
        self._fade_ms = fade_ms
        #: 淡入结束、这一页**真的画在屏幕上**之后调一次（只调一次）。
        #: 主程序用它来预热主窗 —— 见 :meth:`_after_in`。
        self._on_shown = on_shown
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
        """淡入结束 —— 这一页已经在屏幕上了。

        ★ ``on_shown`` 必须在这里调，**不能**在 :meth:`run` 里紧接着
          ``show()`` 调。``run()`` 只是把动画 start 出去就返回了，
          那一刻窗口还是 opacity 0、一帧都没画；在那种时候跑一段
          600ms 的同步工作（建主窗），事件循环被占住，淡入动画会
          **冻在第一帧**上 —— 用户看到的是"点了一下，半天没反应"。

          等淡入跑完再建，这 600ms 就落在用户正看着问候语的时候。

        ★ 停留时间是**扣掉这段工作之后**的余量。

          一开始写成"干完活再开始计时"，结果这一页实际停留
          ``900 + 600 = 1500ms`` —— 问候语本身只要扫一眼，多出来的
          600ms 纯粹是站着等。改成先扣后等，用户看到的始终是
          ``HOLD_MS``，而工作藏在里面。
        """
        started = time.perf_counter()
        if self._on_shown is not None:
            hook, self._on_shown = self._on_shown, None
            hook()
        spent_ms = (time.perf_counter() - started) * 1000.0
        # 留一个下限：工作再慢也要让人看清是谁进来了
        remaining = max(_MIN_HOLD_MS, int(self._hold_ms - spent_ms))
        QTimer.singleShot(remaining, self._fade_out)

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
