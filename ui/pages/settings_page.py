"""⚙️ 设置页 —— 激活码 / 导出 / 清库（Spec 七）。

三件事都**真的接通了 core**，不是死按钮：

* 激活码 → :func:`core.activation.try_activate`（读状态 → 校验 → 通过了才落盘）
* 导出   → :mod:`core.exporter`（明文版 / 脱敏版两个按钮）
* 清库   → 删掉本用户的 ``.enc`` 载荷，**二次确认**

★ 红线 7：换激活码**只锁「导入」**，绝不清空或改动已有成绩库。
  所以「换码」和「清库」在这里是两个完全独立的按钮，
  换码那条路径上一行删文件的代码都没有。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.activation import load_state, try_activate
from core.ai.local_assistant import PLACEHOLDER as LOCAL_PLACEHOLDER
from core.crypto_store import CryptoError, CryptoStore, PAYLOAD_FILES
from core.exporter import export_anonymized_xlsx, export_plain_xlsx
from ui.anim import make_card

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
        self._data_root = root
        self._username = ""
        #: 由主窗口注入：返回当前要导出的记录列表（可能为空）
        self._export_provider: Callable[[], list[dict[str, Any]]] | None = None
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        self._username = username or ""

    def set_export_provider(self, provider: Callable[[], list[dict[str, Any]]]) -> None:
        self._export_provider = provider

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(14)

        title = QLabel("⚙️  设置")
        title.setObjectName("H1")
        box.addWidget(title)

        # --- 激活码 ---
        act_card = make_card()
        act = QVBoxLayout(act_card)
        act.setContentsMargins(24, 20, 24, 20)
        act.setSpacing(10)

        act_title = QLabel("激活码")
        act_title.setObjectName("H2")
        act.addWidget(act_title)

        act_note = QLabel(
            "激活码只管「导入」这一件事。换码、过期、未激活都不会动已有成绩库。"
        )
        act_note.setObjectName("Muted")
        act_note.setWordWrap(True)
        act.addWidget(act_note)

        row = QHBoxLayout()
        self._code_edit = QLineEdit()
        self._code_edit.setPlaceholderText("粘贴或输入激活码")
        self._code_edit.setMinimumHeight(38)
        self._code_edit.setMinimumWidth(280)
        row.addWidget(self._code_edit, 1)

        verify = QPushButton("验证")
        verify.setObjectName("Primary")
        verify.setMinimumHeight(38)
        verify.setMinimumWidth(96)
        verify.clicked.connect(self._do_activate)
        row.addWidget(verify)
        act.addLayout(row)

        self._act_status = QLabel("")
        self._act_status.setObjectName("Muted")
        self._act_status.setWordWrap(True)
        act.addWidget(self._act_status)
        self._refresh_activation()

        box.addWidget(act_card)

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
        return card

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

    # ---------------------------------------------------------------- 激活
    def _refresh_activation(self) -> None:
        state = load_state(self._data_root)
        if state.activated:
            self._act_status.setText(
                f"当前状态：已激活（版本 {state.active_version}，"
                f"{state.activated_year} 年）"
            )
        else:
            self._act_status.setText("当前状态：未激活（导入功能受闸门管控）")

    def _do_activate(self) -> None:
        code = self._code_edit.text().strip()
        if not code:
            QMessageBox.warning(self, "还没填", "请先输入激活码。")
            return
        try:
            result = try_activate(code, root=self._data_root)
        except (CryptoError, ValueError) as exc:
            QMessageBox.critical(self, "激活失败", str(exc))
            self._act_status.setText(f"激活失败：{exc}")
            return

        self._code_edit.clear()
        if result.ok:
            QMessageBox.information(self, "激活成功", result.message)
        else:
            QMessageBox.warning(self, "激活码不可用", result.message)
        self._refresh_activation()

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
