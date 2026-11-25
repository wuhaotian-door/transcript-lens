"""启动预检的离线检测、确认门槛和组件补装。"""

from pathlib import Path
from threading import Event
from zipfile import ZipFile

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from core import startup_environment as environment
from core.updater import DownloadError
from ui import startup_environment as screen


def test_required_check_never_inspects_optional_ai(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "_has_model", lambda: pytest.fail("启动不能扫描模型"))
    monkeypatch.setattr(environment, "probe_backend", lambda: pytest.fail("启动不能加载本地 AI"))
    assert environment.required_environment_problem(tmp_path) == ""
    unusable_root = tmp_path / "file"
    unusable_root.touch()
    assert "无法写入应用数据目录" in environment.required_environment_problem(unusable_root)


def test_optional_setup_is_offered_once_without_probing_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "default_root", lambda: tmp_path)
    monkeypatch.setattr(environment, "_has_model", lambda: False)
    monkeypatch.setattr(environment, "probe_backend", lambda: pytest.fail("引导不能启动探测进程"))
    assert environment.initial_ai_setup() == environment.EnvironmentStatus(True, True)
    monkeypatch.setattr(environment, "_has_model", lambda: pytest.fail("引导已显示，无需重复检查"))
    assert environment.initial_ai_setup() is None


@pytest.mark.parametrize("directory", ["runtime/site", environment.WINDOWS_RUNTIME_DIR_NAME])
def test_optional_setup_does_not_prompt_with_installed_components(monkeypatch, tmp_path, directory):
    monkeypatch.setattr(environment, "default_root", lambda: tmp_path)
    package = tmp_path / directory / "llama_cpp"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    monkeypatch.setattr(environment, "_has_model", lambda: True)
    monkeypatch.setattr(environment, "probe_backend", lambda: pytest.fail("已安装时不能重新加载推理核心"))
    assert environment.initial_ai_setup() is None


@pytest.mark.skipif(environment.sys.platform != "win32", reason="使用 Windows 文件锁")
def test_failed_engine_verification_keeps_old_files_then_can_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "default_root", lambda: tmp_path)
    destination = tmp_path / environment.WINDOWS_RUNTIME_DIR_NAME
    destination.mkdir(parents=True)
    (destination / "previous.txt").write_text("keep")
    verified = False

    def download(_url, target, *, expected_sha256, **_kwargs):
        assert expected_sha256
        package = "llama_cpp" if target.name == "llama.whl" else "diskcache"
        with ZipFile(target, "w") as archive:
            archive.writestr(f"{package}/__init__.py", "")
            archive.writestr("bin/not-needed.dll", "")
        return target

    def probe(path=None):
        if path is None:
            return False
        assert isinstance(path, Path) and (path / "diskcache/__init__.py").exists()
        return verified

    monkeypatch.setattr(environment, "probe_backend", probe)
    monkeypatch.setattr(environment, "download_exe", download)
    status = environment.EnvironmentStatus(needs_engine=True)
    with pytest.raises(DownloadError, match="未能加载"):
        environment.install_missing(status, progress=lambda *_: None, cancel=lambda: False)
    assert (destination / "previous.txt").read_text() == "keep"
    verified = True
    environment.install_missing(status, progress=lambda *_: None, cancel=lambda: False)
    assert (destination / "llama_cpp/__init__.py").is_file()
    assert not (destination / "bin").exists()


def test_startup_prompts_before_download_and_skip_continues(monkeypatch):
    app = QApplication.instance() or QApplication([])
    requested = []
    monkeypatch.setattr(screen, "ensure_runtime_on_path", lambda: None)
    monkeypatch.setattr(screen, "install_missing", lambda status, **_kwargs: requested.append(status))
    monkeypatch.setattr(screen, "run_async", lambda _parent, job, *, on_done, on_error: on_done(job()))

    for confirm in (False, True):
        dialog = screen.StartupEnvironmentDialog(environment.EnvironmentStatus(True, True))
        dialog.show()
        app.processEvents()
        assert requested == []
        button = dialog._download if confirm else dialog._skip
        assert button.isVisible()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert dialog.result() == (QDialog.DialogCode.Accepted if confirm else QDialog.DialogCode.Rejected)
    assert requested == [environment.EnvironmentStatus(True, True)]


def test_ai_check_and_prompt_leave_main_window_interactive(monkeypatch):
    app = QApplication.instance() or QApplication([])
    can_finish = Event()
    clicked = []
    worker_threads = []

    def detect():
        worker_threads.append(QThread.currentThread())
        can_finish.wait(5)
        return environment.EnvironmentStatus(needs_model=True)

    monkeypatch.setattr(screen, "initial_ai_setup", detect)
    window = QPushButton("主界面操作")
    window.clicked.connect(lambda: clicked.append(True))
    window.show()
    try:
        screen.offer_startup_ai(window)
        QTest.mouseClick(window, Qt.MouseButton.LeftButton)
        assert clicked == [True]
        assert window.findChild(QDialog) is None
        can_finish.set()
        for _ in range(100):
            QTest.qWait(10)
            if window.findChild(QDialog) is not None:
                break
        prompt = window.findChild(QDialog)
        assert worker_threads and worker_threads[0] != app.thread()
        assert prompt is not None and prompt.isVisible() and not prompt.isModal()
        assert app.activeModalWidget() is None
        QTest.mouseClick(window, Qt.MouseButton.LeftButton)
        assert clicked == [True, True]
        QTest.mouseClick(prompt._skip, Qt.MouseButton.LeftButton)
    finally:
        can_finish.set()
        window.close()
