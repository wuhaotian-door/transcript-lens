"""导师自核的逐题练习窗口。"""

import random

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QProgressBar

from core.mentor_practice import MODE_LABELS, MODE_PHOTOS
from ui.workbench_widgets import TaskDialog, button, label


def student_caption(row):
    return f"{row['name']}（{row['student_id'].removeprefix('sid:')}）"


class PracticeDialog(TaskDialog):
    def __init__(self, parent, vault, round_, targets, students):
        super().__init__(parent, MODE_LABELS[round_["kind"]])
        self.vault = vault
        self.round = round_
        self.targets = targets
        self.students = students
        self.number = 0
        self.choices = []

        screen = parent.screen().availableGeometry()
        self._card_height = max(112, min(224, (screen.height() - 230) // 2))
        self._photo_height = max(160, min(326, screen.height() - 310))
        self.resize(min(800, screen.width() - 40), min(720, screen.height() - 45))

        top = QHBoxLayout()
        self.question = label("", "H2")
        self.question.setWordWrap(True)
        self.counter = label("", "MentorQuizCounter")
        top.addWidget(self.question, 1)
        top.addWidget(self.counter, alignment=Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(top)
        self.progress = QProgressBar()
        self.progress.setObjectName("MentorQuizProgress")
        self.progress.setAccessibleName("本轮已答题数")
        self.progress.setRange(0, len(targets))
        self.progress.setTextVisible(False)
        self.body.addWidget(self.progress)

        self.photo = label("", "MentorQuizPhoto")
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setFixedHeight(self._photo_height)
        self.body.addWidget(self.photo, alignment=Qt.AlignmentFlag.AlignHCenter)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.options = []
        for index in range(min(4, len(students))):
            option = button("", lambda _=False, i=index: self._select(i), "QuizChoice")
            option.setCheckable(True)
            option.setAutoDefault(False)
            grid.addWidget(option, index // 2, index % 2)
            self.options.append(option)
            key = QShortcut(QKeySequence(str(index + 1)), self)
            key.activated.connect(lambda i=index: self._select(i))
        self.body.addLayout(grid)
        self.body.addStretch()
        self.status.setMinimumHeight(34)
        self.body.addWidget(self.status)

        footer = QHBoxLayout()
        self.score = label("", "Muted")
        footer.addWidget(self.score, 1)
        footer.addWidget(button("关闭练习", self.reject))
        self.next = button("下一题", self._advance, "Primary")
        footer.addWidget(self.next)
        self.body.addLayout(footer)

        self._advance()

    def _pixmap(self, photo, width, height):
        ratio = self.devicePixelRatioF()
        image = QImage.fromData(photo)
        pixmap = QPixmap.fromImage(image.scaled(
            round(width * ratio), round(height * ratio),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
        ))
        pixmap.setDevicePixelRatio(ratio)
        return pixmap

    @staticmethod
    def _state(option, value):
        option.setProperty("answerState", value)
        option.style().unpolish(option)
        option.style().polish(option)
        option.update()

    def _refresh_score(self):
        answers = self.round["answers"]
        self.progress.setValue(len(answers))
        right = sum(answer["correct"] for answer in answers)
        self.score.setText(f"已答 {len(answers)} 题 · 答对 {right} 题")

    def _advance(self):
        if self.number == len(self.targets):
            self.accept()
            return
        self.number += 1
        self.target, self.mode = self.targets[self.number - 1]
        row, photo = self.students[self.target]
        show_choices_as_photos = self.mode == MODE_PHOTOS
        distractors = [sid for sid in self.students if sid != self.target]
        self.choices = [self.target, *random.sample(distractors, len(self.options) - 1)]
        random.shuffle(self.choices)
        self.question.setText(
            f"请选择 {student_caption(row)} 的照片" if show_choices_as_photos else "照片中的学生是谁？"
        )
        self.counter.setText(f"第 {self.number} / {len(self.targets)} 题")
        self.photo.setVisible(not show_choices_as_photos)
        if show_choices_as_photos:
            self.photo.clear()
        else:
            self.photo.setPixmap(self._pixmap(photo, 420, self._photo_height - 12))

        for index, (option, sid) in enumerate(zip(self.options, self.choices)):
            candidate, candidate_photo = self.students[sid]
            option.setChecked(False)
            option.setEnabled(True)
            self._state(option, "")
            option.setFixedHeight(self._card_height if show_choices_as_photos else 55)
            if show_choices_as_photos:
                pixmap = self._pixmap(candidate_photo, 286, self._card_height - 12)
                icon = QIcon(pixmap)
                icon.addPixmap(pixmap, QIcon.Mode.Disabled)
                option.setIcon(icon)
                option.setIconSize(QSize(self.width() // 2 - 50, self._card_height - 10))
                option.setText("")
                option.setAccessibleName(f"照片选项 {index + 1}")
            else:
                option.setIcon(QIcon())
                option.setText(student_caption(candidate))
                option.setAccessibleName(option.text())
        self._refresh_score()
        self.status.clear()
        self.next.setText("查看本轮结果" if self.number == len(self.targets) else "下一题")
        self.next.setEnabled(False)

    def _select(self, index):
        if self._busy or not self.options[index].isEnabled() or not self.vault.unlocked:
            return
        number, target, selected, mode = self.number, self.target, self.choices[index], self.mode

        def work():
            return self.vault.record_practice_answer(self.round, number, mode, target, selected)

        def done(saved):
            if not self.vault.unlocked:
                self.reject()
                return
            self.round = saved
            for option, sid in zip(self.options, self.choices):
                option.setEnabled(False)
                option.setChecked(sid == selected)
                self._state(option, "correct" if sid == target else "wrong" if sid == selected else "")
            answer = student_caption(self.students[target][0])
            self.status.setText(f"回答正确 · {answer}" if saved["answers"][-1]["correct"]
                                else f"回答错误 · 正确答案是 {answer}")
            self._refresh_score()
            self.next.setEnabled(True)
            self.next.setFocus()

        # QPushButton 会先切换选中状态，存盘失败时还原题目，允许重选。
        def failed(_error):
            for option in self.options:
                option.setChecked(False)

        self.execute(work, done, "正在保存本题…", on_error=failed)
