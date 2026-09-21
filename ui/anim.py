"""动画与视觉工具 —— 全部走 QPropertyAnimation，**一处 time.sleep 都没有**。

Spec 八：``fade_in`` / ``fade_out`` / ``switch_stack``。

为什么要单独一个模块
--------------------

1. **动画对象必须留引用**。``QPropertyAnimation`` 一旦被垃圾回收，动画会
   在下一帧静默停住 —— 表现是"偶尔不淡入"，最难查的那种。这里用
   ``_KEEP`` 按 widget 存住，动画结束后再摘掉。
2. **切页要防连点**。用户狂点侧栏时，如果上一段动画还没跑完就再起一段，
   两个动画会同时改同一个 widget 的 opacity，视觉上就是闪黑。
   :class:`StackSwitcher` 用一个 ``_busy`` 锁把这件事挡在门口。
3. **透明度效果的生命周期**。给一个 widget 设 ``QGraphicsOpacityEffect``
   之后再撤掉，有概率把子控件的绘制弄脏；所以这里**只设一次、长期复用**，
   不反复 setGraphicsEffect(None)。

本模块不 import core，也不碰业务。
"""

from __future__ import annotations

import sys
import traceback
from typing import Callable

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QTimer,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QWidget,
)

__all__ = [
    "FADE_IN_MS",
    "FADE_OUT_MS",
    "LOCK_TIMEOUT_MS",
    "SHADOW_BLUR",
    "SHADOW_OFFSET",
    "StackSwitcher",
    "apply_shadow",
    "clear_effect",
    "effect_of",
    "fade_in",
    "fade_out",
    "make_card",
    "set_opacity",
    "window_fade",
    "window_fade_in",
    "window_fade_out",
]

#: 淡入时长（Spec 八：单次动画不超过 250ms）
FADE_IN_MS = 240
#: 淡出时长
FADE_OUT_MS = 180

#: 卡片阴影参数（Spec 一：blurRadius=18，offset=(0, 2)）
SHADOW_BLUR = 18
SHADOW_OFFSET = 2

#: 切页锁的最长持有时间。超过就强制放开 —— 界面绝不能被一次转场卡死。
#: 正常一次切换约 455ms（淡出 180 + 淡入 240 + Qt 调度），1500ms 有三倍余量。
LOCK_TIMEOUT_MS = 1500

#: 正在跑的动画，按 widget 持有 —— 见模块开头第 1 条
_KEEP: dict[int, list[QPropertyAnimation]] = {}


def _retain(widget: QWidget, anim: QPropertyAnimation) -> None:
    key = id(widget)
    _KEEP.setdefault(key, []).append(anim)

    def _drop() -> None:
        bucket = _KEEP.get(key)
        if bucket and anim in bucket:
            bucket.remove(anim)
        if bucket is not None and not bucket:
            _KEEP.pop(key, None)
        # 动画跑完就没用了。留着它反而危险：下面 _schedule_clear() 会把
        # 它的 target（effect）删掉，留一个悬空指针没有好处。
        anim.deleteLater()

    anim.finished.connect(_drop)


def clear_effect(widget: QWidget) -> None:
    """把透明度效果撤掉。

    ★ **这个函数是整个模块最容易漏掉、后果又最严重的一步。**

    ``QGraphicsOpacityEffect`` 会**缓存**它要绘制的源 pixmap。当页面**内部**
    增删控件之后（例如侧写辅导页重算"可抢救项"列表时 deleteLater 掉旧标签），
    那份缓存不会自动失效 —— 效果照旧去画那张已经作废的 pixmap，屏幕上就是
    **整整一页空白**，而且切走再切回来（重新跑一次淡入）才会恢复正常。

    实测复现：跳到侧写辅导页 → 点「考研」触发重建 → 整页变白。

    所以动画一结束就把效果摘掉：页面回到"没有 effect"的普通状态，
    之后无论内部怎么改控件都能正常重绘。下次要淡入时
    :func:`effect_of` 会重新挂一个新的。
    """
    if isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect):
        widget.setGraphicsEffect(None)


def _schedule_clear(widget: QWidget) -> None:
    """延到下一轮事件循环再摘效果。

    不在 ``finished`` 里直接摘：那一刻动画对象还活着（虽然已经停了），
    而摘效果会把它指向的对象删掉。等一拍，让 ``deleteLater`` 先落地。
    """
    QTimer.singleShot(0, lambda: clear_effect(widget))


def apply_shadow(
    widget: QWidget,
    *,
    blur: int = SHADOW_BLUR,
    dy: int = SHADOW_OFFSET,
    color: str = "#0F172A",
    alpha: int = 26,
) -> QGraphicsDropShadowEffect:
    """给卡片挂一层**真阴影**。

    QSS 没有 ``box-shadow``，所以 Spec 要求的卡片阴影只能这么做。
    """
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    tint = QColor(color)
    tint.setAlpha(alpha)
    effect.setColor(tint)
    widget.setGraphicsEffect(effect)
    return effect


def effect_of(widget: QWidget) -> QGraphicsOpacityEffect:
    """拿到（必要时创建）widget 的透明度效果。

    ★ 已经挂了**阴影**效果的控件不能再挂透明度效果 —— 一个 widget 只有一个
      ``graphicsEffect``。这种情况会抛 TypeError，让调用方早点发现，
      而不是悄悄把阴影顶掉。
    """
    existing = widget.graphicsEffect()
    if isinstance(existing, QGraphicsOpacityEffect):
        return existing
    if existing is not None:
        raise TypeError(
            f"{widget.objectName() or type(widget).__name__} 已经挂了"
            f"{type(existing).__name__}，一个 widget 只能有一个 graphicsEffect；"
            "需要淡入淡出的容器不要再挂阴影"
        )
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)
    return effect


def set_opacity(widget: QWidget, value: float) -> None:
    """直接设透明度（不起动画）。"""
    effect_of(widget).setOpacity(max(0.0, min(1.0, value)))


def fade_in(widget: QWidget, ms: int = FADE_IN_MS, on_done: Callable[[], None] | None = None) -> None:
    """0 → 1 淡入。动画结束后**摘掉效果**（见 :func:`clear_effect`）。"""
    effect = effect_of(widget)
    effect.setOpacity(0.0)
    widget.setVisible(True)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _finish() -> None:
        if on_done is not None:
            on_done()
        _schedule_clear(widget)

    anim.finished.connect(_finish)
    _retain(widget, anim)
    anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)


def fade_out(
    widget: QWidget,
    ms: int = FADE_OUT_MS,
    on_done: Callable[[], None] | None = None,
) -> None:
    """1 → 0 淡出，``finished`` 里调 ``on_done``，之后摘掉效果。

    ``on_done`` 里的异常不吞：它是转场的关键一步（切换 index / hide），
    出错必须让人看见，而不是停在一个半透明的页面上。
    """
    effect = effect_of(widget)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(effect.opacity())
    anim.setEndValue(0.0)
    anim.setEasingCurve(QEasingCurve.Type.InCubic)

    def _finish() -> None:
        if on_done is not None:
            on_done()
        _schedule_clear(widget)

    anim.finished.connect(_finish)
    _retain(widget, anim)
    anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)


class StackSwitcher(QObject):
    """带淡入淡出的 QStackedWidget 切换器（Spec 八的 ``switch_stack``）。

    用法::

        self.switcher = StackSwitcher(self.stack)
        self.switcher.switch(2)      # 淡出旧页 → setCurrentIndex → 淡入新页

    ``_busy`` 锁是必须的：不加的话连点侧栏会让多段动画同时改同一个 widget 的
    opacity，屏幕上就是一次闪黑。

    ★★ 但锁必须**保证能松开**。踩过一次：某次运行里锁一直挂着，
       之后所有切页全部失效（界面上表现就是"点侧栏没反应"），
       而且它是间歇性的、跑十次只出一次。所以这里有三道保险：

    1. **try/finally**：起动画时抛异常也一定把锁放开；
    2. **看门狗**：锁持有超过 :data:`LOCK_TIMEOUT_MS` 就强制作废并放开 ——
       宁可少一次淡入淡出，也绝不能让界面变成点不动的；
    3. **异常不吞**：回调里出错会打 traceback 到 stderr，不静默。
    """

    def __init__(self, stack, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stack = stack
        self._busy = False
        #: 切换完成后回调（主窗口用来同步侧栏选中态）
        self._on_switched: Callable[[int], None] | None = None
        #: 看门狗：锁忘了放开时兜底
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._force_release)

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def current(self) -> int:
        return self._stack.currentIndex()

    def on_switched(self, callback: Callable[[int], None]) -> None:
        self._on_switched = callback

    # ------------------------------------------------------------------ 内部
    def _acquire(self) -> None:
        self._busy = True
        self._watchdog.start(LOCK_TIMEOUT_MS)

    def _release(self) -> None:
        self._watchdog.stop()
        self._busy = False

    def _force_release(self) -> None:
        """看门狗到点：无论卡在哪一步，都把锁放开，让界面重新能用。"""
        if self._busy:
            print(
                "[StackSwitcher] 切页锁超时未释放，已强制放开 —— "
                "界面可以继续用，但这一次的淡入淡出可能不完整。",
                file=sys.stderr,
            )
        self._busy = False

    def _run(self, what: str, fn: Callable[[], None]) -> None:
        """跑一步转场；出错就打 traceback 并放开锁，绝不静默卡死。"""
        try:
            fn()
        except BaseException:  # noqa: BLE001 - 转场失败不能让界面卡死
            traceback.print_exc()
            print(f"[StackSwitcher] 转场步骤「{what}」失败，已放开锁。", file=sys.stderr)
            self._release()

    # ------------------------------------------------------------------ 切换
    def switch(self, index: int, *, animate: bool = True) -> bool:
        """切到 ``index``。返回是否真的执行了切换。

        正在动画中、或目标就是当前页 → 返回 ``False``（连点被吃掉）。
        """
        if self._busy or index == self._stack.currentIndex():
            return False
        if not (0 <= index < self._stack.count()):
            return False

        old = self._stack.currentWidget()
        if not animate or old is None:
            self._stack.setCurrentIndex(index)
            self._land(index)
            return True

        self._acquire()
        try:
            fade_out(old, FADE_OUT_MS, lambda: self._run("淡出旧页", lambda: self._after_out(index)))
        except BaseException:  # noqa: BLE001
            traceback.print_exc()
            self._release()
            raise
        return True

    def _after_out(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        new = self._stack.currentWidget()
        if new is None:  # pragma: no cover - 理论上不可达
            self._release()
            return
        set_opacity(new, 0.0)
        fade_in(new, FADE_IN_MS, lambda: self._run("淡入新页", lambda: self._after_in(index)))

    def _after_in(self, index: int) -> None:
        self._release()
        self._land(index)

    def _land(self, index: int) -> None:
        if self._on_switched is not None:
            self._on_switched(index)


def make_card(parent: QWidget | None = None, *, shadow: bool = True, name: str = "Card") -> QFrame:
    """造一张标准卡片：白底 + 圆角 + 细边 +（可选）真阴影。

    ★ 挂了阴影的卡片**不要再淡入淡出** —— 一个 widget 只有一个
      ``graphicsEffect``（见 :func:`effect_of`）。要淡入淡出的是**页面容器**，
      卡片挂在它下面即可。
    """
    card = QFrame(parent)
    card.setObjectName(name)
    card.setFrameShape(QFrame.Shape.NoFrame)
    if shadow:
        apply_shadow(card)
    return card


# ---------------------------------------------------------------------------
# 顶层窗口的淡入淡出
# ---------------------------------------------------------------------------
#
# ★ 顶层窗口**不要**用 QGraphicsOpacityEffect —— 那作用于控件自身的绘制，
#   对一个独立窗口来说合成结果依平台而定（Windows 上经常整个窗口不刷新）。
#   窗口级的淡入淡出要用 windowOpacity，它直接改窗口的图层透明度。


def window_fade(
    window: QWidget,
    start: float,
    end: float,
    ms: int,
    on_done: Callable[[], None] | None = None,
) -> None:
    """改 ``windowOpacity`` 做窗口级淡入淡出。"""
    window.setWindowOpacity(start)
    anim = QPropertyAnimation(window, b"windowOpacity", window)
    anim.setDuration(ms)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(
        QEasingCurve.Type.OutCubic if end > start else QEasingCurve.Type.InCubic
    )
    if on_done is not None:
        anim.finished.connect(lambda: on_done())
    _retain(window, anim)
    anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)


def window_fade_in(window: QWidget, ms: int = FADE_IN_MS, on_done: Callable[[], None] | None = None) -> None:
    """窗口 0 → 1 淡入（先 show 再淡入由调用方负责）。"""
    window_fade(window, 0.0, 1.0, ms, on_done)


def window_fade_out(window: QWidget, ms: int = FADE_OUT_MS, on_done: Callable[[], None] | None = None) -> None:
    """窗口 1 → 0 淡出，``finished`` 里调 ``on_done``（通常在里面 hide + 起下一个窗）。"""
    window_fade(window, window.windowOpacity(), 0.0, ms, on_done)
