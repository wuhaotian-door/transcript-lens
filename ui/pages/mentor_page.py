"""导师自核的两种练习和本地照片候选。"""

import base64
import random
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QHeaderView, QHBoxLayout, QListWidget,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.database import StudentStore
from core.photo_matching import FaceEngine, configure_matching, index_photo, match_photo, valid_consent
from core.workbench_models import installed
from ui.widgets.profile_reading import profile_scroll
from ui.workbench_widgets import TaskDialog, button, fields, label, review_table, unlock


# 图标使用 24 × 24 的局部坐标，在高 DPI 屏幕上保留清晰描边。
_TILE_ICONS = {
    "photo": (((2, 4), (22, 4), (22, 20), (2, 20), (2, 4)),
              ((3, 18), (9, 11), (13, 16), (17, 12), (22, 17)), ((15, 8), (17, 8))),
    "person": (((7, 10), (7, 5), (10, 2), (14, 2), (17, 5), (17, 10), (14, 13), (10, 13), (7, 10)),
               ((3, 22), (3, 19), (7, 16), (17, 16), (21, 19), (21, 22), (3, 22))),
    "stats": (((3, 2), (3, 21), (22, 21)), ((8, 17), (8, 12)),
              ((13, 17), (13, 8)), ((18, 17), (18, 5))),
    "models": (((3, 7), (12, 2), (21, 7), (12, 12), (3, 7)),
               ((3, 12), (12, 17), (21, 12)), ((3, 17), (12, 22), (21, 17))),
    "scan": (((2, 8), (2, 2), (8, 2)), ((16, 2), (22, 2), (22, 8)),
             ((22, 16), (22, 22), (16, 22)), ((8, 22), (2, 22), (2, 16)),
             ((8, 10), (9, 10)), ((15, 10), (16, 10)), ((9, 15), (12, 17), (15, 15))),
    "id": (((2, 5), (22, 5), (22, 19), (2, 19), (2, 5)),
           ((6, 9), (9, 9), (9, 12), (6, 12), (6, 9)), ((5, 15), (10, 15)),
           ((13, 10), (19, 10)), ((13, 14), (19, 14))),
    "index": (((6, 2), (22, 2), (22, 18)), ((2, 6), (18, 6), (18, 22), (2, 22), (2, 6)),
              ((6, 17), (9, 13), (12, 16), (14, 14))),
    "shield": (((12, 2), (21, 6), (20, 14), (17, 19), (12, 22), (7, 19),
                (4, 14), (3, 6), (12, 2)), ((8, 12), (11, 15), (16, 9))),
}


def _tile_icon(name, tone):
    canvas = QPixmap(88, 88)
    canvas.setDevicePixelRatio(2)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    blue = tone == "practice"
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#eaf2ff" if blue else "#e7f5ef"))
    painter.drawRoundedRect(0, 0, 44, 44, 12, 12)
    painter.translate(10, 10)
    pen = QPen(QColor("#275fb7" if blue else "#187058"), 1.7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for first, *rest in _TILE_ICONS[name]:
        path = QPainterPath()
        path.moveTo(*first)
        for point in rest:
            path.lineTo(*point)
        painter.drawPath(path)
    painter.end()
    return canvas


def _tile(icon, title, summary, callback, tone):
    tile = QPushButton()
    tile.setObjectName("MentorTile")
    tile.setProperty("tone", tone)
    tile.setAccessibleName(title)
    tile.setAccessibleDescription(summary)
    tile.setCursor(Qt.CursorShape.PointingHandCursor)
    tile.setMinimumHeight(84)
    tile.clicked.connect(callback)
    row = QHBoxLayout(tile)
    row.setContentsMargins(16, 11, 16, 11)
    row.setSpacing(13)
    image = label()
    image.setPixmap(_tile_icon(icon, tone))
    heading = label(title, "MentorTileTitle")
    description = label(summary, "MentorTileDescription")
    description.setWordWrap(True)
    for child in (image, heading, description):
        child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    text = QVBoxLayout()
    text.setSpacing(3)
    text.addWidget(heading)
    text.addWidget(description)
    row.addWidget(image)
    row.addLayout(text, 1)
    return tile


class MentorPage(QWidget):
    open_student = Signal(str)
    model_center = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.vault = None
        surface = QWidget()
        box = QVBoxLayout(surface)
        box.setContentsMargins(28, 22, 28, 22)
        box.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(label("导师自核", "H1"))
        header.addStretch()
        scope = label("班级", "Muted")
        self.classes = QComboBox()
        self.classes.setMinimumWidth(140)
        scope.setBuddy(self.classes)
        header.addWidget(scope)
        header.addWidget(self.classes)
        self.access = button("解锁练习", self._toggle)
        header.addWidget(self.access)
        box.addLayout(header)

        self.actions = []
        self._add_section(box, "认人练习", "practice", (
            ("photo", "看照片选名字", "根据学生照片选择姓名", lambda: self._quiz(False)),
            ("person", "看名字选照片", "根据学生姓名选择照片", lambda: self._quiz(True)),
            ("stats", "练习统计", "查看答对次数和最近练习", self._history),
            ("models", "前往模型中心", "安装或管理照片匹配模型", self.model_center.emit),
        ))
        self._add_section(box, "照片核对", "verification", (
            ("scan", "照片找人", "从已授权的照片中查找候选", self._match),
            ("id", "按学号查找", "定位学生并打开资料", self._manual),
            ("index", "批量提取照片特征", "处理具备有效授权的照片", self._index),
            ("shield", "配置照片匹配", "设置学校授权和候选阈值", self._configure),
        ))
        self.status = label("", "Muted")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        box.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(profile_scroll(surface))
        self.timer = QTimer(self, interval=750)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._poll()

    def _add_section(self, layout, title, tone, actions):
        layout.addWidget(label(title, "H2"))
        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for index, (icon, title, summary, callback) in enumerate(actions):
            tile = _tile(icon, title, summary, callback, tone)
            grid.addWidget(tile, index // 2, index % 2)
            if icon != "models":
                self.actions.append(tile)
        layout.addLayout(grid)

    def set_vault(self, vault):
        self.vault = vault
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
        self.access.setText("锁定资料" if state else "解锁练习")
        self.access.setEnabled(bool(self.vault and self.vault.username))
        for tile in self.actions:
            tile.setEnabled(state)
        if not state:
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

    def _private_dialog(self, title):
        dialog = TaskDialog(self, title)
        dialog.resize(650, 390)
        vault = self.vault
        timer = QTimer(dialog, interval=500)
        timer.timeout.connect(lambda: dialog.reject() if self.vault is not vault or not vault.unlocked else None)
        timer.start()
        return dialog

    def _show_results(self, candidates, message):
        if not self.vault.unlocked:
            return
        if not candidates:
            self.status.setText(message)
            return
        vault = self.vault
        self.status.clear()
        dialog = self._private_dialog("学生候选")
        tip = label(message, "Muted")
        tip.setWordWrap(True)
        dialog.body.addWidget(tip)
        results = QListWidget()
        for row, score in candidates:
            sid = row["student_id"].removeprefix("sid:")
            points = f"　相似度 {score:.3f}" if score is not None else ""
            results.addItem(f"{row['name']}　{sid}　{row.get('class_name', '')}{points}")
            results.item(results.count() - 1).setData(Qt.ItemDataRole.UserRole, sid)
        dialog.body.addWidget(results, 1)
        controls = QHBoxLayout()
        controls.addStretch()
        controls.addWidget(button("关闭", dialog.reject))
        open_button = button("打开学生资料", dialog.accept, "Primary")
        controls.addWidget(open_button)
        dialog.body.addLayout(controls)
        results.itemActivated.connect(dialog.accept)
        results.setCurrentRow(0)
        if dialog.exec() and self.vault is vault and vault.unlocked:
            current = results.currentItem()
            if current is not None:
                self.open_student.emit(current.data(Qt.ItemDataRole.UserRole))
        dialog.deleteLater()

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
            summary = f"已提取 {good} 人，未授权或过期 {skipped} 人，失败 {len(failures)} 人。"
            self.status.setText(summary)
            if failures:
                dialog = self._private_dialog("照片特征提取结果")
                dialog.body.addWidget(label(summary, "Muted"))
                details = QListWidget()
                details.addItems(failures)
                dialog.body.addWidget(details, 1)
                dialog.body.addWidget(button("关闭", dialog.reject))
                dialog.exec()
                dialog.deleteLater()

        self._task("批量生成照片特征", work, done)

    def _history(self):
        rows = self._rows()
        scope = self.classes.currentText()

        def collect():
            entries = [(row, self.vault.student(row["student_id"]).get("practice", {})) for row in rows]
            return [(row, data) for row, data in entries if data.get("attempts")]

        def done(result):
            dialog = self._private_dialog("练习统计")
            dialog.body.addWidget(label(f"{scope} · {len(result)} 名学生有练习记录", "H2"))
            if result:
                table = QTableWidget(len(result), 5)
                table.setObjectName("ReviewTable")
                table.setHorizontalHeaderLabels(["姓名", "学号", "班级", "答对 / 总次数", "最近练习"])
                review_table(table)
                table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
                for index, (row, data) in enumerate(result):
                    for column, value in enumerate((
                        row["name"], row["student_id"].removeprefix("sid:"), row.get("class_name", ""),
                        f"{data.get('correct', 0)} / {data['attempts']}", data.get("last_practiced", ""),
                    )):
                        table.setItem(index, column, QTableWidgetItem(str(value)))
                table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
                dialog.body.addWidget(table, 1)
            else:
                dialog.body.addWidget(label("当前班级还没有练习记录。", "Muted"))
                dialog.body.addStretch()
            dialog.body.addWidget(button("关闭", dialog.reject), alignment=Qt.AlignmentFlag.AlignRight)
            dialog.exec()
            dialog.deleteLater()

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
        dialog = self._private_dialog("看名字选照片" if picture_choices else "看照片选名字")
        dialog.resize(650, 490)
        question = label("", "H2")
        photo = label()
        target = [None]
        dialog.body.addWidget(question)
        dialog.body.addWidget(photo)
        options = [button("", lambda _=False, i=i: select(i), "QuizChoice") for i in range(min(4, len(usable)))]
        for option in options:
            option.setCheckable(True)
            dialog.body.addWidget(option)
        dialog.body.addWidget(dialog.status)
        dialog.body.addWidget(button("下一题", lambda: advance()))

        def pixmap(photo_text):
            image = QPixmap()
            image.loadFromData(base64.b64decode(photo_text))
            return image.scaled(140, 140, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)

        choices = []

        def state(option, value):
            option.setProperty("answerState", value)
            option.style().unpolish(option)
            option.style().polish(option)
            option.update()

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
                option.setChecked(False)
                option.setEnabled(True)
                state(option, "")
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
            for i, option in enumerate(options):
                option.setChecked(i == index)
                option.setEnabled(False)
                state(option, "correct" if i == target[0] else "wrong" if i == index else "")
            answer = choices[target[0]][0]
            dialog.status.setText("回答正确。" if correct else f"正确答案是 {answer['name']}（{sid}）。")

        advance()
        dialog.exec()
        dialog.deleteLater()

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
        vault = self.vault
        accepted = dialog.exec()
        dialog.deleteLater()
        if accepted and result and self.vault is vault and vault.unlocked:
            finished(result[0])
