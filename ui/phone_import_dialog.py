"""设计联系表字段，并在应用内编辑待导入的电话行。"""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHeaderView, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.student_import import (
    DEFAULT_PHONE_TEMPLATE, DEFAULT_ROSTER_TEMPLATE, HEADERS, ROSTER_HEADERS,
    validate_phone_template, validate_roster_template, write_template,
)
from ui.workbench_widgets import button, label


class ImportTemplateDialog(QDialog):
    def __init__(self, parent, columns, *, mode):
        super().__init__(parent)
        self.phone = mode == "phone"
        self.roles = HEADERS if self.phone else ROSTER_HEADERS
        self.default = DEFAULT_PHONE_TEMPLATE if self.phone else DEFAULT_ROSTER_TEMPLATE
        self.validate = validate_phone_template if self.phone else validate_roster_template
        self.title = "电话模板" if self.phone else "名册模板"
        self.setWindowTitle(f"设置{self.title}")
        self.resize(600, 425)
        self.columns = columns
        body = QVBoxLayout(self)
        heading = label(f"设置{self.title}", "H2")
        body.addWidget(heading)
        guide = label("修改列名和对应字段，可拖动左侧行号调整列顺序。额外列可设为忽略。", "Muted")
        guide.setWordWrap(True)
        body.addWidget(guide)
        self.table = QTableWidget(0, 2)
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setHorizontalHeaderLabels(("Excel 列名", "对应字段"))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.verticalHeader().setSectionsMovable(True)
        body.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("加一列", self._append))
        row.addWidget(button("删除当前列", self._remove))
        row.addWidget(button("恢复默认", lambda: self._load(self.default)))
        row.addStretch(1)
        body.addLayout(row)
        self.status = label("", "Muted")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addWidget(button("导出 Excel 模板", self._export))
        actions.addStretch(1)
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存模板", self._save, "Primary"))
        body.addLayout(actions)
        self._load(columns)

    def _load(self, columns):
        self.table.setRowCount(0)
        for name, role in columns:
            self._add(name, role)
        self.status.clear()

    def _add(self, name, role):
        index = self.table.rowCount()
        self.table.insertRow(index)
        self.table.setItem(index, 0, QTableWidgetItem(name))
        field = QComboBox()
        field.setStyleSheet("QComboBox { padding: 2px 8px; }")
        field.addItem("忽略", "")
        for title in self.roles:
            field.addItem(title, title)
        field.setCurrentIndex(max(0, field.findData(role)))
        self.table.setCellWidget(index, 1, field)

    def _append(self):
        if self.table.rowCount() >= 20:
            self.status.setText("最多添加 20 列。")
            return
        self._add("", "")
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self.table.editItem(self.table.currentItem())

    def _remove(self):
        current = self.table.currentRow()
        if current >= 0:
            self.table.removeRow(current)

    def _read(self):
        return self.validate([
            (self.table.item(i, 0).text(), self.table.cellWidget(i, 1).currentData())
            for i in (self.table.verticalHeader().logicalIndex(row) for row in range(self.table.rowCount()))
        ])

    def _save(self):
        try:
            self.columns = self._read()
        except ValueError as error:
            self.status.setText(str(error))
        else:
            self.accept()

    def _export(self):
        try:
            columns = self._read()
            filename, _ = QFileDialog.getSaveFileName(self, "保存电话模板", f"{self.title}.xlsx", "Excel (*.xlsx)")
            if filename:
                write_template(Path(filename), supplement=self.phone, columns=columns)
                self.status.setText("模板已导出。返回前请保存模板，供本账户匹配列名。")
        except (ValueError, OSError) as error:
            self.status.setText(str(error))


class _TelephoneTable(QTableWidget):
    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_cells()
        else:
            super().keyPressEvent(event)

    def paste_cells(self) -> bool:
        lines = QApplication.clipboard().text().splitlines()
        if not lines:
            return True
        start_row = max(self.currentRow(), 0)
        start_col = max(self.currentColumn(), 0)
        rows = [line.split("\t") for line in lines]
        if start_row + len(rows) > 10_000 or max(map(len, rows)) > self.columnCount() - start_col:
            return False
        self.setRowCount(max(self.rowCount(), start_row + len(rows)))
        for offset, values in enumerate(rows):
            for column, value in enumerate(values, start_col):
                self.setItem(start_row + offset, column, QTableWidgetItem(value))
        return True


class PhoneGridDialog(QDialog):
    def __init__(self, parent, vault, columns, *, old_columns=(), rows=()):
        super().__init__(parent)
        self.setWindowTitle("录入电话")
        self.resize(880, 535)
        self.rows = ()
        body = QVBoxLayout(self)
        body.addWidget(label("录入电话", "H2"))
        hint = label("可直接输入，或在起始单元格粘贴 Excel 区域。填写学号或姓名。留空的电话会保留原值。", "Muted")
        hint.setWordWrap(True)
        body.addWidget(hint)
        self.table = _TelephoneTable(max(10, len(rows)), len(columns))
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setHorizontalHeaderLabels([name for name, _ in columns])
        self.table.horizontalHeader().setDefaultSectionSize(125)
        self.table.verticalHeader().setDefaultSectionSize(30)
        previous = {(role or name): index for index, (name, role) in enumerate(old_columns)}
        for col, (name, role) in enumerate(columns):
            if (old := previous.get(role or name)) is not None:
                for row, values in enumerate(rows):
                    if values[old]:
                        self.table.setItem(row, col, QTableWidgetItem(values[old]))
        body.addWidget(self.table, 1)
        self.status = label("", "Muted")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addWidget(button("粘贴表格", self._paste))
        actions.addWidget(button("增加 10 行", self._append))
        actions.addStretch(1)
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("使用这份电话表", self._save, "Primary"))
        body.addLayout(actions)
        timer = QTimer(self, interval=800)
        timer.timeout.connect(lambda: self.reject() if not vault.unlocked else None)
        timer.start()

    def _paste(self):
        if not self.table.paste_cells():
            self.status.setText("粘贴区域超出表格列数，或总行数超过 10000。")

    def _append(self):
        self.table.setRowCount(min(10_000, self.table.rowCount() + 10))

    def _save(self):
        data = []
        for i in range(self.table.rowCount()):
            row = tuple((self.table.item(i, j).text().strip() if self.table.item(i, j) else "")
                        for j in range(self.table.columnCount()))
            if any(row):
                data.append(row)
        if not data:
            self.status.setText("请先输入至少一行资料。")
            return
        self.rows = tuple(data)
        self.accept()
