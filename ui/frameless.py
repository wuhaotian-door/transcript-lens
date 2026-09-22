"""无边框窗口：圆角 + 真阴影 + 自定义标题栏 + 拖拽 + 边缘缩放。

为什么要有这个模块
------------------

商业质感要求"没有系统那圈复古边框、没有白边、圆角、带阴影"。Qt 里只能靠
``Qt.FramelessWindowHint`` + ``WA_TranslucentBackground`` 自绘，代价是
**系统提供的一切都得自己补**：拖拽、缩放、最大化、关闭。

所以这里把补的部分收成一个模块，两个窗口（登录窗 / 主窗口）共用一套实现。

★★ 三条踩过 / 查证过的坑，都写在代码里了 ★★

1. **圆角不会裁剪子控件。** QSS 的 ``border-radius`` 只管这个控件自己画的背景，
   子控件照样画方的。所以窗口最外圈那几个"贴边"的子控件（侧栏、顶栏…）
   **必须各自带对应角的圆角**，否则四角会露出方角。
2. **拖拽/缩放不要自己算像素。** ``QWindow.startSystemMove()`` /
   ``startSystemResize()`` 把这件事交还给系统：多显示器 DPI、
   Win+方向键贴边、Aero Snap 全都自动正确。手算 ``move()`` 这些全都没有。
   系统不支持时才退回手算。
3. **最大化时透明边距会露馅。** 无边框窗口四周留了
   :data:`SHADOW_MARGIN` 的透明边用来画阴影；最大化之后这圈透明边会变成
   屏幕边缘的一道缝。所以状态一变就把边距收成 0、圆角关掉。

⚠️ **自动化测试不要调 startSystemMove / startSystemResize** ——
它们会起一个**模态的系统循环**，没人松鼠标就永远不返回，测试会挂死。
可测的部分（命中判定、光标形状）都抽成了纯函数，见 :func:`edges_at` /
:func:`cursor_for`。
"""

from __future__ import annotations

import sys
from typing import Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.anim import apply_shadow

__all__ = [
    "CORNER_RADIUS",
    "RESIZE_MARGIN",
    "SHADOW_BLUR",
    "SHADOW_MARGIN",
    "TITLE_BAR_HEIGHT",
    "WindowTitleBar",
    "apply_frameless",
    "build_root_frame",
    "cursor_for",
    "edges_at",
    "install_chrome",
    "property_name_is_safe",
    "toggle_maximize",
]

#: 窗口四周留给阴影的透明边距
SHADOW_MARGIN = 14

#: 窗口圆角（与 ui/styles.qss 里 #WindowRoot 的 border-radius 必须一致）
CORNER_RADIUS = 12

#: 窗口阴影模糊度
SHADOW_BLUR = 15

#: 边缘多少像素以内算"抓边框"
RESIZE_MARGIN = 6

#: 自定义标题栏高度
TITLE_BAR_HEIGHT = 36

#: 标题栏按钮尺寸
_BUTTON_W = 40
_BUTTON_H = 26

#: 标记"当前是最大化"的**动态属性名**，QSS 靠 ``[tlMaximized="true"]`` 选它。
#:
#: ★★ 千万别叫 ``maximized`` ★★ —— ``QWidget`` **自带**一个只读属性
#:    ``maximized``（``Q_PROPERTY(bool maximized READ isMaximized)``）。
#:    往一个只读属性上 ``setProperty`` 会被**静默拒绝**（返回 ``False``，
#:    不抛异常、不打日志），于是 QSS 里的 ``[maximized="true"]`` 永远匹配不上：
#:    最大化时圆角关不掉、白边留着，而一点报错都没有。
#:    实测确认过：``QWidget.metaObject()`` 里 ``maximized`` 的 ``isWritable()`` 是 False。
#:    所以这里换个不可能撞名的名字，并在 :func:`property_name_is_safe` 里钉死。
MAXIMIZED_PROP = "tlMaximized"


# ---------------------------------------------------------------------------
# 纯函数：命中判定与光标（**可单测，不会起系统循环**）
# ---------------------------------------------------------------------------


def edges_at(pos: QPoint, rect: QRect, margin: int = RESIZE_MARGIN) -> Qt.Edge | None:
    """点 ``pos``（窗口坐标）落在 ``rect`` 的哪几条边上？不在边上返回 ``None``。

    ``rect`` 传的是**可见窗口**（即圆角容器）的几何，不是顶层窗口的几何 ——
    四周那圈透明边距是画阴影用的，用户看不见，不该给人去抓。

    ★ 判空**不能写 ``int(edges)``**。PySide6 的 ``Qt.Edge`` 是 ``enum.Flag``
      而不是 ``IntFlag``，``int()`` 直接抛 ``TypeError``。而这个分支是
      **鼠标每动一下都会走到**的，于是"移动鼠标"就变成了一个异常源：
      异常从 ``eventFilter`` 里冒出来，Qt 只能打一行
      "Error calling Python override of QObject::eventFilter()" 然后吞掉 ——
      界面上是"边框光标永远不变"，日志里是看不懂的 TypeError。
      实测崩过一次（自动化里鼠标恰好停在窗口上时才复现）。
      用 ``bool(edges)``（``Flag`` 有定义好的 ``__bool__``）或者 ``edges.value``。
    """
    if not rect.contains(pos):
        return None
    edges = Qt.Edge(0)
    if pos.x() <= rect.left() + margin:
        edges |= Qt.Edge.LeftEdge
    if pos.x() >= rect.right() - margin:
        edges |= Qt.Edge.RightEdge
    if pos.y() <= rect.top() + margin:
        edges |= Qt.Edge.TopEdge
    if pos.y() >= rect.bottom() - margin:
        edges |= Qt.Edge.BottomEdge
    return edges if edges else None


def cursor_for(edges: Qt.Edge | None) -> Qt.CursorShape:
    """边组合 → 鼠标形状。"""
    if edges is None:
        return Qt.CursorShape.ArrowCursor
    left = bool(edges & Qt.Edge.LeftEdge)
    right = bool(edges & Qt.Edge.RightEdge)
    top = bool(edges & Qt.Edge.TopEdge)
    bottom = bool(edges & Qt.Edge.BottomEdge)
    if (left and top) or (right and bottom):
        return Qt.CursorShape.SizeFDiagCursor
    if (right and top) or (left and bottom):
        return Qt.CursorShape.SizeBDiagCursor
    if left or right:
        return Qt.CursorShape.SizeHorCursor
    if top or bottom:
        return Qt.CursorShape.SizeVerCursor
    return Qt.CursorShape.ArrowCursor


# ---------------------------------------------------------------------------
# 自定义标题栏
# ---------------------------------------------------------------------------


class WindowTitleBar(QFrame):
    """细标题栏：左边文字，中间可塞内容，右边 — / □ / ×。

    Signals:
        minimize_requested / maximize_requested / close_requested:
            按钮被点了。**窗口动作由窗口自己接**，标题栏不越权。

    Args:
        content: 塞在标题和按钮中间、并且会吃掉剩余宽度的控件。
            主窗口用它放"用户名 + 激活状态 + 当前页名" —— 那条顶栏本来就是
            窗口最上面的一条，直接当标题栏用，不必再多一条。
        bar_height: 这一条多高。默认 :data:`TITLE_BAR_HEIGHT`。
        object_name: 换一个 objectName 就能换一套 QSS。主窗口传 ``"TopBar"``
            以复用顶栏样式（白色底 + 右上角圆角）。

    拖拽优先走 ``startSystemMove()``：那是系统级移动，能保住多显示器 DPI、
    贴边（Aero Snap）和 Win+方向键。系统不支持时才退回手算 ``move()``。
    """

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(
        self,
        title: str = "",
        *,
        minimizable: bool = True,
        maximizable: bool = False,
        closable: bool = True,
        content: QWidget | None = None,
        bar_height: int = TITLE_BAR_HEIGHT,
        object_name: str = "WindowTitleBar",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setFixedHeight(bar_height)
        self._drag_offset: QPoint | None = None
        self._minimizable = minimizable
        self._maximizable = maximizable

        box = QHBoxLayout(self)
        box.setContentsMargins(14, 0, 6, 0)
        box.setSpacing(8)

        self._title = QLabel(title)
        self._title.setObjectName("WindowTitle")
        # 空标题就别占位置了 —— 主窗口那条顶栏自带内容，不需要再来一个名字
        self._title.setVisible(bool(title))
        box.addWidget(self._title)

        #: 中间那块内容。**必须留引用**，否则会被 GC 掉（控件从界面上消失）。
        self._content = content
        if content is None:
            # 没有内容才用弹性空白把标题顶到左边。
            # ★ 有内容时**绝不能**再加这一条：两侧各一个 stretch，内容就被挤到
            #   正中间去了（踩过：顶栏的"用户名 + 激活状态"跑到了窗口正中）。
            box.addStretch(1)
        else:
            box.addWidget(content, 1)

        if minimizable:
            box.addWidget(self._button("—", "TitleButton", "最小化", self.minimize_requested))
        if maximizable:
            self._max_button = self._button(
                "□", "TitleButton", "最大化 / 还原", self.maximize_requested
            )
            box.addWidget(self._max_button)
        if closable:
            box.addWidget(
                self._button("✕", "TitleButtonClose", "关闭", self.close_requested)
            )

    def _button(self, text: str, name: str, tip: str, signal) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setToolTip(tip)
        button.setFixedSize(_BUTTON_W, _BUTTON_H)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(signal.emit)
        return button

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def set_maximized(self, maximized: bool) -> None:
        """把 □ 换成 ❐，让按钮说的和窗口状态一致。"""
        if not self._maximizable:
            return
        self._max_button.setText("❐" if maximized else "□")

    # ------------------------------------------------------------ 拖拽
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        window = self.window()
        if window.isMaximized():
            # 最大化时不允许拖动：还原+跟随光标是另一套交互，这里不半吊子做
            event.accept()
            return
        handle = window.windowHandle()
        if handle is not None and handle.startSystemMove():
            event.accept()
            return
        self._drag_offset = (
            event.globalPosition().toPoint() - window.frameGeometry().topLeft()
        )
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._drag_offset is None or not (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            super().mouseMoveEvent(event)
            return
        window = self.window()
        if not window.isMaximized():
            window.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._maximizable and event.button() == Qt.MouseButton.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------
# 窗口本身
# ---------------------------------------------------------------------------


def property_name_is_safe(name: str = MAXIMIZED_PROP) -> bool:
    """自检：``name`` 在 QWidget 上是不是**可写**的动态属性名。

    ``setProperty`` 对**只读**属性会静默失败（返回 ``False``，不抛异常）。
    ``QWidget`` 自带的 ``maximized`` / ``minimized`` / ``fullScreen``
    就是只读的，谁拿它们做 QSS 状态开关，谁就会遇到"样式没生效但一切正常"。
    这条自检把那类事故挡在前面。
    """
    if QApplication.instance() is None:  # pragma: no cover - 没有 QApplication 时没法探
        return True
    probe = QFrame()
    meta = probe.metaObject()
    for i in range(meta.propertyCount()):
        prop = meta.property(i)
        if prop.name() == name:
            return prop.isWritable()
    return True


def apply_frameless(window: QWidget) -> None:
    """把顶层窗口变成无边框 + 透明底。

    ``WA_TranslucentBackground`` 是圆角的关键：没有它，窗口底色会把四个圆角
    外面的区域填成方块。设了之后，只有我们自己画的圆角容器是可见的。

    ⚠ 必须在 ``show()`` 之前调用；``setWindowFlag`` 会把已经显示的窗口藏起来。
    """
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    window.setObjectName("FramelessWindow")


def build_root_frame(container: QWidget, *, name: str = "WindowRoot") -> QFrame:
    """在 ``container`` 上铺一层留了阴影边距的布局，返回那个圆角容器。

    之后所有内容都往返回的 frame 里塞。``container`` 对 ``QMainWindow``
    来说应该是 ``centralWidget()``（QMainWindow 自己的布局由 Qt 管着，
    不该硬塞）。
    """
    from PySide6.QtWidgets import QVBoxLayout

    layout = QVBoxLayout(container)
    layout.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
    layout.setSpacing(0)

    frame = QFrame(container)
    frame.setObjectName(name)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    # 窗口阴影。模糊度 15、垂直偏移 2 —— 太大像发光，太硬像描边。
    apply_shadow(frame, blur=SHADOW_BLUR, dy=2)
    layout.addWidget(frame)
    return frame


class _Chrome(QObject):
    """给一个无边框窗口补上：边缘缩放 + 最大化时的边距收放。

    做成 ``QObject`` 挂在窗口上，是为了让它随窗口一起销毁；
    事件过滤器装在 ``QApplication`` 上（子控件会把鼠标事件吃掉，
    只装在顶层窗口上根本收不到）。
    """

    def __init__(
        self,
        window: QWidget,
        root: QFrame,
        *,
        resizable: bool = True,
        maximizable: bool = False,
        corners: Sequence[QWidget] = (),
    ) -> None:
        super().__init__(window)
        self._window = window
        self._root = root
        self._resizable = resizable
        self._maximizable = maximizable
        #: 贴边的子控件（侧栏 / 顶栏 / 标题栏…）。圆角不会裁剪子控件，
        #: 所以它们得各自带对应角的圆角；最大化时又得一起取消。
        self._corners: tuple[QWidget, ...] = tuple(corners)
        self._edge_cursor: Qt.CursorShape | None = None
        #: 手算缩放的中间状态（系统不支持 startSystemResize 时才用）
        self._manual_edges: Qt.Edge | None = None
        self._manual_start_geo: QRect | None = None
        self._manual_start_pos: QPoint | None = None

        app = QApplication.instance()
        self._app = app
        if app is not None:
            app.installEventFilter(self)
        window.installEventFilter(self)

    # ------------------------------------------------------------ 过滤器
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        etype = event.type()

        if obj is self._window and etype == QEvent.Type.WindowStateChange:
            self._sync_state()
            return False
        if obj is self._window and etype in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self._sync_state()
            return False

        if not self._resizable or not isinstance(obj, QWidget):
            return False
        if obj.window() is not self._window:
            return False

        if etype == QEvent.Type.MouseMove:
            if self._manual_edges is not None and (
                event.buttons() & Qt.MouseButton.LeftButton
            ):
                self._manual_resize(event.globalPosition().toPoint())
                return True
            if not event.buttons():
                self._hover(obj.mapTo(self._window, event.position().toPoint()))
        elif (
            etype == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            return self._begin(obj.mapTo(self._window, event.position().toPoint()),
                               event.globalPosition().toPoint())
        elif etype == QEvent.Type.MouseButtonRelease and self._manual_edges is not None:
            self._end_manual()
        elif etype == QEvent.Type.Leave:
            self._set_edge_cursor(Qt.CursorShape.ArrowCursor)
        return False

    # ------------------------------------------------------------ 缩放
    def _root_rect(self) -> QRect:
        """圆角容器在**窗口坐标**里的矩形。

        ★ 不能在 ``_root.geometry()`` 上直接判断：那是相对它自己父控件的，
          而主窗口的父是 ``centralWidget()``、登录窗的父就是窗口本身 ——
          两种情况坐标系不一样。``mapTo`` 一次就统一了。
        """
        return QRect(self._root.mapTo(self._window, QPoint(0, 0)), self._root.size())

    def _hover(self, pos: QPoint) -> None:
        if self._window.isMaximized():
            self._set_edge_cursor(Qt.CursorShape.ArrowCursor)
            return
        self._set_edge_cursor(cursor_for(edges_at(pos, self._root_rect())))

    def _begin(self, pos: QPoint, global_pos: QPoint) -> bool:
        if self._window.isMaximized():
            return False
        edges = edges_at(pos, self._root_rect())
        if edges is None:
            return False
        handle = self._window.windowHandle()
        if handle is not None and handle.startSystemResize(edges):
            return True
        # 系统不支持 → 自己算。grabMouse 保证鼠标移出窗口也能收到 move。
        self._manual_edges = edges
        self._manual_start_geo = self._window.geometry()
        self._manual_start_pos = global_pos
        self._window.grabMouse()
        return True

    def _manual_resize(self, global_pos: QPoint) -> None:
        if self._manual_start_geo is None or self._manual_start_pos is None:
            return
        delta = global_pos - self._manual_start_pos
        geo = QRect(self._manual_start_geo)
        min_w = self._window.minimumWidth()
        min_h = self._window.minimumHeight()
        edges = self._manual_edges
        if edges & Qt.Edge.LeftEdge:
            geo.setLeft(min(geo.left() + delta.x(), geo.right() - min_w))
        if edges & Qt.Edge.RightEdge:
            geo.setRight(max(geo.right() + delta.x(), geo.left() + min_w))
        if edges & Qt.Edge.TopEdge:
            geo.setTop(min(geo.top() + delta.y(), geo.bottom() - min_h))
        if edges & Qt.Edge.BottomEdge:
            geo.setBottom(max(geo.bottom() + delta.y(), geo.top() + min_h))
        self._window.setGeometry(geo)

    def _end_manual(self) -> None:
        self._manual_edges = None
        self._manual_start_geo = None
        self._manual_start_pos = None
        self._window.releaseMouse()

    def _set_edge_cursor(self, shape: Qt.CursorShape) -> None:
        if shape is self._edge_cursor:
            return
        self._edge_cursor = shape
        self._window.setCursor(shape)

    # ------------------------------------------------------------ 最大化
    def _sync_state(self) -> None:
        """最大化时把阴影边距收掉、圆角关掉。

        不收的话，四周那圈透明边会在屏幕边缘留一道缝，
        而圆角会和屏幕直角打架。
        """
        maximized = self._window.isMaximized()
        container = self._root.parentWidget()
        layout = container.layout() if container is not None else None
        if layout is not None:
            margin = 0 if maximized else SHADOW_MARGIN
            if layout.contentsMargins().left() != margin:
                layout.setContentsMargins(margin, margin, margin, margin)
        effect = self._root.graphicsEffect()
        if effect is not None:
            # 最大化时阴影必然被裁掉，关掉它省一次全窗口模糊
            effect.setEnabled(not maximized)

        flag = "true" if maximized else "false"
        for widget in (self._root, *self._corners):
            if str(widget.property(MAXIMIZED_PROP) or "") == flag:
                continue
            widget.setProperty(MAXIMIZED_PROP, flag)
            # ★ 不能信 setProperty 的返回值：只读属性会静默失败，而动态属性
            #   在 PySide6 下也**可能返回假值**（实测 tlRounded 明明写进去了却
            #   返回 False）。所以唯一可信的判据是**读回来对不对**。
            if str(widget.property(MAXIMIZED_PROP) or "") != flag:
                print(
                    f"[frameless] 设不上属性 {MAXIMIZED_PROP}={flag}"
                    f"（{widget.objectName() or type(widget).__name__}）"
                    f" —— 最大化时圆角会不对。",
                    file=sys.stderr,
                )
                continue
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        for bar in self._window.findChildren(WindowTitleBar):
            bar.set_maximized(maximized)

    # ------------------------------------------------------------ 测试用
    @property
    def root(self) -> QFrame:
        return self._root


def install_chrome(
    window: QWidget,
    root: QFrame,
    *,
    resizable: bool = True,
    maximizable: bool = False,
    corners: Sequence[QWidget] = (),
) -> _Chrome:
    """给无边框窗口装上边缘缩放与最大化补丁。

    Args:
        corners: 贴着窗口边的子控件（侧栏 / 顶栏 / 标题栏…）。
            **圆角不裁剪子控件**，所以它们的圆角要由 QSS 单独给，
            最大化时也要跟着一起取消 —— 这里统一驱动它们的状态。

    ★ 返回值**必须挂回窗口上**：事件过滤器不持有 Python 引用，
      只把 chrome 当局部变量的话它会被 GC 掉，缩放就悄悄失效了
      （而 Qt 那边过滤器也被一并摘掉，界面上只是"抓边框没反应"）。
    """
    chrome = _Chrome(
        window, root, resizable=resizable, maximizable=maximizable, corners=corners
    )
    window._tl_chrome = chrome  # noqa: SLF001 - 故意保命，防止被 GC
    return chrome


def toggle_maximize(window: QWidget) -> None:
    """最大化 / 还原。无边框窗口必须自己接这个动作。"""
    if window.isMaximized():
        window.showNormal()
    else:
        window.showMaximized()
