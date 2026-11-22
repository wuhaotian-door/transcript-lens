"""启动时说明待补装的组件，并等用户选择。"""

from threading import Event

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from core import applog
from core.ai.runtime_setup import ensure_runtime_on_path
from core.startup_environment import EnvironmentStatus, detect_environment, install_missing
from core.updater import DownloadCancelled
from ui.workers import run_async


class StartupEnvironmentDialog(QDialog):
    _progress = Signal(str, int)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("应用环境检测")
        self.setMinimumWidth(450)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._phase = "checking"
        self._environment = EnvironmentStatus()
        self._cancel = Event()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(13)
        self._title = QLabel("正在检查应用环境")
        self._title.setObjectName("H2")
        layout.addWidget(self._title)
        self._detail = QLabel("正在检查内置组件、本地推理核心和模型。")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._message = QLabel()
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        self._message.setWordWrap(True)
        self._message.setObjectName("Muted")
        self._message.hide()
        layout.addWidget(self._message)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        layout.addWidget(self._bar)

        actions = QHBoxLayout()
        actions.addStretch()
        self._skip = QPushButton("暂时跳过")
        self._skip.setObjectName("Ghost")
        self._skip.setMinimumHeight(36)
        self._skip.clicked.connect(self.reject)
        self._skip.hide()
        actions.addWidget(self._skip)
        self._download = QPushButton("下载并安装")
        self._download.setObjectName("Primary")
        self._download.setMinimumHeight(36)
        self._download.setAutoDefault(False)
        self._download.clicked.connect(self._start_install)
        self._download.hide()
        actions.addWidget(self._download)
        layout.addLayout(actions)

        self._progress.connect(self._show_progress)
        QTimer.singleShot(0, self, self._start_detection)

    def _start_detection(self) -> None:
        run_async(self, detect_environment, on_done=self._show_detection, on_error=self._detection_failed)

    def _show_detection(self, result: EnvironmentStatus) -> None:
        self._environment = result
        self._bar.hide()
        if result.problem:
            self._phase = "blocked"
            self._title.setText("应用环境需要修复")
            self._detail.setText(result.problem)
            self._skip.setText("关闭应用")
            self._skip.show()
            return
        if not (result.needs_engine or result.needs_model):
            self.accept()
            return
        self._phase = "ready"
        self._title.setText("补齐本地 AI 组件")
        items = []
        if result.needs_engine:
            items.append("本地推理核心（约 8 MB）")
        if result.needs_model:
            items.append("入门 AI 模型 Qwen2.5 0.5B（约 491 MB）")
        self._detail.setText("\n".join(items))
        self._set_message("下载后可使用本地 AI。也可以先登录使用成绩分析和成长档案。")
        self._skip.show()
        self._skip.setFocus()
        self._download.show()

    def _detection_failed(self, error: BaseException) -> None:
        applog.write("environment", f"检测失败 {type(error).__name__}")
        self._phase = "skippable"
        self._bar.hide()
        self._title.setText("环境检测未完成")
        self._detail.setText("你可以继续使用成绩功能，稍后在模型中心配置 AI。")
        self._skip.setText("继续启动")
        self._skip.show()

    def _set_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def _start_install(self) -> None:
        if self._phase != "ready":
            return
        self._phase = "installing"
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
        self.accept()

    def _install_failed(self, error: BaseException) -> None:
        self._phase = "ready"
        self._bar.hide()
        self._title.setText("补齐本地 AI 组件")
        self._set_message("已取消下载，可重试或继续启动。" if isinstance(error, DownloadCancelled) else str(error))
        self._skip.setText("暂时跳过")
        self._skip.setEnabled(True)
        self._download.setText("重试安装")
        self._download.show()

    def reject(self) -> None:
        if self._phase == "installing":
            self._cancel.set()
            self._set_message("正在取消下载…")
            self._skip.setEnabled(False)
        elif self._phase in ("ready", "skippable"):
            self.accept()
        elif self._phase == "blocked":
            super().reject()


def check_before_start() -> bool:
    dialog = StartupEnvironmentDialog()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    ensure_runtime_on_path()
    return True
