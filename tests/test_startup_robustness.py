"""启动期健壮性的判据（v1.9.3）：日志、迁移备份、登录进不去、淡入卡住。

这一批全是**现场事故**换来的，不是假想风险：

1. **日志**（``core/applog``）—— ``--windowed`` 的 exe 没有 stdout/stderr，
   现场出问题时"什么都没有"就是最难查的那种。判据：能落盘、能读回、
   目录不可写时**绝不抛异常**（日志自己把程序搞崩就荒唐了）。
2. **迁移备份**（``core.migrator.backup_vault``）—— 现场数据目录里有 1.1 GB
   模型 + 3.3 GB 历史备份，整目录 ``copytree`` 要几十秒，中途被占用就整次迁移
   失败（用户看到的就是"旧版迁移数据报异常"）。判据：``models`` / ``runtime``
   / ``logs`` / ``cache`` 不进备份，而 ``users.json`` / ``app.salt`` / ``u_*``
   一个不少。
3. **登录进不去**（``ui.login_window``）—— 密码对了却停在"PBKDF2 派生中"不动。
   判据：密码通过之后任何异常都必须**兜住**（落日志 + 弹窗 + 状态改回来 +
   允许再点一次），而不是从按钮槽里冒出去。
4. **登录看门狗** —— 淡出动画没跑完时 ``logged_in`` 必须仍然发出（且只发一次），
   否则用户永远停在登录窗。

★ 规矩（与其它 UI 测试一致）：``QT_QPA_PLATFORM=offscreen`` 必须在 import
PySide6 **之前**设好；所有模态弹窗一律换成探针，一个真的弹窗都不许有。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import applog  # noqa: E402
from core.crypto_store import CryptoStore  # noqa: E402
from core.migrator import SKIP_DIR_NAMES, backup_vault  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def qapp() -> Any:
    """整个模块共用一个 QApplication（offscreen）。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """一个"像真的"的数据目录：核心密文 + 一个大模型 + 运行时 + 日志 + 缓存。"""
    root = tmp_path / "SGMS"
    root.mkdir()
    (root / "users.json").write_text('{"users": []}', encoding="utf-8")
    (root / "app.salt").write_bytes(b"0123456789abcdef01234567")
    user = root / "u_test"
    user.mkdir()
    (user / "archive.json.enc").write_bytes(b"cipher")
    (user / "grade_history.json.enc").write_bytes(b"cipher2")
    # ★ u_<uid>/cache/ 里的聊天历史**不可重建**，备份必须带上（v1.9.3 修的坑）：
    #   它和顶层那个 cache/ 同名，但一个是用户数据、一个是可丢的缓存。
    (user / "cache").mkdir()
    (user / "cache" / "chat_history_1.json.enc").write_bytes(b"chat-cipher")
    for name in ("models", "runtime", "logs", "cache"):
        folder = root / name
        folder.mkdir()
        (folder / "payload.bin").write_bytes(b"x" * 64)
    return root


@pytest.fixture()
def store(tmp_path: Path) -> CryptoStore:
    """注册好一个账户的临时库（``save_user_json`` 要求账户存在）。"""
    st = CryptoStore(str(tmp_path / "store_root"))
    st.register("tester", "pw-123456")
    return st


@pytest.fixture()
def no_modals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """把所有模态弹窗换成记录器，返回 ``[(标题, 正文), …]``。"""
    seen: list[tuple[str, str]] = []

    def fake_critical(_parent: Any, title: str, text: str, *a: Any, **k: Any) -> Any:
        seen.append((str(title), str(text)))
        return QMessageBox.StandardButton.Ok

    def fake_warning(_parent: Any, title: str, text: str, *a: Any, **k: Any) -> Any:
        seen.append((str(title), str(text)))
        return QMessageBox.StandardButton.Ok

    def fake_information(_parent: Any, title: str, text: str, *a: Any, **k: Any) -> Any:
        seen.append((str(title), str(text)))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "critical", staticmethod(fake_critical))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake_information))
    return seen


# ---------------------------------------------------------------------------
# 1. 日志
# ---------------------------------------------------------------------------


def test_applog写入后能读回(tmp_path: Path) -> None:
    root = tmp_path / "SGMS"
    root.mkdir()
    applog.write("boot", "第一行", root=str(root))
    applog.write("login", "第二行", root=str(root))

    log_file = Path(applog.log_path(str(root)))
    assert log_file.exists(), "日志文件没落盘"
    body = log_file.read_text(encoding="utf-8")
    assert "第一行" in body and "第二行" in body
    assert "boot" in body and "login" in body

    tail = applog.tail(10, root=str(root))
    assert "第二行" in tail


def test_applog目录不可写时绝不抛异常(tmp_path: Path) -> None:
    """拿一个**文件**当数据根目录 —— 写日志必然失败，但函数不许炸。"""
    bogus = tmp_path / "not_a_dir"
    bogus.write_text("i am a file", encoding="utf-8")

    applog.write("boot", "这条写不进去", root=str(bogus))  # 不抛即可
    assert applog.tail(5, root=str(bogus)) == ""


def test_applog超过上限会滚动(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "SGMS"
    root.mkdir()
    monkeypatch.setattr(applog, "MAX_BYTES", 200)
    for index in range(12):
        applog.write("boot", f"第 {index} 条" + "y" * 20, root=str(root))

    log_file = Path(applog.log_path(str(root)))
    assert log_file.stat().st_size <= 200 + 200, "滚动后文件仍然过大"
    assert log_file.with_suffix(log_file.suffix + ".1").exists(), "滚动备份没生成"


# ---------------------------------------------------------------------------
# 2. 迁移备份
# ---------------------------------------------------------------------------


def test_备份跳过可重建的大目录(vault: Path) -> None:
    """models/runtime/logs/cache 不进备份；核心数据一个不少。"""
    target = backup_vault(vault, "1.9.1")

    assert target.is_dir()
    for name in SKIP_DIR_NAMES:
        assert not (target / name).exists(), f"{name} 不该进备份"

    assert (target / "users.json").exists()
    assert (target / "app.salt").exists()
    assert (target / "u_test" / "archive.json.enc").read_bytes() == b"cipher"
    assert (target / "u_test" / "grade_history.json.enc").exists()


def test_备份不跳过用户目录里的cache(vault: Path) -> None:
    """★★ ``u_<uid>/cache/`` 是同名目录，但里面是**删了就回不来**的聊天历史。

    v1.9.3 之前 ``_skip`` 按名字在任意深度跳 ``cache``，于是每次迁移的备份里都
    少了 AI 辅导的对话记录。判据：顶层 ``cache/`` 仍然不抄，``u_test/cache/`` 必须抄。
    """
    target = backup_vault(vault, "1.9.1")

    assert not (target / "cache").exists(), "顶层的可重建缓存目录不该进备份"
    chat = target / "u_test" / "cache" / "chat_history_1.json.enc"
    assert chat.is_file(), "u_<uid>/cache 里的聊天历史必须进备份"
    assert chat.read_bytes() == b"chat-cipher"


def test_备份仍然拒绝跳过核心数据(vault: Path) -> None:
    """``users.json`` 缺了就说明备份不可信 —— 必须抛，不能"凑合"。"""
    (vault / "users.json").unlink()
    target = backup_vault(vault, "1.9.1")
    # 没有 users.json 时不报错（原目录本来就没有），但备份里也不能凭空多出来
    assert not (target / "users.json").exists()
    assert (target / "app.salt").exists()


# ---------------------------------------------------------------------------
# 3. 登录：密码对但下一步炸了
# ---------------------------------------------------------------------------


def _login_window(store: CryptoStore, root: Path) -> Any:
    from ui.login_window import LoginWindow

    win = LoginWindow(store=store, root=str(root))
    win._user_box.setCurrentText("tester")
    win._pass_edit.setText("pw-123456")
    return win


def test_密码通过后抛异常也要兜住(
    qapp: Any,
    store: CryptoStore,
    vault: Path,
    no_modals: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现场事故：密码对了，界面永远停在"PBKDF2 派生中"。"""
    from ui import login_window as lw

    def boom(_self: Any, _username: str) -> None:
        raise RuntimeError("磁盘写保护")

    monkeypatch.setattr(lw.LoginWindow, "_handle_remember", boom)
    win = _login_window(store, vault)
    fired: list[str] = []
    win.logged_in.connect(lambda name: fired.append(name))

    win._do_login()

    assert fired == [], "出错了不该进主界面"
    assert win._leaving is False, "失败之后必须允许再点一次"
    assert "进入主界面失败" in win._login_status.text()
    assert any("进不去主界面" in title for title, _ in no_modals), "没有告诉用户原因"
    # 日志里要留下 traceback，否则 windowed exe 上这行等于没写
    log_body = Path(applog.log_path(str(vault))).read_text(encoding="utf-8")
    assert "磁盘写保护" in log_body


def test_密码错误仍然正常报错(
    qapp: Any,
    store: CryptoStore,
    vault: Path,
    no_modals: list[tuple[str, str]],
) -> None:
    """回归：错误密码的既有行为不许被兜底逻辑改坏。"""
    win = _login_window(store, vault)
    win._pass_edit.setText("wrong-password")
    fired: list[str] = []
    win.logged_in.connect(lambda name: fired.append(name))

    win._do_login()

    assert fired == []
    assert "密码错误" in win._login_status.text()
    assert win._leaving is False


def test_登录成功会发出信号(
    qapp: Any,
    store: CryptoStore,
    vault: Path,
    no_modals: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ui import login_window as lw

    # 真的淡出动画在 offscreen 下不会跑，直接**同步**回调，等价于"动画跑完"
    monkeypatch.setattr(lw, "window_fade_out", lambda _w, _ms, on_done: on_done())
    win = _login_window(store, vault)
    fired: list[str] = []
    win.logged_in.connect(lambda name: fired.append(name))

    win._do_login()

    assert fired == ["tester"]
    assert win._left is True


# ---------------------------------------------------------------------------
# 4. 登录看门狗
# ---------------------------------------------------------------------------


def test_看门狗在没有动画时补发信号(
    qapp: Any,
    store: CryptoStore,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动画回调永远不回来时，看门狗必须把 ``logged_in`` 补上。"""
    from ui import login_window as lw

    monkeypatch.setattr(lw, "window_fade_out", lambda *_a, **_k: None)
    win = _login_window(store, vault)
    fired: list[str] = []
    win.logged_in.connect(lambda name: fired.append(name))

    win._leave("tester")
    assert fired == [], "淡出回调还没回来，此时不该发信号"

    win._force_leave()
    assert fired == ["tester"], "看门狗没补发信号"

    win._force_leave()
    assert fired == ["tester"], "看门狗不许重复发信号"


def test_看门狗在正常路径上不重复发(qapp: Any, store: CryptoStore, vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ui import login_window as lw

    def immediate(_win: Any, _ms: int, on_done: Any) -> None:
        on_done()

    monkeypatch.setattr(lw, "window_fade_out", immediate)
    win = _login_window(store, vault)
    fired: list[str] = []
    win.logged_in.connect(lambda name: fired.append(name))

    win._leave("tester")
    assert fired == ["tester"]

    win._force_leave()  # 已经走了，看门狗必须装死
    assert fired == ["tester"]
