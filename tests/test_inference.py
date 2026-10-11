"""本地推理引擎 + 「AI 谈话助手」的回归测试（v1.9 第二阶段 C 任务）。

两条纪律（照 ``tests/test_model_registry.py`` 的老规矩）：

* **绝不碰真实用户目录**：落盘一律走 ``tmp_path`` 上的临时 ``CryptoStore``，
  模型目录一律显式传 ``dest_dir``；
* **不打网络**：引擎是注入的假模块 / 假引擎。

界面部分强制 ``QT_QPA_PLATFORM=offscreen``（没有显示器也要能跑），
并且**断言一个模态弹窗都没弹过** —— 弹窗会把自动化探针的看门狗跑满。

覆盖的硬约束（AGENTS.md 与任务书）：

1. 导入期**不 import** ``llama_cpp``（子进程真跑一遍 + AST 静态检查）；
2. ``core/ai/inference.py`` **零 HTTP import**（照抄 main.py 的正则自查）；
3. 没装引擎时 ``engine_available()`` 给 ``(False, 中文说明)``，
   人话里不许出现包管理指令；
4. 未加载就 ``generate`` / ``stream`` 一律抛 ``InferenceUnavailable``，
   **绝不静默返回空串**；
5. 界面：引擎不可用 / 没模型时窗口照开、输入区禁用、不进主线程阻塞；
6. 谈话记录只落 ``.enc`` 密文，磁盘上扫不到明文。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

# ★ 必须在导入 PySide6 **之前**设置，否则 offscreen 不生效
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 正常跑 pytest 已有
    sys.path.insert(0, str(REPO_ROOT))

from core.ai import inference as inf
from core.ai.inference import (  # noqa: E402
    DEFAULT_N_CTX,
    ENGINE_READY_MESSAGE,
    NO_ENGINE_MESSAGE,
    InferenceUnavailable,
    LocalModel,
    LocalModelEngine,
    engine_available,
    friendly_error,
    list_local_models,
)
from core.ai.model_downloader import MODEL_SUFFIXES  # noqa: E402

INFERENCE_PATH = REPO_ROOT / "core" / "ai" / "inference.py"
CHAT_PATH = REPO_ROOT / "ui" / "chat_dialog.py"

#: main.py ``_network_policy_report`` 用的那套（只认行首的真 import 语句）
_BANNED_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(?:requests|httpx|urllib\.request|urllib3|aiohttp|http\.client|socket|"
    r"openai|anthropic|websocket)(?:[\s.]|$)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# 假引擎 / 假模块
# ---------------------------------------------------------------------------


class _FakeLlama:
    """顶替 ``llama_cpp.Llama``：记录调用、返回可控结果。"""

    built = 0

    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        _FakeLlama.built += 1

    def __call__(self, prompt: str, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        if kwargs.get("stream"):
            return iter([{"choices": [{"text": piece}]} for piece in ("你", "好", "呀")])
        if prompt == "看不懂的结果":
            return {"choices": []}
        return {"choices": [{"text": "好的，我记下了。"}]}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def no_engine(monkeypatch):
    """模拟"本机没装引擎"：``sys.modules`` 里显式置 ``None``（Python 的"不存在"）。"""
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    return None


@pytest.fixture
def fake_module(monkeypatch):
    """注入一个假的 ``llama_cpp`` 模块（任务书要求的测法）。"""
    module = types.ModuleType("llama_cpp")
    module.Llama = _FakeLlama  # type: ignore[attr-defined]
    _FakeLlama.built = 0
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    return module


@pytest.fixture
def model_file(tmp_path):
    """一个**真的存在**的假模型文件（内容无所谓，load 之前会检查存在性）。"""
    path = tmp_path / "fake-model.gguf"
    path.write_bytes(b"GGUF" + b"\x00" * 64)
    return path


# ---------------------------------------------------------------------------
# 1. 导入期与静态闸门
# ---------------------------------------------------------------------------


def test_import_time_does_not_import_llama_cpp():
    """★ 硬约束：import 这个模块**不许**把 ``llama_cpp`` 拉进来。

    本机没装它，一旦导入期真去 import，``--selfcheck`` 和 pytest 会同时炸。
    用一个干净的子进程真跑一遍（比断言 ``sys.modules`` 更硬 —— pytest 进程里
    别的测试可能已经动过 ``sys.modules``）。
    """
    code = "import sys, core.ai.inference; print('llama_cpp' in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", proc.stdout


def test_source_has_no_top_level_llama_cpp_import():
    """AST 层再确认一遍：模块顶层没有任何 ``import llama_cpp``。"""
    tree = ast.parse(INFERENCE_PATH.read_text(encoding="utf-8"))
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert not [name for name in top_level if name.startswith("llama_cpp")], top_level
    assert not re.search(r"^\s*(?:import|from)\s+llama_cpp", INFERENCE_PATH.read_text("utf-8"), re.M)


def test_inference_has_zero_http_imports():
    """★ 硬闸门：``core/ai/*.py`` 零 HTTP import（main.py 会 FAIL 掉违规）。"""
    source = INFERENCE_PATH.read_text(encoding="utf-8")
    hits = _BANNED_IMPORT_RE.findall(source)
    assert hits == [], f"core/ai/inference.py 里出现了联网 import：{hits}"


def test_engine_module_name_is_a_constant():
    """模块名只在一个地方写死，别处一律用常量（免得改不干净）。"""
    source = INFERENCE_PATH.read_text(encoding="utf-8")
    assert inf.ENGINE_MODULE == "llama_cpp"
    assert source.count('"llama_cpp"') == 1


# ---------------------------------------------------------------------------
# 2. engine_available / 文案
# ---------------------------------------------------------------------------


def test_engine_available_without_engine(no_engine):
    """没装引擎：``(False, 中文说明)``，而且说明是人话。"""
    available, message = engine_available()
    assert available is False
    assert message == NO_ENGINE_MESSAGE
    assert "本地推理引擎" in message
    assert "AI 谈话助手" in message
    lowered = message.lower()
    for bad in ("pip", "conda", "install", "```"):
        assert bad not in lowered


def test_engine_available_with_engine(fake_module):
    """已经在 ``sys.modules`` 里的（真加载过 / 测试注入的）算可用。"""
    assert engine_available() == (True, ENGINE_READY_MESSAGE)


def test_friendly_error_strips_package_instructions():
    """错误文案翻人话：抹掉 pip / conda 那半句（AGENTS.md 第 27e 节）。"""
    raw = (
        "Missing optional dependency 'llama-cpp-python'. "
        "Use pip or conda to install llama-cpp-python."
    )
    text = friendly_error(ImportError(raw))
    lowered = text.lower()
    assert "pip" not in lowered and "conda" not in lowered
    assert "ImportError" in text
    assert "本地推理引擎出错了" in text


def test_friendly_error_accepts_plain_string():
    text = friendly_error("boom")
    assert "本地推理引擎出错了" in text and "boom" in text


# ---------------------------------------------------------------------------
# 3. LocalModelEngine：load / generate / stream / unload
# ---------------------------------------------------------------------------


def test_load_without_engine_raises(no_engine, model_file):
    """没装引擎：``load()`` 抛 ``InferenceUnavailable``，文案是那条中文说明。"""
    engine = LocalModelEngine(model_file)
    with pytest.raises(InferenceUnavailable) as excinfo:
        engine.load()
    assert str(excinfo.value) == NO_ENGINE_MESSAGE
    assert engine.is_loaded is False


def test_generate_before_load_raises(fake_module, model_file):
    """★ 未加载就生成 —— 必须抛，**绝不静默返回空串**。"""
    engine = LocalModelEngine(model_file)
    with pytest.raises(InferenceUnavailable):
        engine.generate("你好")
    with pytest.raises(InferenceUnavailable):
        engine.stream("你好")  # 调用那一刻就抛，不等到第一次 next()


def test_load_missing_file_raises(fake_module, tmp_path):
    engine = LocalModelEngine(tmp_path / "没有这个模型.gguf")
    with pytest.raises(InferenceUnavailable) as excinfo:
        engine.load()
    assert "没有这个模型.gguf" in str(excinfo.value)


def test_load_without_model_path_raises(fake_module):
    engine = LocalModelEngine(None)
    assert engine.model_path is None
    with pytest.raises(InferenceUnavailable):
        engine.load()


def test_load_generate_stream_unload(fake_module, model_file):
    """走一遍完整生命周期：加载 → 生成 → 流式 → 释放。"""
    engine = LocalModelEngine(model_file, n_ctx=1024, n_threads=4, chat_format="qwen")
    assert engine.is_loaded is False
    assert engine.model_path == model_file

    llama = engine.load()
    assert engine.is_loaded is True
    assert isinstance(llama, _FakeLlama)
    assert llama.kwargs["model_path"] == str(model_file)
    assert llama.kwargs["n_ctx"] == 1024
    assert llama.kwargs["n_threads"] == 4
    assert llama.kwargs["chat_format"] == "qwen"
    assert llama.kwargs["verbose"] is False

    # 幂等：再 load 一次不该再建一个实例
    assert engine.load() is llama
    assert _FakeLlama.built == 1

    # 一次性生成
    text = engine.generate("写个提纲", max_tokens=64, temperature=0.2)
    assert text == "好的，我记下了。"
    prompt, kwargs = llama.calls[-1]
    assert prompt == "写个提纲"
    assert kwargs["stream"] is False and kwargs["echo"] is False
    assert kwargs["max_tokens"] == 64 and kwargs["temperature"] == pytest.approx(0.2)

    # 停止串原样透传
    engine.generate("写个提纲", stop=["\n辅导员："])
    assert llama.calls[-1][1]["stop"] == ["\n辅导员："]

    # ★ 流式：**一块一块** yield（界面靠它做打字机效果）
    chunks = list(engine.stream("再来一段"))
    assert chunks == ["你", "好", "呀"]
    assert llama.calls[-1][1]["stream"] is True

    # 释放：引用清空、底层 close 调到、再生成抛异常
    engine.unload()
    assert engine.is_loaded is False
    assert llama.closed is True
    engine.unload()  # 反复调用是安全的
    with pytest.raises(InferenceUnavailable):
        engine.generate("你好")


def test_stream_is_lazy_generator(fake_module, model_file):
    """``stream`` 给的是生成器：取一块才产一块。"""
    engine = LocalModelEngine(model_file)
    engine.load()
    stream = engine.stream("x")
    assert hasattr(stream, "__next__")
    assert next(stream) == "你"
    assert list(stream) == ["好", "呀"]


def test_defaults_and_odd_results(fake_module, model_file):
    engine = LocalModelEngine(model_file)
    llama = engine.load()
    assert llama.kwargs["n_ctx"] == DEFAULT_N_CTX
    assert "n_threads" not in llama.kwargs  # 不给就交给引擎自己定
    assert "chat_format" not in llama.kwargs

    with pytest.raises(InferenceUnavailable):
        engine.generate("看不懂的结果")


def test_engine_errors_are_translated(fake_module, model_file, monkeypatch):
    """底层异常（原生扩展什么类型都可能有）一律翻成中文人话。"""
    engine = LocalModelEngine(model_file)
    engine.load()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("GGML_ASSERT: failed, please pip install llama-cpp-python")

    llama = engine._llama  # noqa: SLF001 - 测试故意换掉底层调用
    # 只换类上的 __call__：实例属性对 dunder 调用不生效（特殊方法查的是类型）
    monkeypatch.setattr(type(llama), "__call__", _boom)
    with pytest.raises(InferenceUnavailable) as excinfo:
        engine.generate("你好")
    assert "pip" not in str(excinfo.value).lower()


def test_load_failure_is_translated(fake_module, model_file, monkeypatch):
    """``Llama(...)`` 自己炸了也要翻成人话。"""

    def _boom(**_kwargs):
        raise OSError("cannot load model, run pip install llama-cpp-python")

    monkeypatch.setattr(fake_module, "Llama", _boom)
    engine = LocalModelEngine(model_file)
    with pytest.raises(InferenceUnavailable) as excinfo:
        engine.load()
    assert "pip" not in str(excinfo.value).lower()
    assert engine.is_loaded is False


def test_close_and_del_are_safe_half_initialized():
    """半初始化（``__new__`` 之后没跑 ``__init__``）也不许炸。"""
    half = LocalModelEngine.__new__(LocalModelEngine)
    half.unload()  # 不抛
    half.close()  # 不抛
    half.__del__()  # 不抛
    assert half.is_loaded is False
    assert half.model_path is None


def test_unload_swallows_close_errors(fake_module, model_file):
    engine = LocalModelEngine(model_file)
    llama = engine.load()

    def _boom():
        raise RuntimeError("close 炸了")

    llama.close = _boom  # type: ignore[method-assign]
    engine.unload()  # 不抛
    assert engine.is_loaded is False


# ---------------------------------------------------------------------------
# 4. list_local_models：空目录不炸、磁盘真值优先、等级拿不到也不报错
# ---------------------------------------------------------------------------


def test_list_local_models_empty_dir(tmp_path):
    """★ 空目录（没装任何模型）返回空表，**绝不抛异常**。"""
    assert list_local_models(tmp_path) == []
    assert list_local_models(tmp_path / "根本不存在") == []


def test_list_local_models_reads_disk(tmp_path):
    """磁盘上真有一个非空 ``.gguf`` → 列出来，且路径就是它。"""
    folder = tmp_path / "qwen-demo"
    folder.mkdir()
    payload = b"GGUF" + b"\x11" * 100
    (folder / "qwen-demo.gguf").write_bytes(payload)
    # 空目录 / 空文件都不算装好（AGENTS.md 第 29 节的判据）
    (tmp_path / "empty-dir").mkdir()
    (tmp_path / "zero-size").mkdir()
    (tmp_path / "zero-size" / "z.gguf").write_bytes(b"")

    models = list_local_models(tmp_path)
    assert [model.name for model in models] == ["qwen-demo"]
    model = models[0]
    assert model.path == folder / "qwen-demo.gguf"
    assert model.path.suffix in MODEL_SUFFIXES
    assert model.size == len(payload)
    assert model.label
    assert isinstance(model.usable, bool)
    assert "qwen-demo" in model.display


def test_usable_rule_without_load_level():
    """★ 拿不到负载等级就**列全部**；拿得到就只认轻松 / 勉强。"""
    path = Path("model.gguf")
    assert LocalModel(name="a", label="a", path=path).usable is True
    assert LocalModel(name="a", label="a", path=path, level="").usable is True
    assert LocalModel(name="a", label="a", path=path, level="easy").usable is True
    assert LocalModel(name="a", label="a", path=path, level="tight").usable is True
    assert LocalModel(name="a", label="a", path=path, level="hard").usable is False
    assert (
        LocalModel(name="a", label="a", path=path, level="没见过的键", level_text="困难").usable
        is False
    )
    assert (
        LocalModel(name="a", label="a", path=path, level="没见过的键", level_text="轻松").usable
        is True
    )


def test_size_text_survives_missing_registry():
    model = LocalModel(name="a", label="a", path=Path("a.gguf"), size=0)
    assert model.size_text == ""
    assert "a" in model.display


def test_probe_level_tolerates_registry_without_api():
    """★ B 任务还没写出 ``load_level`` 时：只是没有等级，**不该报错**。"""
    registry = types.SimpleNamespace(LOAD_LABELS={"easy": "轻松"})
    entry = types.SimpleNamespace(note="这是个说明")
    assert inf._probe_level(entry, registry) == ("", "", "这是个说明")  # noqa: SLF001
    assert inf._probe_level(None, registry) == ("", "", "")  # noqa: SLF001
    assert inf._probe_level(entry, None) == ("", "", "这是个说明")  # noqa: SLF001


def test_probe_level_uses_registry_function_when_present():
    """接口就位之后：能拿到键 + 中文标签。"""
    registry = types.SimpleNamespace(
        LOAD_LABELS={"easy": "轻松"},
        load_level=lambda model, ram_gb=None: "easy",
        load_note=lambda model, ram_gb=None: "跑得动",
    )
    entry = types.SimpleNamespace(note="")
    assert inf._probe_level(entry, registry) == ("easy", "轻松", "跑得动")  # noqa: SLF001


# ---------------------------------------------------------------------------
# 5. 界面：ChatDialog / open_chat
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6", reason="界面测试需要 PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ui import chat_dialog as chat  # noqa: E402
from ui.chat_dialog import SYSTEM_PROMPT, ChatDialog, open_chat  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def modal_calls(monkeypatch):
    """★ 把 ``QMessageBox`` 的静态入口全桩掉，并且**真断言一次都没调**。

    ``ChatDialog`` 的设计是"一个模态框都不弹"，所以这个列表必须一直是空的；
    万一哪天有人加了弹窗，这里会直接红（而不是让探针挂死）。
    """
    calls: list[str] = []
    for name in ("information", "warning", "critical", "question", "about"):
        try:
            monkeypatch.setattr(
                QMessageBox,
                name,
                staticmethod(lambda *_a, _n=name, **_k: calls.append(_n)),
            )
        except (TypeError, AttributeError):  # pragma: no cover - PySide 版本差异
            pass
    yield calls
    assert QApplication.activeModalWidget() is None
    assert calls == []


class _FakeChatEngine:
    """给界面用的假引擎：``load`` 什么也不做，``stream`` 逐块给字。"""

    def __init__(self, pieces=("你", "好", "呀")) -> None:
        self.pieces = tuple(pieces)
        self.prompts: list[str] = []
        self.loads = 0
        self.unloads = 0
        self.model_path = Path("fake.gguf")

    def load(self) -> None:
        self.loads += 1

    def stream(self, prompt, max_tokens=512, temperature=0.7, *, stop=None):
        self.prompts.append(prompt)
        yield from self.pieces

    def unload(self) -> None:
        self.unloads += 1


def _model(tmp_path: Path, name: str = "demo", label: str = "演示模型") -> LocalModel:
    path = tmp_path / f"{name}.gguf"
    if not path.exists():
        path.write_bytes(b"GGUF" + b"\x00" * 32)
    return LocalModel(name=name, label=label, path=path, size=path.stat().st_size, level="easy", level_text="轻松")


def _store(tmp_path: Path, username: str = "tester"):
    """临时加密库。★ 必须先 ``register()``（AGENTS.md 第 20c 节踩过）。"""
    from core.crypto_store import CryptoStore

    store = CryptoStore(root=tmp_path / "appdata")
    store.register(username, "Str0ng-Pass-2024")
    return store


def _pump_until(app, predicate, timeout: float = 15.0) -> bool:
    """转事件循环直到条件成立（**不写死等待时长**，AGENTS.md 第 12 节）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(predicate())


def test_dialog_opens_without_engine_or_models(qapp, modal_calls, tmp_path):
    """★ 引擎不可用 / 没模型：窗口照开、输入区禁用、**不弹模态框**。"""
    dialog = ChatDialog(None, models=[])
    dialog.show()
    app = qapp
    app.processEvents()

    assert dialog.isVisible() is True
    assert dialog.isModal() is False
    assert dialog.input_edit.isEnabled() is False
    assert dialog.send_button.isEnabled() is False
    assert dialog.model_combo.isEnabled() is False
    assert "模型" in dialog.status_label.text()
    assert dialog.submit("你好") is False
    assert dialog.messages() == []
    dialog.close()


def test_engine_missing_says_so_even_with_light_models(
    qapp, modal_calls, no_engine, tmp_path
):
    """★ 引擎没装时**不许**只说"去下模型"——那是句假话（下完照样用不了）。

    引擎缺失是更根本的原因，状态行必须先说它，模型层面的事跟在后面。
    """
    store = _store(tmp_path, "noengine")
    dialog = ChatDialog(
        None, store=store, username="noengine", models=[_model(tmp_path)]
    )
    text = dialog.status_label.text()
    assert "本地推理引擎" in text, text
    assert "模型" in text, text
    assert dialog.input_edit.isEnabled() is False
    assert dialog.submit("你好") is False
    dialog.close()


def test_dialog_disables_input_when_only_heavy_models(qapp, modal_calls, tmp_path):
    """装了模型但全是"困难"档：一样禁用，并给一句人话。"""
    heavy = LocalModel(
        name="huge",
        label="超大模型",
        path=tmp_path / "huge.gguf",
        size=1,
        level="hard",
        level_text="困难",
    )
    dialog = ChatDialog(None, models=[heavy])
    assert dialog.input_edit.isEnabled() is False
    assert "重" in dialog.status_label.text() or "模型" in dialog.status_label.text()
    dialog.close()


def test_dialog_streams_answer_and_persists_encrypted_history(
    qapp, modal_calls, tmp_path
):
    """★ 端到端：发一句 → 后台流式回字 → 记录加密落盘 → 重开窗口读得回来。"""
    store = _store(tmp_path)
    fake = _FakeChatEngine(("你", "好", "呀"))
    models = [_model(tmp_path)]

    dialog = ChatDialog(None, store=store, username="tester", engine=fake, models=models)
    dialog.show()
    qapp.processEvents()
    assert dialog.input_edit.isEnabled() is True

    assert dialog.submit("帮我写个谈话提纲") is True
    assert _pump_until(qapp, lambda: dialog._worker is None), "后台生成没能在 15 秒内收尾"  # noqa: SLF001
    qapp.processEvents()

    # 提示词里带着系统提示词和这一句
    assert fake.prompts, "假引擎一次都没被调用"
    assert SYSTEM_PROMPT in fake.prompts[0]
    assert "帮我写个谈话提纲" in fake.prompts[0]

    # 流式结果进了记录框（逐块拼出来的）
    shown = dialog.history_view.toPlainText()
    assert "帮我写个谈话提纲" in shown
    assert "你好呀" in shown

    # 内存里的记录：两条（用户 + 助手），角色分明
    turns = dialog.messages()
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "帮我写个谈话提纲"
    assert turns[1]["text"] == "你好呀"

    # ★ 落盘：只有 .enc，磁盘上扫不到明文
    from core.user_store import user_file

    enc = user_file(store, "tester", chat.CHAT_HISTORY_FILE)
    assert enc is not None and enc.exists()
    assert enc.suffix == ".enc"
    raw = enc.read_bytes()
    assert "帮我写个谈话提纲".encode("utf-8") not in raw
    assert "你好呀".encode("utf-8") not in raw
    assert not list(enc.parent.glob("*.json")), "用户目录里出现了明文 json"
    dialog.close()

    # 重开一个窗口（= 下次打开程序）：记录读得回来
    again = ChatDialog(None, store=store, username="tester", engine=fake, models=models)
    restored = again.history_view.toPlainText()
    assert "帮我写个谈话提纲" in restored
    assert "你好呀" in restored
    assert len(again.messages()) == 2
    assert any("加密" in child.text() for child in again.findChildren(type(again.status_label)))
    again.close()


def test_history_is_grouped_by_day(qapp, modal_calls, tmp_path):
    """★ 记录**按日期分组**落盘（任务书要求），导出文本也照这个分组。"""
    store = _store(tmp_path, "grouped")
    fake = _FakeChatEngine(("收", "到"))
    dialog = ChatDialog(None, store=store, username="grouped", engine=fake, models=[_model(tmp_path)])
    dialog.submit("第一句")
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001
    dialog.submit("第二句")
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001

    from core.user_store import load_user_json

    payload = load_user_json(store, "grouped", chat.CHAT_HISTORY_FILE, {})
    assert payload["schema"] == chat.HISTORY_SCHEMA
    days = payload["days"]
    assert len(days) == 1
    day = next(iter(days))
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", day)
    assert len(days[day]) == 4

    body = dialog.history_as_text()
    assert f"== {day} ==" in body
    assert "第一句" in body and "第二句" in body
    dialog.close()


def test_export_text_writes_utf8_file(qapp, modal_calls, tmp_path, monkeypatch):
    """「导出文本」把整份记录写到用户选的位置（QFileDialog 换成假的）。"""
    store = _store(tmp_path, "exporter")
    fake = _FakeChatEngine(("好",))
    dialog = ChatDialog(None, store=store, username="exporter", engine=fake, models=[_model(tmp_path)])
    dialog.submit("导出我")
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001

    target = tmp_path / "out" / "谈话记录.txt"
    target.parent.mkdir()

    class _FakeFileDialog:
        @staticmethod
        def getSaveFileName(*_args, **_kwargs):
            return str(target), "文本文件 (*.txt)"

    monkeypatch.setattr(chat, "QFileDialog", _FakeFileDialog)
    written = dialog.export_text()
    assert written == str(target)
    body = target.read_text(encoding="utf-8")
    assert "导出我" in body and "好" in body
    dialog.close()


def test_export_without_history_says_so(qapp, modal_calls, tmp_path):
    dialog = ChatDialog(None, models=[])
    assert dialog.export_text() is None
    assert "没有" in dialog.status_label.text()
    dialog.close()


def test_submit_refuses_while_generating(qapp, modal_calls, tmp_path):
    """正在回答时不接受第二条（不排队、不并发）。"""
    store = _store(tmp_path, "busy")

    class _BusyEngine(_FakeChatEngine):
        def stream(self, prompt, max_tokens=512, temperature=0.7, *, stop=None):
            self.prompts.append(prompt)
            for piece in ("好", "的"):
                time.sleep(0.05)  # 保证第二条 submit 落在生成途中
                yield piece

    dialog = ChatDialog(
        None, store=store, username="busy", engine=_BusyEngine(), models=[_model(tmp_path)]
    )
    assert dialog.submit("第一句") is True
    assert dialog.submit("第二句") is False
    assert "还在回答" in dialog.status_label.text()
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001
    # 收尾之后又能发了
    assert dialog.input_edit.isEnabled() is True
    assert [t["text"] for t in dialog.messages() if t["role"] == "user"] == ["第一句"]
    dialog.close()


def test_engine_failure_is_reported_in_status(qapp, modal_calls, tmp_path):
    """引擎在后台抛错：状态行给中文人话，界面不崩、不弹窗。"""

    class _BadEngine(_FakeChatEngine):
        def stream(self, prompt, max_tokens=512, temperature=0.7, *, stop=None):
            raise InferenceUnavailable("本地推理引擎出错了（RuntimeError）。可以再试一次。")
            yield ""  # pragma: no cover - 让它是个生成器

    store = _store(tmp_path, "bad")
    dialog = ChatDialog(None, store=store, username="bad", engine=_BadEngine(), models=[_model(tmp_path)])
    dialog.submit("试试")
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001
    assert "本地推理引擎出错了" in dialog.status_label.text()
    # 用户那一句仍然记下来了（写盘的是真发生过的事）
    assert dialog.messages()[0]["text"] == "试试"
    dialog.close()


def test_open_chat_seeds_prompt_and_auto_starts(qapp, modal_calls, tmp_path, monkeypatch):
    """★ 冻结接口：``open_chat`` 建对话 + 填 ``initial_prompt`` + 自动开始生成。"""
    models = [_model(tmp_path, "alpha", "甲模型"), _model(tmp_path, "beta", "乙模型")]
    fake = _FakeChatEngine(("好", "的"))
    monkeypatch.setattr(chat, "list_local_models", lambda *a, **k: models)
    monkeypatch.setattr(chat, "engine_available", lambda: (True, "已就绪。"))
    monkeypatch.setattr(chat, "LocalModelEngine", lambda *a, **k: fake)

    dialog = open_chat(None, initial_prompt="给我一份提纲", prefer_model="beta")
    assert isinstance(dialog, ChatDialog)
    assert dialog.isVisible() is True
    assert dialog.isModal() is False
    assert dialog.model() is not None and dialog.model().name == "beta"
    # ★ 自动开始生成时输入区是**禁用**的（不然用户能叠第二条），所以这里不能立刻断言
    #   它可用 —— 等这一轮收尾之后再断言（那时它必须已经恢复）。
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001
    assert dialog.input_edit.isEnabled() is True
    assert fake.prompts and "给我一份提纲" in fake.prompts[0]
    assert "给我一份提纲" in dialog.history_view.toPlainText()
    assert "好的" in dialog.history_view.toPlainText()
    dialog.close()


def test_open_chat_falls_back_when_prefer_model_misses(qapp, modal_calls, tmp_path, monkeypatch):
    """``prefer_model`` 命不中就退回默认（第一个可用的）。"""
    models = [_model(tmp_path, "alpha", "甲模型"), _model(tmp_path, "beta", "乙模型")]
    monkeypatch.setattr(chat, "list_local_models", lambda *a, **k: models)
    monkeypatch.setattr(chat, "engine_available", lambda: (True, "已就绪。"))
    monkeypatch.setattr(chat, "LocalModelEngine", lambda *a, **k: _FakeChatEngine())

    dialog = open_chat(None, prefer_model="根本没有这个模型")
    assert dialog.model() is not None and dialog.model().name == "alpha"
    assert dialog.messages() == []  # 没给 initial_prompt 就不该自动生成
    dialog.close()


def test_open_chat_without_engine_still_opens(qapp, modal_calls, monkeypatch):
    """引擎不可用时 ``open_chat`` **照样**返回一个开着的窗口（只是禁用输入）。"""
    monkeypatch.setattr(chat, "list_local_models", lambda *a, **k: [])
    monkeypatch.setattr(chat, "engine_available", lambda: (False, NO_ENGINE_MESSAGE))
    dialog = open_chat(None, initial_prompt="这句发不出去")
    assert dialog.isVisible() is True
    assert dialog.input_edit.isEnabled() is False
    assert dialog.messages() == []
    dialog.close()


def test_coerce_prompt_redacts_student_mapping():
    """★ 传学生字典进来必须**先脱敏**（复用 ``redact_student`` 白名单）。"""
    text = chat._coerce_prompt(  # noqa: SLF001
        {"姓名": "张三", "学号": "2023001", "加权均分": 88.5, "挂科数": 1}
    )
    assert "张三" not in text
    assert "2023001" not in text
    assert "加权均分" in text and "88.5" in text


def test_coerce_prompt_handles_plain_values():
    assert chat._coerce_prompt("  你好  ") == "你好"  # noqa: SLF001
    assert chat._coerce_prompt(None) == ""  # noqa: SLF001
    assert chat._coerce_prompt(["甲", "乙"]) == "甲；乙"  # noqa: SLF001
    assert chat._coerce_prompt(42) == "42"  # noqa: SLF001


def test_system_prompt_is_verbatim():
    """系统提示词逐字照任务书给的（改一个字都要先问）。"""
    assert SYSTEM_PROMPT == (
        "你是一名高校辅导员助理，擅长学生谈话提纲生成、评语润色、侧写分析。"
        "请根据对话上下文提供专业建议。"
    )
    assert chat.CHAT_HISTORY_FILE.endswith(".enc")


def test_ui_texts_have_no_markdown_marks(qapp, modal_calls, tmp_path, monkeypatch):
    """★ 界面文案不许出现 Markdown 记号（AGENTS.md 第 24 节）。"""
    monkeypatch.setattr(chat, "engine_available", lambda: (False, NO_ENGINE_MESSAGE))
    dialog = ChatDialog(None, models=[_model(tmp_path)])
    texts: list[str] = []
    from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTextEdit

    for child in dialog.findChildren(QLabel) + dialog.findChildren(QPushButton):
        texts.extend([child.text(), child.toolTip()])
    for child in dialog.findChildren(QLineEdit):
        texts.extend([child.text(), child.placeholderText(), child.toolTip()])
    for child in dialog.findChildren(QTextEdit):
        texts.extend([child.toPlainText(), child.placeholderText(), child.toolTip()])
    texts.extend([dialog.windowTitle(), dialog.status_label.text()])
    assert texts, "一个控件文案都没收集到，这条测试就没意义了"
    for text in texts:
        for mark in ("**", "__", "`"):
            assert mark not in text, f"界面文案里出现了 Markdown 记号 {mark!r}：{text!r}"
    dialog.close()


def test_chat_dialog_never_uses_message_boxes():
    """★ 这个模块一个 ``QMessageBox`` 都不用：状态行说话，不弹模态框。"""
    source = CHAT_PATH.read_text(encoding="utf-8")
    assert "QMessageBox" not in source
    assert "pip install" not in source and "conda install" not in source
    # 界面文案里的版本号一律从 core.version 取，这里干脆一个都没有
    assert not re.search(r"v?\d+\.\d+\.\d+", source)


def test_background_generation_does_not_block_main_thread(qapp, modal_calls, tmp_path):
    """★ 生成必须**在子线程里**：``submit`` 立刻返回，主线程还能转事件循环。"""
    store = _store(tmp_path, "slow")

    class _SlowEngine(_FakeChatEngine):
        def stream(self, prompt, max_tokens=512, temperature=0.7, *, stop=None):
            self.prompts.append(prompt)
            for piece in ("慢", "慢", "来"):
                time.sleep(0.05)
                yield piece

    dialog = ChatDialog(
        None, store=store, username="slow", engine=_SlowEngine(), models=[_model(tmp_path)]
    )
    started = time.monotonic()
    assert dialog.submit("慢一点") is True
    assert time.monotonic() - started < 1.0, "submit 阻塞了主线程"
    # 主线程在生成期间照样能转事件循环
    assert _pump_until(qapp, lambda: "慢慢来" in dialog.history_view.toPlainText())
    assert _pump_until(qapp, lambda: dialog._worker is None)  # noqa: SLF001
    dialog.close()


def test_closing_dialog_mid_generation_is_safe(qapp, modal_calls, tmp_path):
    """★ 生成途中关窗口：停线程、记录落盘、**不许崩**（AGENTS.md 第 4k 节同族）。"""
    store = _store(tmp_path, "closer")

    class _SlowEngine(_FakeChatEngine):
        def stream(self, prompt, max_tokens=512, temperature=0.7, *, stop=None):
            self.prompts.append(prompt)
            for index in range(50):
                time.sleep(0.02)
                yield f"第{index}块"

    dialog = ChatDialog(
        None, store=store, username="closer", engine=_SlowEngine(), models=[_model(tmp_path)]
    )
    dialog.submit("写长一点")
    qapp.processEvents()
    dialog.close()
    qapp.processEvents()
    assert dialog.messages()[0]["text"] == "写长一点"
    from core.user_store import user_file

    enc = user_file(store, "closer", chat.CHAT_HISTORY_FILE)
    assert enc is not None and enc.exists()
