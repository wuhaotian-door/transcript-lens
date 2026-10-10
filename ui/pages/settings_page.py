"""⚙️ 设置页 —— 导出 / 清库 / AI 开关 / 更新检查。

四件事都**真的接通了 core**，不是死按钮：

* 导出   → :mod:`core.exporter`（明文版 / 脱敏版两个按钮）
* 清库   → 删掉本用户的 ``.enc`` 载荷，**二次确认**
* AI     → 只有开关和占位，**一个网络请求都没有**（charter 七节）
* 更新   → :mod:`core.updater`，**只在点按钮时**联网一次，不自动下载

★ **本页没有激活码卡片**（v1.8 已删除整套激活码系统）。见
  `docs/DEV_SUMMARY.md` §16.1。
  注册登录即全部功能可用，本页不再有、也不许再加任何形式的「解锁」输入框。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.ai.local_assistant import PLACEHOLDER as LOCAL_PLACEHOLDER
from core.ai.local_assistant import MODEL_LOADED_PLACEHOLDER
from core.ai.model_downloader import ModelDownloader, model_dir
from core.crypto_store import CryptoStore, PAYLOAD_FILES
from core.exporter import export_anonymized_xlsx, export_plain_xlsx
from core import update_apply
from core.updater import (
    CURRENT_VERSION,
    GITEE_RELEASES_PAGE,
    STATUS_ERROR,
    UpdateCheck,
    check_for_update,
)
from ui.anim import make_card
from ui.model_center_dialog import open_model_center
from ui.update_dialog import (
    ModuleChoiceDialog,
    ask_restart,
    download_release,
    plan_from_release,
)
from ui.workers import run_async

__all__ = ["AI_SETTINGS_FILE", "SettingsPage"]

#: AI 设置在加密库里的文件名
AI_SETTINGS_FILE = "ai_settings.json.enc"


class SettingsPage(QWidget):
    """设置页。

    Args:
        store: 加密存储（用来定位本用户的载荷文件）。
        root: 数据根目录；``None`` = 默认 AppData。
    """

    def __init__(
        self,
        store: CryptoStore | None = None,
        root: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore(root)
        self._username = ""
        #: 由主窗口注入：返回当前要导出的记录列表（可能为空）
        self._export_provider: Callable[[], list[dict[str, Any]]] | None = None
        #: 更新检查正在进行中（防止连点，也防止回调里重复开弹窗）
        self._checking = False
        #: 最近一次检查拿到的 Release 描述原文。AI 模型清单和更新向导
        #: 都要从它里面解析直链 —— 存在这儿，省得再发一次请求。
        self._release_body = ""
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        self._username = username or ""

    def set_export_provider(self, provider: Callable[[], list[dict[str, Any]]]) -> None:
        self._export_provider = provider

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        """搭页面。

        ★ 五张卡片摞起来**比一屏高**（实测 sizeHint 1187px > 页面 1119px），
          所以必须套一层 ``QScrollArea``。不套的话「更新」那张卡片会被
          挤出可视区 —— 用户根本看不到验收标准里那个「检查更新」按钮，
          而界面上没有任何东西提示"下面还有内容"（AGENTS.md 4c 那一类事故：
          断言全绿、界面上却是坏的）。
        """
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("PageScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        outer.addWidget(self._scroll)

        body = QWidget()
        self._scroll.setWidget(body)

        box = QVBoxLayout(body)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(14)

        title = QLabel("⚙️  设置")
        title.setObjectName("H1")
        box.addWidget(title)

        # --- 导出 ---
        exp_card = make_card()
        exp = QVBoxLayout(exp_card)
        exp.setContentsMargins(24, 20, 24, 20)
        exp.setSpacing(10)

        exp_title = QLabel("导出")
        exp_title.setObjectName("H2")
        exp.addWidget(exp_title)

        exp_note = QLabel(
            "明文版含姓名，仅供本人留档；脱敏版不含姓名也不含学号，学生以 S 编号标识。"
        )
        exp_note.setObjectName("Muted")
        exp_note.setWordWrap(True)
        exp.addWidget(exp_note)

        exp_row = QHBoxLayout()
        plain = QPushButton("导出明文 xlsx")
        plain.setObjectName("Ghost")
        plain.setMinimumHeight(38)
        plain.clicked.connect(lambda: self._do_export(anonymized=False))
        exp_row.addWidget(plain)

        masked = QPushButton("导出脱敏 xlsx")
        masked.setObjectName("Ghost")
        masked.setMinimumHeight(38)
        masked.clicked.connect(lambda: self._do_export(anonymized=True))
        exp_row.addWidget(masked)
        exp_row.addStretch(1)
        exp.addLayout(exp_row)

        self._exp_status = QLabel("先在「📊 智育分析」导入一份成绩表，才能导出。")
        self._exp_status.setObjectName("Muted")
        self._exp_status.setWordWrap(True)
        exp.addWidget(self._exp_status)
        box.addWidget(exp_card)

        # --- 清库 ---
        clr_card = make_card()
        clr = QVBoxLayout(clr_card)
        clr.setContentsMargins(24, 20, 24, 20)
        clr.setSpacing(10)

        clr_title = QLabel("清空成绩库")
        clr_title.setObjectName("H2")
        clr.addWidget(clr_title)

        clr_note = QLabel(
            "删掉本账户在磁盘上的加密成绩文件。账户和密码本身不受影响。\n"
            "这一步不可撤销 —— 删掉之后只能用原始成绩表重新导入。"
        )
        clr_note.setObjectName("Muted")
        clr_note.setWordWrap(True)
        clr.addWidget(clr_note)

        clear_btn = QPushButton("清空成绩库")
        clear_btn.setObjectName("Danger")
        clear_btn.setMinimumHeight(38)
        clear_btn.clicked.connect(self._do_clear)
        clr.addWidget(clear_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self._clr_status = QLabel("")
        self._clr_status.setObjectName("Muted")
        self._clr_status.setWordWrap(True)
        clr.addWidget(self._clr_status)
        box.addWidget(clr_card)

        box.addWidget(self._build_ai_card())

        box.addWidget(self._build_update_card())

        box.addStretch(1)

    # ---------------------------------------------------------------- AI 助手
    def _build_ai_card(self) -> QFrame:
        """AI 助手（charter 第 6 章：**只留开关和接口，不实现任何调用**）。

        ★ 两个开关默认都是**关**的。云端那个打开时会要求填 API Key，
          并且 Key 存进加密库（不是明文配置、不进仓库）。

        ★ 这里**一个网络请求都没有**，连 import 都没有 ——
          见 ``core/ai/cloud_adapter.py`` 顶部的说明。
        """
        from core.ai.cloud_adapter import DESCRIPTION as CLOUD_DESC
        from core.ai.local_assistant import DESCRIPTION as LOCAL_DESC

        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(10)

        title = QLabel("AI 助手")
        title.setObjectName("H2")
        box.addWidget(title)

        note = QLabel(
            "本地助手不联网；云端大模型仅在您主动点击「深度评估」时调用。"
        )
        note.setObjectName("Notice")
        note.setWordWrap(True)
        box.addWidget(note)

        # --- 本地助手 ---
        self._local_ai_box = QCheckBox("启用本地学习助手")
        self._local_ai_box.setChecked(False)
        self._local_ai_box.toggled.connect(self._on_local_ai_toggled)
        box.addWidget(self._local_ai_box)

        local_hint = QLabel(LOCAL_DESC)
        local_hint.setObjectName("Muted")
        local_hint.setWordWrap(True)
        box.addWidget(local_hint)

        # --- 云端助手 ---
        self._cloud_ai_box = QCheckBox("允许辅导员手动启用云端大模型")
        self._cloud_ai_box.setChecked(False)
        self._cloud_ai_box.toggled.connect(self._on_cloud_ai_toggled)
        box.addWidget(self._cloud_ai_box)

        cloud_hint = QLabel(CLOUD_DESC)
        cloud_hint.setObjectName("Muted")
        cloud_hint.setWordWrap(True)
        box.addWidget(cloud_hint)

        key_row = QHBoxLayout()
        self._ai_key_edit = QLineEdit()
        self._ai_key_edit.setPlaceholderText("API Key（存加密库，不回显）")
        # ★ 密码模式：**绝不回显**（charter 铁律 2）
        self._ai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._ai_key_edit.setMinimumHeight(36)
        self._ai_key_edit.setEnabled(False)
        self._ai_key_edit.setMinimumWidth(280)
        key_row.addWidget(self._ai_key_edit, 1)

        self._ai_save_btn = QPushButton("保存 AI 设置")
        self._ai_save_btn.setObjectName("Ghost")
        self._ai_save_btn.setMinimumHeight(36)
        self._ai_save_btn.setEnabled(False)
        self._ai_save_btn.clicked.connect(self._do_save_ai)
        key_row.addWidget(self._ai_save_btn)
        box.addLayout(key_row)

        self._ai_status = QLabel("")
        self._ai_status.setObjectName("Muted")
        self._ai_status.setWordWrap(True)
        box.addWidget(self._ai_status)

        # --- 本地模型管理（charter v1.8 第八节 / 任务书 4.2）----------------
        box.addSpacing(6)

        models_title = QLabel("本地模型")
        models_title.setObjectName("H3")
        box.addWidget(models_title)

        models_note = QLabel(
            "模型文件只下载到本机的数据目录，不上传任何东西。"
            "本版本只做到「下载 + 认出来」，还没有接推理引擎。"
        )
        models_note.setObjectName("Muted")
        models_note.setWordWrap(True)
        box.addWidget(models_note)

        self._models_list = QLabel("（正在读取…）")
        self._models_list.setObjectName("Muted")
        self._models_list.setWordWrap(True)
        box.addWidget(self._models_list)

        model_row = QHBoxLayout()
        self._model_center_btn = QPushButton("模型中心")
        self._model_center_btn.setObjectName("Ghost")
        self._model_center_btn.setMinimumHeight(36)
        self._model_center_btn.setMinimumWidth(110)
        self._model_center_btn.setToolTip(
            "看远端清单，勾选要装的模型，一个个下（绝不自作主张下载）"
        )
        self._model_center_btn.clicked.connect(self._do_open_model_center)
        model_row.addWidget(self._model_center_btn)

        self._model_download_btn = QPushButton("下载新模型")
        self._model_download_btn.setObjectName("Ghost")
        self._model_download_btn.setMinimumHeight(36)
        self._model_download_btn.setMinimumWidth(130)
        self._model_download_btn.clicked.connect(self._do_download_model)
        model_row.addWidget(self._model_download_btn)

        self._model_delete_btn = QPushButton("删除模型")
        self._model_delete_btn.setObjectName("Ghost")
        self._model_delete_btn.setMinimumHeight(36)
        self._model_delete_btn.setMinimumWidth(110)
        self._model_delete_btn.clicked.connect(self._do_delete_model)
        model_row.addWidget(self._model_delete_btn)
        model_row.addStretch(1)
        box.addLayout(model_row)

        self._model_progress = QProgressBar()
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(0)
        self._model_progress.setTextVisible(True)
        self._model_progress.setMinimumHeight(22)
        self._model_progress.setVisible(False)
        box.addWidget(self._model_progress)

        self._model_status = QLabel("")
        self._model_status.setObjectName("Muted")
        self._model_status.setWordWrap(True)
        box.addWidget(self._model_status)

        self._model_downloader = ModelDownloader()
        self._model_downloader.progress.connect(self._on_model_progress)
        self._model_downloader.finished.connect(self._on_model_finished)
        self._model_downloader.error.connect(self._on_model_error)
        self._refresh_models()
        return card

    # ---------------------------------------------------------------- 本地模型
    def _refresh_models(self) -> None:
        """把"已下载哪些模型"刷到界面上（**纯文本，不写 Markdown**）。"""
        try:
            installed = self._model_downloader.list_installed()
        except Exception as exc:  # noqa: BLE001 - 读不出来不该让设置页崩
            self._models_list.setText(f"读取本地模型失败：{type(exc).__name__}")
            return
        if not installed:
            self._models_list.setText(
                "还没有下载任何本地模型。点「下载新模型」从发行版页面提供的地址下载。"
            )
            self._model_delete_btn.setEnabled(False)
            return
        parts = []
        for info in installed:
            size = ModelDownloader.human_size(info.path)
            parts.append(f"· {info.name}（{size}）")
        self._models_list.setText("已下载 " + str(len(installed)) + " 个：\n" + "\n".join(parts))
        self._model_delete_btn.setEnabled(True)

    def _do_open_model_center(self) -> None:
        """打开模型中心：读远端清单 → 勾选 → 串行下载。

        ★ **只有用户点这个按钮才会联网**，程序自己永远不会去下载模型。
        """
        try:
            open_model_center(self, on_installed=self._on_models_installed)
        except Exception as exc:  # noqa: BLE001 - 打不开也不该让设置页崩
            QMessageBox.warning(
                self,
                "模型中心打不开",
                "模型中心没能打开：" + type(exc).__name__ + "。",
            )

    def _on_models_installed(self, model_ids: list[Any]) -> None:
        """模型中心这一轮装好了东西 —— 刷新本地模型那块并报一句。"""
        self._refresh_models()
        names = [str(item) for item in model_ids if str(item)]
        if names:
            self._model_status.setText(
                "模型中心装好了 " + str(len(names)) + " 个模型：" + "、".join(names)
            )

    def _model_catalog(self) -> list[Any]:
        """可用模型清单（带 Release 描述里的直链，拿不到就只有本地已装的）。"""
        body = getattr(self, "_release_body", "")
        try:
            return self._model_downloader.list_available_models(body)
        except Exception:  # noqa: BLE001
            return self._model_downloader.list_available_models("")

    def _do_download_model(self) -> None:
        """选一个模型并下载（**只在用户点这里之后才联网**）。"""
        if self._model_downloader.busy:
            QMessageBox.information(self, "正在下载", "已经有一个下载在进行中了。")
            return

        catalog = self._model_catalog()
        if not catalog:
            QMessageBox.information(
                self, "没有可下载的模型", "发行版里还没有列出任何 AI 模型。"
            )
            return

        labels = [info.display for info in catalog]
        choice, ok = QInputDialog.getItem(
            self, "选择要下载的模型", "可用模型：", labels, 0, False
        )
        if not ok or not choice:
            return
        index = labels.index(choice)
        info = catalog[index]
        if not info.url:
            QMessageBox.information(
                self,
                "这个模型还没有下载地址",
                f"「{info.name}」在发行版说明里没有提供直链。\n\n"
                "请点「检查更新」查看发行版页面，或联系维护者补充下载地址。",
            )
            return

        answer = QMessageBox.question(
            self,
            "开始下载",
            f"即将下载：{info.label or info.name}"
            + (f"（{info.size}）" if info.size else "")
            + f"\n\n保存位置：\n{model_dir(info.name)}\n\n现在开始下载吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._model_progress.setVisible(True)
        self._model_progress.setValue(0)
        self._model_status.setText(f"正在下载「{info.name}」…")
        if not self._model_downloader.download(info.url, info.name):
            self._model_status.setText("没能开始下载。")
            self._model_progress.setVisible(False)

    def _on_model_progress(self, percent: int) -> None:
        if percent < 0:
            # 总大小未知 → 不确定态
            self._model_progress.setRange(0, 0)
            return
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(percent)
        got = self._model_downloader.downloaded_bytes
        total = self._model_downloader.total_bytes
        text = f"正在下载「{self._model_downloader.model_name}」… {percent}%"
        if total:
            text += (
                f"　{ModelDownloader.human_size(got)}"
                f" / {ModelDownloader.human_size(total)}"
            )
        self._model_status.setText(text)

    def _on_model_finished(self, path: str) -> None:
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(100)
        self._model_progress.setVisible(False)
        self._model_status.setText(f"模型已下载：{path}")
        self._refresh_models()
        QMessageBox.information(
            self,
            "模型已下载",
            f"已保存到：\n{path}\n\n"
            "提醒：本版本还没有接推理引擎，点「评估」仍会显示"
            f"「{MODEL_LOADED_PLACEHOLDER}」。",
        )

    def _on_model_error(self, message: str) -> None:
        self._model_progress.setVisible(False)
        self._model_status.setText(message)
        # 取消不是错误 —— 不弹红色报错框
        if "取消" not in message:
            QMessageBox.critical(self, "下载失败", message)

    def _do_delete_model(self) -> None:
        installed = self._model_downloader.list_installed()
        if not installed:
            QMessageBox.information(self, "没有可删除的模型", "本机还没有下载过模型。")
            return
        names = [info.name for info in installed]
        choice, ok = QInputDialog.getItem(
            self, "删除模型", "要删除哪个模型：", names, 0, False
        )
        if not ok or not choice:
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除本地模型「{choice}」吗？\n\n"
            "删除之后要重新下载才能再用（不影响成绩和任何其它数据）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._model_downloader.delete(choice):
            self._model_status.setText(f"已删除模型「{choice}」。")
        else:
            self._model_status.setText(f"删除「{choice}」失败，文件可能正被占用。")
        self._refresh_models()

    def _on_local_ai_toggled(self, checked: bool) -> None:
        self._ai_save_btn.setEnabled(True)
        self._ai_status.setText(
            "本地学习助手已打开 —— 但本版本还没接模型，点「评估」只会显示"
            f"「{LOCAL_PLACEHOLDER}」。" if checked
            else "本地学习助手已关闭。"
        )

    def _on_cloud_ai_toggled(self, checked: bool) -> None:
        self._ai_key_edit.setEnabled(checked)
        self._ai_save_btn.setEnabled(True)
        if checked:
            self._ai_key_edit.setFocus()
            self._ai_status.setText(
                "请填入 API Key 并保存。云端**只会在您点「深度评估」时**被调用，"
                "而且传出去的只有 S 编号 / 均分 / 学分 / 分数区间 / 意愿标签，"
                "不含姓名和学号。"
            )
        else:
            self._ai_status.setText("云端大模型已关闭。")

    def _do_save_ai(self) -> None:
        """把 AI 开关（和 Key）存进加密库。**Key 绝不写日志、绝不回显。**"""
        cloud_on = self._cloud_ai_box.isChecked()
        key = self._ai_key_edit.text().strip()
        if cloud_on and not key:
            QMessageBox.warning(self, "还差 API Key",
                                "启用云端大模型需要填 API Key。")
            self._ai_key_edit.setFocus()
            return
        payload: dict[str, Any] = {
            "local_enabled": self._local_ai_box.isChecked(),
            "cloud_enabled": cloud_on,
        }
        if key:
            payload["api_key"] = key
        elif not cloud_on:
            payload["api_key"] = ""

        try:
            self._save_ai_payload(payload)
        except Exception as exc:  # noqa: BLE001 - 存不上要说清楚
            QMessageBox.critical(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return

        self._ai_key_edit.clear()
        self._ai_save_btn.setEnabled(False)
        self._ai_status.setText(
            "AI 设置已保存到加密库。"
            + ("（云端已启用，Key 已存）" if cloud_on else "（云端未启用）")
        )
        QMessageBox.information(
            self, "已保存",
            "AI 设置已加密存盘。\n\n"
            "提醒：本版本只预留接口，点「深度评估」只会显示占位提示，"
            "不会发起任何请求。",
        )

    def _save_ai_payload(self, payload: dict[str, Any]) -> None:
        from core.crypto_store import CryptoError
        from core.user_store import save_user_json

        if not self._username:
            raise CryptoError("还没登录，AI 设置没地方存")
        save_user_json(self._store, self._username, AI_SETTINGS_FILE, payload)

    def load_ai_settings(self) -> dict[str, Any]:
        """读回 AI 设置（自检/其它页面用）。Key **不参与任何显示**。"""
        from core.user_store import load_user_json

        if not self._username:
            return {}
        data = load_user_json(self._store, self._username, AI_SETTINGS_FILE, {})
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- 更新
    def _build_update_card(self) -> QFrame:
        """更新检查 + 自动更新（charter v1.7 第七节 → v1.8 第六节）。

        ★ **只在用户点按钮时才联网**，没有任何自动检查。
        ★ 请求走 :func:`ui.workers.run_async`（后台线程）——
          在按钮回调里直接 ``urlopen`` 的话，断网时要卡满 5 秒超时，
          期间窗口不重绘（``ui/workers.py`` 开头讲的就是这个）。
        ★ 下载走 :func:`ui.update_dialog.download_release`（后台线程 +
          模态进度条），**每一步都要用户点一下**才动：
          点「检查更新」才查、点「开始下载」才下、点「立即重启」才换。
          charter v1.8 第四节 ❌ 一栏里"自动更新/静默下载"依然禁止 ——
          这里没有任何"启动时顺手更新一下"的路径。
        """
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(10)

        title = QLabel("更新")
        title.setObjectName("H2")
        box.addWidget(title)

        note = QLabel(
            "只在您点「检查更新」时才联网，只读一次版本号。"
            "发现新版本后会先问您要下什么，点「开始下载」才会下载 —— "
            "程序不会在后台自动下载或自动安装。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        box.addWidget(note)

        row = QHBoxLayout()
        self._ver_label = QLabel(f"当前版本 v{CURRENT_VERSION}")
        self._ver_label.setObjectName("H3")
        row.addWidget(self._ver_label)
        row.addSpacing(12)

        self._update_btn = QPushButton("检查更新")
        self._update_btn.setObjectName("Ghost")
        self._update_btn.setMinimumHeight(38)
        self._update_btn.setMinimumWidth(110)
        self._update_btn.clicked.connect(self._do_check_update)
        row.addWidget(self._update_btn)
        row.addStretch(1)
        box.addLayout(row)

        self._upd_status = QLabel(
            "想确认有没有新版本，点一下「检查更新」。"
        )
        self._upd_status.setObjectName("Muted")
        self._upd_status.setWordWrap(True)
        box.addWidget(self._upd_status)
        return card

    def _set_update_status(self, text: str, *, error: bool = False) -> None:
        self._upd_status.setText(text)
        self._upd_status.setObjectName("Danger" if error else "Muted")
        style = self._upd_status.style()
        style.unpolish(self._upd_status)
        style.polish(self._upd_status)

    def _do_check_update(self) -> None:
        """点「检查更新」：后台线程发一次请求，回来再动界面。"""
        if self._checking:
            return
        self._checking = True
        self._update_btn.setEnabled(False)
        self._update_btn.setText("检查中…")
        self._set_update_status("正在检查…")

        def _done(result: Any) -> None:
            self._checking = False
            self._update_btn.setEnabled(True)
            self._update_btn.setText("检查更新")
            self._apply_update_result(result)

        def _failed(exc: BaseException) -> None:
            # check_for_update 自己吞掉所有网络异常，走到这里说明是别的问题
            self._checking = False
            self._update_btn.setEnabled(True)
            self._update_btn.setText("检查更新")
            self._set_update_status(
                f"检查失败，请手动访问仓库。（{type(exc).__name__}）", error=True
            )

        run_async(self, check_for_update, on_done=_done, on_error=_failed)

    def _apply_update_result(self, result: UpdateCheck) -> None:
        """把检查结果落到界面上。**四种状态给四句不同的话**。

        ★ v1.8：发现新版本之后不再是"打开浏览器让你自己下"，
          而是走完整向导（选模块 → 下载 → 问重启）。charter v1.8 第四节
          把"更新下载"加进了白名单，第六节给了完整流程。
          **但"不自动下载"这条没变** —— 每一步都要用户点一下。
        """
        # 把描述留下来：AI 模型清单和下载直链都从它里面解析
        self._release_body = result.body or ""
        if self._release_body:
            self._refresh_models()

        if result.status == STATUS_ERROR:
            self._set_update_status(
                "检查失败，请手动访问仓库查看是否有新版本。", error=True
            )
            return
        self._set_update_status(result.detail)
        if not result.has_newer:
            return

        plan = plan_from_release(result, {"body": result.body})
        if not plan.has_main:
            # 发行版没给直链 → 老老实实退回"打开页面自己下"，
            # 不要假装能自动更新。
            answer = QMessageBox.question(
                self,
                "发现新版本",
                f"发现新版本 v{result.version}（当前 v{CURRENT_VERSION}）。\n\n"
                "这个发行版没有提供程序直链，需要到页面上手动下载。\n"
                "现在打开下载页吗？",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Ok,
            )
            if answer == QMessageBox.StandardButton.Ok:
                QDesktopServices.openUrl(QUrl(GITEE_RELEASES_PAGE))
                self._set_update_status(
                    f"{result.detail} 已打开浏览器：{GITEE_RELEASES_PAGE}"
                )
            return

        self._run_update_wizard(result, plan)

    def _run_update_wizard(self, result: UpdateCheck, plan: Any) -> None:
        """第一步选模块 → 第二步下载 → 第三步问重启。"""
        chooser = ModuleChoiceDialog(plan, self)
        if chooser.exec() != chooser.DialogCode.Accepted:
            self._set_update_status("已取消：没有下载任何内容。")
            return

        self._set_update_status(f"正在下载 v{plan.version}…")
        destination = (
            update_apply.update_dir() / f"transcript-lens_v{plan.version}.exe"
        )
        downloaded = download_release(
            self,
            plan.main_url,
            destination,
            label=(
                f"正在下载主程序 v{plan.version}（{plan.main_size}）\n"
                + (
                    f"这次发布切成 {len(plan.main_parts)} 片，会逐片校验再拼起来。\n"
                    if plan.main_parts
                    else ""
                )
                + "下载中断可以重新点「开始下载」续传。"
            ),
            expected_sha256=plan.main_sha256,
            parts=plan.main_parts,
            expected_size=plan.main_bytes,
        )
        if downloaded is None:
            self._set_update_status("下载已取消，当前程序没有任何改动。", error=True)
            return

        # ★ 下载完就写 update.json —— 这样点「稍后」之后，
        #   下次启动 apply_pending_update 才接得上（见 ui/update_dialog 开头的说明）。
        if update_apply.is_frozen():
            try:
                update_apply.write_pending_update(
                    downloaded,
                    update_apply.current_exe() or "",
                    plan.version,
                    from_version=CURRENT_VERSION,
                    sha256=plan.main_sha256,
                )
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(
                    self, "记录更新状态失败",
                    f"{exc}\n\n新版本已经下载好了，但重启后可能不会自动替换。",
                )

        # --- 选中的 AI 模型（可选，失败不影响主程序更新）-------------------
        for info in plan.selected(chooser.chosen_models()):
            path = download_release(
                self,
                info.url,
                model_dir(info.name) / (info.name + ".gguf"),
                label=f"正在下载 {info.label or info.name}"
                      + (f"（{info.size}）" if info.size else ""),
            )
            if path is None:
                self._set_update_status(
                    f"「{info.name}」没有下载完，主程序更新不受影响。", error=True
                )
                break
            self._refresh_models()

        if ask_restart(self, plan, Path(downloaded)):
            # ★ 交接已经完成，**必须**退出 —— 不退出的话旧进程还占着
            #   exe 文件锁，bat 会重试 60 秒然后放弃。
            self._set_update_status("正在重启以完成更新…")
            app = QApplication.instance()
            if app is not None:
                app.quit()
        else:
            self._set_update_status(
                f"v{plan.version} 已下载完成。下次启动程序时会自动完成替换。"
            )

    # ---------------------------------------------------------------- 导出
    def _do_export(self, *, anonymized: bool) -> None:
        records: list[dict[str, Any]] = []
        if self._export_provider is not None:
            try:
                records = list(self._export_provider())
            except Exception as exc:  # noqa: BLE001 - 导出失败要说清楚
                QMessageBox.critical(self, "导出失败", str(exc))
                return
        if not records:
            QMessageBox.information(
                self,
                "还没有可导出的数据",
                "先在「📊 智育分析」导入一份成绩表再来。",
            )
            return

        default_name = "成绩汇总_脱敏.xlsx" if anonymized else "成绩汇总.xlsx"
        path, _filter = QFileDialog.getSaveFileName(
            self, "导出到", default_name, "Excel 工作簿 (*.xlsx)"
        )
        if not path:
            return
        try:
            result = (
                export_anonymized_xlsx(records, path)
                if anonymized
                else export_plain_xlsx(records, path)
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"{type(exc).__name__}: {exc}")
            self._exp_status.setText(f"导出失败：{exc}")
            return

        self._exp_status.setText(f"已导出 {result.rows} 行 → {result.path}")
        QMessageBox.information(self, "导出完成", f"已写到：\n{result.path}")

    # ---------------------------------------------------------------- 清库
    def _do_clear(self) -> None:
        if not self._username:
            QMessageBox.warning(self, "还不知道是谁", "请先登录。")
            return
        record = self._store.load_users().find(self._username)
        if record is None:
            QMessageBox.warning(self, "找不到账户", f"本机没有「{self._username}」。")
            return

        existing = [
            name
            for name in PAYLOAD_FILES
            if self._store.payload_path(record.uid, name).exists()
        ]
        if not existing:
            self._clr_status.setText("库里本来就是空的，没有可删的东西。")
            QMessageBox.information(self, "不用清", "库里本来就是空的。")
            return

        first = QMessageBox.question(
            self,
            "确认要清空吗",
            f"将删除本账户的 {len(existing)} 个加密文件：\n"
            + "\n".join(f"· {name}" for name in existing)
            + "\n\n删除后无法恢复，只能用原始成绩表重新导入。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return

        second = QMessageBox.warning(
            self,
            "最后确认",
            "这是不可撤销的操作。真的要删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if second != QMessageBox.StandardButton.Yes:
            self._clr_status.setText("已取消，什么都没删。")
            return

        removed, failed = [], []
        for name in existing:
            path = self._store.payload_path(record.uid, name)
            try:
                path.unlink()
                removed.append(name)
            except OSError as exc:
                failed.append(f"{name}（{exc}）")

        text = f"已删除 {len(removed)} 个文件。"
        if failed:
            text += "失败的：" + "；".join(failed)
        self._clr_status.setText(text)
        if failed:
            QMessageBox.warning(self, "部分文件没删掉", text)
        else:
            QMessageBox.information(self, "已清空", text)
