"""逐张确认未能按文件名唯一匹配的照片。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QListWidget, QVBoxLayout

from ui.workbench_widgets import button, label


class PhotoReviewDialog(QDialog):
    def __init__(self, parent, plan):
        super().__init__(parent)
        self.setWindowTitle("核对照片归属")
        self.resize(840, 510)
        self.conflicts = plan.photo_conflicts
        self.roster = {item.sid: item for item in plan.photo_students}
        self.used_by_plan = {item.sid for item in plan.students if item.photo is not None}
        self.decisions = {}
        body = QVBoxLayout(self)
        body.addWidget(label("核对照片归属", "H2"))
        body.addWidget(label("选择左侧照片，再为它指定一名学生。未指定的照片会跳过。", "Muted"))
        columns = QHBoxLayout()
        self.files = QListWidget()
        self.files.setMinimumWidth(205)
        self.files.setMaximumWidth(300)
        self.files.setWordWrap(True)
        for item in self.conflicts:
            self.files.addItem(item.path.name)
        self.files.currentRowChanged.connect(self._show_file)
        columns.addWidget(self.files, 2)
        middle = QVBoxLayout()
        self.photo = label("暂无预览")
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setMinimumSize(170, 190)
        self.photo.setMaximumHeight(230)
        middle.addWidget(self.photo)
        self.reason = label("", "Muted")
        self.reason.setWordWrap(True)
        middle.addWidget(self.reason)
        middle.addStretch(1)
        columns.addLayout(middle, 2)
        choices = QVBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索学号、姓名或班级")
        self.query.textChanged.connect(self._show_students)
        choices.addWidget(self.query)
        self.students = QListWidget()
        choices.addWidget(self.students, 1)
        self.students.itemDoubleClicked.connect(lambda _: self._choose())
        controls = QHBoxLayout()
        controls.addWidget(button("指定给所选学生", self._choose, "Primary"))
        controls.addWidget(button("取消指定", self._unassign))
        choices.addLayout(controls)
        columns.addLayout(choices, 4)
        body.addLayout(columns, 1)
        footer = QHBoxLayout()
        self.status = label("")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        footer.addWidget(button("跳过全部未匹配照片", self.reject))
        footer.addWidget(button("完成核对", self.accept, "Primary"))
        body.addLayout(footer)
        self.files.setCurrentRow(0)

    def _current(self):
        row = self.files.currentRow()
        if 0 <= row < len(self.conflicts):
            return self.conflicts[row]
        return None

    def _show_file(self):
        item = self._current()
        if item is None:
            return
        reader = QImageReader(str(item.path))
        reader.setAutoTransform(True)
        size = reader.size()
        image = QPixmap()
        if size.isValid() and size.width() * size.height() <= 40_000_000:
            reader.setScaledSize(size.scaled(190, 215, Qt.AspectRatioMode.KeepAspectRatio))
            image = QPixmap.fromImage(reader.read())
        self.photo.clear()
        if image.isNull():
            self.photo.setText("无法预览")
        else:
            self.photo.setPixmap(image)
        self.reason.setText(item.reason)
        self.query.clear()
        self._show_students()

    def _show_students(self):
        item = self._current()
        if item is None:
            return
        used = self.used_by_plan | {sid for path, sid in self.decisions.items() if path != item.path}
        term = self.query.text().casefold().strip()
        choices = sorted(self.roster.values(), key=lambda row: (row.sid not in item.candidates, row.sid))
        self.students.clear()
        for row in choices:
            title = f"{row.sid}　{row.name}　{row.class_name}"
            if row.sid in used or term not in title.casefold():
                continue
            self.students.addItem(title + ("　候选" if row.sid in item.candidates else ""))
            option = self.students.item(self.students.count() - 1)
            option.setData(Qt.ItemDataRole.UserRole, row.sid)
            if self.decisions.get(item.path) == row.sid:
                self.students.setCurrentItem(option)

    def _choose(self):
        file = self._current()
        selected = self.students.currentItem()
        if file is None or selected is None:
            self.status.setText("请选择一名学生。")
            return
        sid = selected.data(Qt.ItemDataRole.UserRole)
        self.decisions[file.path] = sid
        self.files.currentItem().setText(f"{file.path.name}　✓ {sid}")
        self.status.setText(f"已指定 {len(self.decisions)} 张。")

    def _unassign(self):
        file = self._current()
        if file is not None:
            self.decisions.pop(file.path, None)
            self.files.currentItem().setText(file.path.name)
            self._show_students()
            self.status.setText(f"已指定 {len(self.decisions)} 张。")
