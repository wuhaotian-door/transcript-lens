"""主窗口里的模型库和 AI 配置。"""

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

from core.ai.settings import AI_SETTINGS_FILE, DEFAULT_AI_ROLE, MAX_ROLE_LENGTH, configured_ai_role, read_ai_settings, validate_ai_role
from core.user_store import save_user_json
from ui.anim import make_card
from ui.model_center_dialog import ModelCenterDialog, _backend_available
from ui.workbench_models_dialog import WorkbenchModelsDialog
from ui.workbench_widgets import button
from ui.widgets.backend_setup import open_backend_setup
from ui.widgets.profile_reading import profile_scroll

class ModelCenterPage(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self._store = store
        self._username = ""
        self._catalog = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        title = QLabel("模型中心")
        title.setObjectName("H1")
        header = QHBoxLayout()
        header.addWidget(title, 1)
        header.addWidget(button("语义检索与照片匹配", self._open_aux_models))
        layout.addLayout(header)

        self._views = QStackedWidget()
        layout.addWidget(self._views, 1)
        self._setup = self._build_setup()
        self._views.addWidget(self._setup)

    def _open_aux_models(self):
        WorkbenchModelsDialog(self, self._store).exec()

    def _build_setup(self) -> QWidget:
        page = QWidget()
        frame = make_card(shadow=False)
        frame.setMinimumWidth(360)
        frame.setMaximumWidth(490)
        card = QVBoxLayout(frame)
        card.setContentsMargins(28, 32, 28, 32)
        card.setSpacing(14)

        heading = QLabel("本机还未下载推理核心")
        heading.setObjectName("H2")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.addWidget(heading)
        detail = QLabel("如需启用 AI，请点击下方按钮下载推理核心。")
        detail.setObjectName("Muted")
        detail.setWordWrap(True)
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.addWidget(detail)
        install = QPushButton("下载推理核心")
        install.setObjectName("Primary")
        install.setMinimumHeight(42)
        install.clicked.connect(self._install_core)
        card.addWidget(install, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout = QVBoxLayout(page)
        layout.addStretch()
        layout.addWidget(frame, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _build_workspace(self) -> None:
        self._tabs = QTabWidget()
        self._tabs.setObjectName("ProfileTabs")
        self._tabs.tabBar().setUsesScrollButtons(False)
        library = QWidget()
        content = QVBoxLayout(library)
        content.setContentsMargins(14, 14, 14, 14)
        self._catalog = ModelCenterDialog(library, embedded=True)
        content.addWidget(self._catalog)
        self._tabs.addTab(library, "模型库")
        self._tabs.addTab(self._build_configuration(), "AI 配置")
        self._views.addWidget(self._tabs)
        self._load_settings()

    def _build_configuration(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        role = make_card(shadow=False)
        role_layout = QVBoxLayout(role)
        role_layout.setContentsMargins(18, 14, 18, 14)
        role_header = QHBoxLayout()
        role_title = QLabel("AI 角色提示词")
        role_title.setObjectName("H2")
        role_header.addWidget(role_title, 1)
        restore = button("恢复默认", lambda: self._role.setPlainText(DEFAULT_AI_ROLE))
        role_header.addWidget(restore)
        role_layout.addLayout(role_header)
        role_note = QLabel(f"每次 AI 对话和侧写生成前都会读取此设定。最多 {MAX_ROLE_LENGTH} 字。")
        role_note.setObjectName("Muted")
        role_note.setWordWrap(True)
        role_layout.addWidget(role_note)
        self._role = QPlainTextEdit()
        self._role.setAccessibleName("AI 角色提示词")
        self._role.setMinimumHeight(140)
        self._role.setPlaceholderText("描述 AI 的角色、回答方式和工作重点")
        role_layout.addWidget(self._role)
        layout.addWidget(role)

        remote = make_card(shadow=False)
        remote_layout = QVBoxLayout(remote)
        remote_layout.setContentsMargins(18, 14, 18, 14)
        remote_title = QLabel("远程 AI")
        remote_title.setObjectName("H2")
        remote_layout.addWidget(remote_title)
        self._cloud = QCheckBox("保存远程 AI 配置")
        remote_layout.addWidget(self._cloud)
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setMinimumHeight(36)
        remote_layout.addWidget(self._key)
        hint = QLabel("远程推理接口待接入。")
        hint.setObjectName("Muted")
        remote_layout.addWidget(hint)
        layout.addWidget(remote)

        status_row = QHBoxLayout()
        self._status = QLabel()
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        status_row.addWidget(self._status, 1)
        self._save = QPushButton("保存设置")
        self._save.setObjectName("Primary")
        self._save.setMinimumHeight(38)
        self._save.clicked.connect(self._save_settings)
        status_row.addWidget(self._save)
        layout.addLayout(status_row)
        layout.addStretch()
        self._cloud.toggled.connect(self._key.setEnabled)
        return profile_scroll(body)

    def set_user(self, username: str) -> None:
        self._username = username
        if self._catalog is not None:
            self._load_settings()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        if not _backend_available():
            self._views.setCurrentWidget(self._setup)
        elif self._catalog is None:
            self._build_workspace()
            self._views.setCurrentWidget(self._tabs)
            self._catalog.load_catalog()
        else:
            self._catalog.refresh_local_models()
            self._views.setCurrentWidget(self._tabs)

    def _install_core(self) -> None:
        open_backend_setup(self)
        self.refresh()

    def _load_settings(self) -> None:
        settings = read_ai_settings(self._store, self._username)
        self._role.setPlainText(configured_ai_role(settings))
        self._role.setEnabled(bool(self._username))
        with QSignalBlocker(self._cloud):
            self._cloud.setChecked(bool(settings.get("cloud_enabled")))
        self._key.clear()
        self._key.setEnabled(self._cloud.isChecked())
        self._key.setPlaceholderText("API Key 已保存，可输入新值更新" if settings.get("api_key") else "API Key")
        self._save.setEnabled(bool(self._username))
        self._status.clear()

    def _save_settings(self) -> None:
        try:
            prompt = validate_ai_role(self._role.toPlainText())
        except ValueError as error:
            self._status.setText(str(error))
            self._role.setFocus()
            return
        try:
            data = read_ai_settings(self._store, self._username, strict=True)
            cloud = self._cloud.isChecked()
            key = self._key.text().strip() or data.get("api_key", "")
            if cloud and not key:
                self._status.setText("请填写 API Key 后保存。")
                self._key.setFocus()
                return
            data.pop("local_enabled", None)
            data.update(role_prompt=prompt, cloud_enabled=cloud, api_key=key if cloud else "")
            save_user_json(self._store, self._username, AI_SETTINGS_FILE, data)
        except Exception:  # 设置读写失败时保留当前输入，供用户重试。
            QMessageBox.warning(self, "保存失败", "无法保存 AI 设置，请重试。")
            return
        self._load_settings()
        self._status.setText("已保存")
