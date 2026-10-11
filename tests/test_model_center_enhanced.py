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

from PySide6.QtCore import Qt  # noqa: E402
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
    dialog = mcd.ModelCenterDialog()
    dialog._machine_ram = RAM_GB
    dialog._machine_vram = VRAM_GB
    dialog._machine_read = True
    dialog._tl_models_dir = models_dir
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


def _rows(dialog: Any) -> list[Any]:
    return list(dialog._iter_rows())


def _buttons_of(dialog: Any, row: Any) -> dict[str, QPushButton]:
    widget = dialog._tree.itemWidget(row, mcd.COLUMN_ACTION)
    return {
        button.text(): button
        for button in widget.findChildren(QPushButton)
    }


def _load_labels(dialog: Any, row: Any) -> list[str]:
    widget = dialog._tree.itemWidget(row, mcd.COLUMN_LOAD)
    return [label.text() for label in widget.findChildren(QLabel)]


def test_界面有六列且操作列写死宽度(center: Any, app: QApplication) -> None:
    """★ 操作列里放的是 ``setCellWidget`` 的按钮 —— 宽度必须写死（4c 第 2 条）。"""
    assert mcd.COLUMN_TITLES == ("名称", "大小", "适配场景", "负载", "状态", "操作")
    header = center._tree.header()
    assert center._tree.columnCount() == len(mcd.COLUMN_TITLES)
    assert header.sectionResizeMode(mcd.COLUMN_ACTION) == mcd.QHeaderView.ResizeMode.Fixed
    center.show()
    app.processEvents()
    assert center._tree.columnWidth(mcd.COLUMN_ACTION) == mcd.ACTION_COLUMN_WIDTH
    # 只要最宽的两个按钮 + 间距放得下，就不能被裁
    assert mcd.ACTION_COLUMN_WIDTH >= mcd.COPY_BUTTON_WIDTH + mcd.UNINSTALL_BUTTON_WIDTH


def test_每行都有负载圆点与中文(center: Any) -> None:
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("hard", min_ram_gb=64.0),
        ],
    )
    rows = _rows(center)
    assert len(rows) == 2
    light_labels = _load_labels(center, rows[0])
    hard_labels = _load_labels(center, rows[1])
    assert light_labels[-1] == "轻松"
    assert hard_labels[-1] == "困难"
    # 圆点是个空文字的小方块 + 圆角；颜色来自 LOAD_COLORS
    dot = center._tree.itemWidget(rows[0], mcd.COLUMN_LOAD).findChildren(QLabel)[0]
    assert dot.text() == ""
    assert LOAD_COLORS[LOAD_EASY] in dot.styleSheet()
    assert "border-radius" in dot.styleSheet()


def test_负载那一列的文案与提示都是纯文本(center: Any) -> None:
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0, note="轻量模型"),
            _model("hard", min_ram_gb=64.0, note="大模型"),
        ],
    )
    rows = _rows(center)
    for row in rows:
        widget = center._tree.itemWidget(row, mcd.COLUMN_LOAD)
        assert widget.toolTip()
        _assert_plain_text(widget.toolTip())
        for label in widget.findChildren(QLabel):
            _assert_plain_text(label.text())
        for column in range(center._tree.columnCount()):
            _assert_plain_text(row.toolTip(column))
    # 困难那行的名称气泡里带上那句提示
    assert "困难" in " ".join(_load_labels(center, rows[1]))


def test_内存不够的行没有ItemIsEnabled(center: Any) -> None:
    """★ 施工单点名的"两条规则别混"：内存闸门只看 ``minRamGb``。"""
    _install(
        center,
        [
            _model("small", min_ram_gb=4.0),
            _model("huge", min_ram_gb=9999.0),
        ],
    )
    rows = _rows(center)
    assert rows[0].flags() & Qt.ItemFlag.ItemIsEnabled
    assert not (rows[1].flags() & Qt.ItemFlag.ItemIsEnabled)
    # 反过来说：「困难」的行**一定**同时没有 ItemIsEnabled —— 困难要求
    # 内存 < minRamGb × 0.7，那必然小于 minRamGb（见 test_困难必然同时被内存闸门挡住）。
    # 下面这行是"内存够、只是显存不够"→「勉强」：这一档照样可选可勾。
    _install(center, [_model("slow", min_ram_gb=4.0, min_vram_gb=999.0)])
    slow = _rows(center)[0]
    assert slow.flags() & Qt.ItemFlag.ItemIsEnabled
    assert slow.checkState(mcd.COLUMN_NAME) == Qt.CheckState.Unchecked


def test_困难的行下载按钮置灰并带提示(center: Any) -> None:
    """三档各自的气泡 —— 判断的先后决定了困难行显示的是哪一句。"""
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("hard", min_ram_gb=64.0, min_vram_gb=999.0),
            _model("mid", min_ram_gb=16.0),
        ],
    )
    rows = _rows(center)
    light_buttons = _buttons_of(center, rows[0])
    hard_buttons = _buttons_of(center, rows[1])
    mid_buttons = _buttons_of(center, rows[2])
    assert light_buttons["下载"].isEnabled()
    assert not hard_buttons["下载"].isEnabled()
    assert hard_buttons["下载"].toolTip() == mcd.HARD_LOAD_TOOLTIP
    assert mcd.HARD_LOAD_TOOLTIP == "当前电脑配置可能无法流畅运行此模型"
    # ★ 「困难」是"内存也不够"的**子集**（困难 ⟹ 内存 < minRamGb × 0.7 < minRamGb），
    #   所以两条判断都成立，先后决定了显示哪一句 —— 任务书点名困难行给这一句，
    #   于是 LOAD_HARD 先判。「勉强但内存不够」（15.4 GB < 16 GB，且 15.4 ≥ 11.2）
    #   这一档仍然拿内存那句：那才是真正拦住它的原因。
    assert not mid_buttons["下载"].isEnabled()
    assert "内存" in mid_buttons["下载"].toolTip()


def test_困难的行即使按到下载也不进队列(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """置灰只是提示；真正的闸门在 ``_on_row_download`` 里。"""
    _install(center, [_model("slow", min_ram_gb=64.0, min_vram_gb=999.0)])
    spy = _MessageSpy(monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(
        center, "_on_download", lambda: started.append("download")
    )
    center._on_row_download("slow")
    assert started == []
    assert any("可能带不动" in text for text in spy.texts)


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
    buttons = _buttons_of(center, _rows(center)[0])
    assert "卸载" in buttons
    assert "下载" not in buttons


# ---------------------------------------------------------------------------
# B5 失败兜底
# ---------------------------------------------------------------------------


def test_失败两次之后出现复制链接(
    center: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(center, [_model("m", min_ram_gb=4.0)])
    assert "复制链接" not in _buttons_of(center, _rows(center)[0])
    center._failures = [("m", "网断了")]
    center._apply_failure_marks()
    center._populate()
    buttons = _buttons_of(center, _rows(center)[0])
    assert "复制链接" in buttons
    assert buttons["复制链接"].isEnabled()
    assert "下载失败" in _rows(center)[0].text(mcd.COLUMN_STATUS)


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
    _buttons_of(center, _rows(center)[0])
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
    # 对话框内部调 build_choices 时不带 dest_dir，不指就会去读真实用户目录。
    monkeypatch.setattr(mcd, "model_dir", lambda name, *a, **k: models_dir / name)
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
    assert first == second == {"ram_gb": 15.4, "vram_gb": 11.9}
    assert len(calls) == 1
    # 界面自己不许有第二套判据：它必须拿到 registry 的等级
    _install(dialog, [_model("m", min_ram_gb=4.0)])
    assert dialog._choices[0].load_level == LOAD_EASY
    assert dialog._choices[0].checked is True
    dialog.deleteLater()


def test_对话框可以真的建起来(center: Any) -> None:
    """冒烟：六列都建好了、没有一行是真的下载按钮在跑。"""
    assert isinstance(center, QDialog)
    assert center._tree.columnCount() == len(mcd.COLUMN_TITLES)
    assert center.windowTitle() == mcd.DIALOG_TITLE
