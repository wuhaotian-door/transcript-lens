"""导师自核的两种练习和本地照片候选。"""

import base64
import random
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QListWidget, QMessageBox, QVBoxLayout, QWidget

from core.database import StudentStore
from core.photo_matching import FaceEngine, configure_matching, index_photo, match_photo, valid_consent
from core.workbench_models import installed
from ui.workbench_widgets import TaskDialog, button, fields, label, unlock


class MentorPage(QWidget):
    open_student = Signal(str)
    model_center = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.vault = None
        box = QVBoxLayout(self)
        box.setContentsMargins(28, 22, 28, 22)
        box.setSpacing(14)
        box.addWidget(label("导师自核", "H1"))
        box.addWidget(label("按班级练习姓名与照片，或用已获授权的照片查找候选。", "Muted"))
        self.classes = QComboBox()
        box.addWidget(self.classes)
        self.access = button("解锁练习", self._toggle)
        box.addWidget(self.access)
        row = QHBoxLayout()
        self.actions = QWidget()
        choices = QVBoxLayout(self.actions)
        choices.addWidget(button("看照片选名字", lambda: self._quiz(False), "Primary"))
        choices.addWidget(button("看名字选照片", lambda: self._quiz(True), "Primary"))
        choices.addWidget(button("练习统计", self._history))
        choices.addWidget(button("照片找人", self._match))
        choices.addWidget(button("按学号查找", self._manual))
        choices.addWidget(button("批量提取已获授权的照片", self._index))
        choices.addWidget(button("配置照片匹配", self._configure))
        choices.addWidget(button("前往模型中心", self.model_center.emit))
        row.addWidget(self.actions, 2)
        self.results = QListWidget()
        self.results.itemActivated.connect(lambda item: self.open_student.emit(item.data(Qt.ItemDataRole.UserRole) or ""))
        row.addWidget(self.results, 3)
        box.addLayout(row, 1)
        self.status = label("", "Muted")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.timer = QTimer(self, interval=750)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._poll()

    def set_vault(self, vault):
        self.vault = vault
        self.results.clear()
        self.status.clear()
        self.reload()
        self._poll()

    def reload(self):
        previous = self.classes.currentText()
        self.classes.clear()
        self.classes.addItem("全部班级")
        if self.vault and self.vault.username:
            rows = StudentStore(store=self.vault.store, username=self.vault.username).rows(include_archived=False)
            self.classes.addItems(sorted({row["class_name"] for row in rows if row.get("class_name")}))
        index = self.classes.findText(previous)
        if index >= 0:
            self.classes.setCurrentIndex(index)

    def _poll(self):
        state = bool(self.vault and self.vault.unlocked)
        self.actions.setEnabled(state)
        self.access.setText("锁定资料" if state else "解锁练习")
        if not state:
            self.results.clear()
            self.status.clear()

    def _toggle(self):
        if self.vault and self.vault.unlocked:
            self.vault.lock()
        else:
            unlock(self, self.vault)
        self._poll()

    def _rows(self):
        rows = StudentStore(store=self.vault.store, username=self.vault.username).rows(include_archived=False)
        name = self.classes.currentText()
        return rows if not name or name == "全部班级" else [row for row in rows if row["class_name"] == name]

    def _show_results(self, candidates, message):
        if not self.vault.unlocked:
            return
        self.results.clear()
        self.status.setText(message)
        for row, score in candidates:
            sid = row["student_id"].removeprefix("sid:")
            points = f"　相似度 {score:.3f}" if score is not None else ""
            self.results.addItem(f"{row['name']}　{sid}　{row.get('class_name', '')}{points}")
            self.results.item(self.results.count() - 1).setData(Qt.ItemDataRole.UserRole, sid)

    def _manual(self):
        form = fields(self, "按学号核对", [("sid", "学号", "")])
        if not form:
            return
        rows = [(row, None) for row in self._rows() if row["student_id"].removeprefix("sid:") == form["sid"].removeprefix("sid:")]
        self._show_results(rows, "双击学生查看资料。" if rows else "当前班级未找到该学号。")

    def _configure(self):
        if not unlock(self, self.vault, force=True):
            return
        form = fields(self, "学校授权与 1:N 校准", [
            ("reference", "部署授权和校准报告编号", ""),
            ("threshold", "报告中的候选阈值", ""),
            ("margin", "报告中的最小候选分差", ""),
        ])
        if not form:
            return
        try:
            configure_matching(self.vault, form["reference"], float(form["threshold"]), float(form["margin"]))
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "未启用匹配", str(error))
            return
        self.status.setText("已启用。请在学生资料逐人记录有效授权后提取照片特征。")

    def _match(self):
        if not installed(self.vault.store.root, "face"):
            self.status.setText("请在模型中心安装照片匹配模型，或按学号核对。")
            return
        if QMessageBox.question(self, "使用查询照片", "确认该照片具备本次核对的处理授权？") != QMessageBox.StandardButton.Yes:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "选择查询照片", filter="图片 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if filename:
            self._task("搜索候选", lambda: match_photo(self.vault, Path(filename)),
                       lambda output: self._show_results(output[1], output[0]))

    def _index(self):
        rows = self._rows()

        def work():
            engine = FaceEngine(self.vault.store.root)
            ok, skipped, failures = 0, 0, []
            for row in rows:
                sid = row["student_id"].removeprefix("sid:")
                if not valid_consent(self.vault.student(sid).get("face_consent", {})):
                    skipped += 1
                    continue
                try:
                    index_photo(self.vault, sid, engine)
                    ok += 1
                except Exception as error:
                    failures.append(f"{sid}　{error}")
            return ok, skipped, failures

        def done(value):
            good, skipped, failures = value
            self.results.clear()
            self.results.addItems(failures)
            self.status.setText(f"已提取 {good} 人，未授权或过期 {skipped} 人，失败 {len(failures)} 人。")

        self._task("批量生成照片特征", work, done)

    def _history(self):
        rows = self._rows()

        def collect():
            entries = [(row, self.vault.student(row["student_id"]).get("practice", {})) for row in rows]
            return [(row, data) for row, data in entries if data.get("attempts")]

        def done(result):
            self.results.clear()
            for row, data in result:
                self.results.addItem(f"{row['name']}　{row['student_id'].removeprefix('sid:')}　答对 {data['correct']} / {data['attempts']}　{data.get('last_practiced', '')}")
            self.status.setText("按所选班级显示练习次数。" if result else "当前班级还没有练习记录。")

        self._task("读取练习统计", collect, done)

    def _quiz(self, picture_choices):
        rows = self._rows()

        def collect():
            return [(row, photo) for row in rows if (photo := self.vault.student(row["student_id"]).get("photo"))]

        def start(usable):
            if len(usable) < 2:
                self.status.setText("至少需要两名有照片的学生。")
            else:
                self._run_quiz(usable, picture_choices)

        self._task("读取练习照片", collect, start)

    def _run_quiz(self, usable, picture_choices):
        dialog = TaskDialog(self, "看名字选照片" if picture_choices else "看照片选名字")
        dialog.resize(650, 490)
        question = label("", "H2")
        photo = label()
        target = [None]
        dialog.body.addWidget(question)
        dialog.body.addWidget(photo)
        options = [button("", lambda _=False, i=i: select(i)) for i in range(min(4, len(usable)))]
        for option in options:
            dialog.body.addWidget(option)
        dialog.body.addWidget(dialog.status)
        dialog.body.addWidget(button("下一题", lambda: advance()))

        def pixmap(photo_text):
            image = QPixmap()
            image.loadFromData(base64.b64decode(photo_text))
            return image.scaled(140, 140, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)

        choices = []
        def advance():
            rows = random.sample(usable, k=len(options))
            choices[:] = rows
            target[0] = random.randrange(len(rows))
            row, photo_text = rows[target[0]]
            question.setText(f"请选择 {row['name']}（{row['student_id'].removeprefix('sid:')}）的照片" if picture_choices else "照片中的学生是谁？")
            if not picture_choices:
                photo.setPixmap(pixmap(photo_text))
            for option, (candidate, pic) in zip(options, choices):
                option.setText("" if picture_choices else f"{candidate['name']}　{candidate['student_id'].removeprefix('sid:')}")
                if picture_choices:
                    option.setIcon(QIcon(pixmap(pic)))
                    option.setIconSize(QSize(96, 66))
                option.setEnabled(True)
            dialog.status.clear()

        def select(index):
            correct = index == target[0]
            sid = choices[target[0]][0]["student_id"].removeprefix("sid:")
            def stats(record):
                entry = record.setdefault("practice", {"attempts": 0, "correct": 0})
                entry["attempts"] += 1
                entry["correct"] += int(correct)
                entry["last_practiced"] = datetime.now().isoformat(timespec="minutes", sep=" ")
                mode = "photo" if picture_choices else "name"
                entry[mode] = int(entry.get(mode, 0)) + 1
            self.vault.update(sid, stats)
            for option in options:
                option.setEnabled(False)
            answer = choices[target[0]][0]
            dialog.status.setText("回答正确。" if correct else f"正确答案是 {answer['name']}（{sid}）。")

        timer = QTimer(dialog, interval=500)
        timer.timeout.connect(lambda: dialog.reject() if not self.vault.unlocked else None)
        timer.start()
        advance()
        dialog.exec()

    def _task(self, title, work, finished):
        dialog = TaskDialog(self, title)
        dialog.setMinimumWidth(440)
        dialog.body.addWidget(dialog.status)
        dialog.body.addWidget(button("关闭", dialog.reject))
        result = []
        def done(value):
            result.append(value)
            dialog.accept()
        dialog.execute(work, done)
        if dialog.exec() and self.vault.unlocked:
            finished(result[0])
