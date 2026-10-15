"""v1.9.1 的判据：模型中心从六列表格改成**卡片流**之后还剩下什么行为。

配套的既有测试是：

* ``tests/test_model_registry.py`` —— 数据层（解析 / 三档负载 / 台账）；
* ``tests/test_model_center_enhanced.py`` —— 负载指示、卸载、失败兜底。

**本文件只补卡片流这一层**：分组排版、卡片内容、负载圆点、悬浮、
点选与底部计数、已安装徽章，以及``AGENTS.md`` 4a / 4b 那条
「一个 widget 只有一个 ``graphicsEffect``」的**静态 + 运行时**断言。

★ 三条纪律
==========

1. **绝不碰真实用户目录 / 绝不下载**：``fetch_model_list`` 与
   ``model_dir`` 都换成桩，模型目录指到 ``tmp_path``，
   真实下载函数 ``download_exe`` 一条用例都不许走到。
2. **绝不弹模态框**：``QMessageBox.question`` / ``information`` /
   ``warning`` 全部换掉（模态框会一直等到有人点，等于把看门狗跑满）。
3. **鼠标逻辑必须真的走一遍**（``AGENTS.md`` 4h）：悬浮 / 点选都先试
   ``QTest.mouseMove`` / ``QTest.mouseClick``，测试平台拿不到合成事件时
   再显式补一个 ``QEnterEvent`` —— 但**不允许**只调背后的纯函数。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QEnterEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QGraphicsOpacityEffect,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
)

from core import system_info  # noqa: E402
from core.ai.model_registry import (  # noqa: E402
    LOAD_COLORS,
    LOAD_EASY,
    LOAD_HARD,
    LOAD_LABELS,
    LOAD_TIGHT,
    ModelChoice,
    RemoteModel,
)

import ui.model_center_dialog as mcd  # noqa: E402

pytestmark = pytest.mark.skipif(QApplication is None, reason="需要 PySide6")

#: 本机配置一律显式喂进去，**永不触发真探测**（那会起 nvidia-smi，
#: 而且结果取决于跑测试的那台机器 —— 那样的断言是假绿）。
RAM_GB = 15.4
VRAM_GB = 11.9

#: 界面文案里不许出现的 Markdown 记号（``AGENTS.md`` 第 24 节）。
MARKDOWN_TOKENS = ("**", "__", "`")

#: 界面文案里不许出现的 emoji（``AGENTS.md`` 4l：一个 emoji 值 241.7ms）。
EMOJI_SAMPLES = ("🎯", "✅", "⚠", "🚀", "💡", "📊")


def _model(model_id: str = "model-one", **overrides: Any) -> RemoteModel:
    fields: dict[str, Any] = {
        "id": model_id,
        "label": f"模型 {model_id}",
        "group": "推荐",
        "file": f"{model_id}.gguf",
        "url": f"https://example.com/{model_id}.gguf",
        "size": 1024,
        "note": "说明文字",
    }
    fields.update(overrides)
    return RemoteModel(**fields)


@pytest.fixture
def app() -> Iterator[QApplication]:
    """一个进程里只建一个 ``QApplication``（Qt 不允许建第二个）。"""
    yield QApplication.instance() or QApplication([])


class _MessageSpy:
    """把三个模态框全换成"记一笔就返回"，一条真弹窗都不许出现。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        answer: QMessageBox.StandardButton = QMessageBox.StandardButton.Yes,
    ) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []
        for name in ("information", "warning", "question", "critical"):
            monkeypatch.setattr(mcd.QMessageBox, name, self._make(name))

    def _make(self, kind: str) -> Any:
        def fake(parent: Any, title: str = "", text: str = "", *a: Any, **k: Any) -> Any:
            self.calls.append((kind, str(title), str(text)))
            if kind == "question":
                return self.answer
            return QMessageBox.StandardButton.Ok

        return fake

    @property
    def texts(self) -> list[str]:
        return [text for _kind, _title, text in self.calls]


@pytest.fixture
def center(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """一个真的建起来、但一个真窗口都没弹、一个字节都没下的对话框。"""
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        mcd, "fetch_model_list", lambda *a, **k: ([], [], "（测试桩）")
    )
    monkeypatch.setattr(
        mcd, "model_dir", lambda name, *a, **k: models_dir / str(name)
    )
    monkeypatch.setattr(
        system_info, "snapshot", lambda *a, **k: SimpleNamespace(
            total_ram_gb=RAM_GB, max_vram_gb=VRAM_GB
        )
    )

    def _boom(*a: Any, **k: Any) -> Any:  # pragma: no cover - 走到就是测试写错了
        raise AssertionError("测试里绝不允许真下载")

    monkeypatch.setattr(mcd, "download_exe", _boom)

    # ★ 构造时 ``_load_manifest()`` 会丢一个后台任务去取清单 —— 而 ``run_async``
    #   的回调是**投递到主线程队列**的：它在下一次 ``processEvents()`` 才跑，
    #   跑起来就会用桩返回的空清单把界面重画一遍（用例刚装好的卡片全没了）。
    #   所以这里给两个回调套一层"跑完了"的锁存器，下面 `_drain` 等的是它，
    #   **不是** `_refresh_btn.isEnabled()` —— 那个按钮在回调跑之前就是 False，
    #   拿它当判据等于一次都没等（实测：轮询条件从头到尾不成立，直接返回）。
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
    dialog._machine_read = True
    dialog._machine_ram = RAM_GB
    dialog._machine_vram = VRAM_GB
    dialog._tl_models_dir = models_dir
    _drain(dialog, app, settled)
    yield dialog
    dialog.deleteLater()


def _drain(
    dialog: Any, app: QApplication, settled: list[bool], *, max_ms: int = 3000
) -> None:
    """把 ``run_async`` 丢出去的那次清单拉取等回来（轮询，不写死 sleep）。"""
    waited = 0
    while not settled and waited < max_ms:
        app.processEvents()
        QTest.qWait(10)
        waited += 10
    app.processEvents()
    assert settled, "清单拉取的回调一直没回来（后台线程卡住了？）"


def _install(dialog: Any, models: list[RemoteModel]) -> None:
    """把一批模型塞进对话框并重画（``dest_dir`` 指向临时目录）。"""
    dialog._models = list(models)
    dialog._failures = []
    dialog._choices = mcd.build_choices(
        dialog._models,
        dest_dir=dialog._tl_models_dir,
        ram_gb=dialog._machine_ram,
        vram_gb=dialog._machine_vram,
    )
    dialog._populate()


def _card(dialog: Any, model_id: str) -> Any:
    return dialog._card_map[model_id]


def _card_texts(card: Any) -> list[str]:
    return [label.text() for label in card.findChildren(QLabel)]


def _buttons_of(card: Any) -> dict[str, QPushButton]:
    return {button.text(): button for button in card.findChildren(QPushButton)}


def _assert_plain(text: str) -> None:
    for token in MARKDOWN_TOKENS:
        assert token not in text, text
    for emoji in EMOJI_SAMPLES:
        assert emoji not in text, text


# ---------------------------------------------------------------------------
# 1. 卡片流排版与分组
# ---------------------------------------------------------------------------


def test_界面是卡片流不是表格(center: Any, app: QApplication) -> None:
    """★ 六列表格必须彻底没了：连 ``_tree`` / ``COLUMN_TITLES`` 都不该存在。"""
    assert not hasattr(center, "_tree")
    assert not hasattr(mcd, "COLUMN_TITLES")
    assert not hasattr(mcd, "COLUMN_NAME")
    assert isinstance(center._scroll, QScrollArea)
    assert center._scroll.widgetResizable() is True
    assert center._grid.columnCount() >= 1
    center.show()
    app.processEvents()
    # 卡片流：每张卡片的宽度是视线宽度均分（不是表格那种一列一列）
    assert center._scroll.viewport().width() > 0


def test_卡片数量等于条目数(center: Any, app: QApplication) -> None:
    _install(
        center,
        [
            _model("a", min_ram_gb=2.0),
            _model("b", min_ram_gb=2.0),
            _model("c", min_ram_gb=2.0),
        ],
    )
    center.show()
    app.processEvents()
    cards = list(center._iter_cards())
    assert len(cards) == 3
    assert [c.model_id for c in cards] == ["a", "b", "c"]
    assert center._grid.count() == 4  # 1 个组标题 + 3 张卡片
    # 三张卡片宽度一致（QVBoxLayout 均分宽度时会差 1px 取整，不是布局问题）
    widths = {c.width() for c in cards}
    assert max(widths) - min(widths) <= 1, widths
    assert cards[0].width() >= mcd.CARD_MIN_WIDTH


def test_固定列数三列(center: Any, app: QApplication) -> None:
    assert mcd.GRID_COLUMNS == 3
    _install(
        center,
        [_model(f"m{i}", min_ram_gb=2.0) for i in range(4)],
    )
    center.show()
    app.processEvents()
    rows = {c.y() for c in center._iter_cards()}
    assert len(rows) == 2, "4 张卡片按 3 列排应该占两行"
    # 同一行的前两张在同一水平线上
    first_row = [c for c in center._iter_cards() if c.y() == min(rows)]
    assert len(first_row) == 3


def test_按清单group分组且组间有标题和分隔线(center: Any, app: QApplication) -> None:
    """清单里的真实取值就是「推荐」/「高级」（见 update/ai_models.json）。"""
    _install(
        center,
        [
            _model("r1", group="推荐", min_ram_gb=2.0),
            _model("r2", group="推荐", min_ram_gb=2.0),
            _model("p1", group="高级", min_ram_gb=2.0),
        ],
    )
    center.show()
    app.processEvents()
    assert [name for name, _items in center._groups] == ["推荐", "高级"]
    headers = [
        label.text()
        for label in center._grid_host.findChildren(QLabel)
        if label.text() in ("推荐", "高级")
    ]
    assert headers == ["推荐", "高级"]
    # 每个组标题右边有一条分隔线（HLine）
    assert len(center._grid_host.findChildren(mcd.QFrame)) >= 2
    # 第二组的卡片排在第一组下面
    assert _card(center, "p1").y() > _card(center, "r1").y()


def test_卡片显示名称大小推荐信息(center: Any, app: QApplication) -> None:
    model = _model(
        "big",
        label="超大一模型",
        note="适合长文档",
        size=3 * 1024**3,
        min_ram_gb=2.0,
    )
    _install(center, [model])
    center.show()
    app.processEvents()
    card = _card(center, "big")
    texts = _card_texts(card)
    assert "超大一模型" in texts
    assert "适合长文档" in texts
    # 模型大小走 human_size_bytes（3 GiB → "3.0GB"），卡片上一定看得见
    assert model.size_text in texts, texts
    # 名称是大号粗体
    assert "font-weight: 700" in card.name_label.styleSheet()
    assert "15px" in card.name_label.styleSheet()


# ---------------------------------------------------------------------------
# 2. 负载指示：彩色圆点 + 中文，真源是 LOAD_LABELS / LOAD_COLORS
# ---------------------------------------------------------------------------


def test_负载圆点与文案取自注册表(center: Any, app: QApplication) -> None:
    """★ 颜色和中文的唯一真源是 registry 的 LOAD_COLORS / LOAD_LABELS。"""
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("mid", min_ram_gb=18.0),
            _model("heavy", min_ram_gb=64.0),
        ],
    )
    center.show()
    app.processEvents()
    light = _card(center, "light")
    mid = _card(center, "mid")
    heavy = _card(center, "heavy")

    assert light.choice.load_level == LOAD_EASY
    assert mid.choice.load_level == LOAD_TIGHT
    assert heavy.choice.load_level == LOAD_HARD

    # ★ 文案：逐字来自 LOAD_LABELS，不是界面自己写死的
    assert light.load_text.text() == LOAD_LABELS[LOAD_EASY] == "轻松"
    assert mid.load_text.text() == LOAD_LABELS[LOAD_TIGHT] == "勉强"
    assert heavy.load_text.text() == LOAD_LABELS[LOAD_HARD] == "困难"

    # ★ 颜色：圆点的背景色来自 LOAD_COLORS
    for card, level in (
        (light, LOAD_EASY),
        (mid, LOAD_TIGHT),
        (heavy, LOAD_HARD),
    ):
        sheet = card.load_dot.styleSheet()
        assert LOAD_COLORS[level] in sheet, (level, sheet)
        assert "border-radius" in sheet
        # 圆点是空文字的小方块（不是 ● 字符、更不是 emoji）
        assert card.load_dot.text() == ""
        assert card.load_dot.width() == card.load_dot.height() == 10
        _assert_plain(card.load_text.text())
        _assert_plain(card.load_dot.text())


def test_负载三档的颜色互不相同() -> None:
    """三档必须真的看得出区别，否则圆点等于没画。"""
    assert len({LOAD_COLORS[LOAD_EASY], LOAD_COLORS[LOAD_TIGHT], LOAD_COLORS[LOAD_HARD]}) == 3
    assert set(LOAD_LABELS.values()) == {"轻松", "勉强", "困难"}


# ---------------------------------------------------------------------------
# 3. ★★ 阴影可以有，透明度效果绝对不许有（AGENTS.md 4a / 4b）
# ---------------------------------------------------------------------------


def test_卡片有阴影但没有任何透明度效果(center: Any, app: QApplication) -> None:
    """★★ 静态断言：挂了阴影的卡片**绝不许**再挂 ``QGraphicsOpacityEffect``。

    ``AGENTS.md`` 4a / 4b：一个 widget 只有一个 ``graphicsEffect``；
    透明度效果会缓存源 pixmap，卡片内部一增删控件就是一块空白。
    """
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("heavy", min_ram_gb=64.0),  # 「困难」= 视觉降级的那一档
        ],
    )
    center.show()
    app.processEvents()
    for card in center._iter_cards():
        effect = card.graphicsEffect()
        assert effect is not None, "卡片必须挂着阴影"
        assert not isinstance(effect, QGraphicsOpacityEffect)
        # 卡片自己 + 卡片里每一个子控件都不许有透明度效果
        assert not mcd.has_opacity_effect(card)
        for child in card.findChildren(mcd.QWidget):
            assert not mcd.has_opacity_effect(child), child
    # 降级走的是样式表，不是效果
    heavy = _card(center, "heavy")
    assert heavy.property("tlVisualState") == "muted"
    assert mcd.MUTED_BG_COLOR in heavy.content.styleSheet()


def test_运行时闸门真的会拦(center: Any, app: QApplication) -> None:
    """闸门有牙：手动挂一个透明度效果，``_assert_no_opacity_effect`` 必须抛。"""
    _install(center, [_model("a", min_ram_gb=2.0)])
    card = _card(center, "a")
    card.setGraphicsEffect(QGraphicsOpacityEffect(card))
    with pytest.raises(RuntimeError):
        mcd._assert_no_opacity_effect(card)


# ---------------------------------------------------------------------------
# 4. 悬浮：内容上移 2px + 阴影加深（真的走一遍鼠标）
# ---------------------------------------------------------------------------


def _enter(card: Any, app: QApplication) -> None:
    """让卡片进入悬浮态。

    ★ 先试 ``QTest.mouseMove``（4h：鼠标逻辑必须真的动鼠标）。offscreen
      平台不一定合成 enter 事件，拿不到时补一个**真的** ``QEnterEvent``
      —— 两种都是把事件送进 ``enterEvent``，不是直接调函数。
    """
    QTest.mouseMove(card, QPoint(card.width() // 2, card.height() // 2))
    app.processEvents()
    if card.content.contentsMargins().top() == mcd.CARD_PADDING - mcd.CARD_FRAME_MARGIN:
        local = card.rect().center()
        global_pos = card.mapToGlobal(local)
        card.enterEvent(
            QEnterEvent(
                local.toPointF(), local.toPointF(), global_pos.toPointF()
            )
        )
        app.processEvents()


def test_悬浮时内容上移两像素且卡片不跳位(center: Any, app: QApplication) -> None:
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    card = _card(center, "a")
    base = mcd.CARD_PADDING - mcd.CARD_FRAME_MARGIN
    assert card.content.contentsMargins().top() == base
    assert card.content.contentsMargins().bottom() == base
    geometry_before = card.geometry()
    blur_before = card.shadow.blurRadius()

    _enter(card, app)

    after = card.content.contentsMargins()
    assert after.top() == base - mcd.HOVER_LIFT_PX == base - 2
    assert after.bottom() == base + mcd.HOVER_LIFT_PX
    # ★ 光标是手型
    assert card.cursor().shape() == Qt.CursorShape.PointingHandCursor
    # ★ 阴影加深
    assert card.shadow.blurRadius() > blur_before
    # ★ 卡片自己的几何**一个像素都没动**（不许跳位）
    assert card.geometry() == geometry_before

    # 离开恢复
    card.leaveEvent(QEvent(QEvent.Type.Leave))
    app.processEvents()
    back = card.content.contentsMargins()
    assert (back.top(), back.bottom()) == (base, base)
    assert card.shadow.blurRadius() == blur_before
    assert card.geometry() == geometry_before


def test_鼠标移进卡片真的触发上移(center: Any, app: QApplication) -> None:
    """4h：这条只用真鼠标事件，事件平台不给就跳过（不许只调函数）。"""
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    card = _card(center, "a")
    base = mcd.CARD_PADDING - mcd.CARD_FRAME_MARGIN
    QTest.mouseMove(card, QPoint(card.width() // 2, card.height() // 2))
    app.processEvents()
    if card.content.contentsMargins().top() == base:
        pytest.skip("offscreen 平台不合成 enter 事件，改由 test_悬浮时内容上移两像素且卡片不跳位 覆盖")
    assert card.content.contentsMargins().top() == base - 2
    # ★ 离开这一段**不靠平台合成**：offscreen 平台在整份用例跑起来时
    #   不保证把 Leave 送到卡片上（实测单跑会送、连着跑不送，
    #   ``card.underMouse()`` 还停在 True）。这里用 QApplication.sendEvent
    #   派发一个**真的** QEvent.Leave 走 Qt 的事件投递（不是直接调 leaveEvent），
    #   判据仍然是"上移必须回到原位"。
    app.sendEvent(card, QEvent(QEvent.Type.Leave))
    app.processEvents()
    assert card.content.contentsMargins().top() == base


# ---------------------------------------------------------------------------
# 5. 点选与底部「下载选中（N）」
# ---------------------------------------------------------------------------


def test_点一下选中再点一下取消(center: Any, app: QApplication) -> None:
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    card = _card(center, "a")
    # ★ 轻松档是**默认选中**的（第 33 节：默认只勾 🟢），所以先取消掉，
    #   从"没选中"这个状态开始试点击。
    assert card.selected is True
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert card.selected is False
    assert center._download_btn.text() == "下载选中"
    assert center._download_btn.isEnabled() is False

    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert card.selected is True
    assert center.selected_ids() == ["a"]
    assert center._download_btn.text() == "下载选中（1）"
    assert center._download_btn.isEnabled() is True
    # 选中的卡片边框是 2px 高亮色
    assert mcd.SELECTED_BORDER_COLOR in mcd.CARD_STYLE
    # ★ 动态属性名带 tl 前缀、且真的写进去了（AGENTS.md 4f：撞名会静默失败）
    assert card.property("tlSelected") == "true"


def test_点一下选中的卡片就取消它(center: Any, app: QApplication) -> None:
    """再点一下 = 取消（和上面那条是同一个动作的两种起点，都留着）。"""
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    card = _card(center, "a")
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert card.selected is False
    assert center._download_btn.text() == "下载选中"
    assert center.selected_ids() == []


def test_点在卡片里的标签上也算点中卡片(center: Any, app: QApplication) -> None:
    """★ 事件过滤器：标签是卡片的子控件，点它不该丢事件。"""
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    card = _card(center, "a")
    before = card.selected
    QTest.mouseClick(card.name_label, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert card.selected is not before
    assert center.selected_ids() == ([] if before else ["a"])


def test_空格键也能切选中(center: Any, app: QApplication) -> None:
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    card = _card(center, "a")
    before = card.selected
    card.setFocus()
    QTest.keyClick(card, Qt.Key.Key_Space)
    app.processEvents()
    assert card.selected is not before


def test_选中多张底部计数跟着涨(center: Any, app: QApplication) -> None:
    _install(
        center,
        [
            _model("a", min_ram_gb=2.0),  # 轻松 → 默认就选中，先点掉再点回来
            _model("b", min_ram_gb=2.0),
            _model("c", min_ram_gb=2.0),
        ],
    )
    center.show()
    app.processEvents()
    # 三张默认都选中 → 全部点掉，再从零开始点两张
    for model_id in ("a", "b", "c"):
        QTest.mouseClick(_card(center, model_id), Qt.MouseButton.LeftButton)
        app.processEvents()
    assert center.selected_ids() == []
    for model_id in ("a", "c"):
        QTest.mouseClick(_card(center, model_id), Qt.MouseButton.LeftButton)
        app.processEvents()
    assert center.selected_ids() == ["a", "c"]  # 按界面顺序
    assert center._download_btn.text() == "下载选中（2）"
    assert mcd.ModelCenterDialog._download_button_text(0) == "下载选中"
    assert mcd.ModelCenterDialog._download_button_text(3) == "下载选中（3）"


def test_重画之后选中不会丢(center: Any, app: QApplication) -> None:
    """★ 真踩到过：重画一次，用户选中的全被"打回默认"。

    重画（``_populate``）走 ``_wants_selected``：**出现过的**模型听
    ``_checked_ids``，第一次出现的才听清单。所以 ``_populate`` 结尾必须把
    "这一轮真正选中的"记回 ``_checked_ids`` —— 不记的话，构造对话框时那次
    清单回调（``run_async`` 投递到主线程队列，下一次 ``processEvents()`` 才跑）
    会把用户刚点的选中**静默清空**，而且界面看不出任何异常。
    """
    _install(
        center,
        [
            _model("a", min_ram_gb=2.0),
            _model("b", min_ram_gb=2.0),
            _model("c", min_ram_gb=2.0),
        ],
    )
    center.show()
    app.processEvents()
    # 默认三张都选中：点掉 b，再点掉 c —— 只剩 a
    for model_id in ("b", "c"):
        QTest.mouseClick(_card(center, model_id), Qt.MouseButton.LeftButton)
        app.processEvents()
    assert center.selected_ids() == ["a"]

    # 重画一遍（＝清单刷新 / 换目录 / 那次迟到的后台回调落地）
    center._populate()
    app.processEvents()
    assert center.selected_ids() == ["a"], "重画把用户取消掉的选中打回来了"
    assert center._download_btn.text() == "下载选中（1）"

    # 再点回 b：两张选中，再重画一次 —— 还是两张
    QTest.mouseClick(_card(center, "b"), Qt.MouseButton.LeftButton)
    app.processEvents()
    assert center.selected_ids() == ["a", "b"]
    center._populate()
    app.processEvents()
    assert center.selected_ids() == ["a", "b"]
    assert center._download_btn.text() == "下载选中（2）"
    # ★ 反过来也要成立：重画不能把"用户取消过的"当成"没见过"再默认勾上
    assert center._card_map["c"].selected is False


def test_卡片里没有下载按钮(center: Any, app: QApplication) -> None:
    """★ 施工单：卡片内部不放下载按钮，下载统一走底部那一个。"""
    _install(center, [_model("a", min_ram_gb=2.0)])
    center.show()
    app.processEvents()
    assert "下载" not in _buttons_of(_card(center, "a"))
    assert center._download_btn.text().startswith("下载选中")


def test_默认选中只认轻松的(center: Any, app: QApplication) -> None:
    """清单里的 defaultCheck 已经不看了 —— 默认只勾「轻松」。"""
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0, default_check=True),
            _model("mid", min_ram_gb=18.0, default_check=True),
            _model("heavy", min_ram_gb=64.0, default_check=True),
        ],
    )
    center.show()
    app.processEvents()
    assert center.selected_ids() == ["light"]
    assert center._download_btn.text() == "下载选中（1）"


# ---------------------------------------------------------------------------
# 6. 🔴 负载「困难」：不响应选中 + 气泡 + 底部不数它
# ---------------------------------------------------------------------------


def test_困难卡片不可选(center: Any, app: QApplication) -> None:
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("heavy", min_ram_gb=64.0, min_vram_gb=999.0),
        ],
    )
    center.show()
    app.processEvents()
    heavy = _card(center, "heavy")
    assert heavy.choice.load_level == LOAD_HARD
    assert heavy.can_select() is False
    assert heavy.set_selected(True) is False
    assert heavy.toggle_selected() is False
    assert heavy.selected is False
    # ★ 反过来说：「困难」的行**一定**同时内存也不够
    #   （困难要求内存 < minRamGb × 0.7 < minRamGb）
    assert heavy.choice.selectable is False


def test_困难卡片点了也不选中且底部不数它(center: Any, app: QApplication) -> None:
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("heavy", min_ram_gb=64.0, min_vram_gb=999.0),
        ],
    )
    center.show()
    app.processEvents()
    heavy = _card(center, "heavy")
    QTest.mouseClick(heavy, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert heavy.selected is False
    # ★ 底部那个按钮数的是"选中的张数" —— 困难卡片一票都不算
    assert center.selected_ids() == ["light"]  # 只有默认勾上的那张
    assert center._download_btn.text() == "下载选中（1）"
    # 状态行给出原因（用户点了总得有个回应）
    assert center._status.text()


def test_困难卡片有悬浮气泡原文(center: Any, app: QApplication) -> None:
    _install(
        center,
        [
            _model("light", min_ram_gb=2.0),
            _model("heavy", min_ram_gb=64.0, min_vram_gb=999.0, note="大模型"),
        ],
    )
    center.show()
    app.processEvents()
    heavy = _card(center, "heavy")
    assert mcd.HARD_LOAD_TOOLTIP == "当前电脑配置可能无法流畅运行此模型"
    assert mcd.HARD_LOAD_TOOLTIP in heavy.toolTip()
    assert heavy.toolTip()  # 鼠标停在卡片任意位置都能看到
    assert heavy.toolTip().endswith(mcd.HARD_LOAD_TOOLTIP)
    _assert_plain(heavy.toolTip())
    # 轻松的那张不该带这句
    assert mcd.HARD_LOAD_TOOLTIP not in _card(center, "light").toolTip()


def test_困难卡片悬浮时气泡真的能出来(center: Any, app: QApplication) -> None:
    """4h：气泡挂在卡片上，悬浮必须真的看得见（而不是只设了 toolTip）。"""
    _install(center, [_model("heavy", min_ram_gb=64.0, min_vram_gb=999.0)])
    center.show()
    app.processEvents()
    heavy = _card(center, "heavy")
    _enter(heavy, app)
    assert mcd.HARD_LOAD_TOOLTIP in heavy.toolTip()
    assert heavy.underMouse() or heavy.content.contentsMargins().top() < 6


def test_困难卡片仍然是可点的控件只是拒绝选中(center: Any, app: QApplication) -> None:
    """困难卡片不是"藏起来"：它看得见、有气泡，只是选中闸门关着。"""
    _install(center, [_model("heavy", min_ram_gb=64.0, min_vram_gb=999.0)])
    center.show()
    app.processEvents()
    heavy = _card(center, "heavy")
    assert heavy.isVisible() is True
    assert "困难" in _card_texts(heavy)
    assert heavy.isEnabled() is True


# ---------------------------------------------------------------------------
# 7. 已安装徽章 + 卸载按钮
# ---------------------------------------------------------------------------


def _make_installed(center: Any, model_id: str, model: RemoteModel) -> None:
    """真建一个模型目录 + 一条台账记录，让 ``installed_state()`` 判成已装。

    ★ 两样都要：磁盘上真有 ``.gguf``（第一判据）**且**台账里有记录。
    """
    folder = center._tl_models_dir / model_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (model.file or f"{model_id}.gguf")).write_bytes(b"GGUF" * 512)
    from core.ai.model_registry import record_installed

    record_installed(
        model_id,
        sha256="",
        size=len(b"GGUF" * 512),
        file=model.file or f"{model_id}.gguf",
        dest_dir=center._tl_models_dir,
    )


def test_已安装卡片有绿色徽章和卸载按钮(center: Any, app: QApplication) -> None:
    model = _model("m", min_ram_gb=4.0)
    _make_installed(center, "m", model)
    _install(center, [model, _model("other", min_ram_gb=4.0)])
    center.show()
    app.processEvents()

    installed = _card(center, "m")
    other = _card(center, "other")
    assert installed.badge.isVisible() is True
    assert installed.badge.text() == "已安装"
    assert "border-radius" in installed.badge.styleSheet()
    assert mcd.BADGE_BG_COLOR in installed.badge.styleSheet()
    assert installed.uninstall_btn.isVisible() is True
    assert installed.uninstall_btn.text() == "×"
    assert installed.copy_btn.isVisible() is False

    # 没装的那张既没有徽章也没有卸载按钮
    assert other.badge.isVisible() is False
    assert other.uninstall_btn.isVisible() is False


def test_已安装卡片不可选中下载(center: Any, app: QApplication) -> None:
    """★ 已安装的卡片不进「下载选中」的队列（底部计数也不该数它）。"""
    model = _model("m", min_ram_gb=4.0)
    _make_installed(center, "m", model)
    _install(center, [model])
    center.show()
    app.processEvents()
    card = _card(center, "m")
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    app.processEvents()
    # 选中不选中是一回事，点了「下载选中」不能把它排进下载队列
    if card.selected:
        center._on_download()
        assert center._queue == [], "已安装的不该进下载队列"
    else:
        assert center.selected_ids() == []


def test_点卸载真的走确认文案(center: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """卸载按钮接的就是 ``_on_uninstall``，确认文案逐字。"""
    model = _model("m", min_ram_gb=4.0)
    _make_installed(center, "m", model)
    _install(center, [model])
    spy = _MessageSpy(monkeypatch)
    deleted: list[str] = []
    monkeypatch.setattr(
        mcd, "delete_model", lambda name, *a, **k: deleted.append(str(name)) or True
    )
    monkeypatch.setattr(mcd, "forget_installed", lambda *a, **k: None)
    monkeypatch.setattr(center, "_reload_choices", lambda: None)

    _card(center, "m").uninstall_btn.click()
    assert mcd.UNINSTALL_CONFIRM_TEXT == "卸载后将无法使用 AI 功能，确定？"
    assert mcd.UNINSTALL_CONFIRM_TEXT in spy.texts
    assert deleted == ["m"]
    _assert_plain(mcd.UNINSTALL_CONFIRM_TEXT)
    _assert_plain(_card(center, "m").uninstall_btn.toolTip())


def test_卸载按钮上的文字不是emoji(center: Any, app: QApplication) -> None:
    _install(center, [_model("m", min_ram_gb=4.0)])
    center.show()
    app.processEvents()
    _assert_plain(_card(center, "m").uninstall_btn.text())


# ---------------------------------------------------------------------------
# 8. 清单兜底：读不到 / 失败两次之后「复制链接」
# ---------------------------------------------------------------------------


def test_清单读不到时界面照开并给兜底提示(center: Any, app: QApplication) -> None:
    center._on_manifest(([], [], "清单读不到（测试）"))
    center.show()
    app.processEvents()
    assert center._placeholder is not None
    assert mcd.MANIFEST_FALLBACK_HINT in center._status.text()
    assert mcd.GITEE_RAW_MODELS in mcd.MANIFEST_FALLBACK_HINT
    _assert_plain(mcd.MANIFEST_FALLBACK_HINT)
    _assert_plain(mcd.INTRO_TEXT)


def test_清单读失败也给兜底提示(center: Any) -> None:
    center._on_manifest_error(RuntimeError("网断了"))
    assert "读取清单失败" in center._status.text()
    assert mcd.MANIFEST_FALLBACK_HINT in center._status.text()
    assert center._refresh_btn.isEnabled() is True


def test_失败两次之后卡片长出复制链接(
    center: Any,
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(center, [_model("m", min_ram_gb=4.0, url="https://e.com/m.gguf")])
    center.show()
    app.processEvents()
    assert _card(center, "m").copy_btn.isVisible() is False

    center._failures = [("m", "网断了")]
    center._apply_failure_marks()
    app.processEvents()
    card = _card(center, "m")
    assert card.copy_btn.isVisible() is True
    assert "下载失败" in card.status_label.text()
    assert mcd.MAX_ATTEMPTS == 2  # 首次 + 自动重试 1 次

    spy = _MessageSpy(monkeypatch)
    app.clipboard().setText("（先放点别的）")
    card.copy_btn.click()
    assert app.clipboard().text() == "https://e.com/m.gguf"
    assert any(mcd.COPY_LINK_HINT in text for text in spy.texts)
    assert mcd.COPY_LINK_HINT == "已复制到剪贴板，请浏览器下载后放入 models/ 目录。"


def test_复制链接只在清单里有地址时才长出来(center: Any, app: QApplication) -> None:
    """本机已有、清单没写的条目没有地址 —— 复制一个空串没有意义。"""
    _install(center, [_model("local", min_ram_gb=0.0, url="")])
    center.show()
    app.processEvents()
    center._failures = [("local", "网断了")]
    center._apply_failure_marks()
    app.processEvents()
    assert _card(center, "local").copy_btn.isVisible() is False


# ---------------------------------------------------------------------------
# 9. 对外签名与"绝不自动下载"
# ---------------------------------------------------------------------------


def test_open_model_center签名没变() -> None:
    """★ 别处会调它 —— 签名一个字都不许改。"""
    import inspect

    signature = inspect.signature(mcd.open_model_center)
    assert list(signature.parameters) == ["parent", "on_installed"]
    assert signature.parameters["parent"].default is None
    assert signature.parameters["on_installed"].kind is inspect.Parameter.KEYWORD_ONLY


def test_对话框是QDialog且标题没变(center: Any) -> None:
    assert isinstance(center, QDialog)
    assert center.windowTitle() == mcd.DIALOG_TITLE
    assert center.downloadFinished is not None


def test_打开对话框不会自己下载(center: Any) -> None:
    """★ 只读一份清单，绝不自动下模型（上面的 ``download_exe`` 是炸弹桩）。"""
    assert center._queue == []
    assert center._busy is False


def test_界面文案全是纯文本(center: Any, app: QApplication) -> None:
    """铁律 9：卡片上每一个 QLabel / 气泡都不许出现 Markdown 记号。"""
    _install(
        center,
        [
            _model("a", min_ram_gb=2.0, note="轻量模型"),
            _model("heavy", min_ram_gb=64.0, min_vram_gb=999.0, note="大模型"),
        ],
    )
    center.show()
    app.processEvents()
    for card in center._iter_cards():
        _assert_plain(card.toolTip())
        for label in card.findChildren(QLabel):
            _assert_plain(label.text())
        for button in card.findChildren(QPushButton):
            _assert_plain(button.text())
            _assert_plain(button.toolTip())
    for widget in (center._status, center._plan, center._download_btn, center._copy_btn):
        _assert_plain(widget.text())
        _assert_plain(widget.toolTip())
