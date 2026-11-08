"""错误信息一键复制（v1.9.3 补充）。

现场问题
========

``--windowed`` 的 exe 没有控制台，程序一旦在某个按钮槽里抛异常，用户看到的
只有"点了没反应"或一句很短的中文提示；维护者拿到的反馈里**没有 traceback、
没有日志路径**，只能来回问。v1.9.3 的 ``core.applog`` 已经把 traceback 写进了
``logs/app.log``，但用户不知道文件在哪，也不会去翻。

这里做两件事：

1. :func:`install_error_copy` 在 ``QApplication`` 上装一个事件过滤器。
   任何以 **Critical 图标**显示的 ``QMessageBox``（现有代码里几十处
   ``QMessageBox.critical(...)``）都会自动多出一个 **「复制错误信息」** 按钮。
   按钮不关闭对话框，只把 **traceback + 日志路径 + 日志末尾** 放进剪贴板。
2. :func:`install_crash_handler` 接管 ``sys.excepthook``：未捕获异常先交给
   ``core.applog`` 落盘，再在主线程弹一个带完整 traceback 的错误框。
   PySide6 6.9.2 的槽函数异常会走 ``sys.excepthook``（已实测），所以这条能
   兜住"按钮点下去就再也没反应"的那类现场。

为什么用事件过滤器，而不是改几十处 ``QMessageBox.critical``
============================================================

* 现有错误弹窗散在 ``ui/`` 和 ``main.py`` 里，逐个改既容易漏，又会把
  "弹窗"和"复制"两件事耦死；
* 过滤器只认 **Critical 图标** 这一件事，所有既有弹窗自动获得按钮；
* 测试里大量用例把 ``QMessageBox.critical`` 换成记录器，过滤器根本不会
  触发 —— 既有测试的"一个真弹窗都不许有"的约定不受影响。

三条硬约束
==========

1. **复制按钮绝不关闭对话框**（普通 ``QPushButton``，不走 QMessageBox 的按钮协议），
   用户可以先复制再关窗；
2. **剪贴板失败不许再弹一个模态框**（那会递归），只把按钮文字改成
   «复制失败» 并保留完整文本在气泡里；
3. **报告里不出现密码 / 授权码 / 找回码** —— 这里只读弹窗文字、
   traceback 与日志末尾；``core.applog`` 本身就不记这些。
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QMessageBox,
    QPushButton,
)

from core import applog
from core.version import CURRENT_VERSION

__all__ = [
    "COPY_BUTTON_TEXT",
    "COPY_DONE_TEXT",
    "COPY_FAILED_TEXT",
    "CRASH_TITLE",
    "LOG_TAIL_LINES",
    "MAX_REPORT_CHARS",
    "build_error_report",
    "install_crash_handler",
    "install_error_copy",
]

#: 用户要求逐字保持的按钮文案。
COPY_BUTTON_TEXT = "复制错误信息"

#: 复制成功 / 失败后按钮上的短提示（不弹第二个框）。
COPY_DONE_TEXT = "已复制"
COPY_FAILED_TEXT = "复制失败"

#: 未捕获异常弹窗的标题。
CRASH_TITLE = "程序运行出错"

#: 按钮气泡。
COPY_TOOLTIP = (
    "复制完整 traceback、日志路径和日志末尾，方便直接粘贴给维护者。"
)

#: 报告里带多少行日志末尾。太多会让粘贴内容变得很长。
LOG_TAIL_LINES = 40

#: 单份报告的上限。traceback 理论上可以很大，粘进聊天框会卡住。
MAX_REPORT_CHARS = 200_000

#: 弹窗不是在异常处理里打开时的诚实说明。
NO_TRACEBACK = "（这个弹窗不是在 Python 异常处理中打开的，没有 traceback）"

#: 日志还没写 / 读不到时的说明。
NO_LOG_TAIL = "（日志文件不存在或为空）"

_ATTR_FILTER = "_tl_error_copy_filter"


def build_error_report(
    *,
    title: str,
    message: str = "",
    traceback_text: str = "",
    details: str = "",
    log_path: str = "",
    log_tail: str = "",
    version: str = "",
    now: str | None = None,
) -> str:
    """把一次错误拼成可直接粘贴的纯文本。

    Args:
        title: 弹窗标题。
        message: 弹窗正文（可含 informativeText）。
        traceback_text: 完整 traceback；空串表示"没有捕获到异常"。
        details: ``QMessageBox.detailedText()``；与 traceback 相同时不重复。
        log_path: ``applog.log_path()`` 的结果。
        log_tail: ``applog.tail()`` 的结果。
        version: 程序版本；省略时用 :data:`core.version.CURRENT_VERSION`。
        now: 时间戳；省略时取当前本地时间（测试可注入固定值）。

    Returns:
        以换行结尾的纯文本；超过 :data:`MAX_REPORT_CHARS` 会截断并注明。
    """
    stamp = now or time.strftime("%Y-%m-%d %H:%M:%S")
    version_text = version or CURRENT_VERSION
    message_text = (message or "").strip() or "（无）"
    trace_text = (traceback_text or "").strip()
    details_text = (details or "").strip()

    lines: list[str] = [
        "===== 成绩单透镜 错误信息 =====",
        f"时间：{stamp}",
        f"版本：v{version_text}",
        f"标题：{title or '（无标题）'}",
        "",
        "----- 错误信息 -----",
        message_text,
    ]
    if details_text and details_text != trace_text:
        lines.extend(["", "----- 详细信息 -----", details_text])
    lines.extend(
        [
            "",
            "----- Traceback -----",
            trace_text or NO_TRACEBACK,
            "",
            "----- 日志路径 -----",
            log_path or "（未知）",
            "",
            f"----- 日志末尾（最近 {LOG_TAIL_LINES} 行）-----",
            (log_tail or "").strip() or NO_LOG_TAIL,
            "",
            "===== 结束 =====",
        ]
    )
    report = "\n".join(lines) + "\n"
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n…（错误信息过长，已截断）\n"
    return report


class _ErrorCopyFilter(QObject):
    """给所有 Critical 图标的 QMessageBox 补一个「复制错误信息」按钮。"""

    def __init__(self, parent: QObject, root: str | None = None) -> None:
        super().__init__(parent)
        self._root = root

    def set_root(self, root: str | None) -> None:
        self._root = root

    def eventFilter(self, obj: Any, event: QEvent) -> bool:
        try:
            if (
                event.type() == QEvent.Type.Show
                and isinstance(obj, QMessageBox)
                and obj.icon() == QMessageBox.Icon.Critical
            ):
                _attach_copy_button(obj, self._root)
        except Exception:  # noqa: BLE001 - 过滤器绝不能把弹窗本身搞崩
            pass
        return False


def install_error_copy(app: Any = None, root: str | None = None) -> bool:
    """在 QApplication 上安装复制按钮过滤器（幂等）。

    Returns:
        ``True`` 表示已安装（或已经装过）；``False`` 表示当前没有 QApplication。
    """
    application = app if app is not None else QApplication.instance()
    if application is None:
        return False
    existing = getattr(application, _ATTR_FILTER, None)
    if existing is not None:
        existing.set_root(root)
        return True
    flt = _ErrorCopyFilter(application, root)
    application.installEventFilter(flt)
    setattr(application, _ATTR_FILTER, flt)
    return True


def _attach_copy_button(box: QMessageBox, root: str | None) -> None:
    """在 Show 事件里给一个 Critical 弹窗加按钮。"""
    if getattr(box, "_tl_copy_added", False):
        return
    # 先抓 traceback：此时 QMessageBox.critical(...) 还停在调用方的 except
    # 块里，sys.exc_info() 正好是触发这个弹窗的那个异常。
    if getattr(box, "_tl_traceback", None) is None:
        info = sys.exc_info()
        if info[0] is not None:
            try:
                box._tl_traceback = "".join(traceback.format_exception(*info))
            except Exception:  # noqa: BLE001
                box._tl_traceback = f"{info[0].__name__}: {info[1]}"
        else:
            box._tl_traceback = ""
    box._tl_copy_root = root

    button = _make_copy_button(box)
    if button is None:
        return
    button.clicked.connect(
        lambda _checked=False, target=box, btn=button: _on_copy_clicked(target, btn)
    )
    box._tl_copy_added = True


def _make_copy_button(box: QMessageBox) -> QPushButton | None:
    """造一个**不会被 QMessageBox 当成"确定"**的复制按钮。

    ★ 为什么不用 ``box.addButton(...)``：QMessageBox 对任何标准按钮都会
      在点击后 `done()` 关窗（实测 ActionRole 也关）。复制失败时窗口一关，
      用户就再也看不到完整错误了。这里把普通 QPushButton 直接塞进
      ``QDialogButtonBox`` 的布局里 —— 它不参与 QMessageBox 的按钮协议，
      点完窗口留着；布局仍由 Qt 负责，不会压住「确定」。
    """
    button_box = box.findChild(QDialogButtonBox)
    if button_box is not None and button_box.layout() is not None:
        button = QPushButton(COPY_BUTTON_TEXT, button_box)
        button.setObjectName("CopyErrorInfoButton")
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setToolTip(COPY_TOOLTIP)
        button_box.layout().addWidget(button)
        button_box.updateGeometry()
        try:
            box.adjustSize()
        except Exception:  # noqa: BLE001 - 调整尺寸失败不影响复制
            pass
        return button
    # 兜底：没有标准按钮盒的极少数弹窗只能走 QMessageBox 的按钮协议。
    fallback = box.addButton(COPY_BUTTON_TEXT, QMessageBox.ButtonRole.ActionRole)
    fallback.setObjectName("CopyErrorInfoButton")
    fallback.setAutoDefault(False)
    fallback.setToolTip(COPY_TOOLTIP)
    return fallback


def _on_copy_clicked(box: QMessageBox, button: Any) -> None:
    """按钮点击：拼报告 → 写剪贴板 → 改按钮文字（不关窗）。"""
    try:
        report = _report_for_box(box)
    except Exception:  # noqa: BLE001 - 拼报告失败也不能把按钮变成异常源
        report = build_error_report(
            title=box.windowTitle(),
            message=box.text(),
            log_path="（拼装错误报告时出错）",
        )
    ok = _copy_to_clipboard(report)
    try:
        button.setText(COPY_DONE_TEXT if ok else COPY_FAILED_TEXT)
        button.setToolTip(
            "已复制到剪贴板，可以直接粘贴反馈。"
            if ok
            else "剪贴板不可用，请手动选中文本复制。"
        )
    except Exception:  # noqa: BLE001 - 按钮可能已被关闭
        pass


def _report_for_box(box: QMessageBox) -> str:
    """从弹窗本身 + 捕获到的 traceback + 日志拼出报告。"""
    root = getattr(box, "_tl_copy_root", None)
    trace_text = getattr(box, "_tl_traceback", "") or ""
    if not trace_text:
        info = sys.exc_info()
        if info[0] is not None:
            trace_text = "".join(traceback.format_exception(*info))
    message = getattr(box, "_tl_report_message", None)
    if not message:
        message = box.text() or ""
        informative = box.informativeText() or ""
        if informative:
            message = f"{message}\n\n{informative}" if message else informative
    try:
        log_path = str(applog.log_path(root))
        log_tail = applog.tail(LOG_TAIL_LINES, root=root)
    except Exception:  # noqa: BLE001 - 日志读不到也要能复制 traceback
        log_path = "（读取日志路径失败）"
        log_tail = ""
    return build_error_report(
        title=box.windowTitle() or "错误",
        message=message,
        traceback_text=trace_text,
        details=box.detailedText() or "",
        log_path=log_path,
        log_tail=log_tail,
    )


def _copy_to_clipboard(text: str) -> bool:
    """写剪贴板；失败返回 ``False``（**不弹第二个模态框**）。"""
    try:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        return True
    except Exception:  # noqa: BLE001 - 某些环境剪贴板链会卡死/不可用
        return False


# ---------------------------------------------------------------------------
# 未捕获异常：先落盘，再弹一个带完整 traceback 的框
# ---------------------------------------------------------------------------

_CRASH_STATE: dict[str, Any] = {
    "installed": False,
    "root": None,
    "showing": False,
    "previous": None,
}


def install_crash_handler(root: str | None = None) -> bool:
    """接管 ``sys.excepthook``，把未捕获异常变成看得见的错误框。

    ★ 调用顺序：先 ``core.applog.install_excepthook()``，再调用本函数。
      这样"落日志"仍然由 applog 负责，本函数只负责在日志之后再弹框。

    Returns:
        ``True`` 表示已安装（或已经装过）；``False`` 表示当前没有 QApplication。
    """
    application = QApplication.instance()
    if application is None:
        return False
    install_error_copy(application, root)
    _CRASH_STATE["root"] = root
    if _CRASH_STATE["installed"]:
        return True

    previous = sys.excepthook
    _CRASH_STATE["previous"] = previous

    def _hook(exc_type, exc, tb) -> None:
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:  # noqa: BLE001
            text = f"{exc_type.__name__}: {exc}"
        # 先让 applog（以及它链着的默认钩子）落盘 / 打印 —— 日志优先。
        try:
            previous(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass
        try:
            _schedule_crash_dialog(text, exc_type, exc)
        except Exception:  # noqa: BLE001 - 钩子自己绝不能炸
            pass

    sys.excepthook = _hook
    _CRASH_STATE["installed"] = True
    return True


def _schedule_crash_dialog(text: str, exc_type: type, exc: BaseException) -> None:
    """把弹框排到 GUI 线程的下一个事件循环（异常可能来自任意线程）。"""
    if threading.current_thread() is not threading.main_thread():
        # 非主线程的未捕获异常没有可靠的 Qt 事件循环可挂，日志里已经留痕。
        return
    QTimer.singleShot(
        0, lambda: _show_crash_dialog(text, exc_type, exc)
    )


def _show_crash_dialog(text: str, exc_type: type, exc: BaseException) -> None:
    """在主线程弹未捕获异常框。带完整 traceback，复制按钮由过滤器补上。"""
    if _CRASH_STATE["showing"]:
        return
    application = QApplication.instance()
    if application is None:
        return
    _CRASH_STATE["showing"] = True
    try:
        root = _CRASH_STATE["root"]
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(CRASH_TITLE)
        box.setText(
            "程序运行时遇到了一个未处理的异常。\n\n"
            f"{exc_type.__name__}: {exc}"
        )
        box.setInformativeText(
            f"详细日志：{applog.log_path(root)}\n\n"
            "点「复制错误信息」可把完整 traceback、日志路径和日志末尾"
            "复制到剪贴板，直接粘贴给维护者即可。\n"
            "程序可能仍能继续使用，但建议复制后重启。"
        )
        # 完整 traceback 放"显示详细信息"里，同时给复制按钮用。
        box.setDetailedText(text)
        box._tl_traceback = text
        # 复制到剪贴板的正文只要"异常摘要"，不要把界面操作说明也粘进去。
        box._tl_report_message = box.text()
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
    finally:
        _CRASH_STATE["showing"] = False
