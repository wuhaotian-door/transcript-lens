"""开屏 —— 自绘无边框窗口，淡入 → 停留 → 淡出 → 发信号。

流程（全部走 QTimer / QPropertyAnimation，**没有一处 time.sleep**）::

    opacity 0 → 1   (200ms)
    停留 420ms      (QTimer.singleShot)
    opacity 1 → 0   (200ms)
    finished 信号   → 主程序决定接下来起登录窗还是直接进主界面

★ 为什么不用 QSplashScreen（换掉它省了 865ms）
--------------------------------------------

原来这一页是 ``QSplashScreen``。实测（每个变体单独起进程，排除"一次性平台初始化"
的干扰，对照组 ``sleep(0.15)`` 量到 150.3ms 校准正常）::

    变体                                      show() 耗时
    --------------------------------------   -----------
    QWidget（普通）                              165 ms
    QWidget + 无边框 + 半透明                     167 ms
    QWidget + 无边框 + 半透明 + opacity 0         164 ms
    QSplashScreen（原样）                       1030 ms   ← 慢 865ms
    QSplashScreen + 无边框                      1028 ms
    QSplashScreen + 无边框 + 半透明              1029 ms
    QSplashScreen + opacity 0                  1035 ms

**跟半透明、无边框、透明度都无关** —— 只要换成 ``QSplashScreen`` 就慢 865ms。
那 865ms 里用户盯着的是一个全黑的屏幕（窗口还没映射出来）。
换成普通 ``QWidget`` 自己画 pixmap 之后，同样的画面 **165ms** 就出来了。

★ 标题里**不要放 emoji**
------------------------

实测同一个 pixmap::

    带 emoji 📊 + 中文 22pt Bold     241.7 ms
    只有中文 22pt Bold                 0.2 ms      ← 快 1200 倍
    纯 ASCII                           0.8 ms

一个字 241ms。原因是 emoji 要现场加载 Segoe UI Emoji 字体并做彩色字形合成 ——
而它只出现在开屏那 300 毫秒里。**删掉它对观感几乎没影响，对启动是纯赚。**

（同理：`ui/main_window.py` 侧栏、`ui/pages/*` 里的 emoji 都在**已映射的窗口**里，
不影响启动，所以保留。）

★ 为什么不用 time.sleep：它会把 Qt 的事件循环整条冻住 —— 窗口不重绘、
  动画不走、点击无响应。屏幕上表现就是"卡死三秒然后突然出现"。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from core.version import CURRENT_VERSION
from ui.anim import window_fade

__all__ = ["FADE_MS", "HOLD_MS", "SplashScreen"]
#: 淡入 / 淡出各用多久。原来 300ms —— 开屏本身只是个"程序起来了"的信号，
#: 不需要 600ms 的过场，200ms 足够看出是淡入而不是闪现。
FADE_MS = 200

#: 完全显示后停留多久。
#:
#: 原来写的是 800ms，注释里挂着"Spec 二"—— **Spec 里根本没有开屏这一节**
#: （已删除的 v1.3 Spec 全文搜不到 开屏 / splash / 动画 / 800ms）。
#: 那是当时随手定的数，不是需求。
#:
#: 260ms 就够看清品牌和 slogan 了。开屏**不是**过场动画，它是一块盖住启动
#: 工作的幕布 —— 真正要看的是下面这条时序，不是停留时长。
#:
#: ★ 时序（``_start_fade_out``）：**先叫下一个窗口，再淡出自己**
#: ------------------------------------------------------------
#:
#: 原来反过来（淡出 → finished → 对方才开始建窗口），于是从"开屏淡完"
#: 到"下一个窗口出现"之间有一段**全黑**的等待。实测那一段是 **765ms**：
#:
#:     load_token              150 ms
#:     MainWindow() 构造       253 ms   ← 6 个页面 + 全量 QSS polish
#:     MainWindow.show()        39 ms
#:     set_user()（解密意愿）   ~310 ms
#:     WelcomeWindow + show     14 ms
#:
#: 开屏存在的全部意义就是遮住这段，结果它自己先撤了。改成先建后淡之后，
#: 这 765ms 落在开屏背后 —— 用户看到的是一块安静的开屏，而不是黑屏。
#: 所以停留只要够看清 logo 即可，剩下的时间由这段工作自然占满。
HOLD_MS = 260

_WIDTH = 460
_HEIGHT = 260
_RADIUS = 16

_BG = "#1E293B"
_ACCENT = "#3B82F6"
_TITLE = "#F8FAFC"
_MUTED = "#94A3B8"

#: 中文一律用雅黑 —— 不设的话 Qt 会退回宋体，字会糊
UI_FONT = "Microsoft YaHei UI"


def _render_pixmap(title: str, version: str, slogan: str) -> QPixmap:
    """把开屏画成一张 pixmap。

    自己画而不是拼控件：要淡入淡出的是一个**窗口**，窗口里塞一堆 QLabel
    反而要在动画期间反复重排布局；一张 pixmap 画一次、之后只改窗口透明度。

    ★ ``title`` 里**不要带 emoji**，原因见模块开头（一个字 241ms）。
    """
    pixmap = QPixmap(_WIDTH, _HEIGHT)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    # 圆角深色底
    path = QPainterPath()
    path.addRoundedRect(0, 0, _WIDTH, _HEIGHT, _RADIUS, _RADIUS)
    painter.fillPath(path, QColor(_BG))

    # 左侧一条主色装饰线
    painter.setClipPath(path)
    painter.fillRect(0, 0, 6, _HEIGHT, QColor(_ACCENT))
    painter.setClipping(False)

    # 标题
    painter.setPen(QColor(_TITLE))
    painter.setFont(QFont(UI_FONT, 22, QFont.Weight.Bold))
    painter.drawText(44, 0, _WIDTH - 60, 150,
                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
                     title)

    # 版本号
    painter.setPen(QColor(_ACCENT))
    painter.setFont(QFont(UI_FONT, 11, QFont.Weight.Bold))
    painter.drawText(46, 158, _WIDTH - 60, 24,
                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     version)

    # slogan
    painter.setPen(QColor(_MUTED))
    painter.setFont(QFont(UI_FONT, 11))
    painter.drawText(46, 188, _WIDTH - 80, 24,
                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     slogan)

    # 底部进度条（纯装饰，表示"在启动"）
    painter.fillRect(44, _HEIGHT - 34, _WIDTH - 88, 3, QColor("#334155"))
    painter.fillRect(44, _HEIGHT - 34, int((_WIDTH - 88) * 0.55), 3, QColor(_ACCENT))

    painter.end()
    return pixmap


class SplashScreen(QWidget):
    """开屏。``finished`` 信号发出后才允许起下一个窗口。

    用信号而不是回调，是因为调用方要在**动画真正结束**之后才能动手 ——
    提前 show 主窗口会让两个窗口在屏幕上打架。

    ★ 它是普通 ``QWidget``，不是 ``QSplashScreen`` —— 见模块开头那 865ms。
    """

    finished = Signal()
    #: 淡出跑完、窗口真的关掉了。
    #:
    #: ★ 调用方必须拿它来决定**什么时候可以丢掉开屏的 Python 引用**。
    #:   顶层窗口没有 parent —— 引用一没就被析构，而它的淡出动画 parent
    #:   就在它身上，动画跑到一半源没了 = 0xC0000409（见 ui/anim.py）。
    dismissed = Signal()

    def __init__(
        self,
        title: str = "成绩单透镜",
        version: str = f"v{CURRENT_VERSION}",
        slogan: str = "把成绩单，变成能坐下来谈的东西",
        *,
        hold_ms: int = HOLD_MS,
        fade_ms: int = FADE_MS,
    ) -> None:
        super().__init__(None)
        self._pixmap = _render_pixmap(title, version, slogan)
        # 无边框 + 置顶 + 拖尾透明：跟原来的 QSplashScreen 观感一致
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 不吃鼠标：开屏期间用户在别处的点击不该被它截走
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWindowOpacity(0.0)
        self.resize(_WIDTH, _HEIGHT)
        self._hold_ms = hold_ms
        self._fade_ms = fade_ms
        self._anim: QPropertyAnimation | None = None
        self._done = False
        self._dismissing = False

    # ------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()

    # ------------------------------------------------------------- 居中
    def center_on_screen(self) -> None:
        """放到主屏正中。"""
        screen = QApplication.primaryScreen()
        if screen is None:  # pragma: no cover - 无显示器时
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.center().y() - self.height() // 2,
        )

    # ------------------------------------------------------------- 时序
    def run(self) -> None:
        """开始整套时序：淡入 → 停留 → 淡出 → ``finished``。"""
        self.center_on_screen()
        self.show()
        self._animate(0.0, 1.0, self._after_fade_in)

    def _animate(self, start: float, end: float, on_done) -> None:
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(self._fade_ms)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(
            QEasingCurve.Type.OutCubic if end > start else QEasingCurve.Type.InCubic
        )
        # ★ 必须留引用：动画对象被 GC 掉的话会在下一帧静默停住
        self._anim = anim
        if on_done is not None:
            # ★ **延后一拍**再回调。`on_done` 最后一步会把开屏关掉，
            #   而 AppFlow 接住 `finished` 之后就把 splash 的引用丢了 ——
            #   开屏当场析构，parent 在它上面的这个动画也跟着没。
            #   可 Qt 发完 `finished` 之后**还要继续用这个动画对象**
            #   （注销计时器、处理 deleteWhenStopped），于是踩空 →
            #   整个进程 0xC0000409 直接消失、一个字都不打。
            #   等这一轮事件循环走完再动手就安全了。
            #   context 用 self：开屏先没了的话这次回调自动跳过。
            anim.finished.connect(lambda: QTimer.singleShot(0, self, on_done))
        anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def _after_fade_in(self) -> None:
        QTimer.singleShot(self._hold_ms, self._start_fade_out)

    def _start_fade_out(self) -> None:
        """停留结束：**先叫下一个窗口，再淡出自己**。

        ★ 顺序是命门，别改回去。

        原来是「淡出 → 发 finished → 对方才开始建窗口」，于是开屏淡完
        到下一个窗口出现之间是一段**全黑**的等待（实测 765ms，明细见
        :data:`HOLD_MS` 的注释）。现在先把 ``finished`` 发出去 ——
        接住它的 ``AppFlow._after_splash`` 会**同步**把主窗和欢迎页建好
        （约 765ms），这段时间用户看着的还是开屏本身；等对方都显示好了，
        再淡出自己，视觉上就是一次干脆的交叉淡入淡出。

        ★ ``finished`` 是**同步**发出去的：Qt 的直接连接在 ``emit()`` 里
          就把所有槽跑完。所以 ``self.finished.emit()`` 这一行返回时，
          下一个窗口已经在屏幕上了。
        """
        if self._done:
            return
        self._done = True
        self.finished.emit()
        self._dismiss()

    def _dismiss(self) -> None:
        """淡出并关闭自己。可以安全地重复调用。"""
        if self._dismissing:
            return
        self._dismissing = True
        window_fade(self, self.windowOpacity(), 0.0, self._fade_ms, self._close_now)

    def _close_now(self) -> None:
        self.close()
        self.dismissed.emit()

    # ------------------------------------------------------------- 测试用
    def skip_to_end(self) -> None:
        """跳过动画直接走完（自检/截图脚本用，不进产品路径）。

        ★ 与真实路径**同序**：先 ``finished``（对方建窗口），再关自己。
          只是不等动画 —— 所以测试走的是同一条因果链，不是另一条。
        """
        if self._done:
            return
        self._done = True
        self.finished.emit()
        self._close_now()
