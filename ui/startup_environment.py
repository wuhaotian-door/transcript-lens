"""进入主界面后，可选的本地 AI 安装引导。"""

from threading import Event

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from core import applog
from core.ai.runtime_setup import ensure_runtime_on_path
from core.startup_environment import EnvironmentStatus, initial_ai_setup, install_missing
from core.updater import DownloadCancelled
from ui.workers import run_async


class StartupEnvironmentDialog(QDialog):
    _progress = Signal(str, int)

    def __init__(self, status: EnvironmentStatus, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("本地 AI 设置")
        self.setMinimumWidth(450)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._installing = False
        self._environment = status
        self._cancel = Event()
        self.destroyed.connect(self._cancel.set)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(13)
        self._title = QLabel("补齐本地 AI 组件")
        self._title.setObjectName("H2")
        layout.addWidget(self._title)
        items = []
        if status.needs_engine:
            items.append("本地推理核心（约 8 MB）")
        if status.needs_model:
            items.append("入门 AI 模型 Qwen2.5 0.5B（约 491 MB）")
        self._detail = QLabel("\n".join(items))
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._message = QLabel("可稍后在「模型中心」配置。")
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        self._message.setWordWrap(True)
        self._message.setObjectName("Muted")
        layout.addWidget(self._message)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.hide()
        layout.addWidget(self._bar)

        actions = QHBoxLayout()
        actions.addStretch()
        self._skip = QPushButton("稍后配置")
        self._skip.setObjectName("Ghost")
        self._skip.setMinimumHeight(36)
        self._skip.clicked.connect(self.reject)
        actions.addWidget(self._skip)
        self._download = QPushButton("下载并安装")
        self._download.setObjectName("Primary")
        self._download.setMinimumHeight(36)
        self._download.setAutoDefault(False)
        self._download.clicked.connect(self._start_install)
        actions.addWidget(self._download)
        layout.addLayout(actions)

        self._progress.connect(self._show_progress)
        self._skip.setDefault(True)

    def _set_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def _start_install(self) -> None:
        if self._installing:
            return
        self._installing = True
        self._cancel.clear()
        self._title.setText("正在准备本地 AI")
        self._download.hide()
        self._skip.setText("取消下载")
        self._bar.show()
        self._show_progress("正在准备下载…", -1)
        run_async(
            self,
            lambda: install_missing(
                self._environment, progress=self._progress.emit, cancel=self._cancel.is_set,
            ),
            on_done=self._install_done,
            on_error=self._install_failed,
        )

    def _show_progress(self, text: str, percent: int) -> None:
        if not self._cancel.is_set():
            self._set_message(text)
            self._bar.setRange(0, 0 if percent < 0 else 100)
            if percent >= 0:
                self._bar.setValue(percent)

    def _install_done(self, _result) -> None:
        ensure_runtime_on_path()
        self.accept()

    def _install_failed(self, error: BaseException) -> None:
        self._installing = False
        self._bar.hide()
        self._title.setText("补齐本地 AI 组件")
        self._set_message("已取消下载，可重试或稍后在模型中心配置。" if isinstance(error, DownloadCancelled) else str(error))
        self._skip.setText("稍后配置")
        self._skip.setEnabled(True)
        self._download.setText("重试安装")
        self._download.show()

    def reject(self) -> None:
        if self._installing:
            self._cancel.set()
            self._set_message("正在取消下载…")
            self._skip.setEnabled(False)
        else:
            super().reject()


def offer_startup_ai(window) -> None:
    def show_setup(status: EnvironmentStatus | None) -> None:
        if status is not None and window.isVisible():
            dialog = StartupEnvironmentDialog(status, window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            dialog.show()

    def log_failure(error: BaseException) -> None:
        applog.write("environment", f"AI 组件检查失败：{type(error).__name__}")

    run_async(window, initial_ai_setup, on_done=show_setup, on_error=log_failure)
