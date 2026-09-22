"""💰 奖学金测算页 —— 从"纯壳"升级成可用（规则见 charter v1.6 第 4 章）。

页面结构::

    ┌ 班级 [CS2023-1 ▾]   [重新读取] ────────────────────────────────┐
    │ ① 德育分（0-100）—— 可直接从 Excel 整列粘贴                      │
    │    排名 | 姓名 | 学号 | 智育分 | 德育分(QSpinBox)                │
    │    [保存德育分]                                                  │
    ├ ② 名额：一等[2] 二等[3] 三等[4]  [保存名额]  ☐允许特等叠加       │
    │    [开始测算]                                                    │
    ├ ③ 结果表格（金/银/铜/紫着色）                                    │
    │    排名 | 姓名 | 学号 | 智育分 | 德育分 | 最终成绩 | 奖项 | 备注  │
    │    统计行 + [导出结果]                                           │
    └──────────────────────────────────────────────────────────────────┘

★ 这一页**一行加权算术都没有**：智育分是导入时算好存进加密库的
  （:class:`core.scholarship.ClassRecord`），这里只调
  :func:`core.scholarship.compute_scholarship`。

★ 为什么德育分用 **delegate 里的 QSpinBox**，而不是每格
  ``setCellWidget(QSpinBox)``：
  ``setCellWidget`` 之后那个格子就**不再是一个 item**，QTableWidget 的
  原生 Ctrl+V 粘贴会跳过它 —— 而"从 Excel 整列粘贴"是这一页最要紧的输入方式
  （几十号人一个个点太慢）。用 :class:`MoralSpinDelegate`，
  双击编辑时是 0-100 的 SpinBox，批量粘贴照常工作。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.crypto_store import CryptoError, CryptoStore
from core.scholarship import (
    ACADEMIC_WEIGHT,
    AWARD_FIRST,
    AWARD_SECOND,
    AWARD_SPECIAL,
    AWARD_THIRD,
    MORAL_WEIGHT,
    SPECIAL_MIN_SCORE,
    ClassRecord,
    Quota,
    ScholarshipRow,
    award_counts,
    compute_scholarship,
    list_classes,
    load_moral,
    load_quota,
    save_moral,
    save_quota,
)
from ui.anim import make_card

__all__ = ["MORAL_MAX", "MORAL_MIN", "ScholarshipPage"]

MORAL_MIN = 0
MORAL_MAX = 100

#: 结果表格列（顺序即列顺序；**从这一份推导下标，禁止手写元组**）
RESULT_COLUMNS: tuple[str, ...] = (
    "排名", "姓名", "学号", "智育分", "德育分", "最终成绩", "奖项", "备注",
)
RANK_COL = 0
NAME_COL = 1
ID_COL = 2
ACADEMIC_COL = 3
MORAL_COL = 4
FINAL_COL = 5
AWARD_COL = 6
NOTE_COL = 7

#: 德育分录入表列
MORAL_COLUMNS: tuple[str, ...] = ("排名", "姓名", "学号", "智育分", "德育分")
M_NAME_COL = 1
M_ID_COL = 2
M_ACADEMIC_COL = 3
M_MORAL_COL = 4

#: 奖项配色（charter 4.5：一等金、二等银、三等铜、特等紫）
_AWARD_STYLE: dict[str, tuple[str, str]] = {
    AWARD_SPECIAL: ("#F3E8FF", "#6B21A8"),   # 紫
    AWARD_FIRST:   ("#FEF3C7", "#92400E"),   # 金
    AWARD_SECOND:  ("#E5E7EB", "#374151"),   # 银
    AWARD_THIRD:   ("#FFEDD5", "#9A3412"),   # 铜
}


class MoralSpinDelegate(QStyledItemDelegate):
    """德育分那一列的编辑器：0-100 的 QSpinBox。

    ★ 只提供**编辑器**，不改变单元格本身是 item 这件事 ——
      所以 QTableWidget 的原生粘贴（从 Excel 复制一列贴进来）照常工作。
      用 ``setCellWidget`` 就会把这一点弄丢。
    """

    def createEditor(self, parent, option, index):  # noqa: N802 - Qt 命名
        editor = QSpinBox(parent)
        editor.setRange(MORAL_MIN, MORAL_MAX)
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor, index) -> None:  # noqa: N802
        try:
            value = int(index.data(Qt.ItemDataRole.EditRole) or 0)
        except (TypeError, ValueError):
            value = 0
        editor.setValue(max(MORAL_MIN, min(MORAL_MAX, value)))

    def setModelData(self, editor, model, index) -> None:  # noqa: N802
        editor.interpretText()
        model.setData(index, str(editor.value()), Qt.ItemDataRole.EditRole)


def _cell(text: str, *, align=Qt.AlignmentFlag.AlignCenter,
          bg: str = "", fg: str = "", bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(int(align | Qt.AlignmentFlag.AlignVCenter))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if bg:
        item.setBackground(QColor(bg))
    if fg:
        item.setForeground(QColor(fg))
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


class ScholarshipPage(QWidget):
    """奖学金测算页。

    Args:
        store: 加密存储。班级、德育分、名额都按用户存在里面。
    """

    def __init__(self, store: CryptoStore | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore()
        self._username = ""
        #: 当前选中的班级快照
        self._record: ClassRecord | None = None
        #: 上一次测算的结果（导出用）
        self._rows: list[ScholarshipRow] = []
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        self._username = username or ""
        self.reload_classes()

    def reload_classes(self, select: str | None = None) -> None:
        """重新扫一遍加密库里的班级，刷新下拉。

        ``select`` 指定要选中哪一个（刚导入完那个班时用）。
        """
        blocked = self._class_box.blockSignals(True)
        previous = select or self._class_box.currentText()
        try:
            self._class_box.clear()
            try:
                records = (list_classes(self._store, self._username)
                           if self._username else [])
            except (CryptoError, OSError):
                records = []
            for record in records:
                self._class_box.addItem(f"{record.class_name}（{record.size} 人）",
                                        record.class_name)
        finally:
            # ★ 必须 finally：中途抛异常也要把信号放回去，
            #   否则这个下拉从此**再也不响应选择**（最难查的那种）。
            self._class_box.blockSignals(blocked)

        if self._class_box.count() == 0:
            self._record = None
            self._set_status("还没有任何班级。先在「📊 智育分析」填好班级号并导入成绩表。")
            self._clear_moral_table()
            self._clear_result_table()
            if hasattr(self, "_head_status"):
                self._head_status.setText("")
            return
        index = self._class_box.findData(previous)
        self._class_box.setCurrentIndex(index if index >= 0 else 0)
        self._load_current_class()

    @property
    def class_count(self) -> int:
        return self._class_box.count()

    @property
    def current_class(self) -> str:
        return self._class_box.currentData() or ""

    def class_names(self) -> list[str]:
        return [self._class_box.itemData(i) for i in range(self._class_box.count())]

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 整页可滚 —— 三块卡片加起来比一屏高，不给滚动条就会被压扁
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(14)

        title = QLabel("💰  奖学金测算")
        title.setObjectName("H1")
        box.addWidget(title)

        formula = QLabel(
            f"最终成绩 = 智育加权（口径① 含全部）× {ACADEMIC_WEIGHT:g}"
            f" + 德育分 × {MORAL_WEIGHT:g}　·　"
            f"特等奖：进入一等名额且每门 ≥ {SPECIAL_MIN_SCORE:g} 分，不占名额"
        )
        formula.setObjectName("Muted")
        formula.setWordWrap(True)
        box.addWidget(formula)

        box.addWidget(self._build_class_card())
        box.addWidget(self._build_moral_card())
        box.addWidget(self._build_quota_card())
        box.addWidget(self._build_result_card())
        box.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    # --- ① 班级 ---
    def _build_class_card(self) -> QFrame:
        card = make_card()
        box = QHBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        label = QLabel("班级")
        label.setObjectName("H2")
        box.addWidget(label)

        self._class_box = QComboBox()
        self._class_box.setMinimumHeight(36)
        self._class_box.setMinimumWidth(220)
        self._class_box.currentIndexChanged.connect(self._load_current_class)
        box.addWidget(self._class_box)

        reload_btn = QPushButton("重新读取")
        reload_btn.setObjectName("Ghost")
        reload_btn.setMinimumHeight(36)
        reload_btn.clicked.connect(lambda: self.reload_classes())
        box.addWidget(reload_btn)
        box.addStretch(1)

        self._head_status = QLabel("")
        self._head_status.setObjectName("Muted")
        box.addWidget(self._head_status)
        return card

    # --- ② 德育分 ---
    def _build_moral_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("① 德育分（0-100）")
        title.setObjectName("H2")
        head.addWidget(title)
        hint = QLabel("双击编辑，或从 Excel 复制一列直接 Ctrl+V 粘贴")
        hint.setObjectName("Muted")
        head.addWidget(hint)
        head.addStretch(1)

        self._save_moral_btn = QPushButton("保存德育分")
        self._save_moral_btn.setObjectName("Primary")
        self._save_moral_btn.setMinimumHeight(34)
        self._save_moral_btn.clicked.connect(self._do_save_moral)
        head.addWidget(self._save_moral_btn)
        box.addLayout(head)

        self._moral_table = QTableWidget(0, len(MORAL_COLUMNS))
        self._moral_table.setHorizontalHeaderLabels(list(MORAL_COLUMNS))
        self._moral_table.setAlternatingRowColors(True)
        self._moral_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._moral_table.verticalHeader().setVisible(False)
        self._moral_table.verticalHeader().setDefaultSectionSize(34)
        self._moral_table.setShowGrid(False)
        self._moral_table.setItemDelegateForColumn(
            M_MORAL_COL, MoralSpinDelegate(self._moral_table)
        )
        self._moral_table.itemChanged.connect(self._on_moral_changed)
        header = self._moral_table.horizontalHeader()
        for col in range(len(MORAL_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(M_MORAL_COL, QHeaderView.ResizeMode.Fixed)
        self._moral_table.setColumnWidth(M_MORAL_COL, 110)
        self._moral_table.setMinimumHeight(150)
        self._moral_table.setMaximumHeight(300)
        box.addWidget(self._moral_table)

        self._moral_status = QLabel("")
        self._moral_status.setObjectName("Muted")
        self._moral_status.setWordWrap(True)
        box.addWidget(self._moral_status)
        return card

    # --- ③ 名额 ---
    def _build_quota_card(self) -> QFrame:
        card = make_card()
        box = QHBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        title = QLabel("② 名额")
        title.setObjectName("H2")
        box.addWidget(title)

        self._quota_spins: dict[str, QSpinBox] = {}
        for key, text in (("first", "一等"), ("second", "二等"), ("third", "三等")):
            box.addWidget(QLabel(text))
            spin = QSpinBox()
            spin.setRange(0, 500)
            spin.setMinimumHeight(34)
            spin.setMinimumWidth(78)
            self._quota_spins[key] = spin
            box.addWidget(spin)

        save_btn = QPushButton("保存名额")
        save_btn.setObjectName("Ghost")
        save_btn.setMinimumHeight(34)
        save_btn.clicked.connect(self._do_save_quota)
        box.addWidget(save_btn)

        self._stack_box = QCheckBox("允许特等叠加一二三等")
        self._stack_box.setToolTip(
            "默认不叠加：拿特等的人**不占**名额，后面的同学往前补。\n"
            "勾上则两个奖都算他的（表格里显示「特等+一等」）。"
        )
        box.addWidget(self._stack_box)
        box.addStretch(1)

        self._run_btn = QPushButton("③ 开始测算")
        self._run_btn.setObjectName("Primary")
        self._run_btn.setMinimumHeight(38)
        self._run_btn.setMinimumWidth(130)
        self._run_btn.clicked.connect(self._do_compute)
        box.addWidget(self._run_btn)
        return card

    # --- ④ 结果 ---
    def _build_result_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("④ 测算结果")
        title.setObjectName("H2")
        head.addWidget(title)
        head.addStretch(1)
        self._export_btn = QPushButton("导出结果")
        self._export_btn.setObjectName("Ghost")
        self._export_btn.setMinimumHeight(34)
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._do_export)
        head.addWidget(self._export_btn)
        box.addLayout(head)

        self._result_table = QTableWidget(0, len(RESULT_COLUMNS))
        self._result_table.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._result_table.setAlternatingRowColors(True)
        self._result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._result_table.verticalHeader().setVisible(False)
        self._result_table.verticalHeader().setDefaultSectionSize(36)
        self._result_table.setShowGrid(False)
        header = self._result_table.horizontalHeader()
        for col in range(len(RESULT_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(RANK_COL, QHeaderView.ResizeMode.Fixed)
        self._result_table.setColumnWidth(RANK_COL, 60)
        self._result_table.setMinimumHeight(200)
        box.addWidget(self._result_table)

        self._stat_label = QLabel("")
        self._stat_label.setObjectName("Muted")
        self._stat_label.setWordWrap(True)
        box.addWidget(self._stat_label)

        self._set_status("先在「📊 智育分析」填好班级号并导入成绩表。")
        return card

    # ---------------------------------------------------------------- 状态行
    def _set_status(self, text: str) -> None:
        if hasattr(self, "_stat_label"):
            self._stat_label.setText(text)

    # ---------------------------------------------------------------- 读班级
    def _load_current_class(self) -> None:
        name = self.current_class
        if not name or not self._username:
            self._record = None
            self._clear_moral_table()
            return
        try:
            records = {r.class_name: r for r in list_classes(self._store, self._username)}
        except (CryptoError, OSError):
            records = {}
        record = records.get(name)
        self._record = record
        if record is None:
            self._clear_moral_table()
            self._head_status.setText("读不出这个班的数据")
            return

        self._head_status.setText(
            f"{record.size} 人　·　来源 {record.source or '（未知）'}"
        )
        quota = load_quota(self._store, self._username, name)
        self._quota_spins["first"].setValue(quota.first)
        self._quota_spins["second"].setValue(quota.second)
        self._quota_spins["third"].setValue(quota.third)
        self._fill_moral_table(record, load_moral(self._store, self._username, name))
        self._clear_result_table()
        self._set_status("德育分和名额都填好后，点「③ 开始测算」。")

    def _clear_moral_table(self) -> None:
        blocked = self._moral_table.blockSignals(True)
        self._moral_table.setRowCount(0)
        self._moral_table.blockSignals(blocked)
        self._moral_status.setText("")

    def _clear_result_table(self) -> None:
        self._result_table.setRowCount(0)
        self._rows = []
        self._export_btn.setEnabled(False)

    def _fill_moral_table(self, record: ClassRecord, moral: dict[str, int]) -> None:
        """按班级名单重建德育分录入表。

        ★ 行序沿用 ``record.students`` 的原始顺序（导入时的顺序），
          **不按成绩排** —— 辅导员是对着花名册录分的，
          一排序他就找不到人了。
        """
        blocked = self._moral_table.blockSignals(True)
        self._moral_table.setRowCount(len(record.students))
        for i, student in enumerate(record.students):
            self._moral_table.setItem(i, 0, _cell(str(i + 1)))
            self._moral_table.setItem(i, M_NAME_COL,
                                      _cell(str(student.get("student") or "")))
            self._moral_table.setItem(i, M_ID_COL,
                                      _cell(str(student.get("student_id") or "—")))
            academic = student.get("academic")
            self._moral_table.setItem(
                i, M_ACADEMIC_COL,
                _cell("—" if academic is None else f"{float(academic):.6f}"),
            )
            value = moral.get(str(student.get("key") or ""))
            # 没录过就给空白（不是 0）—— 0 是"真的德育 0 分"，
            # 空白是"还没录"，这两个必须能分开（红线 2 的同一条道理）
            item = QTableWidgetItem("" if value is None else str(value))
            item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter
                                      | Qt.AlignmentFlag.AlignVCenter))
            self._moral_table.setItem(i, M_MORAL_COL, item)
        self._moral_table.blockSignals(blocked)
        self._refresh_moral_status()

    # ---------------------------------------------------------------- 校验
    def _on_moral_changed(self, item: QTableWidgetItem) -> None:
        """粘贴进来的值可能带小数、负数、超过 100 —— 就地夹到 0-100。"""
        if item.column() != M_MORAL_COL:
            return
        text = item.text().strip()
        if text:
            try:
                value = int(round(float(text)))
            except (TypeError, ValueError):
                value = MORAL_MIN
            value = max(MORAL_MIN, min(MORAL_MAX, value))
            if str(value) != text:
                blocked = self._moral_table.blockSignals(True)
                item.setText(str(value))
                self._moral_table.blockSignals(blocked)
        # ★ 每次改动都重算"已录入 N / M" —— 不重算的话，用户粘完一列
        #   抬头一看还写着"已录入 0 / 6"，会以为没粘进去。
        self._refresh_moral_status()

    def _refresh_moral_status(self) -> None:
        if self._record is None:
            return
        filled = len(self._collect_moral())
        total = self._record.size
        self._moral_status.setText(
            f"已录入 {filled} / {total} 人的德育分。"
            + ("" if filled == total
               else "　⚠ 没录的人按 0 分参与测算，结果表里会注明。")
        )

    def _collect_moral(self) -> dict[str, int]:
        """把表格里的德育分收成 ``{学生 key: 分数}``（空格子 = 没录，不进去）。"""
        out: dict[str, int] = {}
        if self._record is None:
            return out
        for i, student in enumerate(self._record.students):
            if i >= self._moral_table.rowCount():
                break
            cell = self._moral_table.item(i, M_MORAL_COL)
            text = (cell.text() if cell is not None else "").strip()
            if not text:
                continue
            try:
                out[str(student.get("key") or "")] = max(
                    MORAL_MIN, min(MORAL_MAX, int(round(float(text))))
                )
            except (TypeError, ValueError):
                continue
        return out

    # ---------------------------------------------------------------- 动作
    def _do_save_moral(self) -> None:
        if self._record is None:
            QMessageBox.information(self, "还没选班", "先选一个班级。")
            return
        try:
            save_moral(self._store, self._username, self._record.class_name,
                       self._collect_moral())
        except (CryptoError, ValueError, OSError) as exc:
            QMessageBox.critical(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return
        count = len(self._collect_moral())
        self._moral_status.setText(f"已保存 {count} 人的德育分。")
        QMessageBox.information(self, "已保存",
                                f"{self._record.class_name}：{count} 人的德育分已加密存盘。")

    def _do_save_quota(self) -> None:
        if self._record is None:
            QMessageBox.information(self, "还没选班", "先选一个班级。")
            return
        quota = Quota(self._quota_spins["first"].value(),
                      self._quota_spins["second"].value(),
                      self._quota_spins["third"].value())
        try:
            save_quota(self._store, self._username, self._record.class_name, quota)
        except (CryptoError, ValueError, OSError) as exc:
            QMessageBox.critical(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(
            self, "已保存",
            f"{self._record.class_name}：一等 {quota.first} / 二等 {quota.second}"
            f" / 三等 {quota.third} 名。",
        )

    def _do_compute(self) -> None:
        """测算。**不重新解析任何 Excel** —— 用导入时存好的快照。"""
        if self._record is None:
            QMessageBox.information(self, "还没选班",
                                    "先在「📊 智育分析」导入一个班的成绩表。")
            return
        quota = Quota(self._quota_spins["first"].value(),
                      self._quota_spins["second"].value(),
                      self._quota_spins["third"].value())
        if quota.total == 0:
            QMessageBox.information(
                self, "名额都是 0",
                "一/二/三等奖名额都是 0，算出来只有特等奖。\n"
                "先填名额再测算。",
            )
            return

        moral = self._collect_moral()
        rows = compute_scholarship(
            self._record, moral, quota, stackable=self._stack_box.isChecked()
        )
        self._rows = rows
        self._fill_result_table(rows, quota, moral)
        self._export_btn.setEnabled(bool(rows))

    # ---------------------------------------------------------------- 出表
    def _fill_result_table(self, rows: list[ScholarshipRow],
                           quota: Quota, moral: dict[str, int]) -> None:
        self._result_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            bg, fg = _AWARD_STYLE.get(row.award, ("", ""))
            self._result_table.setItem(i, RANK_COL, _cell(str(row.rank), bg=bg, fg=fg))
            self._result_table.setItem(i, NAME_COL, _cell(row.student, bg=bg, fg=fg))
            self._result_table.setItem(
                i, ID_COL, _cell(row.student_id or "—", bg=bg, fg=fg))
            self._result_table.setItem(
                i, ACADEMIC_COL, _cell(row.academic_text, bg=bg, fg=fg))
            self._result_table.setItem(
                i, MORAL_COL, _cell(row.moral_text, bg=bg, fg=fg))
            self._result_table.setItem(
                i, FINAL_COL, _cell(row.final_text, bg=bg, fg=fg, bold=True))
            self._result_table.setItem(
                i, AWARD_COL,
                _cell(row.award or "—", bg=bg, fg=fg, bold=bool(row.award)))
            self._result_table.setItem(i, NOTE_COL,
                                       _cell(row.note, bg=bg, fg=fg))

        counts = award_counts(rows)
        missing = self._record.size - len(moral) if self._record else 0
        text = (
            f"该班 {self._record.size if self._record else 0} 人　·　"
            f"参测 {len(rows)} 人　·　"
            f"特等 {counts[AWARD_SPECIAL]}/—　·　"
            f"一等 {counts[AWARD_FIRST]}/{quota.first}　·　"
            f"二等 {counts[AWARD_SECOND]}/{quota.second}　·　"
            f"三等 {counts[AWARD_THIRD]}/{quota.third}"
        )
        if missing:
            text += f"\n⚠ 有 {missing} 人没录德育分，按 0 分参与测算（结果表备注里有注明）。"
        self._set_status(text)

    # ---------------------------------------------------------------- 导出
    def _do_export(self) -> None:
        if not self._rows or self._record is None:
            return
        default = f"奖学金测算_{self._record.class_name}.xlsx"
        path, _f = QFileDialog.getSaveFileName(
            self, "导出测算结果", default, "Excel 工作簿 (*.xlsx)"
        )
        if not path:
            return
        try:
            written = _write_scholarship_xlsx(
                path, self._record, self._rows,
                Quota(self._quota_spins["first"].value(),
                      self._quota_spins["second"].value(),
                      self._quota_spins["third"].value()),
                self._collect_moral(),
            )
        except Exception as exc:  # noqa: BLE001 - 导出失败要说清楚
            QMessageBox.critical(self, "导出失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(
            self, "导出完成",
            f"已写到：\n{written}\n\n表格含姓名，仅供辅导员本人留档，请勿外传。",
        )


def _write_scholarship_xlsx(path: str, record: ClassRecord,
                            rows: list[ScholarshipRow], quota: Quota,
                            moral: dict[str, int]) -> str:
    """把测算结果写成 xlsx（openpyxl，**不联网**）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    book = Workbook()
    sheet = book.active
    sheet.title = "奖学金测算"

    sheet.append([f"{record.class_name} 奖学金测算"])
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.append([
        f"最终成绩 = 智育(口径①)×{ACADEMIC_WEIGHT:g} + 德育×{MORAL_WEIGHT:g}",
        f"特等奖：进入一等名额且每门 ≥ {SPECIAL_MIN_SCORE:g}，不占名额",
    ])
    sheet.append([
        f"名额：一等 {quota.first} / 二等 {quota.second} / 三等 {quota.third}",
        f"来源：{record.source or '（未知）'}",
    ])
    sheet.append([])

    header_row = sheet.max_row + 1
    sheet.append(list(RESULT_COLUMNS))
    for cell in sheet[header_row]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    fills = {
        AWARD_SPECIAL: PatternFill("solid", fgColor="F3E8FF"),
        AWARD_FIRST: PatternFill("solid", fgColor="FEF3C7"),
        AWARD_SECOND: PatternFill("solid", fgColor="E5E7EB"),
        AWARD_THIRD: PatternFill("solid", fgColor="FFEDD5"),
    }

    for row in rows:
        sheet.append([
            row.rank, row.student, row.student_id,
            row.academic, row.moral, row.final, row.award, row.note,
        ])
        fill = fills.get(row.award)
        if fill is not None:
            for cell in sheet[sheet.max_row]:
                cell.fill = fill

    counts = award_counts(rows)
    sheet.append([])
    sheet.append([
        f"总计 {record.size} 人 / 参测 {len(rows)} 人"
        f" / 已录德育分 {len(moral)} 人"
    ])
    sheet.append([
        f"实际分配：特等 {counts[AWARD_SPECIAL]}"
        f" / 一等 {counts[AWARD_FIRST]}（名额 {quota.first}）"
        f" / 二等 {counts[AWARD_SECOND]}（名额 {quota.second}）"
        f" / 三等 {counts[AWARD_THIRD]}（名额 {quota.third}）"
    ])
    sheet.append([])
    sheet.append(["本文件含学生姓名，仅供辅导员本人留档，请勿外传。"])

    for column, width in zip("ABCDEFGH", (6, 12, 14, 12, 10, 12, 12, 42)):
        sheet.column_dimensions[column].width = width

    book.save(path)
    return path
