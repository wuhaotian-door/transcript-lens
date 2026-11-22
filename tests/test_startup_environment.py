"""启动预检的离线检测、确认门槛和组件补装。"""

from pathlib import Path
from zipfile import ZipFile

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from core import startup_environment as environment
from core.updater import DownloadError
from ui import startup_environment as screen


def test_detection_reports_only_missing_local_components(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "default_root", lambda: tmp_path)
    monkeypatch.setattr(environment, "_has_model", lambda: False)
    monkeypatch.setattr(environment, "probe_backend", lambda: pytest.fail("没有核心时不用启动探测进程"))
    assert environment.detect_environment() == environment.EnvironmentStatus(True, True)
    (tmp_path / "runtime/site/llama_cpp").mkdir(parents=True)
    monkeypatch.setattr(environment, "probe_backend", lambda: False)
    monkeypatch.setattr(environment, "_has_model", lambda: True)
    assert environment.detect_environment() == environment.EnvironmentStatus(True, False)
    monkeypatch.setattr(environment, "probe_backend", lambda: True)
    assert environment.detect_environment() == environment.EnvironmentStatus()


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
    monkeypatch.setattr(screen, "detect_environment", lambda: environment.EnvironmentStatus(True, True))
    monkeypatch.setattr(screen, "install_missing", lambda status, **_kwargs: requested.append(status))
    monkeypatch.setattr(screen, "run_async", lambda _parent, job, *, on_done, on_error: on_done(job()))

    for confirm in (False, True):
        dialog = screen.StartupEnvironmentDialog()
        dialog.show()
        app.processEvents()
        assert requested == []
        button = dialog._download if confirm else dialog._skip
        assert button.isVisible()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert dialog.result() == QDialog.DialogCode.Accepted
    assert requested == [environment.EnvironmentStatus(True, True)]
