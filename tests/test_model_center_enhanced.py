"""v1.9 B2 / B4 / B5 的判据：模型中心的负载指示、卸载、失败兜底。

配套的既有测试是 ``tests/test_model_registry.py``（数据层的 109 条）。
**本文件只补这三件事**，别在这里重复那 109 条：

* **B4 负载指示**：``minVramGb`` 的解析、三档判据（含边界）、
  默认只勾「轻松」、``runnable`` 的语义、界面那一格的圆点与中文；
* **B2 卸载**：「卸载」按钮 → 确认框（文案逐字）→ 删目录 → 销账 → 刷新；
  取消则**一个字节都不动**；
* **B5 失败兜底**：失败两次之后那一行出现「复制链接」，
  点了把直链送进剪贴板并说清"下载后放进 models/ 目录"。

★ 两条纪律（和既有测试一致）
===========================

1. **绝不碰真实用户目录**：凡是要读盘 / 落盘 / 删模型的调用，
   一律把 ``dest_dir`` 指到 ``tmp_path``。卸载那几条连
   ``core.ai.model_downloader.delete_model`` 都换成了记录的假函数 ——
   真让它跑一次，删的就是开发机自己的模型目录。
2. **绝不弹真窗口、绝不弹模态框**：所有 GUI 用例 ``QT_QPA_PLATFORM=offscreen``
   （见下面的 ``pytestmark``），并且把 ``QMessageBox.question`` /
   ``information`` / ``warning`` 全部换掉。
   模态框会**一直等到有人点**，在无人值守的测试里就是把看门狗跑满
   （``AGENTS.md`` 第 31 节的探针也踩过这个坑）。

★ 探测一律桩掉
============

- 数据层：桩 ``model_registry._probe_system``（唯一的探测入口），
  顺手断言"没有条目要求显存时压根不探测"；
- 界面层：桩 ``core.system_info.snapshot`` —— ``_machine_kwargs()`` 里是**函数内**
  import，模块上并没有 ``mcd.snapshot`` 这个名字，桩错地方会 AttributeError。

真去问一次显卡要起 ``nvidia-smi`` 子进程，几十到几百毫秒，
而且**结果取决于跑测试的这台机器** —— 那样的断言是假绿。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from core.ai import model_registry as mr  # noqa: E402
from core import system_info  # noqa: E402
from core.ai.model_registry import (  # noqa: E402
    LOAD_COLORS,
    LOAD_EASY,
    LOAD_HARD,
    LOAD_LABELS,
    LOAD_TIGHT,
    RAM_TIGHT_RATIO,
    ModelChoice,
    RemoteModel,
    build_choices,
    default_checked,
    load_level,
    load_note,
    parse_model_list,
)

import ui.model_center_dialog as mcd  # noqa: E402

#: 全文件统一 offscreen —— 这里一条用例都不需要真窗口。
pytestmark = pytest.mark.skipif(
    QApplication is None, reason="需要 PySide6"
)

HEX64_A = "a" * 64

#: 会出现在界面文案里的 Markdown 记号（``AGENTS.md`` 铁律 9）。
MARKDOWN_TOKENS = ("**", "__", "`")

#: 界面文案里不许出现的 emoji（``AGENTS.md`` 4l：一个 emoji 值 241.7ms）。
EMOJI_SAMPLES = ("📊", "🎯", "✅", "⚠", "🚀", "💡")


# ---------------------------------------------------------------------------
# 公共素材
# ---------------------------------------------------------------------------


def _entry(**overrides: Any) -> dict[str, Any]:
    """造一条一定合法的清单条目，再用 ``overrides`` 改它。"""
    item: dict[str, Any] = {
        "id": "model-one",
        "label": "一号模型",
        "group": "推荐",
        "file": "model-one.gguf",
        "url": "https://example.com/model-one.gguf",
        "size": 1024,
        "sha256": HEX64_A,
        "minRamGb": 4,
        "defaultCheck": True,
        "note": "说明文字",
    }
    item.update(overrides)
    return item


def _model(model_id: str = "model-one", **overrides: Any) -> RemoteModel:
    fields: dict[str, Any] = {
        "id": model_id,
        "url": "https://example.com/x.gguf",
        "size": 1024,
    }
    fields.update(overrides)
    return RemoteModel(**fields)


def _fake_system(ram: float | None, vram: float | None) -> SimpleNamespace:
    """一个和 :class:`core.system_info.SystemInfo` 同形的假探测结果。"""
    return SimpleNamespace(total_ram_gb=ram, max_vram_gb=vram)


#: 本文件里"这台测试机的配置"—— 一律显式传进 ``load_level`` / ``build_choices``。
#:
#: ★ 两个数字都给，是为了让用例**永不触发真探测**：只传 ``ram_gb`` 而不传
#: ``vram_gb`` 时，只要有条目写了 ``minVramGb``，registry 就会去起一次
#: ``nvidia-smi``（本机没有，于是拿到 ``None``），结果就取决于跑测试的机器了。
#: 显式传 ``None``（= 没查到独显）与"本机真没有独显"**判据一致，但确定**。
RAM_GB = 15.4
VRAM_GB = 11.9


def _no_probe(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把探测换成"一调用就记一笔 + 返回空结果"，返回调用计数器。"""
    calls: list[int] = []

    def spy() -> Any:
        calls.append(1)
        return _fake_system(None, None)

    monkeypatch.setattr(mr, "_probe_system", spy)
    return calls


def _checked_ids(choices: list[ModelChoice]) -> list[str]:
    return [choice.model.id for choice in choices if choice.checked]


@pytest.fixture
def app() -> Iterator[QApplication]:
    """一个进程里只建一个 ``QApplication``（Qt 不允许建第二个）。"""
    instance = QApplication.instance() or QApplication([])
    yield instance


# ---------------------------------------------------------------------------
# B4-1 解析：minVramGb 的三种写法
# ---------------------------------------------------------------------------


def test_解析minVramGb数字() -> None:
    models, warnings = parse_model_list(
        {"schema": 1, "models": [_entry(minVramGb=6)]}
    )
    assert warnings == []
    assert models[0].min_vram_gb == 6.0


def test_解析minVramGb字符串带单位() -> None:
    """★ 手写清单里最常见的写法就是 ``"6GB"``，必须认得出。"""
    for text in ("6GB", "6gb", "6 gb", "6G", " 6 GB "):
        models, warnings = parse_model_list(
            {"schema": 1, "models": [_entry(minVramGb=text)]}
        )
        assert warnings == [], text
        assert models[0].min_vram_gb == 6.0, text


def test_解析minVramGb非法值给中文警告且不退化() -> None:
    """★ 认不出时必须是 ``None``（= 没要求），**不许退化成 0**。"""
    models, warnings = parse_model_list(
        {"schema": 1, "models": [_entry(minVramGb="很多")]}
    )
    assert models[0].min_vram_gb is None
    assert len(warnings) == 1
    assert "minVramGb" in warnings[0]
    assert "很多" in warnings[0]
    # 警告里必须点名字段本身，否则用户会去清单里找一个根本没写错的字段
    assert "minRamGb" not in warnings[0]


def test_解析minRamGb的警告文案没被显存改动带跑() -> None:
    """``_memory_gb`` 加了 ``field`` 参数，``minRamGb`` 那条文案必须**逐字没变**。"""
    _, warnings = parse_model_list(
        {"schema": 1, "models": [_entry(minRamGb="很多")]}
    )
    assert len(warnings) == 1
    assert "minRamGb 写的是「很多」" in warnings[0]
    assert "minVramGb" not in warnings[0]


def test_解析minVramGb没写就是没要求() -> None:
    models, warnings = parse_model_list(
        {"schema": 1, "models": [_entry()]}
    )
    assert warnings == []
    assert models[0].min_vram_gb is None


def test_解析minVramGb三种非法写法都不退化成零() -> None:
    for bad in ("很多", True, -1):
        models, warnings = parse_model_list(
            {"schema": 1, "models": [_entry(minVramGb=bad)]}
        )
        assert models[0].min_vram_gb is None, bad
        assert warnings, bad


def test_解析minVramGb也认下划线写法() -> None:
    """清单里两种命名都出现过，别只认驼峰。"""
    models, _ = parse_model_list(
        {"schema": 1, "models": [_entry(min_vram_gb="3GB")]}
    )
    assert models[0].min_vram_gb == 3.0


# ---------------------------------------------------------------------------
# B4-2 三档判据（含边界）
# ---------------------------------------------------------------------------


def test_内存正好卡在min_ram_gb是轻松() -> None:
    model = _model(min_ram_gb=8.0)
    assert load_level(model, ram_gb=8.0, vram_gb=None) == LOAD_EASY
    # 少一点点（浮点误差范围内）仍算够
    assert load_level(model, ram_gb=8.0 - 1e-12, vram_gb=None) == LOAD_EASY
    # 真的少一点点就不够了
    assert load_level(model, ram_gb=7.9, vram_gb=None) == LOAD_TIGHT


def test_内存正好卡在零点七倍是勉强() -> None:
    model = _model(min_ram_gb=10.0)          # 0.7 倍 = 7.0
    assert RAM_TIGHT_RATIO == 0.7
    assert load_level(model, ram_gb=7.0, vram_gb=None) == LOAD_TIGHT
    assert load_level(model, ram_gb=6.99, vram_gb=None) == LOAD_HARD


def test_内存充足但有显存要求且显存不够是勉强() -> None:
    """★ 第 1 条是三个条件与在一起：显存不够 → 不是轻松，落到 0.7 倍那条。"""
    model = _model(min_ram_gb=8.0, min_vram_gb=6.0)
    assert load_level(model, ram_gb=32.0, vram_gb=11.9) == LOAD_EASY
    assert load_level(model, ram_gb=32.0, vram_gb=6.0) == LOAD_EASY
    assert load_level(model, ram_gb=32.0, vram_gb=5.9) == LOAD_TIGHT


def test_有显存要求但没查到独显按勉强处理() -> None:
    """内存够、显存未知 —— 不是轻松（没有"有独显"这个事实），但也不判困难。"""
    model = _model(min_ram_gb=8.0, min_vram_gb=6.0)
    # ★ 必须走 ``system=`` 注入口：传 ``vram_gb=None`` 会**真的去探测本机**
    #   （探测条件里就有一条是 ``vram_gb is None and need_vram is not None``），
    #   于是结果取决于跑测试的机器有没有独显 —— 本机有，这条就假绿了。
    assert load_level(model, system=_fake_system(16.0, None)) == LOAD_TIGHT


def test_探不到内存时按不拦处理() -> None:
    """★ 和 ``check_memory`` 同一个态度：探测失败不该让用户一条都勾不上。

    ★ 这里**不能**用 ``ram_gb=None`` 表示"没测到"：那个值本身就会触发真探测
      （本机 15.38 GB），"没测到"于是变成了"测到了 15.38"。要表达"探测失败"，
      注入一个两个字段都是 ``None`` 的快照（``system`` 非 None 就不再探测）。
    """
    model = _model(min_ram_gb=64.0)
    assert load_level(model, system=_fake_system(None, None)) == LOAD_EASY
    assert load_level(model, system=_fake_system(None, 11.9)) == LOAD_EASY
    # 内存未知优先：连"显存不够"也不该拦（拦了就是一台探测失败的机器一条都勾不上）
    assert load_level(
        _model(min_ram_gb=64.0, min_vram_gb=24.0), system=_fake_system(None, 8.0)
    ) == LOAD_EASY


def test_内存未知优先于显存不足() -> None:
    """★ "内存未知"压过"显存不够"：``not have_ram_known`` 那条在 0.7 倍之前。

    反过来说，**「显存未知」不是「内存未知」** —— 内存测得出来、只是没查到独显时
    照样按 0.7 倍判（见 :func:`test_有显存要求但没查到独显按勉强处理`）。
    """
    assert load_level(
        _model(min_ram_gb=64.0, min_vram_gb=2.0), system=_fake_system(None, 0.0)
    ) == LOAD_EASY


def test_没声明内存要求时任何内存都算轻松() -> None:
    model = _model(min_ram_gb=0.0)
    assert load_level(model, ram_gb=1.0, vram_gb=None) == LOAD_EASY


def test_load_level不探测显存当没有条目要求它时(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 没有一条写 ``minVramGb`` 时，一次 ``nvidia-smi`` 都不该起。"""
    calls = _no_probe(monkeypatch)
    models = [_model("a", min_ram_gb=4.0), _model("b", min_ram_gb=8.0)]
    choices = build_choices(models, ram_gb=16.0, vram_gb=None)
    assert calls == []
    assert [c.load_level for c in choices] == [LOAD_EASY, LOAD_EASY]


def test_load_level真的需要显存时才探测(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _no_probe(monkeypatch)
    build_choices([_model("a", min_ram_gb=4.0, min_vram_gb=6.0)], ram_gb=None)
    assert calls, "声明了 minVramGb 就该去问一次显卡"


def test_负载等级的颜色与标签是齐的() -> None:
    assert set(LOAD_LABELS) == {LOAD_EASY, LOAD_TIGHT, LOAD_HARD}
    assert set(LOAD_COLORS) == {LOAD_EASY, LOAD_TIGHT, LOAD_HARD}
    assert LOAD_LABELS[LOAD_EASY] == "轻松"
    assert LOAD_LABELS[LOAD_TIGHT] == "勉强"
    assert LOAD_LABELS[LOAD_HARD] == "困难"
    for color in LOAD_COLORS.values():
        assert color.startswith("#") and len(color) == 7


# ---------------------------------------------------------------------------
# B4-3 load_note
# ---------------------------------------------------------------------------


def _assert_plain_text(text: str) -> None:
    for token in MARKDOWN_TOKENS:
        assert token not in text, text
    for emoji in EMOJI_SAMPLES:
        assert emoji not in text, text


def test_load_note说清本机多少需要多少() -> None:
    note = load_note(_model(min_ram_gb=8.0, min_vram_gb=6.0), ram_gb=15.4, vram_gb=11.9)
    assert "15.4 GB" in note
    assert "需要 8 GB" in note
    assert "11.9 GB" in note
    assert "需要 6 GB" in note
    _assert_plain_text(note)


def test_load_note不含Markdown记号() -> None:
    cases = [
        load_note(_model(min_ram_gb=4.0), ram_gb=15.4, vram_gb=None),
        load_note(_model(min_ram_gb=4.0, min_vram_gb=2.0), ram_gb=15.4, vram_gb=11.9),
        load_note(
            _model(min_ram_gb=4.0, min_vram_gb=2.0),
            system=_fake_system(None, None),
        ),
        load_note(_model(min_ram_gb=64.0), ram_gb=15.4, vram_gb=None),
    ]
    for note in cases:
        assert note
        _assert_plain_text(note)


def test_load_note探不到内存时说清是估算不是零() -> None:
    note = load_note(_model(min_ram_gb=4.0), system=_fake_system(None, None))
    assert "未探测到本机内存" in note
    assert "按最低要求估算" in note


def test_load_note没声明要求时补一句集显说明() -> None:
    note = load_note(_model(min_ram_gb=4.0), ram_gb=15.4, vram_gb=None)
    assert mr.VRAM_OPTIONAL_REMINDER in note


# ---------------------------------------------------------------------------
# B4-4 默认只勾「轻松」
# ---------------------------------------------------------------------------


def test_checked_by_default只看负载等级() -> None:
    easy = ModelChoice(model=_model("a"), load_level=LOAD_EASY)
    tight = ModelChoice(model=_model("b"), load_level=LOAD_TIGHT)
    hard = ModelChoice(model=_model("c"), load_level=LOAD_HARD)
    assert easy.checked_by_default is True
    assert tight.checked_by_default is False
    assert hard.checked_by_default is False


def test_defaultCheck为真但等级非轻松时绝不预勾() -> None:
    """★ B4 的核心行为变更：清单说"推荐"也不算数，本机跑得轻松才算数。"""
    model = _model("big", min_ram_gb=16.0, default_check=True)
    choices = build_choices([model], ram_gb=12.0, vram_gb=None)
    assert choices[0].load_level == LOAD_TIGHT     # 12 / 16 = 0.75 ≥ 0.7
    assert choices[0].model.default_check is True
    assert choices[0].checked_by_default is False
    assert choices[0].checked is False


def test_default_checked按等级筛并保留兼容签名() -> None:
    models = [
        _model("easy", min_ram_gb=4.0),
        _model("tight", min_ram_gb=16.0, min_vram_gb=24.0),   # 内存够、显存不够
        _model("hard", min_ram_gb=64.0),
    ]
    # 12 GB 内存：easy 轻松；tight 的 12 / 16 = 0.75 ≥ 0.7 但显存不够 → 勉强；
    # hard 的 12 < 64 × 0.7 → 困难。只有「轻松」被预勾。
    assert default_checked(models, ram_gb=12.0, vram_gb=8.0) == ["easy"]
    # 没查到独显 → 判据一样落在「勉强」（"显存未知"≠"内存未知"，不判轻松）
    assert default_checked(models, ram_gb=12.0, vram_gb=None) == ["easy"]


def test_默认只勾轻松不是全部不勾() -> None:
    """★ 施工单点名的一条：这个筛选**不是**恒返回空列表。"""
    models = [
        _model("light", min_ram_gb=2.0),
        _model("mid", min_ram_gb=4.0),
        _model("heavy", min_ram_gb=64.0),
    ]
    choices = build_choices(models, ram_gb=15.4, vram_gb=None)
    assert _checked_ids(choices) == ["light", "mid"]
    assert choices, "至少得有一条被预勾"


def test_build_choices把负载等级算进条目里() -> None:
    models = [
        _model("light", min_ram_gb=2.0),
        _model("hard", min_ram_gb=64.0),
        _model("vram-short", min_ram_gb=2.0, min_vram_gb=24.0),
    ]
    choices = build_choices(models, ram_gb=15.4, vram_gb=11.9)
    by_id = {c.model.id: c for c in choices}
    assert by_id["light"].load_level == LOAD_EASY
    assert by_id["hard"].load_level == LOAD_HARD
    assert by_id["vram-short"].load_level == LOAD_TIGHT
    assert by_id["light"].load_text == "轻松"
    assert by_id["light"].load_note
    _assert_plain_text(by_id["light"].load_note)


# ---------------------------------------------------------------------------
# B4-5 runnable 的语义
# ---------------------------------------------------------------------------


def test_runnable是轻松或勉强() -> None:
    assert ModelChoice(model=_model(), load_level=LOAD_EASY).runnable is True
    assert ModelChoice(model=_model(), load_level=LOAD_TIGHT).runnable is True
    assert ModelChoice(model=_model(), load_level=LOAD_HARD).runnable is False


def test_runnable不等于selectable() -> None:
    """★ 两条闸门各管各的：内存不够 → 不可选；负载困难 → 不可勾但**可选**。"""
    hard_but_roomy = ModelChoice(
        model=_model("h", min_ram_gb=4.0),
        memory_ok=True,
        load_level=LOAD_HARD,
    )
    assert hard_but_roomy.selectable is True
    assert hard_but_roomy.runnable is False

    tight_but_memory_short = ModelChoice(
        model=_model("t", min_ram_gb=64.0),
        memory_ok=False,
        load_level=LOAD_TIGHT,
    )
    assert tight_but_memory_short.selectable is False
    assert tight_but_memory_short.runnable is True


def test_困难必然同时被内存闸门挡住() -> None:
    """★ 两条规则的交点：困难 ⟹ 内存也不够（不是两条互不相干的判据）。

    ``hard`` 要求 ``have_ram < min_ram_gb × 0.7``，而内存闸门（``memory_ok``）要求
    ``have_ram ≥ min_ram_gb`` —— 前者成立时后者必然不成立。所以界面上「困难」的行
    一定同时没有 ``ItemIsEnabled``。「困难」自己那条规则真正管的是**预勾**和
    **下载按钮置灰**：按钮是 ``setCellWidget`` 放进去的控件，**不受
    ``ItemIsEnabled`` 影响** —— 这正是它必须单独置灰的原因。
    """
    models = [_model("huge", min_ram_gb=64.0)]
    choices = build_choices(models, ram_gb=16.0, vram_gb=None)
    assert choices[0].load_level == LOAD_HARD
    assert choices[0].selectable is False        # 内存闸门看的是 minRamGb
    assert choices[0].runnable is False
    assert choices[0].checked is False           # 困难不预勾


# ---------------------------------------------------------------------------
# 界面（offscreen + 桩掉全部 QMessageBox）
# ---------------------------------------------------------------------------


class _MessageSpy:
    """把三个 ``QMessageBox`` 静态方法换掉，记下调用并给出预定答复。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: Any = None) -> None:
        self.asked: list[tuple[Any, ...]] = []
        self.answer = (
            QMessageBox.StandardButton.No if answer is None else answer
        )

        def question(*args: Any, **kwargs: Any) -> Any:
            self.asked.append(args)
            return self.answer

        def information(*args: Any, **kwargs: Any) -> Any:
            self.asked.append(args)
            return QMessageBox.StandardButton.Ok

        def warning(*args: Any, **kwargs: Any) -> Any:
            self.asked.append(args)
            return QMessageBox.StandardButton.Ok

        monkeypatch.setattr(mcd.QMessageBox, "question", question)
        monkeypatch.setattr(mcd.QMessageBox, "information", information)
        monkeypatch.setattr(mcd.QMessageBox, "warning", warning)

    @property
    def texts(self) -> list[str]:
        return [str(arg) for call in self.asked for arg in call]


@pytest.fixture
def center(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """一个真的建起来、但一个真窗口都没弹的 ``ModelCenterDialog``。

    本机读数也一起喂好（``_machine_read = True``）—— 建这个对话框时界面会去
    问一次系统，桩掉它就不会起 ``nvidia-smi``，结果也不随跑测试的机器变。

    ★ ``model_dir`` 也一起指到 ``tmp_path``：对话框自己调 ``_reload_choices()``
      时**不带** ``dest_dir``，不桩就会去读（卸载时还会去删）用户真实的
      ``%LOCALAPPDATA%\\SGMS\\models``。
    """
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        mcd, "fetch_model_list", lambda *a, **k: ([], [], "（测试桩）")
    )
    monkeypatch.setattr(
        mcd, "model_dir", lambda name, *a, **k: models_dir / name
    )
    monkeypatch.setattr(
        system_info, "snapshot", lambda *a, **k: _fake_system(RAM_GB, VRAM_GB)
    )
    # ★ 构造时 ``_load_manifest()`` 丢出去的那次清单拉取，回调是**投递到主线程
    #   队列**的：它要等下一次 processEvents 才跑，跑起来就用桩返回的空清单
    #   把界面重画一遍。这里套一层"跑完了"的锁存器，把这次回调**先等回来**，
    #   用例后面装的卡片才不会被它冲掉（也不许拿 ``_refresh_btn.isEnabled()``
    #   当判据 —— 那个按钮在回调跑之前就是 False）。
    settled: list[bool] = []
    real_done = mcd.ModelCenterDialog._on_manifest
    real_error = mcd.ModelCenterDialog._on_manifest_error

    def _spy_done(self: Any, result: Any) -> None:
        try:
            real_done(self, result)
        finally:
            settled.append(True)

    def _spy_error(self: Any, exc: BaseException) -> None:
        try:
            real_error(self, exc)
        finally:
            settled.append(True)

    monkeypatch.setattr(mcd.ModelCenterDialog, "_on_manifest", _spy_done)
    monkeypatch.setattr(mcd.ModelCenterDialog, "_on_manifest_error", _spy_error)

    dialog = mcd.ModelCenterDialog()
    dialog._machine_ram = RAM_GB
    dialog._machine_vram = VRAM_GB
    dialog._machine_read = True
    dialog._tl_models_dir = models_dir
    waited = 0
    while not settled and waited < 3000:
        app.processEvents()
        QTest.qWait(10)
        waited += 10
    app.processEvents()
    assert settled, "清单拉取的回调一直没回来（后台线程卡住了？）"
    yield dialog
    dialog.deleteLater()


def _install(dialog: Any, models: list[RemoteModel]) -> None:
    """把一批模型塞进对话框并重画（``dest_dir`` 指向临时目录，绝不碰真模型）。"""
    dialog._models = list(models)
    dialog._failures = []
    dialog._choices = build_choices(
        dialog._models,
        dest_dir=dialog._tl_models_dir,
        ram_gb=dialog._machine_ram,
        vram_gb=dialog._machine_vram,
    )
    dialog._populate()


def _cards(dialog: Any) -> list[Any]:
    """按界面顺序拿卡片（v1.9.1：原来的 ``_rows(dialog)`` 就是它）。"""
    return list(dialog._iter_cards())


def _card(dialog: Any, model_id: str) -> Any:
    return dialog._card_map[model_id]


def _buttons_of(dialog: Any, card: Any) -> dict[str, QPushButton]:
    """卡片上的按钮（卡片内部**没有**下载按钮，只有卸载 / 复制链接）。"""
    return {button.text(): button for button in card.findChildren(QPushButton)}


def _boom(message: str) -> Any:
    """一个"被调到就炸"的替身 —— 用来钉死"这条路径根本不该走到"。"""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(message)

    return _raise


def _load_labels(dialog: Any, card: Any) -> list[str]:
    """卡片上"负载"那一格的文字（圆点是个空文字 QLabel）。"""
    return [label.text() for label in card.load_holder.findChildren(QLabel)]


def test_界面是卡片流而不是六列表格(center: Any, app: QApplication) -> None:
    """★ v1.9.1：六列表格**已经整体换成卡片流**，这条改成守新结构。

    老判据（``COLUMN_TITLES`` 六列 + 操作列 ``Fixed`` 宽度）随表格一起作废 ——
    那六个列名常量也**已经从模块里删掉**了（留着会让人以为还能拼回去）。
    4c 第 2 条那条坑（``ResizeToContents`` 不算 ``setCellWidget`` 的控件）
    在卡片流里不存在了：卡片宽度由 ``QGridLayout`` 均分，不再有"操作列"。
    新结构的完整判据在 ``tests/test_model_center_card.py``；这里只留一条
    "表格已经拆干净、卡片列数写死"的冒烟，防止有人把 ``QTreeWidget`` 搬回来。
    """
    assert not hasattr(center, "_tree"), "六列表格应该已经拆掉了"
    assert not hasattr(mcd, "COLUMN_TITLES"), "六列那套常量不该再留着"
    assert isinstance(mcd.GRID_COLUMNS, int) and mcd.GRID_COLUMNS >= 1
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    # 卡片流是 QGridLayout 排的：每张卡片都在格子里，且列下标 < GRID_COLUMNS
    cards = list(center._iter_cards())
    assert len(cards) == 1
    index = center._grid.indexOf(cards[0])
    assert index >= 0
    row, column, _span_r, _span_c = center._grid.getItemPosition(index)
    assert 0 <= column < mcd.GRID_COLUMNS
    assert row >= 0


def test_每行都有负载圆点与中文(center: Any) -> None:
    """卡片流版：每张卡片的负载格里都是"彩色圆点 + 中文"（不是 emoji）。"""
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("hard", min_ram_gb=64.0),
        ],
    )
    cards = _cards(center)
    assert len(cards) == 2
    light_labels = _load_labels(center, cards[0])
    hard_labels = _load_labels(center, cards[1])
    assert light_labels[-1] == "轻松"
    assert hard_labels[-1] == "困难"
    # 圆点是个空文字的小方块 + 圆角；颜色来自 LOAD_COLORS
    dot = cards[0].load_dot
    assert dot.text() == ""
    assert LOAD_COLORS[LOAD_EASY] in dot.styleSheet()
    assert "border-radius" in dot.styleSheet()


def test_负载那一格的文案与提示都是纯文本(center: Any) -> None:
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0, note="轻量模型"),
            _model("hard", min_ram_gb=64.0, note="大模型"),
        ],
    )
    cards = _cards(center)
    for card in cards:
        # 卡片整张的气泡（清单说明 + 困难那句）与负载格的气泡都是纯文本
        for tip in (card.toolTip(), card.load_holder.toolTip()):
            assert tip
            _assert_plain_text(tip)
        for label in card.load_holder.findChildren(QLabel):
            _assert_plain_text(label.text())
        _assert_plain_text(card.name_label.text())
        _assert_plain_text(card.note_label.text())
    # 困难那张的名称气泡里带上那句提示
    assert "困难" in " ".join(_load_labels(center, cards[1]))
    assert mcd.HARD_LOAD_TOOLTIP in cards[1].toolTip()


def test_内存不够的卡片不能选中(center: Any) -> None:
    """★ 施工单点名的"两条规则别混"：内存闸门只看 ``minRamGb``。

    v1.9.1 把表格行的 ``ItemIsEnabled`` 换成了卡片的 ``can_select()`` ——
    语义一个字没变：内存不够的**整张卡片不可选**，不只是不勾。
    """
    _install(
        center,
        [
            _model("small", min_ram_gb=4.0),
            _model("huge", min_ram_gb=9999.0),
        ],
    )
    cards = _cards(center)
    assert cards[0].can_select() is True
    assert cards[1].can_select() is False
    assert cards[1].choice.selectable is False
    # 反过来说：「困难」的卡片**一定**同时不可选 —— 困难要求
    # 内存 < minRamGb × 0.7，那必然小于 minRamGb（见 test_困难必然同时被内存闸门挡住）。
    # 下面这行是"内存够、只是显存不够"→「勉强」：这一档照样可选可勾。
    _install(center, [_model("slow", min_ram_gb=4.0, min_vram_gb=999.0)])
    slow = _cards(center)[0]
    assert slow.can_select() is True
    assert slow.selected is False  # 勉强档默认不勾


def test_困难的行下载按钮置灰并带提示(center: Any) -> None:
    """三档各自的气泡 —— 卡片流版：卡片内部没有下载按钮，闸门在底部那个按钮上。"""
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("hard", min_ram_gb=64.0, min_vram_gb=999.0),
            _model("mid", min_ram_gb=16.0),
        ],
    )
    cards = _cards(center)
    light, hard, mid = cards
    assert light.can_select() is True
    assert hard.can_select() is False
    assert "下载" not in _buttons_of(center, hard)
    assert mcd.HARD_LOAD_TOOLTIP == "当前电脑配置可能无法流畅运行此模型"
    assert mcd.HARD_LOAD_TOOLTIP in hard.toolTip()
    # ★ 「困难」是"内存也不够"的**子集**（困难 ⟹ 内存 < minRamGb × 0.7 < minRamGb），
    #   所以两条判断都成立，先后决定了显示哪一句 —— 任务书点名困难卡片给这一句，
    #   于是 LOAD_HARD 先判。「勉强但内存不够」（15.4 GB < 16 GB，且 15.4 ≥ 11.2）
    #   这一档仍然拿内存那句：那才是真正拦住它的原因。
    assert mid.can_select() is False
    assert "内存" in mid.status_label.toolTip()
    # 底部按钮只数能下载的那些：轻松一张（默认选中）
    center.show()
    assert center._download_btn.text() == "下载选中（1）"


def test_困难的行即使按到下载也不进队列(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """置灰只是提示；真正的闸门在点卡片那一下（``_on_card_clicked``）。

    v1.9.1 起卡片内部没有下载按钮 —— 点「困难」卡片既不选中，也不会进下载队列。
    """
    _install(center, [_model("slow", min_ram_gb=64.0, min_vram_gb=999.0)])
    started: list[str] = []
    monkeypatch.setattr(mcd, "download_exe", _boom("困难那张卡片不该走到下载"))
    monkeypatch.setattr(center, "_start_one", lambda *a, **k: started.append("start"))
    # 点卡片：困难的既不选中，也不启动下载
    center._on_card_clicked(_card(center, "slow"))
    assert started == []
    assert center.selected_ids() == []
    assert center._download_btn.text() == "下载选中"
    # 卡片角落那个 × 是卸载用的，不是下载：卡片上**没有**下载入口
    # （复制链接是卡片自己的兜底入口，跟下载无关）。
    assert "下载" not in _buttons_of(center, _card(center, "slow"))
    assert set(_buttons_of(center, _card(center, "slow"))) == {"×", "复制链接"}
    # 底部那个按钮真的按下去也不会开始下载（一张都没选中）：它自己先拦住，
    # 走的是"还没有选中任何模型"那条提示，压根不该碰下载 Job。
    notices: list[str] = []
    monkeypatch.setattr(
        mcd.QMessageBox, "information", lambda *a, **k: notices.append(a[2])
    )
    center._on_download()
    assert started == []
    assert notices and "还没有选中" in notices[0]


# ---------------------------------------------------------------------------
# B2 卸载
# ---------------------------------------------------------------------------


def test_确认文案逐字(center: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 逐字要求，改一个字都是返工。"""
    assert mcd.UNINSTALL_CONFIRM_TEXT == "卸载后将无法使用 AI 功能，确定？"
    _install(center, [_model("m", min_ram_gb=4.0)])
    spy = _MessageSpy(monkeypatch)
    monkeypatch.setattr(mcd, "delete_model", lambda *a, **k: True)
    center._on_uninstall("m")
    assert mcd.UNINSTALL_CONFIRM_TEXT in spy.texts


def test_卸载取消时一个字节都不动(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(center, [_model("m", min_ram_gb=4.0)])
    _MessageSpy(monkeypatch, answer=QMessageBox.StandardButton.No)
    deleted: list[str] = []
    forgotten: list[str] = []
    monkeypatch.setattr(
        mcd, "delete_model", lambda name, *a, **k: deleted.append(name) or True
    )
    monkeypatch.setattr(
        mcd, "forget_installed", lambda name, *a, **k: forgotten.append(name)
    )
    center._on_uninstall("m")
    assert deleted == []
    assert forgotten == []


def test_卸载确认后先删目录再销账(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(center, [_model("m", min_ram_gb=4.0)])
    _MessageSpy(monkeypatch, answer=QMessageBox.StandardButton.Yes)
    order: list[str] = []
    monkeypatch.setattr(
        mcd, "delete_model", lambda name, *a, **k: order.append(f"delete:{name}") or True
    )
    monkeypatch.setattr(
        mcd,
        "forget_installed",
        lambda name, *a, **k: order.append(f"forget:{name}"),
    )
    reloaded: list[str] = []
    monkeypatch.setattr(center, "_reload_choices", lambda: reloaded.append("reload"))
    center._on_uninstall("m")
    # ★ 顺序有意义：先销账再删目录的话，rmtree 一失败就留下一个卸不掉的目录
    assert order == ["delete:m", "forget:m"]
    assert reloaded == ["reload"]


def test_删不掉时不销账(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(center, [_model("m", min_ram_gb=4.0)])
    _MessageSpy(monkeypatch, answer=QMessageBox.StandardButton.Yes)
    forgotten: list[str] = []
    monkeypatch.setattr(mcd, "delete_model", lambda *a, **k: False)
    monkeypatch.setattr(
        mcd, "forget_installed", lambda name, *a, **k: forgotten.append(name)
    )
    monkeypatch.setattr(center, "_reload_choices", lambda: None)
    center._on_uninstall("m")
    assert forgotten == []
    assert "没能删掉" in center._status.text()


def test_已装的行才有卸载按钮(center: Any) -> None:
    """真建一个模型文件，让 ``installed_state()``（以磁盘为准）判成已装。

    ★ 要**两样都齐**：磁盘上的 ``models/m/*.gguf``（第一步永远是磁盘）**和**
      账本里的一条记录。少了账本那条，``_state_from()`` 一样返回"未装" ——
      它的判据是"磁盘上真有 **且** 账本里有"。
    """
    folder = center._tl_models_dir / "m"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.gguf").write_bytes(b"GGUF" * 512)
    mr.record_installed(
        "m",
        sha256="",
        size=1024,
        file="m.gguf",
        dest_dir=center._tl_models_dir,
    )
    _install(center, [_model("m", min_ram_gb=4.0)])
    assert center._choices[0].state == mr.STATE_INSTALLED
    card = _cards(center)[0]
    # ★ 卡片流：装好的那张长出绿色徽章 + 卸载小按钮，卡片内部**没有**下载按钮
    #   判可见性用 ``isVisibleTo(card)`` 而不是 ``isVisible()``：对话框自己
    #   没 ``show()``，``isVisible()`` 会因为顶层窗口不可见而一律 False
    #   （量的是"整条父链都可见吗"，测不了"这个控件被显示了吗"）。
    assert card.badge.isVisibleTo(card) is True
    assert card.badge.text() == "已安装"
    assert card.uninstall_btn.isVisibleTo(card) is True
    assert card.uninstall_btn.text() == "×"
    assert "下载" not in _buttons_of(center, card)


# ---------------------------------------------------------------------------
# B5 失败兜底
# ---------------------------------------------------------------------------


def test_失败两次之后出现复制链接(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.9.1：卡片流里「复制链接」是**卡片上常驻的兜底入口**。

    表格时代它只在重试两次失败之后才出现（操作列一次只放一个按钮）；
    卡片里有位置，所以有地址的卡片**一开始就带**这个入口 —— 失败两次之后
    界面上要能看到"为什么失败"，而不是多长出一个按钮。
    """
    _install(center, [_model("m", min_ram_gb=4.0)])
    card = _cards(center)[0]
    assert "复制链接" in _buttons_of(center, card)
    assert card.status_label.text() == mr.STATE_MISSING
    center._failures = [("m", "网断了")]
    center._apply_failure_marks()
    center._populate()
    card = _cards(center)[0]
    buttons = _buttons_of(center, card)
    assert "复制链接" in buttons
    assert buttons["复制链接"].isEnabled()
    assert "下载失败" in card.status_label.text()
    # ★ 重画之后失败标记要重新贴上去（卡片是新建的，标记不会自己跟过来）
    assert "网断了" in card.status_label.text()


def test_重试上限是两次() -> None:
    """首次 + 自动重试 1 次（施工单 B5）。"""
    assert mcd.MAX_ATTEMPTS == 2


def test_复制链接把直链放进剪贴板(
    center: Any,
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        center,
        [_model("m", min_ram_gb=4.0, url="https://example.com/m.gguf")],
    )
    spy = _MessageSpy(monkeypatch)
    app.clipboard().setText("（先放点别的）")
    center._on_copy_link("m")
    assert app.clipboard().text() == "https://example.com/m.gguf"
    assert any("models/ 目录" in text for text in spy.texts)
    assert mcd.COPY_LINK_HINT == "已复制到剪贴板，请浏览器下载后放入 models/ 目录。"


def test_复制链接的提示文案是纯文本(center: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_plain_text(mcd.COPY_LINK_HINT)
    _assert_plain_text(mcd.INTRO_TEXT)
    _assert_plain_text(mcd.HARD_LOAD_TOOLTIP)
    _assert_plain_text(mcd.UNINSTALL_CONFIRM_TEXT)


def test_没有地址的条目不会被复制(
    center: Any,
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(center, [_model("local", min_ram_gb=0.0, url="")])
    spy = _MessageSpy(monkeypatch)
    app.clipboard().setText("（原样）")
    center._on_copy_link("local")
    assert app.clipboard().text() == "（原样）"
    assert any("没有下载地址" in text for text in spy.texts)


# ---------------------------------------------------------------------------
# 界面接线：本机读数只探一次
# ---------------------------------------------------------------------------


def test_界面复用同一份本机读数(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        mcd, "fetch_model_list", lambda *a, **k: ([], [], "（测试桩）")
    )
    # 这条用例自己建对话框（不走 center fixture），所以模型目录要自己指走 ——
    # 桩要**照抄 registry 的语义**（拿 ``.`` 探根目录时不能被清洗成 ``model``）。
    monkeypatch.setattr(
        mcd, "model_dir", lambda name, *a, **k: models_dir / mr._safe_name(name)
    )
    calls: list[int] = []

    def fake_snapshot(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return _fake_system(15.4, 11.9)

    # ★ 桩的是 ``core.system_info.snapshot`` 本身，不是 ``mcd.snapshot``：
    #   后者是 `_machine_kwargs()` 里的**函数内** import，模块上没有这个名字
    #   （界面不许自己写第二套探测，所以它只借 system_info 那一处真源）。
    monkeypatch.setattr(system_info, "snapshot", fake_snapshot)
    dialog = mcd.ModelCenterDialog()
    dialog._tl_models_dir = models_dir
    first = dialog._machine_kwargs()
    second = dialog._machine_kwargs()
    # ★ ``dest_dir`` 也在这一份 kwargs 里（v1.9.1 加）：对话框自己调
    #   build_choices / merged_with_local 都走它 —— 少传一次就会去读真实的
    #   ``%LOCALAPPDATA%\SGMS\models``，测试桩拦不住，界面还会冒出"本机已有"。
    assert first == second
    assert first["ram_gb"] == 15.4
    assert first["vram_gb"] == 11.9
    assert Path(first["dest_dir"]) == models_dir
    assert len(calls) == 1
    # 界面自己不许有第二套判据：它必须拿到 registry 的等级
    _install(dialog, [_model("m", min_ram_gb=4.0)])
    assert dialog._choices[0].load_level == LOAD_EASY
    assert dialog._choices[0].checked is True
    dialog.deleteLater()


def test_对话框可以真的建起来(center: Any) -> None:
    """冒烟：卡片流建好了、没有一行是真的下载按钮在跑。"""
    assert isinstance(center, QDialog)
    assert not hasattr(center, "_tree"), "六列表格应该已经拆掉了"
    assert center.windowTitle() == mcd.DIALOG_TITLE
    assert center._scroll is not None
    assert center._grid is not None
