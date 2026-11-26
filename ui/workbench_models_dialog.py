"""辅助模型的下载、校验与管理。"""

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMessageBox, QProgressBar, QVBoxLayout

from core.workbench_models import ASSETS, GROUPS, install_models, installed, uninstall_models
from ui.anim import make_card
from ui.workbench_widgets import TaskDialog, button, label

MODEL_INFO = {
    "semantic": ("规章语义检索", "BGE Small Chinese v1.5 · INT8", "按含义查找相关学校规章和谈话记录。"),
    "face": ("照片匹配", "YuNet 2023mar + SFace 2021dec INT8", "检测照片中的人脸，并与已授权学生的照片比对。"),
}


class WorkbenchModelsDialog(TaskDialog):
    progress = Signal(str, int, int)

    def __init__(self, parent, store):
        super().__init__(parent, "辅助模型")
        self.store = store
        self._active = ""
        self.setMinimumWidth(620)
        self.body.addWidget(label("辅助模型", "H2"))
        self.cards = {}
        for group, (title, model, detail) in MODEL_INFO.items():
            card = make_card(shadow=False, name="WorkbenchStrip")
            box = QVBoxLayout(card)
            box.setContentsMargins(17, 15, 17, 15)
            box.setSpacing(8)
            heading = QHBoxLayout()
            heading.addWidget(label(title, "H3"), 1)
            names = GROUPS[group]
            total = sum(ASSETS[name][1] for name in names)
            heading.addWidget(label(f"{total / 1024**2:.1f} MiB", "Muted"))
            box.addLayout(heading)
            box.addWidget(label(model, "WorkbenchModelName"))
            hint = label(detail, "Muted")
            hint.setWordWrap(True)
            box.addWidget(hint)
            entry = {"card": card}
            actions = QHBoxLayout()
            actions.addStretch(1)
            entry["download"] = button("下载", lambda _=False, g=group: self._download(g), "Primary")
            entry["import"] = button("离线导入", lambda _=False, g=group: self._import(g))
            entry["ready"] = button("已安装", lambda: None, "WorkbenchModelReady")
            entry["ready"].setEnabled(False)
            entry["uninstall"] = button("卸载", lambda _=False, g=group: self._uninstall(g))
            for action in ("ready", "import", "uninstall", "download"):
                actions.addWidget(entry[action])
            box.addLayout(actions)
            self.body.addWidget(card)
            self.cards[group] = entry
        self._meter = QProgressBar()
        self._meter.setTextVisible(True)
        self._meter.setObjectName("AuxModelProgress")
        self._meter.setMinimumHeight(19)
        self._meter.hide()
        self.body.addWidget(self._meter)
        self.body.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(button("关闭", self.reject))
        self.body.addLayout(actions)
        self.progress.connect(self._update_progress)
        self._refresh()

    def _refresh(self):
        for group, entry in self.cards.items():
            ready = installed(self.store.root, group)
            entry["card"].setVisible(not self._active or group == self._active)
            for action in ("download", "import"):
                entry[action].setVisible(not ready and not self._active)
            for action in ("ready", "uninstall"):
                entry[action].setVisible(ready and not self._active)

    def _finish(self, message=None):
        self._active = ""
        self._meter.hide()
        self._refresh()
        if message:
            self.status.setText(message)

    def _update_progress(self, asset, done, total):
        if not self._active:
            return
        self._meter.setRange(0, total)
        self._meter.setValue(done)
        self.status.setText(f"{MODEL_INFO[self._active][1]} · {asset} · "
                            f"{done / 1024**2:.1f}/{total / 1024**2:.1f} MiB")

    def _install(self, group, source=None):
        if self._busy:
            return
        self._active = group
        self._refresh()
        self._meter.setRange(0, 0)
        self._meter.show()
        model = MODEL_INFO[group][1]
        self.execute(lambda: install_models(self.store.root, group, source, progress=self.progress.emit),
                     lambda _: self._finish(f"{model} 已安装。"),
                     f"{'正在核对' if source else '正在连接'} {model}…",
                     on_error=lambda _: self._finish())

    def _download(self, group):
        title = MODEL_INFO[group][1]
        if QMessageBox.question(
            self, "下载本地模型", f"是否下载 {title} 并校验文件？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._install(group)

    def _import(self, group):
        names = "、".join(GROUPS[group])
        folder = QFileDialog.getExistingDirectory(self, f"选择包含 {names} 的文件夹")
        if folder:
            self._install(group, Path(folder))

    def _uninstall(self, group):
        title = MODEL_INFO[group][1]
        answer = QMessageBox.question(
            self, "卸载本地模型", f"是否卸载 {title}？再次使用时可重新下载。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.execute(lambda: uninstall_models(self.store.root, group),
                         lambda _: self._finish(f"{title} 已卸载。"), "正在卸载模型…",
                         on_error=lambda _: self._refresh())
