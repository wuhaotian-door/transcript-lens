"""学校规章的导入、检索、资料编辑与出处预览。"""

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from core.knowledge import (
    FIELD_LENGTHS, FIELD_TITLES, DocumentValidationError, DuplicateDocumentError,
    KnowledgeLibrary, policy_context, validate_metadata, validate_scope,
)
from core.workbench_models import installed
from ui.workbench_widgets import TaskDialog, button, fields, label


class _OptionalDate(QWidget):
    def __init__(self, value: str, empty: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        self.mode.addItems((empty, "指定日期"))
        self.calendar = QDateEdit(QDate.currentDate())
        self.calendar.setCalendarPopup(True)
        self.calendar.setDisplayFormat("yyyy-MM-dd")
        self.calendar.lineEdit().setReadOnly(True)
        initial = QDate.fromString(value, "yyyy-MM-dd")
        if initial.isValid():
            self.calendar.setDate(initial)
            self.mode.setCurrentIndex(1)
        self.mode.currentIndexChanged.connect(lambda index: self.calendar.setVisible(bool(index)))
        self.calendar.setVisible(bool(self.mode.currentIndex()))
        row.addWidget(self.mode)
        row.addWidget(self.calendar, 1)
        row.addStretch(1)

    def value(self):
        return self.calendar.date().toString("yyyy-MM-dd") if self.mode.currentIndex() else ""


class _DocumentForm(QDialog):
    def __init__(self, parent, title, initial, *, scope_only=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.values = None
        self.controls = {}
        self._scope_only = scope_only
        layout = QVBoxLayout(self)
        layout.setContentsMargins(19, 17, 19, 17)
        form = QFormLayout()
        keys = ("school", "year") if scope_only else tuple(FIELD_TITLES)
        for key in keys:
            current = str(initial.get(key) or "")
            if key == "year":
                control = QComboBox()
                control.addItem("请选择学年", "")
                control.addItem("通用", "通用")
                year = date.today().year
                for start in range(year + 6, year - 91, -1):
                    choice = f"{start}-{start + 1}"
                    control.addItem(choice, choice)
                if current and control.findData(current) < 0:
                    control.addItem(current, current)
                control.setCurrentIndex(max(0, control.findData(current)))
            else:
                control = QLineEdit(current)
                control.setMaxLength(FIELD_LENGTHS[key])
                if key == "school":
                    control.setPlaceholderText("填写学校全名，或填写通用")
            control.setAccessibleName(FIELD_TITLES[key])
            self.controls[key] = control
            form.addRow(FIELD_TITLES[key] + ("（选填）" if key == "version" else " *"), control)
        if not scope_only:
            self.controls["start"] = _OptionalDate(str(initial.get("start") or ""), "未指定")
            self.controls["end"] = _OptionalDate(str(initial.get("end") or ""), "至今")
            form.addRow("生效日期", self.controls["start"])
            form.addRow("截止日期", self.controls["end"])
        layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存", self.accept, "Primary"))
        layout.addLayout(actions)

    def accept(self):
        data = {}
        for key, control in self.controls.items():
            if isinstance(control, QComboBox):
                data[key] = control.currentData()
            elif isinstance(control, QLineEdit):
                data[key] = control.text()
            else:
                data[key] = control.value()
        try:
            self.values = validate_scope(**data) if self._scope_only else validate_metadata(data)
        except DocumentValidationError as error:
            QMessageBox.warning(self, "检查资料信息", str(error))
            widget = self.controls[error.field]
            (widget.mode if isinstance(widget, _OptionalDate) else widget).setFocus()
            return
        super().accept()


class KnowledgeDialog(TaskDialog):
    def __init__(self, parent, store, username):
        super().__init__(parent, "学校规章")
        self.resize(920, 620)
        self.setMinimumSize(750, 500)
        self._scope_data = {}
        self.library = KnowledgeLibrary(store, username)
        self.body.addWidget(label("学校规章", "H2"))
        self.scope_label = label("", "Muted")
        self.scope_label.setWordWrap(True)
        self.body.addWidget(self.scope_label)
        row = QHBoxLayout()
        row.addWidget(button("导入资料", self._import, "Primary"))
        row.addWidget(button("设置检索范围", self._scope))
        row.addWidget(button("重建向量索引", self._rebuild))
        row.addStretch(1)
        self.body.addLayout(row)

        search = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索标题、学校、来源或正文")
        self.query.setClearButtonEnabled(True)
        self.query.setMaxLength(180)
        self.query.returnPressed.connect(self._search)
        search.addWidget(self.query, 1)
        search.addWidget(button("搜索资料", self._search))
        search.addWidget(button("全部资料", self._all))
        self.body.addLayout(search)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        self.docs = QListWidget()
        self.docs.setAlternatingRowColors(True)
        self.docs.setMouseTracking(True)
        self.docs.setMinimumWidth(270)
        self.docs.setWordWrap(True)
        self.docs.setObjectName("KnowledgeResults")
        split.addWidget(self.docs)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setObjectName("KnowledgePreview")
        self.view.setPlaceholderText("选择资料后可查看信息与正文段落。")
        split.addWidget(self.view)
        split.setStretchFactor(1, 1)
        self.body.addWidget(split, 1)

        actions = QHBoxLayout()
        self._read_btn = button("查看正文", self._excerpts)
        self._edit_btn = button("编辑资料", self._edit)
        self._delete_btn = button("删除资料", self._delete, "Danger")
        for control in (self._read_btn, self._edit_btn, self._delete_btn):
            actions.addWidget(control)
        actions.addStretch(1)
        actions.addWidget(button("试查有效条款", self._search_policy))
        actions.addWidget(button("关闭", self.reject))
        self.body.addLayout(actions)
        self.body.addWidget(self.status)
        self.docs.currentItemChanged.connect(self._selection)
        self.docs.itemActivated.connect(lambda _: self._excerpts())
        self._selection()
        self._load()

    def _controls(self):
        return self.query, self.docs

    def _load(self, *, select=None, notice=""):
        query = self.query.text().strip()

        def fetch():
            return self.library.list(query), self.library.scope(), installed(self.library.store.root, "semantic")

        def shown(result):
            docs, self._scope_data, semantic = result
            kind = "语义与关键词检索" if semantic else "关键词检索"
            self.scope_label.setText(f"{self._scope_data.get('school') or '未设学校'} · "
                                     f"{self._scope_data.get('year') or '未设学年'} · {kind}")
            self.docs.clear()
            selected = None
            for doc in reversed(docs):
                item = QListWidgetItem(f"{doc['title']}\n{doc['school']} · {doc['year']} · {doc['source']}", self.docs)
                item.setData(Qt.ItemDataRole.UserRole, doc)
                item.setToolTip(doc["title"])
                if doc["id"] == select:
                    selected = item
            if docs:
                self.docs.setCurrentItem(selected or self.docs.item(0))
            else:
                self._selection()
                self.view.setPlainText("未找到匹配的资料。" if query else "尚未导入学校规章。")
            count = f"找到 {len(docs)} 份资料" if query else f"共 {len(docs)} 份资料"
            self.status.setText(notice or count)

        self.execute(fetch, shown, "正在读取学校规章…", controls=self._controls())

    def _form(self, title, initial, *, scope_only=False):
        dialog = _DocumentForm(self, title, initial, scope_only=scope_only)
        values = dialog.values if dialog.exec() else None
        dialog.deleteLater()
        return values

    def _import(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择学校规章文件",
                                                  filter="制度文档 (*.pdf *.docx *.txt *.md)")
        if not filename:
            return
        path = Path(filename)
        self.execute(lambda: self.library.find_duplicate(path), lambda existing: self._prepare_import(path, existing),
                     "正在检查文件…", controls=self._controls())

    def _confirm_overwrite(self, title):
        return QMessageBox.question(
            self, "文件已导入", f"相同文件已保存为「{title}」。是否覆盖资料信息和索引？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _prepare_import(self, path, existing):
        if existing and not self._confirm_overwrite(existing["title"]):
            self.status.setText("已保留原资料。")
            return
        initial = existing or {**self._scope_data, "title": path.stem}
        metadata = self._form("覆盖学校规章" if existing else "导入学校规章", initial)
        if metadata is None:
            return

        def add(overwrite=None):
            def failed(error):
                if isinstance(error, DuplicateDocumentError):
                    if self._confirm_overwrite(error.title):
                        add(error.digest)
                    else:
                        self.status.setText("已保留原资料。")

            def done(digest):
                self.query.clear()
                self._load(select=digest, notice="已覆盖原资料。" if overwrite else "资料已导入。")

            self.execute(lambda: self.library.add(path, metadata, overwrite_digest=overwrite), done,
                         "正在提取和索引文档…", controls=self._controls(), on_error=failed)

        add(existing["id"] if existing else None)

    def _scope(self):
        values = self._form("当前检索范围", self._scope_data, scope_only=True)
        if values is not None:
            self.execute(lambda: self.library.set_scope(**values), lambda _: self._load(notice="检索范围已保存。"),
                         controls=self._controls())

    def _selected(self):
        item = self.docs.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _selection(self, *_):
        doc = self._selected()
        for control in (self._read_btn, self._edit_btn, self._delete_btn):
            control.setEnabled(doc is not None and not self._busy)
        if doc is None:
            self.view.clear()
            return
        version = f"\n版本　{doc['version']}" if doc.get("version") else ""
        self.view.setPlainText(
            f"{doc['title']}\n\n来源　{doc['source']}\n学校　{doc['school']}\n学年　{doc['year']}{version}\n"
            f"生效　{doc.get('start') or '未指定'}\n截止　{doc.get('end') or '至今'}\n"
            f"文件　{doc.get('filename') or '—'}\n索引　{doc['count']} 段"
        )

    def _excerpts(self):
        doc = self._selected()
        if doc:
            self.execute(lambda: self.library.excerpts(doc["id"]), lambda rows: self.view.setPlainText(
                "\n\n".join(f"{row['location']}\n{row['text']}" for row in rows)
            ), controls=self._controls())

    def _edit(self):
        doc = self._selected()
        if not doc:
            return
        metadata = self._form("编辑资料信息", doc)
        if metadata is not None:
            def done(_):
                self.query.clear()
                self._load(select=doc["id"], notice="资料信息已更新。")

            self.execute(lambda: self.library.edit(doc["id"], metadata), done, controls=self._controls())

    def _delete(self):
        doc = self._selected()
        if not doc:
            return
        answer = QMessageBox.question(
            self, "删除学校规章", f"从知识库删除「{doc['title']}」及其索引？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.execute(lambda: self.library.delete(doc["id"]), lambda _: self._load(notice="资料已删除。"),
                         controls=self._controls())

    def _search(self):
        self._load()

    def _all(self):
        self.query.clear()
        self._load()

    def _search_policy(self):
        form = fields(self, "试查当前有效条款", [("question", "检索问题", "")])
        if form and form["question"]:
            self.execute(lambda: self.library.search(form["question"]),
                         lambda rows: self.view.setPlainText(policy_context(rows)),
                         "正在搜索有效条款…", controls=self._controls())

    def _rebuild(self):
        if not installed(self.library.store.root, "semantic"):
            self.status.setText("请先在模型中心安装语义检索模型。")
            return
        self.execute(self.library.rebuild, lambda _: self._load(notice="语义索引已重建。"),
                     "正在重建语义索引…", controls=self._controls())
