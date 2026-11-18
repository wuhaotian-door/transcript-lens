"""独立模型中心的导航、核心状态和原有 AI 配置。"""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from core.crypto_store import CryptoStore
from core.user_store import load_user_json, save_user_json
from ui.main_window import MainWindow, NAV_ITEMS
from ui.model_center_dialog import ModelCenterDialog
from ui.pages import model_center_page as models


def test_sidebar_and_profile_open_embedded_model_center(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    ready = False
    catalog_loads = []
    monkeypatch.setattr(models, "_backend_available", lambda: ready)
    monkeypatch.setattr(ModelCenterDialog, "load_catalog", lambda self: catalog_loads.append(self))

    def install(_parent):
        nonlocal ready
        ready = True
        return True

    monkeypatch.setattr(models, "open_backend_setup", install)
    window = MainWindow(root=str(tmp_path))
    window.show()
    models_index = next(i for i, (key, _) in enumerate(NAV_ITEMS) if key == "models")
    try:
        QTest.mouseClick(window._nav_buttons[models_index], Qt.MouseButton.LeftButton)
        QTest.qWait(450)
        page = window.model_center_page
        assert window._stack.currentWidget() is page
        assert page._catalog is None and not catalog_loads
        button = next(b for b in page.findChildren(QPushButton) if b.text() == "下载推理核心")
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert page._views.currentWidget() is page._tabs
        assert catalog_loads == [page._catalog] and not page._catalog.isWindow()

        window.goto(window._stack.indexOf(window.archive_page))
        QTest.qWait(450)
        side = window.archive_page._semester_page
        menu_action = next(a for a in side._model_picker.menu().actions() if a.text() == "前往模型中心")
        menu_action.trigger()
        QTest.qWait(450)
        assert window._stack.currentWidget() is page
        assert window._nav_buttons[models_index].isChecked()
        assert len(catalog_loads) == 1
    finally:
        window.close()
        app.processEvents()


def test_model_center_loads_account_settings_without_exposing_key(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(models, "_backend_available", lambda: True)
    monkeypatch.setattr(ModelCenterDialog, "load_catalog", lambda self: None)
    store = CryptoStore(root=tmp_path)
    user = "验收账号"
    store.register(user, "ModelCenter1234")
    existing = {"local_enabled": False, "cloud_enabled": True, "api_key": "sample"}
    save_user_json(store, user, models.AI_SETTINGS_FILE, existing)
    page = models.ModelCenterPage(store)
    page.set_user(user)
    page.show()
    try:
        assert page._cloud.isChecked()
        assert page._key.text() == ""
        page._local.setChecked(True)
        page._save.click()
        actual = load_user_json(store, user, models.AI_SETTINGS_FILE, {})
        assert {field: actual[field] for field in existing} == {**existing, "local_enabled": True}
        assert page._status.text() == "已保存"
        assert all("sample" not in label.text() for label in page.findChildren(QLabel))
    finally:
        page.close()
        app.processEvents()
