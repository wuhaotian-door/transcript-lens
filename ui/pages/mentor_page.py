"""导师自核、错题重练和本地照片候选。"""

import base64
from binascii import Error as Base64Error
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QHeaderView, QHBoxLayout, QListWidget,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.database import StudentStore
from core.mentor_practice import (
    MODE_LABELS, MODE_NAMES, MODE_PHOTOS, MODE_REVIEW, new_round, regular_targets, review_targets,
)
from core.photo_matching import FaceEngine, configure_matching, index_photo, match_photo, valid_consent
from core.workbench_models import installed
from ui.mentor_practice_dialog import PracticeDialog, student_caption
from ui.widgets.profile_reading import profile_scroll
from ui.workbench_widgets import TaskDialog, button, fields, label, review_table, unlock


# 图标使用 24 × 24 的局部坐标，在高 DPI 屏幕上保留清晰描边。
_TILE_ICONS = {
    "photo": (((2, 4), (22, 4), (22, 20), (2, 20), (2, 4)),
              ((3, 18), (9, 11), (13, 16), (17, 12), (22, 17)), ((15, 8), (17, 8))),
    "person": (((7, 10), (7, 5), (10, 2), (14, 2), (17, 5), (17, 10), (14, 13), (10, 13), (7, 10)),
               ((3, 22), (3, 19), (7, 16), (17, 16), (21, 19), (21, 22), (3, 22))),
    "review": (((20, 7), (16, 3), (8, 3), (4, 7), (4, 14), (8, 19), (16, 19), (20, 15)),
               ((20, 2), (20, 7), (15, 7)), ((9, 10), (12, 13), (16, 9))),
    "stats": (((3, 2), (3, 21), (22, 21)), ((8, 17), (8, 12)),
              ((13, 17), (13, 8)), ((18, 17), (18, 5))),
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
            ("photo", "看照片选名字", "看清照片后选择姓名 · 每轮 20 题", lambda: self._quiz(MODE_NAMES)),
            ("person", "看名字选照片", "从大图中选择对应照片 · 每轮 20 题", lambda: self._quiz(MODE_PHOTOS)),
            ("review", "错题练习", "重练当前班级认错的学生", lambda: self._quiz(MODE_REVIEW)),
            ("stats", "练习统计", "按轮查看得分与逐题结果", self._history),
        ))
        self._add_section(box, "照片核对", "verification", (
            ("scan", "照片找人", "从已授权的照片中查找候选", self._match),
            ("id", "按学号查找", "定位学生并打开资料", self._manual),
            ("index", "批量提取照片特征", "处理具备有效授权的照片", self._index),
            ("shield", "配置照片匹配", "设置学校授权和候选阈值", self._configure),
        ))
        models = button("前往模型中心", self.model_center.emit)
        box.addWidget(models, alignment=Qt.AlignmentFlag.AlignRight)
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

    def _watch_vault(self, dialog):
        vault = self.vault
        timer = QTimer(dialog, interval=500)
        timer.timeout.connect(lambda: dialog.reject() if self.vault is not vault or not vault.unlocked else None)
        timer.start()

    def _private_dialog(self, title, parent=None):
        dialog = TaskDialog(parent or self, title)
        dialog.resize(650, 390)
        self._watch_vault(dialog)
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

    @staticmethod
    def _practice_table(headers, rows):
        table = QTableWidget(len(rows), len(headers))
        table.setObjectName("ReviewTable")
        table.setHorizontalHeaderLabels(headers)
        review_table(table)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setToolTip(item.text())
                table.setItem(i, j, item)
        return table

    def _history(self):
        scope = self.classes.currentText()
        vault = self.vault

        def collect():
            rounds = vault.practice_rounds()
            return [round_ for round_ in rounds if scope == "全部班级" or round_["scope"] == scope]

        def done(rounds):
            dialog = self._private_dialog("练习统计")
            dialog.resize(810, 472)
            dialog.body.addWidget(label(f"{scope} · {len(rounds)} 轮练习", "H2"))
            controls = QHBoxLayout()
            controls.addStretch()
            controls.addWidget(button("关闭", dialog.reject))
            if rounds:
                rows = []
                for round_ in rounds:
                    answered = len(round_["answers"])
                    correct = sum(answer["correct"] for answer in round_["answers"])
                    rows.append((round_["started_at"][:19], MODE_LABELS[round_["kind"]], round_["scope"],
                                 f"{answered} / {round_['total']}", correct, answered - correct,
                                 "已完成" if answered == round_["total"] else "未完成"))
                table = self._practice_table(
                    ["开始时间", "练习类型", "班级", "已答 / 题量", "答对", "答错", "状态"], rows
                )
                header = table.horizontalHeader()
                for column in (0, 1, 3, 4, 5, 6):
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
                dialog.body.addWidget(table, 1)

                def details():
                    index = table.currentRow()
                    if index >= 0 and vault.unlocked:
                        self._practice_details(rounds[index], dialog)

                table.itemActivated.connect(details)
                controls.addWidget(button("查看逐题结果", details, "Primary"))
                table.selectRow(0)
            else:
                dialog.body.addWidget(label("这里会显示新版中已答过题的练习轮次。", "Muted"))
                dialog.body.addStretch()
            dialog.body.addLayout(controls)
            dialog.exec()
            dialog.deleteLater()

        self._task("读取练习统计", collect, done)

    def _practice_details(self, round_, parent=None):
        vault = self.vault
        answers = round_["answers"]
        needed = {answer[key] for answer in answers for key in ("target", "selected")}

        def collect():
            names = {}
            rows = StudentStore(store=vault.store, username=vault.username).rows()
            for row in rows:
                ref = vault.student(row["student_id"]).get("practice_ref")
                if ref in needed:
                    names[ref] = student_caption(row)
                    if len(names) == len(needed):
                        break
            return names

        def done(names):
            dialog = self._private_dialog("本轮逐题结果", parent)
            dialog.resize(820, 490)
            right = sum(answer["correct"] for answer in answers)
            dialog.body.addWidget(label(
                f"{MODE_LABELS[round_['kind']]} · {round_['scope']} · {round_['started_at'][:19]}", "H2"
            ))
            dialog.body.addWidget(label(
                f"已答 {len(answers)} / {round_['total']} 题 · 答对 {right} 题 · 答错 {len(answers) - right} 题",
                "Muted",
            ))
            missing = "学生资料已移除"
            table = self._practice_table(["题号", "题型", "你的答案", "正确答案", "结果"], [
                (index, MODE_LABELS[answer["mode"]], names.get(answer["selected"], missing),
                 names.get(answer["target"], missing), "正确" if answer["correct"] else "错误")
                for index, answer in enumerate(answers, 1)
            ])
            for column in (0, 1, 4):
                table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
            dialog.body.addWidget(table, 1)
            dialog.body.addWidget(button("关闭", dialog.reject), alignment=Qt.AlignmentFlag.AlignRight)
            dialog.exec()
            dialog.deleteLater()

        self._task("读取逐题结果", collect, done, parent=parent)

    def _quiz(self, kind):
        rows = self._rows()
        vault = self.vault
        scope = self.classes.currentText()

        def collect():
            students, ids_by_ref = {}, {}
            for row in rows:
                sid = row["student_id"].removeprefix("sid:")
                data = vault.student(sid)
                if not data.get("photo"):
                    continue
                try:
                    photo = base64.b64decode(data["photo"], validate=True)
                except (Base64Error, TypeError, ValueError):
                    continue
                if QImage.fromData(photo).isNull():
                    continue
                students[sid] = (row, photo)
                if ref := data.get("practice_ref"):
                    ids_by_ref[ref] = sid
            targets = (review_targets(vault.practice_rounds(), ids_by_ref) if kind == MODE_REVIEW
                       else regular_targets(list(students), kind))
            return students, targets

        def start(result):
            students, targets = result
            if len(students) < 2:
                self.status.setText("当前班级至少需要两名有可用照片的学生。")
                return
            if not targets:
                self.status.setText("当前班级没有待重练的错题。")
                return
            dialog = PracticeDialog(self, vault, new_round(scope, kind, len(targets)), targets, students)
            self._watch_vault(dialog)
            finished = dialog.exec()
            round_ = dialog.round
            dialog.deleteLater()
            if finished and self.vault is vault and vault.unlocked:
                self._practice_details(round_)

        self._task("读取练习照片", collect, start)

    def _task(self, title, work, finished, *, parent=None):
        dialog = TaskDialog(parent or self, title)
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
