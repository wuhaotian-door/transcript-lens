"""本地推理后端的安装卡片（v1.9.2）。

BackendSetupCard 是一个三态卡片：就绪 / 可一键安装 / 缺少系统 Python。
模型中心与档案页的 AI 辅导入口共用这一个组件。

约束：

* 纯 PySide6 实现，自己绝不联网；pip 由 core.ai.runtime_setup 在后台线程里跑。
* 子线程绝不碰控件：线程只把输出行放进 queue.Queue，主线程用 QTimer 轮询取走；
  线程结束后经 QTimer.singleShot(0, ...) 回到主线程更新界面。
* 绝不静默安装：只有用户点按钮、并在确认框里点「是」之后才会启动安装。
* 所有文案都是纯文本，不使用 Markdown 记号。
"""

from __future__ import annotations

import logging
import queue
from typing import List, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.ai import runtime_setup

logger = logging.getLogger(__name__)

__all__ = [
    "BackendSetupCard",
    "COPY_HINT_TEXT",
    "INSTALL_QUESTION",
    "TITLE_TEXT",
]

TITLE_TEXT = "本地推理后端"
INSTALL_BUTTON_TEXT = "一键安装"
CANCEL_BUTTON_TEXT = "取消"
COPY_BUTTON_TEXT = "复制命令"
READY_BADGE_TEXT = "已就绪"
COPY_HINT_TEXT = "已复制，请在命令行执行后重启程序"

# 只有用户点过确认弹窗，才会联网安装。
INSTALL_QUESTION = "将联网安装 llama-cpp-python 到本程序目录，约 60MB，确定继续？"
CONFIRM_TITLE = "安装推理后端"

# 安装线程结束后回主线程的延迟（毫秒）；0 = 排到事件循环下一轮。
POLL_INTERVAL_MS = 120
MAX_LINES_SHOWN = 2000
STOP_WAIT_MS = 3000


def _tail_lines(lines: List[str], limit: int = MAX_LINES_SHOWN) -> List[str]:
    """输出很长时只保留最后一段，避免 QPlainTextEdit 被撑爆。"""
    if len(lines) <= limit:
        return list(lines)
    notice = f"（输出较长，只显示最后 {limit} 行）"
    return [notice, *lines[-limit:]]


class BackendSetupCard(QFrame):
    """推理后端安装卡片。三态：ready / installable / missing-python。"""

    ready = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")

        self._state = ""
        self._thread: Optional[runtime_setup.BackendInstallThread] = None
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._output: List[str] = []
        self._last_command = ""
        self._shutdown = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        self._title = QLabel(TITLE_TEXT, self)
        self._title.setObjectName("H2")
        layout.addWidget(self._title)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        # 任务书 §4 指定的「未安装」文案：单列一行，不改 backend_status() 的措辞
        # （那边的文案被 core 的测试逐字断言）。
        self._install_hint = QLabel("", self)
        self._install_hint.setObjectName("Muted")
        self._install_hint.setWordWrap(True)
        layout.addWidget(self._install_hint)

        self._ready_badge = QLabel(READY_BADGE_TEXT, self)
        self._ready_badge.setObjectName("Notice")
        layout.addWidget(self._ready_badge)

        self._progress = QPlainTextEdit(self)
        self._progress.setReadOnly(True)
        self._progress.setMaximumBlockCount(MAX_LINES_SHOWN)
        self._progress.setMinimumHeight(96)
        self._progress.setPlaceholderText("安装输出会显示在这里。")
        layout.addWidget(self._progress)

        self._result = QLabel("", self)
        self._result.setWordWrap(True)
        layout.addWidget(self._result)

        self._command_row = QWidget(self)
        command_layout = QHBoxLayout(self._command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(8)
        # ★ 命令用 QLabel 而不是只读 QLineEdit：只读 QLineEdit 会被
        #   findChildren(QLineEdit) 当成「输入框」，档案页 AI 辅导的测试就把它
        #   当成了对话框输入框。QLabel 可选可复制，同样满足「单行显示完整命令」。
        self._command_label = QLabel("", self._command_row)
        self._command_label.setObjectName("Muted")
        self._command_label.setWordWrap(True)
        self._command_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        command_layout.addWidget(self._command_label, 1)
        self._copy_button = QPushButton(COPY_BUTTON_TEXT, self._command_row)
        command_layout.addWidget(self._copy_button)
        layout.addWidget(self._command_row)

        self._copy_hint = QLabel("", self)
        self._copy_hint.setObjectName("Muted")
        self._copy_hint.setWordWrap(True)
        layout.addWidget(self._copy_hint)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        self._install_button = QPushButton(INSTALL_BUTTON_TEXT, self)
        self._cancel_button = QPushButton(CANCEL_BUTTON_TEXT, self)
        buttons.addWidget(self._install_button)
        buttons.addWidget(self._cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_install)

        self._install_button.clicked.connect(self._request_install)
        self._cancel_button.clicked.connect(self._cancel_install)
        self._copy_button.clicked.connect(self._copy_command)

        self.refresh()

    # -- 对外接口 -----------------------------------------------------------

    def refresh(self) -> None:
        """重新探测状态并刷新界面；安装进行中时只刷新文案，不动按钮。"""
        if self._shutdown:
            return
        try:
            available, message = runtime_setup.backend_status()
        except Exception:  # noqa: BLE001 - 状态探测失败也必须给出人话
            logger.exception("探测推理后端状态失败")
            available, message = False, "暂时无法判断推理后端的状态，请稍后重试。"
        python = None if available else self._system_python()
        self._apply_state(available, message, python)

    def stop(self) -> None:
        """窗口关闭时调用：停掉轮询并请求取消安装。"""
        self._shutdown = True
        self._poll_timer.stop()
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        try:
            thread.cancel()
        except Exception:  # noqa: BLE001
            logger.debug("取消安装线程失败", exc_info=True)
        try:
            thread.join(STOP_WAIT_MS / 1000.0)
        except Exception:  # noqa: BLE001
            logger.debug("等待安装线程收尾失败", exc_info=True)

    # -- 状态渲染 -----------------------------------------------------------

    @staticmethod
    def _system_python() -> Optional[str]:
        try:
            return runtime_setup.find_system_python()
        except Exception:  # noqa: BLE001 - 探不到就当没有
            logger.warning("探测系统 Python 失败", exc_info=True)
            return None

    def _fallback_command(self) -> str:
        try:
            return runtime_setup.install_command_text()
        except Exception:  # noqa: BLE001
            return ""

    def _apply_state(self, available: bool, message: str, python: Optional[str]) -> None:
        installing = self._state == "installing"
        if available:
            state = "ready"
        elif python:
            state = "installable"
        else:
            state = "missing-python"
        if installing and not available:
            # 安装进行中：保持进度区与取消按钮，只更新标题下的说明。
            self._state = "installing"
            self._status.setText(message)
            return
        self._state = state
        self._status.setText(message)

        ready = state == "ready"
        self._ready_badge.setVisible(ready)
        self._install_button.setVisible(not ready and state == "installable")
        self._install_button.setEnabled(not ready and state == "installable")
        self._cancel_button.setVisible(False)
        self._install_hint.setVisible(state == "installable")
        if state == "installable":
            self._install_hint.setText(
                "本地推理组件未安装，点击一键安装（耗时约10~30秒，无需手动操作）"
            )
        self._result.setVisible(bool(self._result.text()))

        if state == "missing-python":
            if not self._last_command:
                self._last_command = self._fallback_command()
            if self._last_command:
                self._command_label.setText(self._last_command)
                self._command_row.setVisible(True)
            else:
                self._command_row.setVisible(False)
        elif state == "ready":
            if self._output:
                self._progress.setPlainText("\n".join(_tail_lines(self._output)))
            self._progress.setVisible(bool(self._output))
            self._command_row.setVisible(False)
        else:
            self._progress.setVisible(False)
            # 上一次失败留下过命令：继续摆着，方便用户复制后重试。
            show_command = bool(self._last_command) and bool(self._result.text())
            self._command_row.setVisible(show_command)
            self._copy_hint.setVisible(False)

    # -- 安装流程 -----------------------------------------------------------

    def _request_install(self) -> None:
        """用户点「一键安装」：先确认，确认后才启动后台线程。"""
        if self._thread is not None:
            return
        answer = QMessageBox.question(
            self,
            CONFIRM_TITLE,
            INSTALL_QUESTION,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_install()

    def _start_install(self) -> None:
        if self._thread is not None:
            return
        python = self._system_python()
        if not python:
            # 没有系统 Python：不给「一键安装」这条路，刷新成缺 Python 态。
            self.refresh()
            return
        self._output = []
        self._progress.clear()
        self._progress.setVisible(True)
        self._last_command = ""
        try:
            self._last_command = runtime_setup.install_command_text(python)
        except Exception:  # noqa: BLE001
            self._last_command = ""
        self._result.setText("正在安装，请不要关闭程序。")
        self._result.setVisible(True)
        self._copy_hint.setVisible(False)
        self._state = "installing"
        self._install_button.setEnabled(False)
        self._cancel_button.setVisible(True)
        self._cancel_button.setEnabled(True)
        thread = runtime_setup.BackendInstallThread(
            on_line=self._enqueue_line,
            python=python,
        )
        self._thread = thread
        self._poll_timer.start()
        thread.start()

    def _enqueue_line(self, line: str) -> None:
        """子线程回调：只往队列里塞一行，绝不在子线程碰控件。"""
        try:
            self._queue.put_nowait(line)
        except queue.Full:  # pragma: no cover - 队列无上限
            logger.debug("安装输出队列已满，丢弃一行")

    def _drain_queue(self) -> None:
        lines: List[str] = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self._output.extend(lines)
        for line in lines:
            self._progress.appendPlainText(line)
        bar = self._progress.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _poll_install(self) -> None:
        self._drain_queue()
        thread = self._thread
        if thread is None:
            self._poll_timer.stop()
            return
        try:
            alive = bool(thread.is_alive())
        except Exception:  # noqa: BLE001
            alive = False
        if not alive:
            self._poll_timer.stop()
            # 关键：回主线程再更新界面，绝不在子线程里碰控件。
            QTimer.singleShot(0, self._finish_install)

    def _finish_install(self) -> None:
        if self._shutdown:
            return
        thread = self._thread
        if thread is None:
            return
        self._poll_timer.stop()
        self._drain_queue()
        self._thread = None
        result = thread.result
        if result is None:
            result = runtime_setup.InstallResult(
                ok=False,
                message="安装线程没有返回结果，请重试。",
                lines=[],
                command=self._last_command,
            )
        if result.command:
            self._last_command = result.command
        self._cancel_button.setEnabled(True)
        self._result.setText(result.message)
        self._result.setVisible(True)

        installed = bool(result.ok)
        if installed:
            self._copy_hint.setVisible(False)
        else:
            fallback = self._last_command or self._fallback_command()
            if fallback:
                self._last_command = fallback
                self._command_label.setText(fallback)
                self._command_row.setVisible(True)
            self._copy_hint.setVisible(False)
        self.refresh()
        if installed:
            self.ready.emit()

    def _cancel_install(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._cancel_button.setEnabled(False)
        self._copy_hint.setVisible(False)
        try:
            thread.cancel()
        except Exception:  # noqa: BLE001
            logger.debug("取消安装失败", exc_info=True)
        self._result.setText("正在取消安装，请稍候。")
        self._result.setVisible(True)

    # -- 复制命令 -----------------------------------------------------------

    def _copy_command(self) -> None:
        text = (
            self._command_label.text().strip()
            or self._last_command
            or self._fallback_command()
        )
        if not text:
            return
        self._last_command = text
        try:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(text)
        except Exception:  # noqa: BLE001 - 没有剪贴板时退化为只显示
            logger.warning("写剪贴板失败", exc_info=True)
            self._copy_hint.setText("复制失败，请手动选中上面的命令复制。")
            self._copy_hint.setVisible(True)
            return
        self._copy_hint.setText(COPY_HINT_TEXT)
        self._copy_hint.setVisible(True)

    # -- 关闭 ---------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口名
        self.stop()
        super().closeEvent(event)
