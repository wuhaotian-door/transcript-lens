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
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
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

#: 淡入时长。
#:
#: ★ 240ms 实测一次切页要 **439ms**（中位，见下表），用户明确反馈"明显的卡顿"。
#:   130ms 是一次切页 **~137ms** —— 落到「100ms 以内算瞬时、200ms 以上能感到迟滞」
#:   这条感知线的下面。数字是量出来的，不是拍的：
#:
#:   淡出 + 淡入      实测切页中位
#:   ------------    ------------
#:   180 + 240         439 ms   ← 改之前
#:    90 + 150         260 ms
#:     0 + 150         166 ms
#:     0 + 130        ~137 ms   ← 现在
FADE_IN_MS = 130

#: 淡出时长。**0 = 不淡出，直接换页**（见 :meth:`StackSwitcher.switch`）。
#:
#: 为什么砍掉淡出：淡出那一段是纯粹的等待 —— 旧页在变淡的过程中，新页还没开始
#: 出现，用户盯着一个越来越空的屏幕。上表里 180→0 一项就省掉近 300ms，
#: 是整条链上最划算的一刀。换来的是"点哪页立刻到哪页"，这正是桌面软件该有的手感
#: （浏览器标签页、VS Code 都是瞬切）。
#:
#: 想要回淡出：把这里设回 180 即可，``switch()`` 会自动走回淡出分支，
#: 两条路径都有测试覆盖。
FADE_OUT_MS = 0

#: 卡片阴影参数（Spec 一：blurRadius=18，offset=(0, 2)）
SHADOW_BLUR = 18
SHADOW_OFFSET = 2

#: 切页锁的最长持有时间。超过就把这一页**直接落到位**（见 :meth:`StackSwitcher._force_finish`）。
#:
#: 1500ms —— charter v1.6 第 5.2 节指定的值。
#:
#: 中途调高过一阵（2500ms）：当时一次切页中位 ~520ms，机器忙时见过一次 1513ms，
#: 1500 会误砍一次本来能正常结束的动画。但那两个数都是**旧的 420ms 动画 +
#: 首次绘制一个重页面**造成的。现在一次切页中位 **~137ms**，
#: 1500ms 是 10 倍余量，够宽了，所以回落到了 charter 的值。
#:
#: ★ 它的职责**不是**"卡住多久算卡住"，而是"绝不能把一个本来会正常结束的
#:   动画砍掉"。哪天动画又变长了，**先重量再决定**要不要放宽，
#:   不要因为"感觉不够快"就往下调。
LOCK_TIMEOUT_MS = 1500

#: 正在跑的动画，按 widget 持有 —— 见模块开头第 1 条
_KEEP: dict[int, list[QPropertyAnimation]] = {}


def is_alive(obj: object) -> bool:
    """这个 PySide 包装对象背后的 C++ 对象还在不在。

    ★★ 为什么必须有它 ★★

    动画的 parent 就是它作用的那个 widget（``QPropertyAnimation(target,
    b"...", widget)``）。**widget 被销毁时动画跟着一起没**，而 Qt 的信号发射
    并不会因此停下 —— 连接表是引用计数的，发送者死了它照样把剩下的槽逐个调完。

    于是"排在后面的槽碰到一个已经释放的包装对象"就成立，**表现是整个进程
    直接消失（``0xC0000409``），stdout/stderr 一个字都不打** —— 打包成
    ``--windowed`` 的 exe 之后，用户看到的就是"打完招呼就没了，点任务栏也没反应"。

    实测：登录成功 → 欢迎页淡出 → ``finished`` 里把欢迎页引用丢掉 →
    欢迎页当场析构 → 紧接着 ``_drop`` 碰了一下已经没了的动画 → 进程消失。
    """
    try:
        import shiboken6

        return bool(shiboken6.isValid(obj))
    except Exception:  # noqa: BLE001 - 查不了就当它还活着，别把正常路径拦死
        return True


def _retain(widget: QWidget, anim: QPropertyAnimation) -> None:
    """保住动画对象，并在跑完之后把它摘干净。

    ★ **连接顺序是命门**：``_drop`` 必须是 ``finished`` 上的**第一个**槽。

    ``on_done`` 回调里常常会把触发动画的那个窗口丢掉（欢迎页淡出 → 起主窗、
    登录窗淡出 → 起主窗），窗口一析构，parent 在它上面的动画也就没了。
    要是 ``_drop`` 排在 ``on_done`` **后面**，它就会在一个已经释放的对象上
    调 ``deleteLater()`` —— 直接段错误，而且什么都不打印。
    """
    key = id(widget)
    _KEEP.setdefault(key, []).append(anim)

    def _drop() -> None:
        bucket = _KEEP.get(key)
        if bucket is not None and not is_alive(anim):
            # 动画已经跟着它的 parent 一起没了，直接清账，绝不碰它
            _KEEP.pop(key, None)
            return
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

    控件已经销毁时**直接返回**：这个函数在 ``deleteLater`` 路径上会被延后调用，
    那一刻控件可能已经没了（见 :func:`is_alive`）。
    """
    if not is_alive(widget):
        return
    if isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect):
        widget.setGraphicsEffect(None)


def _schedule_clear(widget: QWidget) -> None:
    """延到下一轮事件循环再摘效果。

    不在 ``finished`` 里直接摘：那一刻动画对象还活着（虽然已经停了），
    而摘效果会把它指向的对象删掉。等一拍，让 ``deleteLater`` 先落地。
    """
    QTimer.singleShot(0, lambda: clear_effect(widget) if is_alive(widget) else None)


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

    # ★ _retain 必须在 connect(_finish) **之前**：它连的 _drop 要当
    #   finished 上的第一个槽，否则 on_done 里销毁了 widget 之后，
    #   _drop 会去碰一个已经释放的动画对象（见 _retain / is_alive）。
    _retain(widget, anim)
    anim.finished.connect(_finish)
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

    _retain(widget, anim)
    anim.finished.connect(_finish)
    anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)


class _PageCover(QWidget):
    """切页时盖住内容区的一层**纯色幕布**。

    ★★ 为什么要有这个东西（这是"切页后界面略微抖动"的正解）★★

    原来是给**新页**挂 ``QGraphicsOpacityEffect`` 做 0→1 淡入。问题在于：
    一旦挂了效果，Qt 就把整页渲染进**一张离屏 pixmap**，而那条路径上
    **文字的光栅化方式不一样**（没有 ClearType，字形的次像素取整也不同）。
    动画跑完、效果一摘，整页**重新光栅化一遍** —— 所有字都换了个画法。

    实测（抓整窗，带对照组校准）：挂着效果 @1.0 和摘掉效果两张图，
    **17.4% 的像素不一样，最大通道差 240**
    （同一个坐标上一个版本是深色字 `#0f173b`、另一个版本是纯白 `#ffffff`）。
    这个重画发生在淡入结束之后约 30~60ms，用户看到的就是"切过去之后抖一下"。

    修法：**页面永远不挂效果**（永远走正常渲染路径，永远不重光栅化）。
    要淡入的改成盖在上面的一层纯色幕布淡出：
    幕布是一整块纯色，离屏渲染和直接渲染画出来**逐像素一致**，
    摘效果时没有任何可看出来的变化。

    视觉上和"新页 0→1 淡入"完全一样 —— 因为新页本来就贴在
    ``#F1F5F9`` 的背景上从背景里浮出来。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("PageCover")
        # QSS 给非顶层 QWidget 设 background 必须开这个属性
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(parent.rect())


class StackSwitcher(QObject):
    """带淡入淡出的 QStackedWidget 切换器（Spec 八的 ``switch_stack``）。

    用法::

        self.switcher = StackSwitcher(self.stack)
        self.switcher.switch(2)      # 换页 → 幕布淡出 → 新页显现

    ★ 转场做法见 :class:`_PageCover`：**不动页面的效果**，
      改淡一层盖在上面的纯色幕布。这样页面永远走正常渲染路径，
      不会在动画结束后被重新光栅化（那就是"抖动"的来源）。

    ``_busy`` 锁是必须的：不加的话连点侧栏会让多段动画同时改同一个 widget 的
    opacity，屏幕上就是一次闪黑。

    ★★ 但锁必须**保证能松开**。踩过一次：某次运行里锁一直挂着，
       之后所有切页全部失效（界面上表现就是"点侧栏没反应"），
       而且它是间歇性的、跑十次只出一次。所以这里有三道保险：

    1. **try/finally**：起动画时抛异常也一定把锁放开；
    2. **看门狗**：锁持有超过 :data:`LOCK_TIMEOUT_MS` 就**把这一页直接落到位**
       （不是只放锁）—— 宁可少一次淡入淡出，也绝不能让界面变成点不动的；
    3. **异常不吞**：回调里出错会打 traceback 到 stderr，不静默。
    """

    def __init__(self, stack, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stack = stack
        self._busy = False
        #: 这一趟要切到哪一页。看门狗靠它把转场直接做完。
        self._pending: int | None = None
        #: 正在淡出的幕布（同时最多一块）
        self._cover: _PageCover | None = None
        #: 切换完成后回调（主窗口用来同步侧栏选中态）
        self._on_switched: Callable[[int], None] | None = None
        #: 看门狗：锁忘了放开时兜底
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._force_finish)
        # 幕布要跟着内容区一起变宽变高（窗口缩放、最大化）
        stack.installEventFilter(self)

    # ------------------------------------------------------------------ 幕布
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if obj is self._stack and event.type() == QEvent.Type.Resize:
            if self._cover is not None and is_alive(self._cover):
                self._cover.setGeometry(self._stack.rect())
        return False

    def _raise_cover(self) -> _PageCover:
        """造一块盖住内容区的幕布并置顶。"""
        cover = _PageCover(self._stack)
        cover.show()
        cover.raise_()
        self._cover = cover
        return cover

    def _drop_cover(self) -> None:
        """把幕布收掉。

        ★ 用 ``deleteLater`` 而**不是**直接 ``deleteLater`` 之外的任何同步销毁：
          幕布身上挂着正在跑的动画，同步删它就是 AGENTS.md §4k 那个
          use-after-free（进程 0xC0000409 静默消失）。
        """
        cover = self._cover
        self._cover = None
        if cover is not None and is_alive(cover):
            cover.hide()
            cover.deleteLater()

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
        self._pending = None

    def _force_finish(self) -> None:
        """看门狗到点：把这一次转场直接做完。"""
        self._finish_now("切页超时未完成")

    def _finish_now(self, reason: str) -> None:
        """**把这一次转场直接做完**，而不是只把锁放开。

        ★ 只放锁的写法（``self._busy = False``）留下两个真实的坑：

        1. 正在跑的动画**还在继续**。用户这时再点侧栏，两段动画会同时改
           同一个 opacity —— 正是 ``_busy`` 本来要防的闪黑；
        2. 界面会停在一个**自相矛盾**的状态：侧栏亮着 A，内容还是 B，
           而且永远不会自愈（把锁放开不等于把页切过去）。

        所以这里干脆把 index 落到位、把残留的透明度效果摘干净、再放开锁。
        宁可这一次没有淡入淡出，也不要一个会说谎、点不动的界面。
        """
        index = self._pending
        self._watchdog.stop()
        if not self._busy:  # pragma: no cover - _release 已经停了定时器
            return
        print(
            f"[StackSwitcher] {reason}，已直接把这一页落到位"
            f"（跳过剩余动画，index={index}）。",
            file=sys.stderr,
        )
        self._busy = False
        self._pending = None
        if index is None:  # pragma: no cover - 有 _busy 就一定有 _pending
            return
        self._drop_cover()
        self._stack.setCurrentIndex(index)
        # 幕布方案下页面不该有透明度效果；这里再兜一遍，
        # 免得以后有人改回"给页面挂效果"时留下"缓存 pixmap 不失效 → 整页空白"。
        for i in range(self._stack.count()):
            clear_effect(self._stack.widget(i))
        self._land(index)

    def _run(self, what: str, fn: Callable[[], None]) -> None:
        """跑一步转场；出错就打 traceback 并把这一页落到位，绝不静默卡死。"""
        try:
            fn()
        except BaseException:  # noqa: BLE001 - 转场失败不能让界面卡死
            traceback.print_exc()
            self._finish_now(f"转场步骤「{what}」失败")

    # ------------------------------------------------------------------ 切换
    def switch(self, index: int, *, animate: bool = True) -> bool:
        """切到 ``index``。返回是否真的执行了切换。

        正在动画中、或目标就是当前页 → 返回 ``False``（连点被吃掉）。

        ★ ``FADE_OUT_MS == 0`` 时走**瞬时换页**分支：不挂透明度效果、
          不建动画对象，直接换 index 再淡入新页。少掉的是一条
          ``QGraphicsOpacityEffect`` 的建立/拆除 + 一段 180ms 的空等。
        """
        if self._busy or index == self._stack.currentIndex():
            return False
        if not (0 <= index < self._stack.count()):
            return False

        old = self._stack.currentWidget()
        if not animate or old is None:
            self._drop_cover()
            self._stack.setCurrentIndex(index)
            self._land(index)
            return True

        # ★ 先记下"要切到哪一页"：看门狗和异常兜底都靠它把转场做完。
        self._pending = index
        self._acquire()
        try:
            if FADE_OUT_MS <= 0:
                # 瞬时换页：旧页直接消失，新页淡入。
                # 一样走 _run —— 出异常时它会调用 _finish_now 把页面落到位，
                # 而不是把锁挂在半路上（见 _force_finish 的注释）。
                self._run("瞬时换页", lambda: self._after_out(index))
            else:
                fade_out(
                    old,
                    FADE_OUT_MS,
                    lambda: self._run("淡出旧页", lambda: self._after_out(index)),
                )
        except BaseException:  # noqa: BLE001
            traceback.print_exc()
            self._release()
            raise
        return True

    def _after_out(self, index: int) -> None:
        """换页 + 盖幕布 + 幕布淡出。

        ★ 顺序是命门，三件事一件都不能挪：

        1. **先换 index** —— 新页立刻以**正常渲染路径**画出来（这才是最终画面）；
        2. **再盖幕布** —— 一块纯色挡在上面，用户此刻看到的是幕布；
        3. **幕布淡出** —— 新页从背景里浮出来，视觉上就是"淡入"。

        新页自始至终**没有 graphicsEffect**，所以永远不会有
        "动画结束后整页重光栅化"那一下（见 :class:`_PageCover`）。

        （旧写法是给新页挂 ``QGraphicsOpacityEffect`` 从 0 淡到 1 —— 功能一样，
        但摘效果时整页会重画一遍，实测 17.4% 的像素变化、最大差 240。）
        """
        if self._pending != index:
            # 看门狗已经替我们把这一页落到位了，别再动
            return
        self._stack.setCurrentIndex(index)
        if self._stack.currentWidget() is None:  # pragma: no cover - 理论上不可达
            self._release()
            return
        cover = self._raise_cover()
        fade_out(
            cover,
            FADE_IN_MS,
            lambda: self._run("幕布淡出", lambda: self._after_in(index)),
        )

    def _after_in(self, index: int) -> None:
        self._drop_cover()
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
    """改 ``windowOpacity`` 做窗口级淡入淡出。

    ★★ **连接顺序是这一个函数的命门，改动前先读这段** ★★

    ``_retain`` 必须**先调**，``anim.finished.connect(...)`` 必须**后接**。
    因为 ``_retain`` 连进去的 ``_drop`` 要当第一个槽：

    ``on_done`` 的典型工作就是"这个窗口没用了，起下一个" ——
    欢迎页淡出 → ``AppFlow._after_welcome`` 把欢迎页引用丢掉 → 窗口当场析构。
    而动画的 parent 就是这个窗口，**窗口一没动画也跟着没**。
    要是 ``_drop`` 排在 ``on_done`` 后面，Qt 走完 ``on_done`` 接着就会在
    一个已经释放的对象上调 ``deleteLater()``：

        **整个进程 0xC0000409 直接消失，stdout/stderr 一个字都不打。**

    打包成 ``--windowed`` 的 exe 之后，用户看到的就是
    「开屏 → 打个招呼 → 没了，点任务栏也没反应」。
    实测复现并靠逐层 trace 定位到这一行（见 `%TEMP%\\tl_boot_trace.py`）。

    ⚠️ **光把 `_drop` 排到前面还不够**（试过，照样崩）：Qt 发完 ``finished``
    之后 ``QAbstractAnimationPrivate::setState`` **还要继续用这个动画对象**
    （注销统一计时器、按 deleteWhenStopped 决定删不删）。窗口一析构、
    parent 在它上面的动画跟着没，栈上那句话就踩空了。

    所以 ``on_done`` 一律**延后一拍**再调：等这一轮事件循环走完，
    动画彻底收工，谁来销毁窗口都安全。

    ★ 用 ``QTimer.singleShot(0, window, on_done)`` 的 **context 重载**：
    窗口先一步没了的话，这一次回调会被自动跳过，不会去碰一个死对象。
    """
    window.setWindowOpacity(start)
    anim = QPropertyAnimation(window, b"windowOpacity", window)
    anim.setDuration(ms)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(
        QEasingCurve.Type.OutCubic if end > start else QEasingCurve.Type.InCubic
    )
    # ★ _retain 必须在 connect 之前 —— 它连的 _drop 要当 finished 上的第一个槽
    _retain(window, anim)
    if on_done is not None:
        anim.finished.connect(
            lambda: QTimer.singleShot(0, window, on_done)
        )
    anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)


def window_fade_in(window: QWidget, ms: int = FADE_IN_MS, on_done: Callable[[], None] | None = None) -> None:
    """窗口 0 → 1 淡入（先 show 再淡入由调用方负责）。"""
    window_fade(window, 0.0, 1.0, ms, on_done)


def window_fade_out(window: QWidget, ms: int = FADE_OUT_MS, on_done: Callable[[], None] | None = None) -> None:
    """窗口 1 → 0 淡出，``finished`` 里调 ``on_done``（通常在里面 hide + 起下一个窗）。"""
    window_fade(window, window.windowOpacity(), 0.0, ms, on_done)
