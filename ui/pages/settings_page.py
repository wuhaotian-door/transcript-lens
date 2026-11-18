"""设置页。管理成绩导出、GPA 换算、软件更新和成绩库。

* 导出     → :mod:`core.exporter`（明文版 / 脱敏版两个按钮）
* 删除成绩库 → :func:`core.database.clear_library`，**先盘点 → 二次确认 → 才删**
  （v1.9.1 修掉了"导入完成绩还说没有成绩库"那个误报：判据从"文件在不在"改成
  "``grade_history`` 表里真有没有记录 / 侧写 / 快照"）
* GPA 规则 → :mod:`core.gpa`（内置及用户自定义方案）
* 更新     → :mod:`core.updater`，**只在点按钮时**联网一次，不自动下载

★ **本页没有激活码卡片**（v1.8 已删除整套激活码系统）。见
  `docs/DEV_SUMMARY.md` §16.1。
  注册登录即全部功能可用，本页不再有、也不许再加任何形式的「解锁」输入框。

★ 界面文案一律纯文本：**不出现 Markdown 记号**（AGENTS §24，运行时闸门会扫）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSignalBlocker, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ai.model_downloader import model_dir
from core.crypto_store import CryptoError, CryptoStore
from core.database import clear_library, library_inventory
from core.exporter import export_anonymized_xlsx, export_plain_xlsx
from core import update_apply
from core.gpa import (
    DEFAULT_SCHEME, MAX_CUSTOM_SCHEMES, SchemeError, custom_id, grade_point,
    load_settings, resolve_scheme, save_settings, scheme_options, validate_scheme,
)
from core.updater import (
    CURRENT_VERSION,
    GITEE_RELEASES_PAGE,
    STATUS_ERROR,
    UpdateCheck,
    check_for_update,
)
from ui.anim import make_card
from ui.update_dialog import (
    ModuleChoiceDialog,
    ask_restart,
    download_release,
    plan_from_release,
)
from ui.workers import run_async

__all__ = ["SettingsPage", "clear_confirm_text", "clear_summary_text"]


# ---------------------------------------------------------------------------
# 清库的两段界面文案（**纯函数**，pytest 直接可测，不碰 Qt）
# ---------------------------------------------------------------------------


def clear_confirm_text(inventory: dict[str, Any]) -> str:
    """「删除成绩库」的确认文案 —— **把要删的东西一条条写出来**。

    ★ 不许只说"清空成绩库"就开删：成绩、侧写、归档快照是三类不同的数据，
      用户点确认之前必须看得见自己将失去什么（AGENTS §30 第 4 条同源：
      删用户数据必须是用户主动、且知道自己在删什么）。
    """
    lines: list[str] = []
    rows = int(inventory.get("grade_rows") or 0)
    profiles = int(inventory.get("profile_rows") or 0)
    files = list(inventory.get("files") or [])
    if rows:
        lines.append(f"· 成绩记录 {rows} 条（grade_history）")
    if profiles:
        lines.append(f"· 学生侧写 {profiles} 份（student_profiles）")
    if inventory.get("archive"):
        lines.append("· 成长档案快照 1 份（archive.json.enc）")
    classes = list(inventory.get("class_files") or [])
    if classes:
        lines.append(f"· 班级成绩快照 {len(classes)} 份（{classes[0]} 等）")
    legacy = list(inventory.get("legacy_files") or [])
    if legacy:
        lines.append("· 旧版本残留的加密文件 " + "、".join(legacy))
    if rows or profiles:
        lines.append("· 已保存的 AI 侧写正文（如果勾选了上面的选项）")
    if files:
        lines.append("· 磁盘上的加密文件 " + str(len(files)) + " 个")
    if not lines:
        return "没有找到可删除的数据。"
    # ★ v1.9.2：聊天缓存（u_<uid>/cache/chat_history_<学号>.json.enc）由
    #   core.database.clear_library 一并清掉 —— 确认弹窗必须如实写出来，
    #   不能删了用户不知道（AGENTS §34：聊天历史按学生落盘）。
    lines.append("· 该生的 AI 聊天缓存也会一并清除")
    return "将删除本账户的下列数据：\n" + "\n".join(dict.fromkeys(lines))


def clear_summary_text(report: dict[str, Any]) -> str:
    """清完之后的回执（**写清删了多少**，不要只说"已清空"）。"""
    rows = int(report.get("grade_rows") or 0)
    profiles = int(report.get("profile_rows") or 0)
    files = len(list(report.get("files") or []))
    parts: list[str] = []
    if rows:
        parts.append(f"成绩记录 {rows} 条")
    if profiles:
        parts.append(f"侧写 {profiles} 份")
    if files:
        parts.append(f"加密文件 {files} 个")
    failed = list(report.get("failed") or [])
    text = "已删除：" + "、".join(parts) + "。" if parts else "没有任何数据被删除。"
    if failed:
        text += "\n没删掉的：" + "；".join(failed)
    return text


class GpaSchemeDialog(QDialog):
    """编辑分档或线性换算并预览单科绩点。"""

    def __init__(self, definition: dict[str, Any] | None, parent: QWidget) -> None:
        super().__init__(parent)
        source = definition or {}
        self._scheme_id = source.get("id") or custom_id()
        self.setWindowTitle("编辑 GPA 方案" if definition else "新建 GPA 方案")
        self.resize(520, 560)
        box = QVBoxLayout(self)

        form = QFormLayout()
        self._name = QLineEdit(source.get("name", ""))
        self._name.setMaxLength(40)
        form.addRow("方案名称", self._name)
        self._mode = QComboBox()
        self._mode.addItem("分档换算", "table")
        self._mode.addItem("线性换算", "linear")
        self._mode.setCurrentIndex(self._mode.findData(source.get("kind", "table")))
        form.addRow("换算方式", self._mode)
        self._precision = QSpinBox()
        self._precision.setRange(0, 6)
        self._precision.setValue(source.get("round", 2))
        form.addRow("显示小数位数", self._precision)
        box.addLayout(form)

        self._modes = QStackedWidget()
        table_page = QWidget()
        table_box = QVBoxLayout(table_page)
        table_box.setContentsMargins(0, 0, 0, 0)
        self._rows = QTableWidget(0, 2)
        self._rows.setHorizontalHeaderLabels(("最低分（含）", "绩点"))
        self._rows.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._rows.setMinimumHeight(145)
        for floor, point in source.get("rows", ((90, 4), (60, 1), (0, 0))):
            self._insert_row(self._rows.rowCount(), str(floor), str(point))
        table_box.addWidget(self._rows, 1)
        actions = QHBoxLayout()
        add = QPushButton("增加一档")
        add.clicked.connect(self._add_row)
        actions.addWidget(add)
        remove = QPushButton("删除所选档")
        remove.clicked.connect(self._remove_row)
        actions.addWidget(remove)
        actions.addStretch()
        table_box.addLayout(actions)
        self._modes.addWidget(table_page)

        linear_page = QWidget()
        linear = QFormLayout(linear_page)
        linear.setContentsMargins(0, 0, 0, 0)
        self._linear = {}
        for key, caption, default in (
            ("a", "系数（乘以成绩）", 0.1), ("b", "截距（加到结果）", -5),
            ("min_point", "最低绩点", 0), ("max_point", "最高绩点", 5),
        ):
            field = QLineEdit(str(source.get(key, default)))
            linear.addRow(caption, field)
            self._linear[key] = field
            field.textChanged.connect(self._refresh)
        self._modes.addWidget(linear_page)
        self._modes.setCurrentIndex(self._mode.currentIndex())
        box.addWidget(self._modes, 1)

        preview = QFormLayout()
        mapping = "，".join(f"{label}={score:g}" for label, score in source.get("grade_map", {}).items())
        self._mapping = QLineEdit(mapping)
        self._mapping.setPlaceholderText("例如 优=95，良=85（选填）")
        preview.addRow("等级映射", self._mapping)
        self._sample = QLineEdit("85")
        self._result = QLabel()
        preview.addRow("试算成绩", self._sample)
        preview.addRow("试算绩点", self._result)
        box.addLayout(preview)
        feedback = QScrollArea()
        feedback.setWidgetResizable(True)
        feedback.setFrameShape(QFrame.Shape.NoFrame)
        feedback.setMaximumHeight(82)
        self._errors = QLabel()
        self._errors.setStyleSheet("color: #B42318;")
        self._errors.setTextFormat(Qt.TextFormat.PlainText)
        self._errors.setWordWrap(True)
        feedback.setWidget(self._errors)
        box.addWidget(feedback)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        box.addWidget(self._buttons)

        self._mode.currentIndexChanged.connect(self._modes.setCurrentIndex)
        self._mode.currentIndexChanged.connect(self._refresh)
        for field in (self._name, self._mapping, self._sample):
            field.textChanged.connect(self._refresh)
        self._precision.valueChanged.connect(self._refresh)
        self._rows.itemChanged.connect(self._refresh)
        self._refresh()

    def _insert_row(self, row: int, floor: str, point: str) -> None:
        self._rows.insertRow(row)
        self._rows.setItem(row, 0, QTableWidgetItem(floor))
        self._rows.setItem(row, 1, QTableWidgetItem(point))

    def _add_row(self) -> None:
        row = max(0, self._rows.rowCount() - 1)
        self._insert_row(row, "", "")
        self._rows.setCurrentCell(row, 0)
        self._rows.editItem(self._rows.item(row, 0))
        self._refresh()

    def _remove_row(self) -> None:
        row = self._rows.currentRow()
        if row >= 0:
            self._rows.removeRow(row)
            self._refresh()

    def definition(self) -> dict[str, Any]:
        kind = self._mode.currentData()
        data = {"id": self._scheme_id, "name": self._name.text(),
                "kind": kind, "round": self._precision.value()}
        if kind == "table":
            data["rows"] = [
                tuple(self._rows.item(row, col).text() if self._rows.item(row, col) else ""
                      for col in range(2))
                for row in range(self._rows.rowCount())
            ]
        else:
            data.update({key: field.text() for key, field in self._linear.items()})
        if self._mapping.text().strip():
            mapping = {}
            for entry in self._mapping.text().replace("，", ",").split(","):
                label, _, score = entry.partition("=")
                if not label.strip() or not score.strip() or label.strip() in mapping:
                    raise SchemeError("等级映射格式为「优=95，良=85」，每个等级只填写一次。")
                mapping[label.strip()] = score.strip()
            data["grade_map"] = mapping
        return data

    def _validated(self) -> dict[str, Any] | None:
        try:
            data = self.definition()
            errors = validate_scheme(data)
        except SchemeError as exc:
            errors = [str(exc)]
        self._errors.setText("\n".join(errors))
        self._buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not errors)
        return data if not errors else None

    def _refresh(self) -> None:
        data = self._validated()
        point = grade_point(self._sample.text(), data) if data else None
        self._result.setText("—" if point is None else f"{point:.{data['round']}f}")

    def accept(self) -> None:
        if self._validated() is not None:
            super().accept()

class SettingsPage(QWidget):
    """设置页。

    Args:
        store: 加密存储（用来定位本用户的载荷文件）。
        root: 数据根目录；``None`` = 默认 AppData。
    """

    #: 「删除成绩库」真的删完之后的广播：``(成绩条数, 侧写份数, 删掉的加密文件数)``。
    #:
    #: ★ 档案页 / 智育分析页接到它就该 ``reload()`` —— 数据层已经干净了，
    #:   界面不刷新会继续显示一份"已经删掉的成绩"（AGENTS §18：一处真源 + 信号刷新）。
    library_cleared = Signal(int, int, int)
    gpa_scheme_changed = Signal(str)

    def __init__(
        self,
        store: CryptoStore | None = None,
        root: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore(root)
        self._username = ""
        self._gpa_settings = load_settings()
        #: 由主窗口注入：返回当前要导出的记录列表（可能为空）
        self._export_provider: Callable[[], list[dict[str, Any]]] | None = None
        #: 更新检查正在进行中（防止连点，也防止回调里重复开弹窗）
        self._checking = False
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        self._username = username or ""
        self._gpa_settings = load_settings(self._store, self._username)
        self._sync_gpa_card()

    def set_export_provider(self, provider: Callable[[], list[dict[str, Any]]]) -> None:
        self._export_provider = provider

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """每次切到设置页都顺手刷新「删除成绩库」的状态。

        ★ v1.9.2：清完库再切回设置页时，这里必须已经写着「没有成绩库」——
          判据是 ``grade_history`` 表为空。**只改状态文案**，不动任何数据。
        """
        super().showEvent(event)
        self._refresh_clear_status()

    def _refresh_clear_status(self) -> None:
        """按 ``grade_history`` 表把「删除成绩库」的状态文案对齐。

        读不到库时**一个字都不改**（页面还没登录 / 还没解锁都属正常）。
        """
        label = getattr(self, "_clr_status", None)
        if label is None or not self._username:
            return
        try:
            inventory = library_inventory(self._store, self._username)
        except Exception:  # noqa: BLE001 - 只是状态文案，读不到就维持原样
            return
        if not inventory.get("account"):
            return
        if int(inventory.get("grade_rows") or 0):
            # 有新成绩了 —— 把上一次"已删除 / 已取消"的旧状态清掉，别让它说谎
            label.setText("")
        else:
            label.setText("没有成绩库：本账户还没有导入过成绩。")

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        """排列导出、GPA、更新和成绩库操作。"""
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

        title = QLabel("设置")
        title.setObjectName("H1")
        box.addWidget(title)

        # --- 导出 ---
        exp_card = make_card()
        exp = QVBoxLayout(exp_card)
        exp.setContentsMargins(24, 20, 24, 20)
        exp.setSpacing(10)

        exp_title = QLabel("导出")
        exp_title.setObjectName("H2")
        exp_row = QHBoxLayout()
        exp_row.addWidget(exp_title, 1)
        plain = QPushButton("含姓名版")
        plain.setToolTip("导出含学生姓名的 Excel 文件")
        plain.setObjectName("Ghost")
        plain.setMinimumHeight(38)
        plain.clicked.connect(lambda: self._do_export(anonymized=False))
        exp_row.addWidget(plain)

        masked = QPushButton("脱敏版")
        masked.setToolTip("导出移除姓名和学号的 Excel 文件")
        masked.setObjectName("Ghost")
        masked.setMinimumHeight(38)
        masked.clicked.connect(lambda: self._do_export(anonymized=True))
        exp_row.addWidget(masked)
        exp.addLayout(exp_row)

        self._exp_status = QLabel("")
        self._exp_status.setObjectName("Muted")
        self._exp_status.setWordWrap(True)
        exp.addWidget(self._exp_status)
        box.addWidget(exp_card)

        # --- 删除成绩库 ---
        clr_card = make_card()
        clr = QVBoxLayout(clr_card)
        clr.setContentsMargins(24, 20, 24, 20)
        clr.setSpacing(10)

        clr_title = QLabel("删除成绩库")
        clr_title.setObjectName("H2")
        clr.addWidget(clr_title)

        #: 侧写 / 生成物跟着成绩一起清（**默认勾选**，用户可以取消）
        self._clear_profiles_box = QCheckBox("同时删除侧写与档案快照")
        self._clear_profiles_box.setChecked(True)
        self._clear_profiles_box.setToolTip(
            "不勾选的话只清成绩记录，侧写正文和成长档案快照会留下（它们就对不上了）"
        )
        clr_row = QHBoxLayout()
        clr_row.addWidget(self._clear_profiles_box, 1)
        clear_btn = QPushButton("删除成绩库")
        clear_btn.setObjectName("Danger")
        clear_btn.setMinimumHeight(38)
        clear_btn.clicked.connect(self._do_clear)
        clr_row.addWidget(clear_btn)
        clr.addLayout(clr_row)

        self._clr_status = QLabel("")
        self._clr_status.setObjectName("Muted")
        self._clr_status.setWordWrap(True)
        clr.addWidget(self._clr_status)
        box.addWidget(self._build_gpa_card())

        box.addWidget(self._build_update_card())
        box.addWidget(clr_card)
        box.addStretch(1)

    # ---------------------------------------------------------------- GPA 规则
    def _build_gpa_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(10)
        title = QLabel("GPA 换算规则")
        title.setObjectName("H2")
        box.addWidget(title)

        row = QHBoxLayout()
        self._gpa_combo = QComboBox()
        self._gpa_combo.setMinimumHeight(34)
        self._gpa_combo.currentIndexChanged.connect(self._on_gpa_scheme_changed)
        row.addWidget(self._gpa_combo, 1)
        self._gpa_edit = QPushButton("编辑")
        self._gpa_edit.setObjectName("Ghost")
        self._gpa_edit.clicked.connect(lambda: self._edit_gpa_scheme(self.gpa_definition()))
        row.addWidget(self._gpa_edit)
        self._gpa_delete = QPushButton("删除")
        self._gpa_delete.setObjectName("Ghost")
        self._gpa_delete.clicked.connect(self._delete_gpa_scheme)
        row.addWidget(self._gpa_delete)
        box.addLayout(row)

        self._gpa_status = QLabel("")
        self._gpa_status.setObjectName("Muted")
        self._gpa_status.setWordWrap(True)
        self._gpa_status.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self._gpa_status)
        self._sync_gpa_card()
        return card

    def _sync_gpa_card(self) -> None:
        current = self.load_gpa_scheme()
        with QSignalBlocker(self._gpa_combo):
            self._gpa_combo.clear()
            for item in scheme_options(self._gpa_settings["custom"]):
                self._gpa_combo.addItem(item.get("label") or item["name"], item["id"])
            self._gpa_combo.addItem("自定义…")
            self._gpa_combo.setCurrentIndex(self._gpa_combo.findData(current))
        self._gpa_combo.setEnabled(bool(self._username))
        is_custom = current.startswith("custom:")
        self._gpa_edit.setVisible(is_custom)
        self._gpa_delete.setVisible(is_custom)

        definition = self.gpa_definition()
        if definition["kind"] == "table":
            rule = "，".join(f"≥{floor:g} → {point:g}" for floor, point in definition["rows"])
        else:
            a, b = definition["a"], definition["b"]
            rule = (f"成绩 × {a:g} {b:+g}，绩点范围 "
                    f"{definition['min_point']:g}–{definition['max_point']:g}")
        note = definition.get("note", "")
        if not note and definition.get("grade_map"):
            note = "、".join(f"{label} {score:g}" for label, score in definition["grade_map"].items()) + "。"
        self._gpa_status.setText(f"当前方案：{rule}。{note}")

    def load_gpa_scheme(self) -> str:
        return self._gpa_settings["scheme"]

    def gpa_definition(self) -> dict[str, Any]:
        return resolve_scheme(self.load_gpa_scheme(), self._gpa_settings["custom"])

    def _save_gpa_settings(self, **changes: Any) -> bool:
        try:
            self._gpa_settings = save_settings(self._store, self._username, **changes)
        except (OSError, ValueError, CryptoError) as exc:
            self._sync_gpa_card()
            self._gpa_status.setText(f"GPA 设置保存失败：{exc}")
            return False
        self._sync_gpa_card()
        self.gpa_scheme_changed.emit(self.load_gpa_scheme())
        return True

    def _on_gpa_scheme_changed(self, index: int) -> None:
        if index < 0 or not self._username:
            return
        scheme = self._gpa_combo.itemData(index)
        if scheme is None:
            self._sync_gpa_card()
            self._edit_gpa_scheme()
        elif scheme != self.load_gpa_scheme():
            self._save_gpa_settings(scheme=scheme)

    def _edit_gpa_scheme(self, definition: dict[str, Any] | None = None) -> None:
        items = self._gpa_settings["custom"]
        if definition is None and len(items) >= MAX_CUSTOM_SCHEMES:
            self._gpa_status.setText("自定义方案最多 10 个，请先删除不用的方案。")
            return
        dialog = GpaSchemeDialog(definition, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        saved = dialog.definition()
        if definition is None:
            existing = {item["id"] for item in items}
            while saved["id"] in existing:
                saved["id"] = custom_id()
            items = [*items, saved]
        else:
            items = [saved if item["id"] == saved["id"] else item for item in items]
        self._save_gpa_settings(scheme=saved["id"], custom=items)

    def _delete_gpa_scheme(self) -> None:
        scheme = self.load_gpa_scheme()
        if not scheme.startswith("custom:"):
            return
        name = self.gpa_definition()["name"]
        answer = QMessageBox.question(
            self, "删除 GPA 方案", f"确定删除「{name}」并切回默认方案？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        items = [item for item in self._gpa_settings["custom"] if item["id"] != scheme]
        if self._save_gpa_settings(scheme=DEFAULT_SCHEME, custom=items):
            self._gpa_status.setText("已切回默认方案。" + self._gpa_status.text())

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
        row = QHBoxLayout()
        row.addWidget(title, 1)
        self._ver_label = QLabel(f"v{CURRENT_VERSION}")
        self._ver_label.setObjectName("H3")
        row.addWidget(self._ver_label)
        row.addSpacing(12)

        self._update_btn = QPushButton("检查更新")
        self._update_btn.setObjectName("Ghost")
        self._update_btn.setMinimumHeight(38)
        self._update_btn.setMinimumWidth(110)
        self._update_btn.clicked.connect(self._do_check_update)
        row.addWidget(self._update_btn)
        box.addLayout(row)

        self._upd_status = QLabel("")
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

        self._run_update_wizard(plan)

    def _run_update_wizard(self, plan: Any) -> None:
        """选择、下载、准备安装并确认重启。"""
        chooser = ModuleChoiceDialog(plan, self)
        if chooser.exec() != chooser.DialogCode.Accepted:
            self._set_update_status("已取消：没有下载任何内容。")
            return

        self._set_update_status(f"正在下载 v{plan.version}…")
        destination = (
            update_apply.update_dir() / f"transcript-lens_v{plan.version}.exe"
        )
        target = update_apply.current_exe()
        root = self._store.root

        def prepare(path: Path) -> None:
            update_apply.prepare_update(path, target, plan.version, sha256=plan.main_sha256, root=root)

        downloaded = download_release(
            self, plan.main_url, destination,
            label=f"正在下载主程序 v{plan.version}",
            expected_sha256=plan.main_sha256,
            parts=plan.main_parts,
            expected_size=plan.main_bytes,
            prepare=prepare if target is not None else None,
        )
        if downloaded is None:
            self._set_update_status("更新未完成，可重新检查更新。", error=True)
            return

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

        if ask_restart(self, plan, Path(downloaded), root=root):
            # ★ 交接已经完成，**必须**退出 —— 不退出的话旧进程还占着
            #   exe 文件锁，bat 会重试 60 秒然后放弃。
            self._set_update_status("正在重启以完成更新…")
            app = QApplication.instance()
            if app is not None:
                app.quit()
        elif target is not None:
            self._set_update_status(f"v{plan.version} 已准备好，下次启动时完成安装。")
        else:
            self._set_update_status(f"v{plan.version} 已下载。")

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

    # ---------------------------------------------------------------- 删除成绩库
    def _do_clear(self) -> None:
        """「删除成绩库」—— **先盘点、再问、最后才删**。

        ★ v1.9.1 修的就是这里：原来的判据是"磁盘上有没有 ``grades.enc`` /
          ``tags.json.enc``"，而 v1.9 起成绩进了
          :class:`core.database.GradeHistoryDB`（``grade_history.json.enc``）——
          于是导入完成绩点这个按钮，界面说"库里本来就是空的"，
          而成长档案页明明有一整份名单。

        ★ 现在的判据只有一条：``library_inventory()`` 里 ``grade_rows`` 为空
          （= **``grade_history`` 表为空**）。

        ★ v1.9.2 再把判据收紧到「成绩历史表为空」：清完库之后即使磁盘上还
          残留快照文件，这里也必须说「没有成绩库」—— 否则就是那个旧 bug：
          界面说有名单、设置页说没数据。**聊天缓存**由
          :func:`core.database.clear_library` 一并清掉（确认弹窗已如实写明）。

        ★ 删除动作全在 :func:`core.database.clear_library` 里（数据层，可单测），
          这里只做三件事：问用户、调它、报结果。
        """
        if not self._username:
            QMessageBox.warning(self, "还不知道是谁", "请先登录。")
            return

        inventory = library_inventory(self._store, self._username)
        if not inventory.get("account"):
            QMessageBox.warning(self, "找不到账户", f"本机没有「{self._username}」。")
            return
        if not int(inventory.get("grade_rows") or 0):
            # ★ v1.9.2：判据 = ``grade_history`` 表为空（**不是**"磁盘上有没有
            #   文件"，也不是侧写/快照还剩几份）。清完库再点进设置页必须走到
            #   这里，不许再出现"界面说有名单、设置页说没数据"。
            self._clr_status.setText("没有成绩库：本账户还没有导入过成绩。")
            QMessageBox.information(
                self, "没有成绩库",
                "本账户还没有导入过成绩，没有可删除的数据。",
            )
            return

        with_profiles = self._clear_profiles_box.isChecked()
        first = QMessageBox.question(
            self,
            "确认要删除吗",
            clear_confirm_text(inventory)
            + "\n\n删除后无法恢复，只能用原始成绩表重新导入。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            self._clr_status.setText("已取消，什么都没删。")
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

        try:
            report = clear_library(
                self._store,
                self._username,
                with_profiles=with_profiles,
                with_generated=with_profiles,
            )
        except Exception as exc:  # noqa: BLE001 - 删失败要说清楚，不许静默
            QMessageBox.critical(self, "删除失败", f"{type(exc).__name__}: {exc}")
            self._clr_status.setText(f"删除失败：{exc}")
            return

        text = clear_summary_text(report)
        self._clr_status.setText(
            text + "　相关页面（成长档案 / 智育分析）会重新读一次库。"
        )
        if report.get("failed"):
            QMessageBox.warning(self, "部分文件没删掉", text)
        else:
            QMessageBox.information(self, "已删除", text)
        # ★ 数据层已经干净了，广播出去让别的页面 reload()（AGENTS §18）
        self.library_cleared.emit(
            int(report.get("grade_rows") or 0),
            int(report.get("profile_rows") or 0),
            len(list(report.get("files") or [])),
        )
