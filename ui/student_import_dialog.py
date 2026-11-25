"""可预览的学生名册与照片导入。"""

import csv
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QProgressBar, QPlainTextEdit,
)

from core.database import StudentStore
from core.student_import import apply_import, plan_import, write_template
from ui.workbench_widgets import TaskDialog, button, label, unlock


class StudentImportDialog(TaskDialog):
    completed = Signal(int, str)
    progress = Signal(int, int)

    def __init__(self, parent, vault):
        super().__init__(parent, "导入学生资料")
        self.setMinimumSize(680, 500)
        self.vault = vault
        self.file = None
        self.photos = None
        self.plan = None
        self.issues = []
        heading = label("导入学生资料", "H2")
        self.body.addWidget(heading)
        row = QHBoxLayout()
        row.addWidget(button("选择 Excel", self._file))
        row.addWidget(button("选择照片文件夹", self._photos))
        row.addWidget(button("下载模板", self._template))
        row.addStretch(1)
        self.body.addLayout(row)
        self.selection = label("请选择 Excel。照片文件夹可选。", "Muted")
        self.selection.setWordWrap(True)
        self.body.addWidget(self.selection)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.body.addWidget(self.preview, 1)
        self.replace = QCheckBox("确认用表中的姓名和班级更新已有学生")
        self.body.addWidget(self.replace)
        self.bar = QProgressBar()
        self.bar.hide()
        self.body.addWidget(self.bar)
        self.progress.connect(self._progress)
        self.body.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addWidget(button("导出待补清单", self._export))
        actions.addStretch(1)
        actions.addWidget(button("预览", self._preview))
        self.submit = button("确认导入", self._commit, "Primary")
        self.submit.setEnabled(False)
        actions.addWidget(self.submit)
        self.body.addLayout(actions)

    def _selection(self):
        self.selection.setText(f"{self.file.name if self.file else '未选文件'}  /  {self.photos.name if self.photos else '未选照片目录'}")
        self.plan = None
        self.issues = []
        self.preview.clear()
        self.submit.setEnabled(False)

    def _file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择学生资料", filter="Excel (*.xlsx)")
        if filename:
            self.file = Path(filename)
            self._selection()

    def _photos(self):
        folder = QFileDialog.getExistingDirectory(self, "选择按学号命名的照片文件夹")
        if folder:
            self.photos = Path(folder)
            self._selection()

    def _template(self):
        filename, _ = QFileDialog.getSaveFileName(self, "保存导入模板", "学生资料模板.xlsx", "Excel (*.xlsx)")
        if filename:
            self.execute(lambda: write_template(Path(filename)), lambda _: self.status.setText("已保存模板。"))

    def _preview(self):
        if not self.file:
            self.status.setText("请先选择 Excel 文件。")
            return
        self.submit.setEnabled(False)

        def shown(plan):
            self.plan = plan
            self.issues = list(plan.issues)
            roster = StudentStore(store=self.vault.store, username=self.vault.username)
            present = {student["student_id"]: student for student in roster.rows()}
            for entry in plan.students:
                student = present.get(f"sid:{entry.sid}")
                if student and (student["name"] != entry.name or student["class_name"] != entry.class_name):
                    self.issues.append((entry.row, entry.sid, "冲突", "姓名或班级与已存资料不同，请检查后确认。"))
            rows = [f"{item.sid}　{item.name}　{item.class_name}　{'已匹配照片' if item.photo else '待补照片'}"
                    for item in plan.students]
            self.preview.setPlainText(f"可预览 {len(rows)} 人，共 {len(self.issues)} 条提示\n\n"
                                      + "\n".join(rows + [""] + self._issue_lines()))
            self.submit.setEnabled(bool(rows))

        self.execute(lambda: plan_import(self.file, self.photos), shown, "正在核对模板与照片…")

    def _progress(self, done, total):
        self.bar.setRange(0, total)
        self.bar.setValue(done)

    def _commit(self):
        if self.plan is None or not unlock(self, self.vault):
            return
        plan = self.plan
        replace = self.replace.isChecked()
        self.bar.setRange(0, 0)
        self.bar.show()

        def done(result):
            count, self.issues = result
            self.preview.setPlainText(f"已提交 {count} 人\n\n" + "\n".join(self._issue_lines()))
            self.submit.setEnabled(False)
            self.bar.hide()
            if count:
                rejected = {sid for _, sid, kind, _ in self.issues if kind == "拒绝"}
                first = next((item.sid for item in plan.students if item.sid not in rejected), "")
                self.completed.emit(count, first)

        self.execute(lambda: apply_import(self.vault, plan, replace_identity=replace,
                                          progress=self.progress.emit), done, "正在保存密文…")

    def _issue_lines(self):
        return [f"{row or '—'}　{sid or '—'}　{kind}　{detail}" for row, sid, kind, detail in self.issues]

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出待补清单", "学生资料待补.csv", "CSV (*.csv)")
        if not path:
            return
        with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("Excel 行", "学号", "类型", "说明"))
            for row in self.issues:
                writer.writerow(tuple("'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v for v in row))
        self.status.setText("待补清单已导出。")
