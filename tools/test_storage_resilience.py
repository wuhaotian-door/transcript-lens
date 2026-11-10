"""存储故障回归；独立于封板 tests/，仅使用 pytest 临时目录和假数据。

运行：python -m pytest tools/test_storage_resilience.py -q
"""

from __future__ import annotations

import errno
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import crypto_store as crypto, migrator, session_token  # noqa: E402


def locked_error() -> PermissionError:
    error = PermissionError(errno.EACCES, "locked")
    error.winerror = 32
    return error


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "SGMS"
    root.mkdir()
    for name, content in {
        "users.json": b'{"users": [], "vault_version": "1.9.1"}',
        "app.salt": b"fake-salt",
        "u_fake/grades.enc": b"fake-encrypted-grades",
        "u_fake/cache/chat.json.enc": b"fake-encrypted-chat",
        "u_fake/audit/changes.json.enc": b"fake-encrypted-audit",
        "models/model.gguf": b"regenerable",
        "cache/cache.bin": b"regenerable",
        "backup_vold/data.enc": b"old-backup",
        "stale.tmp": b"unfinished",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def test_legacy_locked_temp_does_not_block_new_write(tmp_path: Path) -> None:
    path = tmp_path / "session.enc"
    legacy = path.with_name(path.name + ".tmp")
    legacy.mkdir()  # 固定文件名即使被目录占住，也不应影响写入。
    crypto._atomic_write_bytes(path, b"cipher")
    assert path.read_bytes() == b"cipher"
    assert legacy.is_dir()
    assert set(tmp_path.iterdir()) == {path, legacy}


def test_concurrent_writes_are_complete_and_leave_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "grades.enc"
    payloads = [bytes([i]) * 10000 for i in range(24)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda blob: crypto._atomic_write_bytes(path, blob), payloads))
    assert path.read_bytes() in payloads
    assert list(tmp_path.iterdir()) == [path]


def test_replace_retries_transient_windows_lock(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "grades.enc"
    replace = os.replace
    attempts = []
    sleeps = []

    def flaky(src, dst):
        attempts.append(src)
        if len(attempts) < 3:
            raise locked_error()
        replace(src, dst)

    monkeypatch.setattr(crypto.os, "replace", flaky)
    monkeypatch.setattr(crypto.time, "sleep", sleeps.append)
    crypto._atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new"
    assert len(attempts) == 3 and sleeps == [0.05, 0.1]


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 文件共享锁")
def test_real_windows_open_handle_keeps_old_target_safe(tmp_path: Path) -> None:
    path = tmp_path / "grades.enc"
    path.write_bytes(b"old-cipher")
    with path.open("rb") as reader:
        with pytest.raises(PermissionError):
            crypto._atomic_write_bytes(path, b"new-cipher")
        assert reader.read() == b"old-cipher"
    assert path.read_bytes() == b"old-cipher"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["replace", "fsync", "disk_full"])
def test_failed_write_preserves_old_bytes_and_cleans_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / "grades.enc"
    path.write_bytes(b"old")
    calls = []

    def fail(*args):
        calls.append(1)
        if failure == "replace":
            raise locked_error()
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(crypto.time, "sleep", lambda _: None)
    monkeypatch.setattr(crypto.os, "fsync" if failure == "fsync" else "replace", fail)
    with pytest.raises(OSError):
        crypto._atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]
    assert len(calls) == (4 if failure == "replace" else 1)


def test_backup_preserves_all_user_data_and_excludes_root_cache(vault):
    target = migrator.backup_vault(vault, "1.9.1")
    assert target.parent == vault
    assert (target / "u_fake/cache/chat.json.enc").read_bytes() == b"fake-encrypted-chat"
    assert (target / "u_fake/audit/changes.json.enc").read_bytes() == b"fake-encrypted-audit"
    assert not (target / "models").exists()
    assert not (target / "cache").exists()
    assert not (target / "backup_vold").exists()
    assert not (target / "stale.tmp").exists()


def test_same_timestamp_backups_never_overwrite(vault, monkeypatch):
    monkeypatch.setattr(migrator, "_stamp", lambda: "fixed")
    one = migrator.backup_vault(vault, "1.9.1")
    two = migrator.backup_vault(vault, "1.9.1")
    assert one != two and one.is_dir() and two.is_dir()
    assert not (two / one.name).exists()


def test_backup_uses_persistent_fallback_when_primary_is_unwritable(vault, monkeypatch):
    mkdir = Path.mkdir

    def guarded(path, *args, **kwargs):
        if path.parent == vault and path.name.startswith(migrator.BACKUP_PREFIX):
            raise PermissionError("primary backup denied")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded)
    target = migrator.backup_vault(vault, "1.9.1")
    assert not target.is_relative_to(vault)
    assert target.parent.parent.name == "SGMS_backups"
    assert (target / "users.json").read_bytes() == (vault / "users.json").read_bytes()
    assert (target / "app.salt").read_bytes() == b"fake-salt"
    assert (target / "u_fake/grades.enc").read_bytes() == b"fake-encrypted-grades"


@pytest.mark.parametrize("damaged", ["users.json", "app.salt", "u_fake/grades.enc", "u_fake/cache/chat.json.enc"])
def test_incomplete_or_corrupt_backup_is_rejected_and_cleaned(vault, monkeypatch, damaged):
    copy = migrator.shutil.copytree

    def corrupt(src, dst, **kwargs):
        # copytree 递归也走模块符号：只在最外层破坏目标。
        result = copy(src, dst, **kwargs)
        (Path(dst) / damaged).write_bytes(b"corrupt")
        return result

    # 禁止递归 monkeypatch 误伤：包装最外层时先恢复原 copytree。
    def outer(src, dst, **kwargs):
        monkeypatch.setattr(migrator.shutil, "copytree", copy)
        return corrupt(src, dst, **kwargs)

    before = (vault / "users.json").read_bytes()
    monkeypatch.setattr(migrator.shutil, "copytree", outer)
    with pytest.raises(migrator.MigrationError, match="备份内容不完整"):
        migrator.backup_vault(vault, "1.9.1")
    assert (vault / "users.json").read_bytes() == before
    assert not list(vault.glob("backup_v1.9.1_*"))


def test_all_backup_locations_fail_without_modifying_vault(vault, tmp_path, monkeypatch):
    parents = [vault, tmp_path / "alternate"]
    monkeypatch.setattr(migrator, "_backup_parents", lambda _: parents)
    monkeypatch.setattr(migrator, "_unique_dir", lambda *_: (_ for _ in ()).throw(PermissionError("denied")))
    original = (vault / "users.json").read_bytes()
    with pytest.raises(migrator.MigrationError, match="权限、磁盘空间"):
        migrator.migrate_store(crypto.CryptoStore(vault))
    assert (vault / "users.json").read_bytes() == original


def test_failed_partial_copy_is_removed_before_fallback(vault, tmp_path, monkeypatch):
    copy = migrator.shutil.copytree
    monkeypatch.setattr(migrator, "_backup_parents", lambda _: [vault, tmp_path / "alternate"])

    def partial(src, dst, **kwargs):
        if Path(dst).parent == vault:
            (Path(dst) / "partial.enc").write_bytes(b"partial")
            raise PermissionError("copy denied")
        monkeypatch.setattr(migrator.shutil, "copytree", copy)
        return copy(src, dst, **kwargs)

    monkeypatch.setattr(migrator.shutil, "copytree", partial)
    target = migrator.backup_vault(vault, "1.9.1")
    assert target.parent == tmp_path / "alternate"
    assert not list(vault.glob("backup_v1.9.1_*"))


def test_failed_migration_rolls_back_original_index_atomically(vault, monkeypatch):
    store = crypto.CryptoStore(vault)
    before = store.users_path.read_bytes()

    def fail(_):
        store.users_path.write_bytes(b"partial-index")
        raise OSError("write failed")

    monkeypatch.setattr(store, "save_users", fail)
    with pytest.raises(migrator.MigrationError, match="已回滚"):
        migrator.migrate_store(store)
    assert store.users_path.read_bytes() == before
    assert not list(vault.glob(".users.json.*.tmp"))


def test_save_token_translates_io_failure_without_exposing_payload(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(session_token, "save_encrypted", denied)
    with pytest.raises(crypto.CryptoError, match="未能保存") as caught:
        session_token.save_token("fake-user", root=tmp_path)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert "fake-user" not in str(caught.value)


def test_unreadable_token_falls_back_to_password_login(tmp_path, monkeypatch):
    session_token.token_path(tmp_path).write_bytes(b"cipher")
    monkeypatch.setattr(session_token, "load_encrypted", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    assert session_token.load_token(tmp_path) is None
    assert session_token.token_info(tmp_path)["reason"] == "corrupt"


def test_unreadable_token_metadata_falls_back_to_password_login(tmp_path, monkeypatch):
    original = Path.exists

    def denied(path):
        if path == session_token.token_path(tmp_path):
            raise PermissionError("metadata denied")
        return original(path)

    monkeypatch.setattr(Path, "exists", denied)
    assert session_token.load_token(tmp_path) is None


def test_fallback_migration_preserves_decryptable_payload(tmp_path, monkeypatch):
    import json

    root = tmp_path / "SGMS"
    store = crypto.CryptoStore(root, iterations=1)
    store.register("fake-user", "fake-password")
    data_path = root / "u_fake" / "archive.json.enc"
    payload = {"fake-student": "fake-private-grade"}
    crypto.save_encrypted(payload, data_path, root=root)
    index = json.loads(store.users_path.read_text(encoding="utf-8"))
    index["vault_version"] = index["__vault_version__"] = "1.9.1"
    store.users_path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(migrator, "_backup_parents", lambda _: [tmp_path / "persistent-backups"])
    outcome = migrator.migrate_store(store)
    assert outcome.performed
    assert str(outcome.backup_dir) in outcome.detail
    assert crypto.load_encrypted(data_path, root=root) == payload
    backup_payload = outcome.backup_dir / "u_fake" / "archive.json.enc"
    assert crypto.load_encrypted(backup_payload, root=outcome.backup_dir) == payload
    assert b"fake-private-grade" not in backup_payload.read_bytes()
    assert store.login("fake-user", "fake-password").username == "fake-user"


@pytest.mark.parametrize("remember", [True, False])
def test_remember_io_failure_still_emits_logged_in(tmp_path, monkeypatch, remember):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui import login_window as lw

    app = QApplication.instance() or QApplication([])
    store = crypto.CryptoStore(tmp_path, iterations=1)
    store.register("fake-user", "fake-password")
    window = lw.LoginWindow(store=store, root=str(tmp_path))
    window._user_box.setCurrentText("fake-user")
    window._pass_edit.setText("fake-password")
    window._remember.setChecked(remember)
    warnings = []
    logged_in = []

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(lw, "save_token" if remember else "clear_token", denied)
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    monkeypatch.setattr(lw, "window_fade_out", lambda window, ms, callback: callback())
    window.logged_in.connect(logged_in.append)
    try:
        window._do_login()
        assert logged_in == ["fake-user"]
        assert len(warnings) == 1
        assert "本次仍然正常登录" in warnings[0]
        if not remember:
            assert "旧的自动登录凭证可能仍然有效" in warnings[0]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_release_upgrade_from_1_9_3_changes_only_version_metadata():
    data = {
        "vault_version": "1.9.3",
        "__vault_version__": "1.9.3",
        "users": [],
        "unchanged_payload": {"fake_key": "fake_value"},
    }
    migrated = migrator.migrate("1.9.3", data)
    assert migrated["vault_version"] == "1.9.4"
    assert migrated["__vault_version__"] == "1.9.4"
    assert migrated["unchanged_payload"] == data["unchanged_payload"]
    assert migrated["_migration"]["hops"] == ["v1.9.3→v1.9.4"]
    assert data["vault_version"] == "1.9.3"
