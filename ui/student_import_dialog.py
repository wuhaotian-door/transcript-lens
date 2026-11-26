"""学生名册、电话和照片的三个独立导入入口。"""

import csv
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QProgressBar,
    QPlainTextEdit, QStackedWidget, QTabBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.crypto_store import CryptoError
from core.database import StudentStore
from core.student_import import (
    HEADERS, MissingClassError, apply_import, grid_workbook, import_template, plan_import,
    plan_supplement, resolve_photos, save_import_template,
)
from ui.phone_import_dialog import ImportTemplateDialog, PhoneGridDialog
from ui.photo_review_dialog import PhotoReviewDialog
from ui.workbench_widgets import TaskDialog, button, label, review_table, unlock


class StudentImportDialog(TaskDialog):
    completed = Signal(int, str)
    progress = Signal(int, int)
    MODES = ("roster", "phone", "photo")

    def __init__(self, parent, vault):
        super().__init__(parent, "导入学生资料")
        self.resize(885, 680)
        self.setMinimumSize(730, 560)
        self.vault = vault
        self.file = self.phone_file = self.photos = None
        self.grid_columns = self.grid_rows = ()
        self.plan = None
        self.issues = []
        self.body.addWidget(label("导入学生资料", "H2"))
        self.modes = QTabBar()
        self.modes.setExpanding(False)
        for title in ("学生名册导入", "电话导入", "照片导入"):
            self.modes.addTab(title)
        self.body.addWidget(self.modes)
        self.inputs = QStackedWidget()
        self.body.addWidget(self.inputs)

        self.default_class = QLineEdit()
        self.default_class.setMaxLength(80)
        self.default_class.setPlaceholderText("表中缺少班级时填写")
        self.default_class.setAccessibleName("默认班级")
        self._input_page(
            "学号和姓名必填。可按需映射班级、性别，缺班级时可指定默认班级。",
            (button("选择名册 Excel", self._file), button("设置名册模板", self._template)),
            ("默认班级", self.default_class),
        )
        self._input_page(
            "为在册学生导入电话。选择 Excel 或直接在应用内录入。",
            (button("选择电话表", self._file), button("设置电话模板", self._template),
             button("录入电话", self._enter_phones)),
        )
        self.photo_scope = QComboBox()
        self.photo_scope.setAccessibleName("照片匹配范围")
        self._refresh_classes()
        self._input_page(
            "选择照片文件夹后自动识别「学号-姓名」等文件名。无法确认的照片可手工指定。",
            (button("选择照片文件夹", self._photos),), ("照片匹配范围", self.photo_scope),
        )
        self.selection = label("", "Muted")
        self.selection.setWordWrap(True)
        self.body.addWidget(self.selection)
        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(("学号", "姓名", "班级", "导入内容"))
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        review_table(self.preview)
        self.body.addWidget(self.preview, 2)
        self.notices = QPlainTextEdit()
        self.notices.setPlaceholderText("预览后在这里查看待核对项。")
        self.notices.setReadOnly(True)
        self.notices.setMaximumHeight(110)
        self.body.addWidget(self.notices, 1)
        self.replace = QCheckBox()
        self.body.addWidget(self.replace)
        self.bar = QProgressBar()
        self.bar.hide()
        self.body.addWidget(self.bar)
        self.progress.connect(self._progress)
        self.body.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addWidget(button("导出核对清单", self._export))
        actions.addWidget(button("清除选择", self._reset))
        actions.addStretch()
        actions.addWidget(button("预览", self._preview))
        self.submit = button("导入", self._commit, "Primary")
        actions.addWidget(self.submit)
        self.body.addLayout(actions)

        self.modes.currentChanged.connect(self._change_mode)
        self.default_class.textChanged.connect(self._selection)
        self.default_class.returnPressed.connect(self._preview)
        self.photo_scope.currentIndexChanged.connect(self._selection)
        self._change_mode(0)

    @property
    def mode(self):
        return self.MODES[self.modes.currentIndex()]

    def _input_page(self, guide, actions, extra=None):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        hint = label(guide, "Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        for action in actions:
            row.addWidget(action)
        if extra:
            caption, control = extra
            row.addSpacing(8)
            row.addWidget(label(caption))
            row.addWidget(control, 1)
        else:
            row.addStretch()
        layout.addLayout(row)
        self.inputs.addWidget(panel)

    def _refresh_classes(self):
        previous = self.photo_scope.currentData()
        self.photo_scope.blockSignals(True)
        self.photo_scope.clear()
        self.photo_scope.addItem("所有在册班级", "")
        roster = StudentStore(store=self.vault.store, username=self.vault.username)
        groups = {row["class_name"] for row in roster.rows(include_archived=False) if row.get("class_name")}
        for name in sorted(groups):
            self.photo_scope.addItem(name, name)
        self.photo_scope.setCurrentIndex(max(0, self.photo_scope.findData(previous)))
        self.photo_scope.blockSignals(False)

    def _change_mode(self, index):
        self.inputs.setCurrentIndex(index)
        self.replace.setText((
            "确认用表中的姓名、班级和性别更新已有学生",
            "覆盖已有的对应电话",
            "覆盖已有照片",
        )[index])
        self.replace.setChecked(False)
        self._selection()

    def _selection(self):
        path = (self.file, self.phone_file, self.photos)[self.modes.currentIndex()]
        text = f"已选 {path.name}" if path is not None else ""
        if self.mode == "phone" and self.grid_rows:
            text = f"已录入 {len(self.grid_rows)} 行"
        self.selection.setText(text)
        self.selection.setVisible(bool(text))
        self.plan = None
        self.issues = []
        self.preview.setRowCount(0)
        self.notices.clear()
        self.status.clear()
        self.bar.hide()
        self.submit.setEnabled(False)

    def _reset(self):
        if self.mode == "roster":
            self.file = None
            self.default_class.clear()
        elif self.mode == "phone":
            self.phone_file = None
            self.grid_columns = self.grid_rows = ()
        else:
            self.photos = None
        self._selection()

    def _file(self):
        title = "选择名册 Excel" if self.mode == "roster" else "选择电话表"
        filename, _ = QFileDialog.getOpenFileName(self, title, filter="Excel (*.xlsx)")
        if not filename:
            return
        if self.mode == "roster":
            self.file = Path(filename)
        else:
            self.phone_file = Path(filename)
            self.grid_columns = self.grid_rows = ()
        self._selection()
        self._preview()

    def _photos(self):
        folder = QFileDialog.getExistingDirectory(self, "选择照片文件夹")
        if folder:
            self.photos = Path(folder)
            self._selection()
            self._preview()

    def _template(self):
        mode = self.mode
        columns = import_template(self.vault.store, self.vault.username, mode)
        dialog = ImportTemplateDialog(self, columns, mode=mode)
        if dialog.exec():
            try:
                save_import_template(self.vault.store, self.vault.username, dialog.columns, mode)
            except (CryptoError, OSError, ValueError) as error:
                self.status.setText(str(error))
            else:
                self._selection()
                self.status.setText("模板已保存。")
        dialog.deleteLater()

    def _enter_phones(self):
        if not unlock(self, self.vault):
            return
        columns = import_template(self.vault.store, self.vault.username, "phone")
        dialog = PhoneGridDialog(self, self.vault, columns, old_columns=self.grid_columns, rows=self.grid_rows)
        if dialog.exec():
            self.phone_file = None
            self.grid_columns, self.grid_rows = columns, dialog.rows
            self._selection()
            self._preview()
        dialog.table.clearContents()
        dialog.deleteLater()

    def _controls(self):
        return self.modes, self.photo_scope, self.default_class, self.replace

    def _preview(self):
        mode = self.mode
        source = {"roster": self.file, "phone": self.phone_file or self.grid_rows, "photo": self.photos}[mode]
        if not source:
            self.status.setText("请选择该页的导入来源。")
            return
        self._selection()
        roster_file, phone_file, photos = self.file, self.phone_file, self.photos
        rows, grid_columns = self.grid_rows, self.grid_columns
        vault = self.vault
        scope = self.photo_scope.currentData() or ""
        default_class = self.default_class.text()

        def prepare():
            if mode == "photo":
                return plan_supplement(vault.store, vault.username, None, photos, photo_class=scope)
            columns = import_template(vault.store, vault.username, mode)
            if mode == "phone":
                if rows:
                    return plan_supplement(vault.store, vault.username, None, None,
                                           columns=grid_columns, grid=grid_workbook(grid_columns, rows))
                return plan_supplement(vault.store, vault.username, phone_file, None, columns=columns)
            result = plan_import(roster_file, columns=columns, default_class=default_class, include_contacts=False)
            roster = StudentStore(store=vault.store, username=vault.username)
            present = {student["student_id"]: student for student in roster.rows()}
            for entry in result.students:
                old = present.get(f"sid:{entry.sid}") or {}
                if any(value and old.get(key) and old[key] != value for key, value in
                       (("name", entry.name), ("class_name", entry.class_name), ("gender", entry.gender))):
                    result.issues.append((entry.row, entry.sid, "冲突", "资料与已有学生不同，请核对是否覆盖。"))
            return result

        def shown(plan):
            if plan.photo_conflicts:
                review = PhotoReviewDialog(self, plan)
                decisions = dict(review.decisions) if review.exec() else {}
                review.deleteLater()
                plan = resolve_photos(plan, decisions)
            self.plan = plan
            self.issues = list(plan.issues)
            self.preview.setRowCount(len(plan.students))
            for row, entry in enumerate(plan.students):
                if mode == "photo":
                    details = entry.photo.name if entry.photo else "—"
                elif mode == "phone":
                    details = "、".join(header for header, value in zip(HEADERS[3:], entry.phones) if value)
                else:
                    details = f"性别：{entry.gender}" if entry.gender else "无性别字段值"
                for col, value in enumerate((entry.sid, entry.name, entry.class_name, details)):
                    item = QTableWidgetItem(value)
                    if col == 3:
                        item.setToolTip(value)
                    self.preview.setItem(row, col, item)
            self.status.setText(f"可导入 {len(plan.students)} 人，共 {len(self.issues)} 条核对项。")
            self.notices.setPlainText("\n".join(self._issue_lines()))
            self.submit.setEnabled(bool(plan.students))

        def failed(error):
            if isinstance(error, MissingClassError):
                self.default_class.setFocus()
                self.default_class.selectAll()

        self.execute(prepare, shown, "正在核对学生资料…", controls=self._controls(), on_error=failed)

    def _progress(self, done, total):
        self.bar.setRange(0, total)
        self.bar.setValue(done)

    def _commit(self):
        if self.plan is None or not unlock(self, self.vault):
            return
        plan = self.plan
        mode = self.mode
        replace = self.replace.isChecked()
        self.bar.setRange(0, 0)
        self.bar.show()

        def done(result):
            count, self.issues = result
            self.notices.setPlainText("\n".join(self._issue_lines()))
            self.status.setText(f"已导入 {count} 人，共 {len(self.issues)} 条核对项。")
            self.submit.setEnabled(False)
            self.plan = None
            self.bar.hide()
            if mode == "phone":
                self.grid_columns = self.grid_rows = ()
                self.selection.setVisible(self.phone_file is not None)
            if count:
                if mode == "roster":
                    self._refresh_classes()
                skipped = {sid for _, sid, kind, _ in self.issues if kind in {"拒绝", "跳过"}}
                first = next((item.sid for item in plan.students if item.sid not in skipped), "")
                self.completed.emit(count, first)

        self.execute(lambda: apply_import(self.vault, plan, replace_identity=replace and mode == "roster",
                                          replace_existing=replace and mode != "roster",
                                          progress=self.progress.emit), done, "正在导入…",
                     controls=self._controls(), on_error=lambda _: self.bar.hide())

    def _issue_lines(self):
        return [f"{row or '—'}　{sid or '—'}　{kind}　{detail}" for row, sid, kind, detail in self.issues]

    def _export(self):
        filename, _ = QFileDialog.getSaveFileName(self, "导出核对清单", "学生资料核对.csv", "CSV (*.csv)")
        if not filename:
            return
        try:
            with Path(filename).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("Excel 行", "学号", "类型", "说明"))
                for row in self.issues:
                    writer.writerow(tuple("'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v for v in row))
        except OSError as error:
            self.status.setText(f"无法保存清单：{error.strerror or '请检查文件是否被占用'}")
        else:
            self.status.setText("核对清单已导出。")
