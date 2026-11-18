"""完成回调、安装准备及 Windows 替换交接的回归测试。"""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from core import update_apply
from ui import update_dialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_download_completion_runs_installation_before_returning(app, monkeypatch, tmp_path):
    destination = tmp_path / "update.exe"
    events = []

    class RecordingDialog(update_dialog.DownloadProgressDialog):
        def show_installing(self):
            super().show_installing()
            assert self.windowTitle() == "正在安装更新"
            events.append("installing")

        def finish(self):
            super().finish()
            events.append("finished")

    def download(url, dest, progress, **kwargs):
        progress(0, 32)
        dest.write_bytes(b"new build")
        progress(32, 32)
        return dest

    def prepare(path):
        assert path == destination and path.read_bytes() == b"new build"
        events.append("prepared")

    monkeypatch.setattr(update_dialog, "download_exe", download)
    monkeypatch.setattr(update_dialog, "DownloadProgressDialog", RecordingDialog)
    result = update_dialog.download_release(
        None, "http://localhost/update.exe", destination, label="下载", prepare=prepare,
    )
    assert result == str(destination)
    assert events[-1] == "finished"
    assert set(events[:-1]) == {"installing", "prepared"}


def test_download_eta_does_not_count_resumed_bytes_as_current_speed(app, monkeypatch):
    now = 100.0
    monkeypatch.setattr(update_dialog.time, "monotonic", lambda: now)
    dialog = update_dialog.DownloadProgressDialog("下载")
    mb = 1024 * 1024
    dialog.on_progress(900 * mb, 1000 * mb)
    assert "预计剩余 计算中" in dialog.describe()
    now += 1
    dialog.on_progress(910 * mb, 1000 * mb)
    assert "10.0 MB/s" in dialog.describe()
    assert "预计剩余 9 秒" in dialog.describe()
    dialog.finish()


def test_backup_failure_leaves_program_and_handoff_untouched(monkeypatch, tmp_path):
    current = tmp_path / "current.exe"
    incoming = tmp_path / "incoming.exe"
    handoff = tmp_path / "update.json"
    current.write_bytes(b"old")
    incoming.write_bytes(b"new")

    def fail(*args, **kwargs):
        raise OSError("backup blocked")

    monkeypatch.setattr(update_apply, "backup_current_exe", fail)
    with pytest.raises(OSError, match="backup blocked"):
        update_apply.prepare_update(incoming, current, "9.9.9", json_path=handoff)

    assert current.read_bytes() == b"old"
    assert incoming.read_bytes() == b"new"
    assert not handoff.exists()
    assert not list(tmp_path.glob(".current.exe.update-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows executable handoff")
def test_pending_apply_uses_prepared_backup_and_independent_restart(monkeypatch, tmp_path):
    current = tmp_path / "当前 & 程序.exe"
    incoming = tmp_path / "incoming.exe"
    state = tmp_path / "update.json"
    log = tmp_path / "update.log"
    current.write_bytes(b"old")
    incoming.write_bytes(b"new")
    checksum = hashlib.sha256(b"new").hexdigest()
    update_apply.prepare_update(incoming, current, "9.9.9", root=tmp_path,
                                sha256=checksum, json_path=state)
    metadata = update_apply.read_pending_update(state)
    assert Path(metadata["backup_exe"]).read_bytes() == b"old"
    staged = Path(metadata["pending_exe"])
    assert staged.parent == current.parent and staged.read_bytes() == b"new"
    monkeypatch.setattr(update_apply, "current_exe", lambda: current)
    launched = []

    def popen(command, **kwargs):
        launched.append((command, kwargs))
        return SimpleNamespace(pid=1)

    result = update_apply.apply_pending_update(
        root=tmp_path, frozen=True, json_path=state, popen=popen,
        bat_path=tmp_path / "apply.bat", log_path=log,
    )
    assert result.handed_off
    assert len(launched) == 1
    _, args = launched[0]
    assert args["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert args["env"]["TL_UPDATE_TARGET"] == str(current)
    assert args["cwd"] == str(current.parent)
    assert current.read_bytes() == b"old"


def test_pending_from_same_version_cannot_override_manual_install(monkeypatch, tmp_path):
    current = tmp_path / "current.exe"
    stale = tmp_path / "stale.exe"
    state = tmp_path / "update.json"
    current.write_bytes(b"fixed")
    stale.write_bytes(b"published-old")
    update_apply.write_pending_update(stale, current, update_apply.CURRENT_VERSION, path=state)
    monkeypatch.setattr(update_apply, "current_exe", lambda: current)

    result = update_apply.apply_pending_update(frozen=True, json_path=state)
    assert not result.handed_off
    assert current.read_bytes() == b"fixed"
    assert not state.exists()
