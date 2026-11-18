"""在同一菜单中选择已安装的模型或进入模型配置。"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMenu, QToolButton


CORE_MISSING_HINT = "本机暂无推理核心，请点击下方按钮下载。安装后可在模型中心下载模型。"


def model_label(model) -> str:
    return str(getattr(model, "display", "") or getattr(model, "label", "")
               or getattr(model, "name", "") or "")


class ModelPicker(QToolButton):
    model_selected = Signal(object)
    library_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ModelPicker")
        self.setMinimumHeight(34)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self.set_models([], None)

    def set_models(self, models, selected) -> None:
        self._menu.clear()
        for model in models:
            action = self._menu.addAction(model_label(model))
            action.setCheckable(True)
            action.setChecked(model is selected)
            action.triggered.connect(lambda _checked=False, value=model: self.model_selected.emit(value))
        if models:
            self._menu.addSeparator()
        self._menu.addAction("前往模型中心", self.library_requested.emit)
        label = model_label(selected) if selected is not None else ""
        self.setText(f"当前模型  {label[:22]}{'…' if len(label) > 22 else ''}" if label else "选择模型")
        self.setToolTip(label or "选择本地模型，或前往模型中心")
