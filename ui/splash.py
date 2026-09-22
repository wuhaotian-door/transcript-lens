"""开屏 —— QSplashScreen，淡入 → 停留 → 淡出 → 发信号。

流程（Spec 二，全部走 QTimer / QPropertyAnimation，**没有一处 time.sleep**）::

    opacity 0 → 1   (300ms)
    停留 800ms      (QTimer.singleShot)
    opacity 1 → 0   (300ms)
    finished 信号   → 主程序决定接下来起登录窗还是直接进主界面

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
from PySide6.QtWidgets import QApplication, QSplashScreen

__all__ = ["FADE_MS", "HOLD_MS", "SplashScreen"]

#: 淡入 / 淡出各用多久
FADE_MS = 300
#: 完全显示后停留多久（Spec 二：800ms）
HOLD_MS = 800

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

    QSplashScreen 只认 pixmap，所以文字得自己用 QPainter 画上去 ——
    好处是字号、间距、圆角完全可控。
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


class SplashScreen(QSplashScreen):
    """开屏。``finished`` 信号发出后才允许起下一个窗口。

    用信号而不是回调，是因为调用方要在**动画真正结束**之后才能动手 ——
    提前 show 主窗口会让两个窗口在屏幕上打架。
    """

    finished = Signal()

    def __init__(
        self,
        title: str = "📊 成绩单透镜",
        version: str = "v1.0",
        slogan: str = "把成绩单，变成能坐下来谈的东西",
        *,
        hold_ms: int = HOLD_MS,
        fade_ms: int = FADE_MS,
    ) -> None:
        super().__init__(_render_pixmap(title, version, slogan))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.0)
        self._hold_ms = hold_ms
        self._fade_ms = fade_ms
        self._anim: QPropertyAnimation | None = None
        self._done = False

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
        self._animate(1.0, 0.0, self._emit_finished)

    def _emit_finished(self) -> None:
        if self._done:  # 防重入：finished 只该发一次
            return
        self._done = True
        self.close()
        self.finished.emit()

    # ------------------------------------------------------------- 测试用
    def skip_to_end(self) -> None:
        """跳过动画直接发 ``finished``（自检/截图脚本用，不进产品路径）。"""
        self._emit_finished()
