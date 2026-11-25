"""成长档案内的学生资料和谈话时间线。"""

import base64
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from core.database import GradeHistoryDB, StudentStore
from core.photo_matching import index_photo, interview_vector, revoke_consent, search_interviews, set_consent
from core.sensitive_vault import CONTACT_FIELDS, CONTACT_LABELS, mask_phone
from core.student_import import PHONE, encode_photo
from ui.workbench_widgets import TaskDialog, button, fields, label, unlock


class StudentDetails(QWidget):
    back = Signal()
    model_center = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.vault = None
        self.sid = ""
        self._showing = False
        self._shown_field = ""
        self._poll = QTimer(self, interval=750)
        self._poll.timeout.connect(self.check_lock)
        self._poll.start()
        self._hide_phone = QTimer(self, singleShot=True, interval=30000)
        self._hide_phone.timeout.connect(self.hide_phone)

        box = QVBoxLayout(self)
        box.setContentsMargins(22, 16, 22, 20)
        heading = QHBoxLayout()
        heading.addWidget(button("返回", self.back.emit))
        self.heading = label("", "H1")
        heading.addWidget(self.heading, 1)
        self.access = button("解锁资料", self._toggle_lock)
        heading.addWidget(self.access)
        heading.addWidget(button("AI 辅导", self._ai, "Primary"))
        box.addLayout(heading)
        self.fact = label("", "Muted")
        box.addWidget(self.fact)
        self.notice = label("照片、联系方式、谈话记录和人工总结已锁定。", "Muted")
        box.addWidget(self.notice)

        self.contents = QSplitter(Qt.Orientation.Horizontal, self)
        box.addWidget(self.contents, 1)
        left = QWidget()
        left.setMinimumWidth(326)
        left_box = QVBoxLayout(left)
        self.photo = label()
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.photo.setFixedHeight(150)
        left_box.addWidget(self.photo)
        photos = QHBoxLayout()
        photos.addWidget(button("更换照片", self._change_photo))
        photos.addWidget(button("放大照片", self._show_photo))
        left_box.addLayout(photos)
        contacts = QFormLayout()
        self.hints = {}
        for field, name in zip(CONTACT_FIELDS, CONTACT_LABELS):
            row = QHBoxLayout()
            hint = label("")
            hint.setMinimumWidth(106)
            hint.setWordWrap(True)
            self.hints[field] = hint
            row.addWidget(hint, 1)
            reveal = button("查看", lambda _=False, f=field: self._reveal(f))
            copy = button("复制", lambda _=False, f=field: self._copy(f))
            for action in (reveal, copy):
                action.setFixedWidth(54)
                row.addWidget(action)
            contacts.addRow(name, row)
        left_box.addLayout(contacts)
        left_box.addWidget(button("编辑联系方式", self._edit_contacts))
        left_box.addStretch(1)
        self.contents.addWidget(left)

        right = QWidget()
        right_box = QVBoxLayout(right)
        summary_row = QHBoxLayout()
        summary_row.addWidget(label("人工总结", "H2"), 1)
        summary_row.addWidget(button("保存总结", self._save_summary))
        right_box.addLayout(summary_row)
        self.summary = QPlainTextEdit()
        self.summary.setPlaceholderText("记录辅导员的总结与后续安排")
        self.summary.setMaximumHeight(135)
        right_box.addWidget(self.summary)
        interview_row = QHBoxLayout()
        interview_row.addWidget(label("谈话记录", "H2"), 1)
        interview_row.addWidget(button("新增", self.new_interview, "Primary"))
        interview_row.addWidget(button("编辑", self._edit_interview))
        interview_row.addWidget(button("删除", self._remove_interview))
        right_box.addLayout(interview_row)
        search = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("检索该学生的谈话")
        self.query.returnPressed.connect(self._search)
        search.addWidget(self.query, 1)
        search.addWidget(button("检索", self._search))
        search.addWidget(button("全部", self.refresh))
        right_box.addLayout(search)
        self.interviews = QListWidget()
        right_box.addWidget(self.interviews, 1)
        self.interviews.itemActivated.connect(lambda _: self._edit_interview())
        face_row = QHBoxLayout()
        face_row.addWidget(button("记录照片匹配授权", self._record_consent))
        face_row.addWidget(button("撤回授权", self._revoke))
        face_row.addWidget(button("生成照片特征", self._index_face))
        right_box.addLayout(face_row)
        self.face_status = label("", "Muted")
        self.face_status.setWordWrap(True)
        right_box.addWidget(self.face_status)
        self.contents.addWidget(right)
        self.contents.setStretchFactor(0, 2)
        self.contents.setStretchFactor(1, 3)
        self.contents.hide()

    def open_student(self, vault, sid: str) -> bool:
        self.forget()
        self.vault = vault
        row = StudentStore(store=self.store, username=vault.username).get(sid)
        if row is None:
            return False
        sid = sid.removeprefix("sid:")
        self.sid = sid
        self.heading.setText(row["name"])
        grade_rows = GradeHistoryDB(store=self.store, username=vault.username).rows()
        terms = {entry["semester"] for entry in grade_rows if entry.get("student_id") in (sid, f"sid:{sid}")}
        self.fact.setText(f"{sid}　|　{row.get('class_name') or '未分班'}　|　已记录 {len(terms)} 个学期")
        self.refresh()
        return True

    def forget(self):
        self.sid = ""
        self._clear_sensitive()
        self.heading.clear()
        self.fact.clear()

    def _clear_sensitive(self):
        self._hide_phone.stop()
        self._shown_field = ""
        self._showing = False
        self.contents.hide()
        self.notice.setText("照片、联系方式、谈话记录和人工总结已锁定。")
        self.notice.show()
        self.access.setText("解锁资料")
        self.photo.clear()
        self.summary.clear()
        self.interviews.clear()
        self.query.clear()
        self.face_status.clear()
        for hint in self.hints.values():
            hint.clear()

    def _toggle_lock(self):
        if self.vault and self.vault.unlocked:
            self.vault.lock()
            self._clear_sensitive()
        elif unlock(self, self.vault):
            self.refresh()

    def check_lock(self):
        if self._showing and (self.vault is None or not self.vault.unlocked):
            self._clear_sensitive()

    def refresh(self):
        if not self.sid or not self.vault or not self.vault.unlocked:
            self._clear_sensitive()
            return
        try:
            data = self.vault.student(self.sid)
        except Exception as error:
            self._clear_sensitive()
            self.notice.setText(str(error))
            return
        self.notice.hide()
        self.contents.show()
        self._showing = True
        self.access.setText("锁定资料")
        self._shown_field = ""
        self._paint_photo(data.get("photo", ""))
        for field, widget in self.hints.items():
            widget.setText(data.get("contacts", {}).get(field, {}).get("hint", "未登记"))
        self.summary.setPlainText(data.get("summary", ""))
        self._render_interviews(data.get("interviews", []))
        consent = data.get("face_consent", {})
        status = "已提取特征" if data.get("face_vector") else "尚无特征"
        self.face_status.setText(f"授权有效期至 {consent['expires']}　{status}" if consent else "此学生未授权照片匹配")

    def _pixmap(self, photo):
        image = QPixmap()
        if photo:
            image.loadFromData(base64.b64decode(photo), "JPEG")
        return image

    def _paint_photo(self, photo):
        image = self._pixmap(photo)
        if image.isNull():
            self.photo.setText("缺照片")
        else:
            self.photo.setPixmap(image.scaled(170, 150, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))

    def _show_photo(self):
        if not unlock(self, self.vault):
            return
        photo = self._pixmap(self.vault.student(self.sid).get("photo"))
        if not photo.isNull():
            dialog = TaskDialog(self, "学生照片")
            widget = label()
            widget.setPixmap(photo.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
            dialog.body.addWidget(widget)
            timer = QTimer(dialog, interval=500)
            timer.timeout.connect(lambda: dialog.reject() if not self.vault.unlocked else None)
            timer.start()
            dialog.exec()

    def _change_photo(self):
        if not unlock(self, self.vault):
            return
        filename, _ = QFileDialog.getOpenFileName(self, "选择学生照片", filter="图片 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not filename:
            return

        def execute():
            photo = encode_photo(Path(filename))
            def change(record):
                record["photo"] = photo
                record.pop("face_vector", None)
            self.vault.update(self.sid, change)
        self._task("更换照片", execute)

    def _ai(self):
        if not self.sid or not self.vault:
            return
        from ui.grounded_chat_dialog import GroundedChatDialog

        GroundedChatDialog(self, self.vault, self.sid, open_models=self.model_center.emit).exec()

    def _reveal(self, field):
        self.hide_phone()
        number = self.vault.phone(self.sid, field)
        self._shown_field = field
        self.hints[field].setText(number or "未登记")
        self._hide_phone.start()

    def hide_phone(self):
        if self._shown_field and self.vault and self.vault.unlocked and self.sid:
            field = self._shown_field
            data = self.vault.student(self.sid)
            self.hints[field].setText(data.get("contacts", {}).get(field, {}).get("hint", "未登记"))
        self._shown_field = ""

    def _copy(self, field):
        value = self.vault.phone(self.sid, field, action="phone_copy")
        if value:
            QApplication.clipboard().setText(value)

    def _edit_contacts(self):
        current = self.vault.student(self.sid).get("contacts", {})
        form = [(f, f"{name}（{current.get(f, {}).get('hint', '未登记')}）", "")
                for f, name in zip(CONTACT_FIELDS, CONTACT_LABELS)]
        values = fields(self, "填写新电话，留空保留，输入「清除」移除", form)
        if not values:
            return
        if any(value not in ("清除", "") and not PHONE.fullmatch(value) for value in values.values()):
            QMessageBox.warning(self, "电话格式", "请检查电话号码。")
            return

        def change(data):
            contacts = data.setdefault("contacts", {})
            for field, value in values.items():
                if value == "清除":
                    contacts.pop(field, None)
                elif value:
                    contacts[field] = {"hint": mask_phone(value), "cipher": self.vault.seal_phone(self.sid, field, value)}
        self._task("保存联系方式", lambda: self.vault.update(self.sid, change))

    def _save_summary(self):
        summary = self.summary.toPlainText().strip()
        if len(summary) > 20000:
            QMessageBox.warning(self, "总结过长", "人工总结最多 20000 字符。")
            return
        def save():
            self.vault.update(self.sid, lambda record: record.update(summary=summary))
            self.vault.audit("summary_update", self.sid)
        self._task("保存总结", save)

    def _render_interviews(self, records):
        self.interviews.clear()
        for entry in sorted(records, key=lambda row: row["date"], reverse=True):
            self.interviews.addItem(f"{entry['date']}　{entry['method']}　{entry['content']}")
            self.interviews.item(self.interviews.count() - 1).setData(Qt.ItemDataRole.UserRole, entry["id"])

    def new_interview(self):
        self._edit_record({})

    def _edit_interview(self):
        item = self.interviews.currentItem()
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        for entry in self.vault.student(self.sid).get("interviews", []):
            if entry["id"] == key:
                self._edit_record(entry)
                return

    def _edit_record(self, entry):
        if not unlock(self, self.vault):
            return
        dialog = TaskDialog(self, "谈话记录")
        guard = QTimer(dialog, interval=500)
        guard.timeout.connect(lambda: dialog.reject() if not self.vault.unlocked else None)
        guard.start()
        dialog.resize(570, 420)
        form = QFormLayout()
        date_edit = QLineEdit(entry.get("date", datetime.now().isoformat(timespec="minutes", sep=" ")))
        method = QLineEdit(entry.get("method", "面谈"))
        method.setMaxLength(40)
        form.addRow("时间", date_edit)
        form.addRow("方式", method)
        dialog.body.addLayout(form)
        content = QPlainTextEdit(entry.get("content", ""))
        dialog.body.addWidget(content, 1)
        dialog.body.addWidget(dialog.status)
        key = entry.get("id") or uuid4().hex

        def save():
            try:
                date_value = datetime.fromisoformat(date_edit.text()).isoformat(timespec="minutes", sep=" ")
                summary = content.toPlainText().strip()
                method_text = method.text().strip()
                if not summary or not method_text or len(summary) > 20000:
                    raise ValueError
            except ValueError:
                dialog.status.setText("时间格式为 YYYY-MM-DD HH:mm。请填写方式和谈话内容（最多 20000 字符）。")
                return

            def write():
                vector = interview_vector(self.store.root, summary)
                record = {"id": key, "date": date_value, "method": method_text,
                          "content": summary, "vector": vector}
                def change(data):
                    records = [r for r in data.get("interviews", []) if r["id"] != key]
                    data["interviews"] = [*records, record]
                self.vault.update(self.sid, change)
                self.vault.audit("interview_update" if entry else "interview_create", self.sid)

            dialog.execute(write, lambda _: dialog.accept(), "正在保存谈话…")

        dialog.body.addWidget(button("保存记录", save, "Primary"))
        if dialog.exec():
            self.refresh()

    def _remove_interview(self):
        item = self.interviews.currentItem()
        if item is None or QMessageBox.question(self, "删除谈话记录", "删除所选记录及其检索特征？") != QMessageBox.StandardButton.Yes:
            return
        key = item.data(Qt.ItemDataRole.UserRole)

        def remove():
            def change(data):
                data["interviews"] = [entry for entry in data.get("interviews", []) if entry["id"] != key]
            self.vault.update(self.sid, change)
            self.vault.audit("interview_delete", self.sid)

        self._task("删除谈话记录", remove)

    def _search(self):
        query = self.query.text().strip()
        if not query:
            self.refresh()
            return
        self._task("检索谈话", lambda: search_interviews(self.vault, self.sid, query), self._render_interviews)

    def _record_consent(self):
        data = fields(self, "记录学生单独授权", [
            ("ref", "授权记录编号", ""), ("until", "有效截止日期 YYYY-MM-DD", ""),
        ])
        if data:
            self._task("记录授权", lambda: set_consent(self.vault, self.sid, data["ref"], data["until"]))

    def _revoke(self):
        if QMessageBox.question(self, "撤回授权", "撤回后删除此学生的照片特征和本机托管副本中的对应特征。") == QMessageBox.StandardButton.Yes:
            self._task("撤回授权", lambda: revoke_consent(self.vault, self.sid))

    def _index_face(self):
        self._task("生成照片特征", lambda: index_photo(self.vault, self.sid))

    def _task(self, title, action, done=None):
        dialog = TaskDialog(self, title)
        dialog.setMinimumWidth(440)
        dialog.body.addWidget(dialog.status)
        dialog.body.addWidget(button("关闭", dialog.reject))
        result = []

        def finished(value):
            result.append(value)
            dialog.accept()

        dialog.execute(action, finished, title + "…")
        if dialog.exec():
            if done is not None:
                done(result[0])
            else:
                self.refresh()
