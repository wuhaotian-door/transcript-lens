"""v1.9.8 P0：没有发信配置时的「本地找回码」模式。

背景（handoff/README.md P0）：安装包不附带 ``%LOCALAPPDATA%\\SGMS\\email.json``，
公开仓库也不能内置 SMTP 授权码，所以新用户一注册就撞上
``EmailNotConfiguredError: 邮件 API 尚未接入`` —— **注册功能等于不可用**。

v1.9.8 的方案 C：本机没有发信配置时

* 注册**不要求邮箱**（邮箱 + 验证码那一块整块隐藏）；
* 注册成功后把找回码**直接显示在屏幕上**，必须勾选确认保存才能继续；
* 忘记密码走「账号 + 找回码 + 新密码」；
* 配了发信的用户，一切照旧（邮箱 + 验证码 + 寄出找回码）。

本文件锁住四件事：

1. ``mail_configured()`` 的判据（没有配置 / 有 smtp 配置 / 配置写坏）；
2. **空邮箱**也能建立找回码，并且换密之后**旧成绩库密钥不变**（数据一条不丢）；
3. ``LoginWindow`` 在没有配置时**隐藏**注册页的邮箱块；
4. ``RecoveryCodeDialog`` **必须勾选才放行**（防"看都没看就点掉"）。

四个环境约定照 ``tests/test_import_review_ui.py``：``QT_QPA_PLATFORM=offscreen``
必须在 import PySide6 之前设；绝不碰真实 ``%LOCALAPPDATA%\\SGMS``（全部 ``tmp_path``）；
弹窗自己收干净；不写注册表。
"""

from __future__ import annotations

import os
from itertools import count
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.crypto_store import FILE_GRADES, CryptoStore  # noqa: E402
from core.email_recovery import (  # noqa: E402
    MailConfig,
    mail_configured,
    save_config,
)
from core.recovery import (  # noqa: E402
    check_recovery_code,
    enable_recovery,
    normalize_recovery_code,
    recovery_info,
    reset_password_recovery,
)

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "another-strong-pass"

#: 每个用例一个自增目录名（见 ``data_root`` 夹具的注释）
_COUNTER = count()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app() -> QApplication:
    """离屏 QApplication；整个模块共用一个。"""
    inst = QApplication.instance()
    if inst is None:
        inst = QApplication([])
    return inst  # type: ignore[return-value]


@pytest.fixture()
def data_root() -> Path:
    """每个用例一个干净的数据根。

    ★ 故意**不用** ``tmp_path``：本机 DSH 沙箱里 ``tempfile.mkdtemp()``
    建的目录（``os.mkdir(path, 0o700)``）**不可写**，pytest 的 basetemp 一用就
    PermissionError。这里用默认权限的 ``mkdir`` 建在仓库的 ``.tl_tmp/`` 下
    （已 gitignore），对产品代码没有任何影响。
    """
    base = Path(__file__).resolve().parents[1] / ".tl_tmp" / "recovery_tests"
    base.mkdir(parents=True, exist_ok=True)
    root = base / f"case_{os.getpid()}_{next(_COUNTER)}"
    root.mkdir()
    return root


@pytest.fixture()
def store(data_root: Path) -> CryptoStore:
    # 建临时库必须先 register()（AGENTS.md §20c：账户不存在时 save 会抛）
    return CryptoStore(data_root)


# ---------------------------------------------------------------------------
# 一、mail_configured()
# ---------------------------------------------------------------------------


def test_mail_configured_false_without_file(data_root: Path) -> None:
    assert mail_configured(data_root) is False


def test_mail_configured_true_with_smtp_config(data_root: Path) -> None:
    save_config(
        MailConfig(
            provider="smtp",
            endpoint="smtp.163.com:465",
            api_key="dummy-key",
            sender="noreply@example.com",
        ),
        root=data_root,
    )
    assert mail_configured(data_root) is True


def test_mail_configured_false_when_file_is_corrupt(data_root: Path) -> None:
    from core.email_recovery import config_path

    path = config_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")

    # 写坏了 = 发不出去 = 按本地模式走，而不是让注册整个卡死
    assert mail_configured(data_root) is False


def test_mail_configured_false_when_provider_is_not_smtp(data_root: Path) -> None:
    save_config(MailConfig(provider="", api_key="k", sender="a@b.c"), root=data_root)
    assert mail_configured(data_root) is False


@pytest.mark.parametrize("endpoint", ["", "imap.163.com:993", "smtp.163.com:65536"])
def test_invalid_mail_endpoint_uses_local_registration(data_root: Path, endpoint: str) -> None:
    save_config(
        MailConfig(provider="smtp", endpoint=endpoint, api_key="dummy-key", sender="a@example.com"),
        root=data_root,
    )
    assert mail_configured(data_root) is False


# ---------------------------------------------------------------------------
# 二、空邮箱也能建立找回码 —— 而且换完密码数据还在
# ---------------------------------------------------------------------------


def test_enable_recovery_without_email(store: CryptoStore) -> None:
    session = store.register("zhangsan", PASSWORD)

    code = enable_recovery(session, "", store=store)

    assert len(normalize_recovery_code(code)) == 24
    info = recovery_info(store, "zhangsan")
    assert info["enabled"] is True
    assert info["email"] == ""
    assert check_recovery_code(store, "zhangsan", code) is True


def test_reset_password_keeps_the_same_vault_key(store: CryptoStore) -> None:
    session = store.register("lisi", PASSWORD)

    # 先存一条"成绩"密文，换密之后必须还能用同一把钥匙打开
    store.save(session, FILE_GRADES, {"score": 91})
    code = enable_recovery(session, "", store=store)

    again = reset_password_recovery(store, "lisi", code, NEW_PASSWORD)

    assert again.key == session.key, "换密码不许换库密钥（换了就等于成绩全丢）"
    # 新密码能登录、旧密码不能
    store.login("lisi", NEW_PASSWORD)
    with pytest.raises(Exception):
        store.login("lisi", PASSWORD)
    assert store.load(again, FILE_GRADES) == {"score": 91}


def test_enable_recovery_with_email_still_records_it(
    store: CryptoStore, data_root: Path
) -> None:
    save_config(
        MailConfig(provider="smtp", api_key="k", sender="a@example.com"),
        root=data_root,
    )
    session = store.register("wangwu", PASSWORD)

    enable_recovery(session, "wangwu@example.com", store=store)

    info = recovery_info(store, "wangwu")
    assert info["enabled"] is True
    assert info["email"] == "wangwu@example.com"


def test_blank_email_is_treated_as_local_mode(store: CryptoStore) -> None:
    session = store.register("zhaoliu", PASSWORD)
    code = enable_recovery(session, "   ", store=store)
    assert recovery_info(store, "zhaoliu")["email"] == ""
    assert check_recovery_code(store, "zhaoliu", code) is True


# ---------------------------------------------------------------------------
# 三、登录窗：没有配置时注册页的邮箱块必须藏起来
# ---------------------------------------------------------------------------


def test_login_window_hides_mail_block_without_config(
    app: QApplication, store: CryptoStore, data_root: Path
) -> None:
    from ui.login_window import LoginWindow

    win = LoginWindow(store=store, root=data_root)
    try:
        assert win._mail_ready is False
        # 显式 hide() 过 —— 父窗没 show 时 isHidden() 仍是 True，能区分"藏了"与"没藏"
        assert win._mail_box.isHidden() is True
    finally:
        win.deleteLater()


def test_login_window_shows_mail_block_when_configured(
    app: QApplication, store: CryptoStore, data_root: Path
) -> None:
    from ui.login_window import LoginWindow

    save_config(
        MailConfig(provider="smtp", endpoint="smtp.163.com:465", api_key="k", sender="a@example.com"),
        root=data_root,
    )
    win = LoginWindow(store=store, root=data_root)
    try:
        assert win._mail_ready is True
        assert win._mail_box.isHidden() is False
    finally:
        win.deleteLater()


@pytest.mark.parametrize(("smtp_enabled", "with_email"), [(False, True), (True, False), (True, True)])
def test_register_shows_recovery_code_without_email_or_when_delivery_fails(
    app: QApplication, store: CryptoStore, data_root: Path, monkeypatch,
    smtp_enabled: bool, with_email: bool,
) -> None:
    from core import email_recovery
    from ui import login_window

    if smtp_enabled:
        save_config(
            MailConfig(provider="smtp", endpoint="smtp.163.com:465", api_key="dummy-key",
                       sender="a@example.com"),
            root=data_root,
        )
    checked = []
    shown = []
    monkeypatch.setattr(login_window, "check_email_code", lambda *args, **kwargs: checked.append(args))
    monkeypatch.setattr(login_window, "show_recovery_code", lambda _parent, code: shown.append(code))
    monkeypatch.setattr(login_window, "run_async",
                        lambda _parent, work, *, on_done, on_error: on_done(work()))

    def reject_delivery(*args, **kwargs):
        raise email_recovery.EmailSendError("test mail unavailable")

    monkeypatch.setattr(email_recovery, "send_recovery_mail", reject_delivery)
    window = login_window.LoginWindow(store=store, root=data_root)
    try:
        window._new_user.setText("test_registration")
        window._new_pass.setText(PASSWORD)
        window._new_pass2.setText(PASSWORD)
        window._new_email.setText("test@example.com" if with_email else "")
        window._new_code.setText("123456")
        window._do_register()
        assert bool(checked) == (smtp_enabled and with_email)
        assert len(shown) == 1
        assert check_recovery_code(store, "test_registration", shown[0])
        from core.user_store import load_user_json
        assert load_user_json(store, "test_registration", "first_run.json.enc")["pending"] is True
        store.login("test_registration", PASSWORD)
    finally:
        window.deleteLater()


def test_recovery_dialog_for_current_account_uses_saved_code_and_revokes_token(
    app: QApplication, store: CryptoStore, data_root: Path, monkeypatch
) -> None:
    from core.session_token import save_token
    from ui import recover_dialog
    from ui.pages.account_page import _ChangePasswordDialog

    original = store.register("recover-user", PASSWORD)
    store.register("another-user", PASSWORD)
    store.save(original, FILE_GRADES, {"score": 91})
    code = enable_recovery(original, "test@example.com", store=store)
    token = save_token("recover-user", root=data_root)
    monkeypatch.setattr(recover_dialog, "run_async",
                        lambda _parent, work, *, on_done, on_error: on_done(work()))
    choice = _ChangePasswordDialog("recover-user", can_use_recovery=True)
    dialog = recover_dialog.RecoverDialog(store, username="recover-user")
    try:
        choice._choose_recovery()
        assert choice.use_recovery
        assert not dialog._account.isEnabled()
        assert dialog._account.count() == 1
        assert "找回码邮件" in dialog._hint.text()
        dialog._code.setText(code)
        dialog._new_pass.setText(NEW_PASSWORD)
        dialog._new_pass2.setText(NEW_PASSWORD)
        dialog._do_reset()
        assert dialog.username == "recover-user"
        session = store.login("recover-user", NEW_PASSWORD)
        assert store.load(session, FILE_GRADES) == {"score": 91}
        assert not token.exists()
    finally:
        choice.deleteLater()
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# 四、找回码弹窗：不勾确认就不放行
# ---------------------------------------------------------------------------


def test_recovery_code_dialog_requires_confirmation(app: QApplication) -> None:
    from ui.recovery_code_dialog import RecoveryCodeDialog

    dlg = RecoveryCodeDialog("ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2")
    try:
        assert dlg._ok.isEnabled() is False, "没勾『已保存』就点得动 —— 等于没拦"
        dlg._saved.setChecked(True)
        assert dlg._ok.isEnabled() is True
    finally:
        # ★ 未确认时 close() 会弹「确定要丢弃吗」的模态框，离屏测试会**永远卡住**；
        #   这里直接标记已确认再删，不走关窗守卫。
        dlg._confirmed = True
        dlg.deleteLater()


def test_recovery_code_dialog_copies_the_code(app: QApplication) -> None:
    from ui.recovery_code_dialog import RecoveryCodeDialog

    code = "ABCD-EFGH-JKMN-PQRS-TUVW-XYZ2"
    dlg = RecoveryCodeDialog(code)
    try:
        dlg._copy()
        assert QApplication.clipboard().text() == code
    finally:
        dlg._confirmed = True
        dlg.deleteLater()
