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
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.activation import load_state, try_activate
from core.crypto_store import CryptoError, CryptoStore, PAYLOAD_FILES
from core.exporter import export_anonymized_xlsx, export_plain_xlsx
from ui.anim import make_card

__all__ = ["SettingsPage"]


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

        box.addStretch(1)

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
