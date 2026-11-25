"""用户主动安装的语义检索与照片匹配模型。"""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMessageBox

from core.workbench_models import GROUPS, install_models, installed
from ui.workbench_widgets import TaskDialog, button, label


class WorkbenchModelsDialog(TaskDialog):
    def __init__(self, parent, store):
        super().__init__(parent, "语义检索与照片匹配模型")
        self.store = store
        self.setMinimumWidth(540)
        self.labels = {}
        for group, title in (("semantic", "规章语义检索"), ("face", "照片匹配")):
            row = QHBoxLayout()
            state = label(title, "H2")
            self.labels[group] = (title, state)
            row.addWidget(state, 1)
            row.addWidget(button("下载", lambda _=False, g=group: self._download(g), "Primary"))
            row.addWidget(button("离线导入", lambda _=False, g=group: self._import(g)))
            self.body.addLayout(row)
        self.body.addWidget(self.status)
        self._refresh()

    def _refresh(self):
        for group, (title, state) in self.labels.items():
            state.setText(title + ("　已安装" if installed(self.store.root, group) else "　未安装"))

    def _download(self, group):
        if QMessageBox.question(self, "下载本地模型", "从模型发布站下载固定版本并校验完整性？") == QMessageBox.StandardButton.Yes:
            self.execute(lambda: install_models(self.store.root, group), lambda _: self._refresh(),
                         "正在下载并校验模型…")

    def _import(self, group):
        names = "、".join(GROUPS[group])
        folder = QFileDialog.getExistingDirectory(self, f"选择包含 {names} 的文件夹")
        if folder:
            self.execute(lambda: install_models(self.store.root, group, Path(folder)), lambda _: self._refresh(),
                         "正在核对本地模型文件…")
