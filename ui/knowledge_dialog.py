"""制度库管理和出处预览。"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QListWidget, QMessageBox, QPlainTextEdit, QSplitter

from core.knowledge import KnowledgeLibrary, policy_context
from core.workbench_models import installed
from ui.workbench_widgets import TaskDialog, button, fields, label


class KnowledgeDialog(TaskDialog):
    def __init__(self, parent, store, username):
        super().__init__(parent, "规章制度知识库")
        self.setMinimumSize(760, 530)
        self.library = KnowledgeLibrary(store, username)
        self.body.addWidget(label("规章制度知识库", "H2"))
        self.scope_label = label("", "Muted")
        self.body.addWidget(self.scope_label)
        row = QHBoxLayout()
        row.addWidget(button("导入制度", self._import, "Primary"))
        row.addWidget(button("设置学校与学年", self._scope))
        row.addWidget(button("重建向量索引", self._rebuild))
        row.addStretch(1)
        self.body.addLayout(row)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        self.docs = QListWidget()
        self.docs.itemActivated.connect(lambda _: self._excerpts())
        split.addWidget(self.docs)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlaceholderText("显示所选文档的段落或检索结果")
        split.addWidget(self.view)
        split.setStretchFactor(1, 1)
        self.body.addWidget(split, 1)
        actions = QHBoxLayout()
        actions.addWidget(button("查看段落", self._excerpts))
        actions.addWidget(button("删除文档", self._delete))
        actions.addStretch(1)
        actions.addWidget(button("检索测试", self._search))
        self.body.addLayout(actions)
        self.body.addWidget(self.status)
        self._load()

    def _load(self):
        self.docs.clear()
        scope = self.library.scope()
        kind = "语义模型可用" if installed(self.library.store.root, "semantic") else "关键词检索，可在模型中心安装语义模型"
        self.scope_label.setText(f"{scope.get('school', '未选学校')}　{scope.get('year', '未选学年')}　|　{kind}")
        for doc in self.library.list():
            index_state = "语义已索引" if doc.get("model") else "仅关键词"
            version = f" / {doc['version']}" if doc.get("version") else ""
            self.docs.addItem(f"{doc['title']}{version}\n{doc['source']}　{doc['year']}　{doc['count']} 段　{index_state}")
            self.docs.item(self.docs.count() - 1).setData(Qt.ItemDataRole.UserRole, doc["id"])

    def _import(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择规章或学校背景文件",
                                                  filter="制度文档 (*.pdf *.docx *.txt *.md)")
        if not filename:
            return
        scope = self.library.scope()
        path = Path(filename)
        metadata = fields(self, "文档信息", [
            ("title", "标题", path.stem), ("source", "发布部门或来源", ""),
            ("school", "适用学校或「通用」", scope.get("school", "")),
            ("year", "学年或「通用」", scope.get("year", "")),
            ("version", "版本（选填）", ""), ("start", "生效时间 YYYY-MM-DD（选填）", ""),
            ("end", "失效时间 YYYY-MM-DD（选填）", ""),
        ])
        if metadata is not None:
            self.execute(lambda: self.library.add(path, metadata), lambda _: self._load(), "正在提取和索引文档…")

    def _scope(self):
        scope = self.library.scope()
        form = fields(self, "当前检索范围", [("school", "学校", scope.get("school", "")),
                                             ("year", "学年", scope.get("year", ""))])
        if form and all(form.values()):
            self.execute(lambda: self.library.set_scope(**form), lambda _: self._load())

    def _selected(self):
        item = self.docs.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _excerpts(self):
        digest = self._selected()
        if digest:
            self.execute(lambda: self.library.excerpts(digest), lambda rows: self.view.setPlainText(
                "\n\n".join(f"{row['location']}\n{row['text']}" for row in rows)
            ))

    def _delete(self):
        digest = self._selected()
        if digest and QMessageBox.question(self, "删除制度文档", "删除所选文档及其索引？") == QMessageBox.StandardButton.Yes:
            def done(_):
                self.view.clear()
                self._load()
            self.execute(lambda: self.library.delete(digest), done)

    def _search(self):
        form = fields(self, "试查制度", [("question", "检索问题", "")])
        if form and form["question"]:
            self.execute(lambda: self.library.search(form["question"]),
                         lambda rows: self.view.setPlainText(policy_context(rows)), "正在搜索相关制度…")

    def _rebuild(self):
        if not installed(self.library.store.root, "semantic"):
            self.status.setText("请先在模型中心安装语义检索模型。")
            return
        self.execute(self.library.rebuild, lambda _: self._load(), "正在后台重建，当前索引仍可使用…")
