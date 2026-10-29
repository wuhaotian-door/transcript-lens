"""core.ai.runtime_setup 与 ui.widgets.backend_setup 的回归测试（v1.9.2）。

全部用 pytest + monkeypatch + tmp_path，不联网、不安装任何包：

* 安装线程的三种结果（成功 / 失败 / 取消 / 超时）都靠伪造的 subprocess.Popen 驱动；
* find_system_python 的候选与探测全部打桩，绝不真的去拉解释器；
* UI 部分用 offscreen 的 QApplication 构造卡片，并桩掉所有 QMessageBox。
"""

from __future__ import annotations

import os

# 必须在 import PySide6 之前设置，否则没有显示器时开不了 QApplication。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib.util
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import core.ai.runtime_setup as rt


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeProcess:
    """最小可用的子进程替身：只实现安装线程真正会用到的那几个成员。"""

    def __init__(
        self,
        returncode: int | None = 0,
        lines: list[str] | None = None,
        block: bool = False,
    ) -> None:
        self._lines = list(lines or [])
        self._returncode = returncode
        self._exited = not block
        self.stdout = iter(self._lines)
        self.terminated = False
        self.killed = False
        self.command: list[str] | None = None
        self.kwargs: dict = {}
        self._cond = threading.Condition()

    @property
    def returncode(self) -> int | None:
        """与真实 subprocess.Popen 保持一致：运行中为 None，退出后才是返回码。"""
        return self._returncode if self._exited else None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int | None:
        with self._cond:
            while not self._exited:
                self._cond.wait(0.05)
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        with self._cond:
            self._exited = True
            self._cond.notify_all()

    def kill(self) -> None:
        self.killed = True
        with self._cond:
            if self._returncode is None:
                self._returncode = 1
            self._exited = True
            self._cond.notify_all()


@pytest.fixture()
def isolated_sys_path(monkeypatch):
    """把 sys.path 换成一个干净列表，测完由 monkeypatch 原样恢复。"""
    original = ["C:\\existing\\lib", "C:\\another\\lib"]
    monkeypatch.setattr(sys, "path", list(original))
    return original


@pytest.fixture()
def no_system_python(monkeypatch, tmp_path):
    """让 find_system_python() 一条候选都找不到。"""
    monkeypatch.delenv("SGMS_PYTHON", raising=False)
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing-localappdata"))
    monkeypatch.setattr(rt, "_iter_system_drive_roots", lambda: iter(()))


@pytest.fixture()
def qapp():
    """offscreen 的 QApplication（复用同一个实例，避免重复创建）。"""
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------


def test_constants_match_contract():
    assert rt.ENGINE_MODULE == "llama_cpp"
    assert rt.RUNTIME_DIR_NAME == "runtime/site"
    assert rt.PIP_PACKAGE == "llama-cpp-python"
    assert rt.PIP_EXTRA_INDEX == "https://mirrors.aliyun.com/pypi/simple/"
    assert rt.INSTALL_TIMEOUT == 600
    assert rt.PYTHON_PROBE_TIMEOUT == 20
    assert rt.INSTALL_OK_MESSAGE == "推理后端安装成功"
    assert rt.INSTALL_CANCELLED_MESSAGE == "已取消安装"
    assert rt.BACKEND_READY_MESSAGE == "推理后端已就绪"


def test_engine_module_matches_inference():
    from core.ai.inference import ENGINE_MODULE

    assert rt.ENGINE_MODULE == ENGINE_MODULE


def test_runtime_dir_uses_crypto_store_root(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    assert rt.runtime_dir() == tmp_path / "runtime" / "site"
    # 只算路径，不建目录
    assert not rt.runtime_dir().exists()


# ---------------------------------------------------------------------------
# ensure_runtime_on_path
# ---------------------------------------------------------------------------


def test_ensure_runtime_on_path_inserts_at_front(monkeypatch, tmp_path, isolated_sys_path):
    target = tmp_path / "runtime" / "site"
    target.mkdir(parents=True)
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)

    assert rt.ensure_runtime_on_path() is True
    assert sys.path[0] == str(target)
    assert sys.path[1:] == isolated_sys_path


def test_ensure_runtime_on_path_is_idempotent(monkeypatch, tmp_path, isolated_sys_path):
    target = tmp_path / "runtime" / "site"
    target.mkdir(parents=True)
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)

    assert rt.ensure_runtime_on_path() is True
    after_first = list(sys.path)
    assert rt.ensure_runtime_on_path() is True
    assert sys.path == after_first
    assert sum(1 for entry in sys.path if entry == str(target)) == 1


def test_ensure_runtime_on_path_does_not_reorder_existing_entry(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "site"
    target.mkdir(parents=True)
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    before = ["C:\\first", str(target), "C:\\last"]
    monkeypatch.setattr(sys, "path", list(before))

    assert rt.ensure_runtime_on_path() is True
    assert sys.path == before


def test_ensure_runtime_on_path_missing_dir_changes_nothing(monkeypatch, tmp_path, isolated_sys_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)  # runtime 目录不存在
    before = list(sys.path)

    assert rt.ensure_runtime_on_path() is False
    assert sys.path == before


def test_ensure_runtime_on_path_accepts_explicit_directory(tmp_path, isolated_sys_path):
    target = tmp_path / "explicit" / "site"
    target.mkdir(parents=True)

    assert rt.ensure_runtime_on_path(target) is True
    assert sys.path[0] == str(target)


# ---------------------------------------------------------------------------
# pip 命令
# ---------------------------------------------------------------------------


def test_pip_argv_targets_package_and_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    argv = rt.pip_argv("C:\\Python313\\python.exe")

    assert argv[0] == "C:\\Python313\\python.exe"
    assert argv[1:4] == ["-m", "pip", "install"]
    assert "--target" in argv
    assert str(tmp_path / "runtime" / "site") in argv
    assert argv[-1] == rt.PIP_PACKAGE
    assert "--disable-pip-version-check" in argv
    assert "--no-input" in argv


def test_pip_argv_extra_index_switch(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)

    with_index = rt.pip_argv("python.exe", extra_index=True)
    assert "--extra-index-url" in with_index
    assert rt.PIP_EXTRA_INDEX in with_index
    assert with_index.index("--extra-index-url") < with_index.index(rt.PIP_PACKAGE)

    without_index = rt.pip_argv("python.exe", extra_index=False)
    assert "--extra-index-url" not in without_index
    assert rt.PIP_EXTRA_INDEX not in without_index
    assert without_index[-1] == rt.PIP_PACKAGE


def test_pip_argv_defaults_to_system_python(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    monkeypatch.setattr(rt, "find_system_python", lambda: "C:\\found\\python.exe")
    assert rt.pip_argv()[0] == "C:\\found\\python.exe"


def test_install_command_text_is_single_line_with_package(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    text = rt.install_command_text("C:\\Python313\\python.exe")

    assert rt.PIP_PACKAGE in text
    assert rt.PIP_EXTRA_INDEX in text
    assert "\n" not in text
    assert "-m pip install" in text


def test_install_command_text_quotes_paths_with_spaces(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    text = rt.install_command_text("C:\\Program Files\\Python313\\python.exe")
    assert text.startswith('"C:\\Program Files\\Python313\\python.exe"')


# ---------------------------------------------------------------------------
# find_system_python
# ---------------------------------------------------------------------------


def test_find_system_python_returns_none_without_candidates(no_system_python):
    assert rt.find_system_python() is None


def test_find_system_python_skips_candidate_without_pip(monkeypatch, tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(rt, "_iter_candidate_pythons", lambda: iter([str(exe)]))
    probed: list[str] = []

    def fake_probe(path, timeout=rt.PYTHON_PROBE_TIMEOUT):
        probed.append(path)
        return False

    monkeypatch.setattr(rt, "_probe_python", fake_probe)
    assert rt.find_system_python() is None
    assert probed == [str(exe)]


def test_find_system_python_returns_first_working_candidate(monkeypatch, tmp_path):
    first = tmp_path / "first.exe"
    second = tmp_path / "second.exe"
    monkeypatch.setattr(rt, "_iter_candidate_pythons", lambda: iter([str(first), str(second)]))
    monkeypatch.setattr(rt, "_probe_python", lambda path, timeout=None: path == str(second))
    assert rt.find_system_python() == str(second)


def test_probe_python_runs_import_pip(monkeypatch):
    captured: dict = {}

    class FakeProbe:
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

        def wait(self, timeout=None):
            captured["wait_timeout"] = timeout
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(rt.subprocess, "Popen", FakeProbe)
    assert rt._probe_python("C:\\Python313\\python.exe") is True
    assert captured["argv"][0] == "C:\\Python313\\python.exe"
    assert captured["argv"][1] == "-c"
    assert "import pip" in captured["argv"][2]
    assert captured["wait_timeout"] == rt.PYTHON_PROBE_TIMEOUT


# ---------------------------------------------------------------------------
# is_backend_available / backend_status
# ---------------------------------------------------------------------------


def test_is_backend_available_uses_find_spec(monkeypatch):
    monkeypatch.setattr(rt, "ensure_runtime_on_path", lambda *a, **k: True)
    monkeypatch.delitem(sys.modules, rt.ENGINE_MODULE, raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert rt.is_backend_available() is True


def test_is_backend_available_false_when_spec_missing(monkeypatch):
    monkeypatch.setattr(rt, "ensure_runtime_on_path", lambda *a, **k: True)
    monkeypatch.delitem(sys.modules, rt.ENGINE_MODULE, raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert rt.is_backend_available() is False


def test_is_backend_available_swallows_find_spec_errors(monkeypatch):
    monkeypatch.setattr(rt, "ensure_runtime_on_path", lambda *a, **k: True)
    monkeypatch.delitem(sys.modules, rt.ENGINE_MODULE, raising=False)

    def boom(name):
        raise ValueError("bad spec")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert rt.is_backend_available() is False


def test_is_backend_available_shortcuts_loaded_module(monkeypatch):
    monkeypatch.setattr(rt, "ensure_runtime_on_path", lambda *a, **k: True)

    def boom(name):  # pragma: no cover - 不该被调用
        raise AssertionError("已加载的模块不该再查 find_spec")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    monkeypatch.setitem(sys.modules, rt.ENGINE_MODULE, object())
    assert rt.is_backend_available() is True


def test_backend_status_ready(monkeypatch):
    monkeypatch.setattr(rt, "is_backend_available", lambda: True)
    assert rt.backend_status() == (True, "推理后端已就绪")


def test_backend_status_not_installed_with_python(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    monkeypatch.setattr(rt, "is_backend_available", lambda: False)
    monkeypatch.setattr(rt, "find_system_python", lambda: "C:\\Python313\\python.exe")

    available, message = rt.backend_status()
    assert available is False
    assert "还没有安装" in message
    assert "可以一键安装" in message
    assert "**" not in message and "`" not in message


def test_backend_status_half_installed_without_python(monkeypatch, tmp_path):
    target = tmp_path / "runtime" / "site"
    target.mkdir(parents=True)
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    monkeypatch.setattr(rt, "is_backend_available", lambda: False)
    monkeypatch.setattr(rt, "find_system_python", lambda: None)

    available, message = rt.backend_status()
    assert available is False
    assert str(target) in message
    assert "没有找到" in message
    assert "**" not in message and "`" not in message


# ---------------------------------------------------------------------------
# InstallResult 与安装线程（伪造 Popen，不真跑 pip）
# ---------------------------------------------------------------------------


def test_install_result_defaults():
    result = rt.InstallResult(ok=True, message="好")
    assert result.lines == []
    assert result.command == ""
    other = rt.InstallResult(ok=False, message="坏")
    result.lines.append("x")
    assert other.lines == []


def test_install_thread_without_python_reports_command(no_system_python):
    thread = rt.BackendInstallThread()
    thread.run()

    assert thread.result is not None
    assert thread.result.ok is False
    assert "命令" in thread.result.message
    assert rt.PIP_PACKAGE in thread.result.command


def _patch_popen(monkeypatch, fake):
    def fake_popen(argv, **kwargs):
        fake.command = argv
        fake.kwargs = kwargs
        return fake

    monkeypatch.setattr(rt.subprocess, "Popen", fake_popen)


def test_install_thread_success(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    fake = FakeProcess(returncode=0, lines=["准备安装", "安装完成"])
    _patch_popen(monkeypatch, fake)
    ensured: list[bool] = []
    monkeypatch.setattr(rt, "ensure_runtime_on_path", lambda *a, **k: ensured.append(True) or True)

    shown: list[str] = []
    thread = rt.BackendInstallThread(on_line=shown.append, python="C:\\Python313\\python.exe")
    thread.run()

    assert thread.result is not None
    assert thread.result.ok is True
    assert thread.result.message == "推理后端安装成功"
    assert shown == ["准备安装", "安装完成"]
    assert thread.result.lines == ["准备安装", "安装完成"]
    assert ensured == [True]
    assert rt.PIP_PACKAGE in thread.result.command

    # Popen 参数：合并 stdout/stderr、文本模式、utf-8、绝不弹黑框
    assert fake.kwargs["stdout"] == subprocess.PIPE
    assert fake.kwargs["stderr"] == subprocess.STDOUT
    assert fake.kwargs["text"] is True
    assert fake.kwargs["encoding"] == "utf-8"
    assert fake.kwargs["errors"] == "replace"
    assert fake.kwargs["creationflags"] == rt._CREATE_NO_WINDOW
    assert fake.command[0] == "C:\\Python313\\python.exe"


def test_install_thread_failure_keeps_tail_of_output(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    fake = FakeProcess(
        returncode=1,
        lines=["first line", "second line", "third line", "fourth line"],
    )
    _patch_popen(monkeypatch, fake)
    thread = rt.BackendInstallThread(python="C:\\Python313\\python.exe")
    thread.run()

    assert thread.result is not None
    assert thread.result.ok is False
    message = thread.result.message
    assert "返回码 1" in message
    assert "second line" in message
    assert "third line" in message
    assert "fourth line" in message
    assert "first line" not in message
    assert rt.PIP_PACKAGE in thread.result.command


def test_install_thread_cancel(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    fake = FakeProcess(returncode=None, lines=["step-1"])
    _patch_popen(monkeypatch, fake)

    holder: dict = {}

    def on_line(line):
        if "step-1" in line:
            holder["thread"].cancel()

    thread = rt.BackendInstallThread(on_line=on_line, python="C:\\Python313\\python.exe")
    holder["thread"] = thread
    thread.run()

    assert thread.result is not None
    assert thread.result.ok is False
    assert thread.result.message == "已取消安装"
    assert fake.terminated is True


def test_install_thread_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)
    fake = FakeProcess(returncode=0, block=True)
    _patch_popen(monkeypatch, fake)
    thread = rt.BackendInstallThread(python="C:\\Python313\\python.exe", timeout=0.2)
    thread.run()

    assert thread.result is not None
    assert thread.result.ok is False
    assert "超时" in thread.result.message
    assert fake.terminated is True
    assert fake.killed is True


def test_install_thread_popen_oserror_is_caught(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "default_root", lambda: tmp_path)

    def boom(argv, **kwargs):
        raise OSError("拒绝访问")

    monkeypatch.setattr(rt.subprocess, "Popen", boom)
    thread = rt.BackendInstallThread(python="C:\\Python313\\python.exe")
    thread.run()

    assert thread.result is not None
    assert thread.result.ok is False
    assert "拒绝访问" in thread.result.message


# ---------------------------------------------------------------------------
# 源码卫生
# ---------------------------------------------------------------------------


def test_runtime_setup_has_no_qt_or_http_imports():
    source = Path(rt.__file__).read_text(encoding="utf-8")
    banned = (
        "pyside6",
        "pyqt5",
        "pyqt6",
        "requests",
        "httpx",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "http.client",
        "socket",
        "openai",
        "anthropic",
    )
    lowered = source.lower()
    for name in banned:
        assert f"import {name}" not in lowered
        assert f"from {name}" not in lowered


def test_sources_are_utf8_without_bom():
    data = Path(rt.__file__).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("utf-8")


# ---------------------------------------------------------------------------
# UI 卡片
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend_module():
    return pytest.importorskip("ui.widgets.backend_setup")


def _stub_status(monkeypatch, available, message, python="C:\\fake\\python.exe"):
    monkeypatch.setattr(rt, "backend_status", lambda: (available, message))
    monkeypatch.setattr(rt, "find_system_python", lambda: python)


def test_card_ready_state(monkeypatch, qapp, backend_module):
    _stub_status(monkeypatch, True, "推理后端已就绪", python=None)
    card = backend_module.BackendSetupCard()

    assert card._title.text() == "本地推理后端"
    assert card._status.text() == "推理后端已就绪"
    assert card._ready_badge.isVisibleTo(card)
    assert card._install_button.isHidden()
    assert card._command_row.isHidden()


def test_card_installable_state(monkeypatch, qapp, backend_module):
    _stub_status(
        monkeypatch,
        False,
        "程序目录里还没有安装推理后端。本机找到了可用的系统 Python，可以一键安装。",
    )
    card = backend_module.BackendSetupCard()

    assert card._status.text().startswith("程序目录里还没有安装推理后端")
    assert card._install_button.isVisibleTo(card)
    assert card._install_button.isEnabled()
    assert card._cancel_button.isHidden()
    assert card._command_row.isHidden()
    assert card._ready_badge.isHidden()


def test_card_missing_python_state_shows_command(monkeypatch, qapp, backend_module):
    _stub_status(
        monkeypatch,
        False,
        "程序目录里还没有安装推理后端。本机没有找到可用的系统 Python，请复制下面的命令在命令行里执行。",
        python=None,
    )
    card = backend_module.BackendSetupCard()

    assert card._install_button.isHidden()
    assert card._command_row.isVisibleTo(card)
    assert rt.PIP_PACKAGE in card._command_label.text()
    # ★ v1.9.2 终版：命令用 QLabel（不用只读 QLineEdit，后者会被 findChildren 当成
    #   "输入框"误导档案页测试），所以验的是"可选可复制"不再是 isReadOnly。
    from PySide6.QtCore import Qt  # noqa: PLC0415 - 只这一条断言用得到

    assert card._command_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse


def test_card_copy_command_writes_clipboard(monkeypatch, qapp, backend_module):
    _stub_status(
        monkeypatch,
        False,
        "程序目录里还没有安装推理后端。本机没有找到可用的系统 Python，请复制下面的命令在命令行里执行。",
        python=None,
    )
    card = backend_module.BackendSetupCard()
    card._copy_button.click()

    assert qapp.clipboard().text() == card._command_label.text()
    assert card._copy_hint.text() == backend_module.COPY_HINT_TEXT
    assert card._copy_hint.isVisibleTo(card)


def test_card_install_requires_confirmation(monkeypatch, qapp, backend_module):
    _stub_status(
        monkeypatch,
        False,
        "程序目录里还没有安装推理后端。本机找到了可用的系统 Python，可以一键安装。",
    )
    calls: list[str] = []

    def refuse(*args, **kwargs):
        calls.append("question")
        return backend_module.QMessageBox.StandardButton.No

    monkeypatch.setattr(backend_module.QMessageBox, "question", refuse)
    card = backend_module.BackendSetupCard()
    card._install_button.click()

    assert calls == ["question"]  # 确认框被问过一次
    assert card._thread is None  # 点了「否」就绝不该启动安装
    assert "llama-cpp-python" in backend_module.INSTALL_QUESTION
    assert "60MB" in backend_module.INSTALL_QUESTION


def test_card_install_success_emits_ready(monkeypatch, qapp, backend_module):
    state = {"installed": False}

    def fake_status():
        if state["installed"]:
            return True, "推理后端已就绪"
        return False, "程序目录里还没有安装推理后端。本机找到了可用的系统 Python，可以一键安装。"

    monkeypatch.setattr(rt, "backend_status", fake_status)
    monkeypatch.setattr(rt, "find_system_python", lambda: "C:\\fake\\python.exe")
    monkeypatch.setattr(
        backend_module.QMessageBox,
        "question",
        lambda *args, **kwargs: backend_module.QMessageBox.StandardButton.Yes,
    )

    class FakeThread:
        def __init__(self, *, on_line=None, python=None, timeout=None):
            self.on_line = on_line
            self.python = python
            self.result = rt.InstallResult(
                ok=True,
                message=rt.INSTALL_OK_MESSAGE,
                lines=["done"],
                command="python -m pip install llama-cpp-python",
            )

        def start(self):
            state["installed"] = True
            if self.on_line is not None:
                self.on_line("done")

        def is_alive(self):
            return False

        def cancel(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(rt, "BackendInstallThread", FakeThread)

    card = backend_module.BackendSetupCard()
    hits: list[bool] = []
    card.ready.connect(lambda: hits.append(True))
    card._install_button.click()

    assert card._thread is not None  # 线程已启动
    card._poll_install()  # 主线程轮询：取输出 + 发现线程已结束
    qapp.processEvents()  # 执行 QTimer.singleShot(0, _finish_install)
    if card._thread is not None:  # 个别平台 processEvents 不派发 0ms 定时器
        card._finish_install()

    assert hits == [True]
    assert card._status.text() == "推理后端已就绪"
    assert card._install_button.isHidden()


def test_card_labels_have_no_markdown(monkeypatch, qapp, backend_module):
    _stub_status(
        monkeypatch,
        False,
        "程序目录里还没有安装推理后端。本机没有找到可用的系统 Python，请复制下面的命令在命令行里执行。",
        python=None,
    )
    card = backend_module.BackendSetupCard()

    texts = [
        card._title.text(),
        card._status.text(),
        card._result.text(),
        card._ready_badge.text(),
        card._copy_hint.text(),
        card._command_label.text(),
        backend_module.INSTALL_QUESTION,
        backend_module.COPY_HINT_TEXT,
    ]
    for text in texts:
        assert "**" not in text
        assert "`" not in text
        assert "__" not in text


def test_backend_setup_module_has_no_network_imports(qapp, backend_module):
    source = Path(backend_module.__file__).read_text(encoding="utf-8").lower()
    for name in ("requests", "httpx", "urllib.request", "socket", "http.client"):
        assert f"import {name}" not in source
        assert f"from {name}" not in source

# ---------------------------------------------------------------------------
# 阻塞版 install()（脚本 / 测试入口）—— 补 v1.9.2 任务书的 install() 契约
# ---------------------------------------------------------------------------


class _FakeInstallThread:
    """``BackendInstallThread`` 的替身：不真的起进程，结果由 ``script`` 造。"""

    script = None          # callable(fake) -> InstallResult
    alive = False

    def __init__(self, *, on_line=None, python=None, timeout=rt.INSTALL_TIMEOUT):
        self.on_line = on_line
        self.python = python
        self.timeout = timeout
        self.result = None
        self.cancel_called = False
        self.joined = False

    def start(self):
        if _FakeInstallThread.script is not None:
            self.result = _FakeInstallThread.script(self)

    def is_alive(self):
        return _FakeInstallThread.alive and not self.cancel_called

    def join(self):
        self.joined = True

    def cancel(self):
        self.cancel_called = True


@pytest.fixture
def fake_thread(monkeypatch):
    monkeypatch.setattr(rt, "BackendInstallThread", _FakeInstallThread)
    _FakeInstallThread.script = None
    _FakeInstallThread.alive = False
    yield _FakeInstallThread
    _FakeInstallThread.script = None
    _FakeInstallThread.alive = False


def test_install_wrapper_returns_success(fake_thread, monkeypatch):
    fake_thread.script = lambda fake: rt.InstallResult(
        ok=True, message=rt.INSTALL_OK_MESSAGE, lines=["ok"], command="python -m pip")
    monkeypatch.setattr(rt, "is_backend_available", lambda: True)

    result = rt.install()

    assert result.ok is True
    assert result.cancelled is False
    assert result.reason == rt.INSTALL_OK_MESSAGE
    assert result.command == "python -m pip"


def test_install_wrapper_rechecks_after_success(fake_thread, monkeypatch):
    """返回码 0 但复核不过 —— 必须判失败，不能报"装好了"。"""
    fake_thread.script = lambda fake: rt.InstallResult(
        ok=True, message=rt.INSTALL_OK_MESSAGE, lines=["x"], command="cmd")
    monkeypatch.setattr(rt, "is_backend_available", lambda: False)

    result = rt.install()

    assert result.ok is False
    assert result.reason.startswith("安装命令执行完了，但导入 llama-cpp-python 仍然失败")


def test_install_wrapper_cancel_sets_cancelled(fake_thread, monkeypatch):
    fake_thread.alive = True
    cancel = threading.Event()
    cancel.set()

    result = rt.install(cancel=cancel)

    assert result.ok is False
    assert result.cancelled is True
    assert result.reason == rt.INSTALL_CANCELLED_MESSAGE
    assert cancel.is_set()


def test_install_wrapper_failure_reason_contains_command(fake_thread, monkeypatch):
    command = "python -m pip install --target X llama-cpp-python"
    fake_thread.script = lambda fake: rt.InstallResult(
        ok=False, message="推理后端安装失败", lines=["boom"], command=command)
    monkeypatch.setattr(rt, "is_backend_available", lambda: False)

    result = rt.install()

    assert result.ok is False
    assert command in result.reason
    assert "boom" in result.output
    assert result.output == "boom"


def test_install_result_output_is_tail_limited():
    result = rt.InstallResult(ok=False, message="x", lines=["a" * (rt.OUTPUT_TAIL_MAX + 50)])
    assert len(result.output) == rt.OUTPUT_TAIL_MAX
