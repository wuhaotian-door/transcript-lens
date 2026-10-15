"""v1.9.1 问题 7 判据：人工回显确认弹窗（``ui/review_dialog.py``）的可拖动布局。

任务书要求（逐条对应到下面的用例）：

1. 主体改 ``QSplitter``：预览表 与 「提示 + 锚点校验」之间加**可见分隔条与抓手**，
   默认比例 预览 70~75% / 提示 25~30%，初始尺寸 **至少 1000x700**；
2. 拖动即时生效、比例在本次对话框生命周期内保持；**尽力**记住上次位置
   （键名 ``grade_import/splitter_state``），读坏了要静默降级；
3. 提示区内容多时**自己滚**，不许把表格挤扁；
4. 表头常驻、行号常驻、30 门课两个方向都能滚、**列宽有下限**；
5. 既有功能一个字不改：列映射提示、异常行数、锚点命中、校验结论、
   确认 / 取消的返回值语义。

四条环境约定（照 ``tests/test_model_center_enhanced.py`` / ``test_profile_merge.py``）：

* **``QT_QPA_PLATFORM=offscreen`` 必须在 import PySide6 之前设**；
* **绝不碰真实用户目录** —— 状态文件路径由 autouse 夹具换成 ``tmp_path`` 下的
  ini，真 ``%LOCALAPPDATA%\\SGMS`` 一个字节都不动；
* **每个用例的模态窗必须自己收干净**（autouse 夹具 ``no_leaked_modals``）——
  ``show()`` 出来的 ``ReviewDialog`` 挂在 ``QApplication`` 的模态栈上，
  丢着不管会污染**下一个测试文件**（v1.9.1 实测：``tests/test_inference.py``
  的 ``modal_calls`` 因此一次报 15 个 ERROR）；
* 弹的是 **inline QTest 鼠标事件**（``QSplitter`` 的抓手逻辑挂在自己的
  ``mouseMoveEvent`` 上 —— 只调 ``setSizes`` 验不出"拖得动"，见 ``AGENTS.md`` 4h）。

★ 「不写注册表」这条铁律怎么守的：``QSettings`` 显式 ``IniFormat`` + **显式文件
路径**（``<数据根>/ui_state.ini``）。Windows 上 ``QSettings()`` 默认
``NativeFormat``，那是 ``HKEY_CURRENT_USER`` 注册表 —— 本文件第 "六" 节直接断言
格式与落盘路径，把这条选择钉住。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QEvent, QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
)

import ui.review_dialog as rd  # noqa: E402
from core.parser import Category, Course, parse_dataframe  # noqa: E402
from ui.review_dialog import (  # noqa: E402
    MIN_COLUMN_WIDTH,
    SPLITTER_KEY,
    ReviewDialog,
    confirm_courses,
)

#: 换掉夹具之前的**真**函数：用来断言默认落盘位置（数据目录，不是注册表）。
REAL_STATE_PATH = rd.state_path

#: 界面文案里不许出现的 Markdown 记号（``AGENTS.md`` §24 闸门）。
MARKDOWN_TOKENS = ("**", "__", "`")


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    """整个模块共用一个 QApplication（offscreen）。"""
    instance = QApplication.instance() or QApplication([])
    yield instance  # type: ignore[misc]


@pytest.fixture(autouse=True)
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """**每一条用例**都把状态文件钉到 tmp_path —— 真用户目录永不被碰。"""
    target = tmp_path / "ui_state.ini"
    monkeypatch.delenv(rd.STATE_ENV, raising=False)
    monkeypatch.setattr(rd, "state_path", lambda: target)
    return target


def _leaked_modal(before: list[Any]) -> Any:
    """返回"本次用例留下的"模态窗；干净 —— 或栈顶是别人留下的 —— 返回 ``None``。

    ``before`` 是本用例开始前就存在的顶层窗口：那些**不归本文件管**，
    既不能替别人关掉，也不能把别人的脏账算到自己头上。
    """
    modal = QApplication.activeModalWidget()
    if modal is None or any(modal is kept for kept in before):
        return None
    return modal


@pytest.fixture(autouse=True)
def no_leaked_modals(app: QApplication, state_file: Path) -> Iterator[None]:
    """★ 每条用例收尾：关掉**本用例创建**的顶层窗口，然后断言模态栈干净。

    为什么这条夹具非有不可（v1.9.1 实测，**测试侧的资源泄漏，不是产品 bug**）：

    ``ReviewDialog`` 用 ``show()`` 起来之后就是**进程级**的模态窗 —— 它挂在
    ``QApplication`` 自己的模态栈上，且自身有两处引用环
    （``splitterMoved -> lambda: self._save_timer.start()``、
    ``_save_timer.timeout -> self.remember_split``），Python 侧不会即时回收。
    于是"开一个、不管它"的用例会把对话框**留在进程里、留在模态栈上**。

    后果不是本文件红：跑到后面的 ``tests/test_inference.py`` 时，它的
    ``modal_calls`` 夹具在 teardown 里断言 ``QApplication.activeModalWidget() is None``
    —— 15 条用例一起报 ERROR，而错在**上一个文件**（与产品代码无关）。
    所以泄漏必须在**自己的文件里**先炸：收干净之后立刻验一次模态栈。

    ★ 顺序是有意的：夹具显式依赖 :func:`state_file`，所以这段 teardown 跑在
    ``monkeypatch`` 撤销**之前** —— 关窗会走 ``done() -> remember_split()``，
    写的是 tmp 里那个 ini，**真 ``%LOCALAPPDATA%\\SGMS`` 一个字节都不动**。
    """
    before = list(QApplication.topLevelWidgets())
    yield
    QApplication.processEvents()
    for widget in QApplication.topLevelWidgets():
        if any(widget is kept for kept in before):
            continue
        widget.close()  # 模态窗一关就退出 Qt 的模态栈（微探针验过：close 后 activeModalWidget 即 None）
        widget.deleteLater()
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    leftover = _leaked_modal(before)
    assert leftover is None, (
        f"用例结束时还有本文件创建的模态窗没关：{leftover!r} —— "
        "它会被下一个测试文件（tests/test_inference.py 的 modal_calls）逮住并报成别人的错"
    )


# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------


def course(index: int, *, risky: bool = False, name: str | None = None) -> Course:
    """一所真 :class:`core.parser.Course`（不是 duck-typing 的假对象）。"""
    return Course(
        name=name if name is not None else f"课程{index}",
        category=Category.UNKNOWN if risky else Category.REQUIRED,
        credit=0.0 if risky else 3.0,
        col_index=index + 2,
        credit_source="" if risky else "header:regex:3",
        category_source="" if risky else "header",
        warnings=["没抠出来"] if risky else [],
    )


def courses(count: int, *, risky_every: int = 0) -> list[Course]:
    return [
        course(i, risky=bool(risky_every) and i % risky_every == 0)
        for i in range(count)
    ]


def build(course_list: Any = None, **kwargs: Any) -> ReviewDialog:
    return ReviewDialog(
        courses(3) if course_list is None else course_list, **kwargs
    )


def write_state(path: Path, value: Any = None) -> None:
    """往状态文件里塞一个值。

    默认塞一段**合法但无意义**的字节 —— 好让 ``_restore_split`` 走到
    ``restoreState`` 那一步（键不存在时它会提前 return，那样测不到降级分支）。
    """
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    settings.setValue(
        SPLITTER_KEY, QByteArray(b"partial-state") if value is None else value
    )
    settings.sync()


def show(dialog: ReviewDialog) -> ReviewDialog:
    dialog.show()
    QApplication.processEvents()
    return dialog


def ratio(dialog: ReviewDialog) -> float:
    sizes = dialog.splitter.sizes()
    return sizes[0] / sum(sizes)


def labels_of(widget: Any) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


def all_texts(dialog: ReviewDialog) -> list[str]:
    texts = labels_of(dialog)
    texts += [button.text() for button in dialog.findChildren(QPushButton)]
    texts += [dialog.windowTitle()]
    texts += [
        str(item.text()) for item in (dialog.table.item(r, c)
                                      for r in range(dialog.table.rowCount())
                                      for c in range(dialog.table.columnCount()))
    ]
    texts += [dialog.table.horizontalHeaderItem(c).text()
              for c in range(dialog.table.columnCount())]
    return [t for t in texts if t]


def click_ok(dialog: ReviewDialog) -> None:
    QTest.mouseClick(dialog.buttons[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()


# ---------------------------------------------------------------------------
# 一、主体骨架：QSplitter / 默认比例 / 初始尺寸
# ---------------------------------------------------------------------------


def test_主体是左右两栏的QSplitter(app: QApplication) -> None:
    dialog = show(build(courses(5)))
    splitter = dialog.splitter
    assert isinstance(splitter, QSplitter)
    assert splitter.orientation() == Qt.Orientation.Horizontal
    assert splitter.count() == 2
    assert isinstance(splitter.widget(0), QTableWidget)
    assert isinstance(splitter.widget(1), QScrollArea)
    assert splitter.widget(0) is dialog.table


def test_默认比例是七三(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    value = ratio(dialog)
    assert 0.6 < value < 0.8, f"默认比例 {value:.4f} 不在 0.6~0.8"
    assert abs(value - 0.7) < 0.03, f"默认比例 {value:.4f} 偏离 70/30 太多"


def test_初始尺寸至少1000x700(app: QApplication) -> None:
    dialog = build(courses(30))
    assert dialog.width() >= 1000
    assert dialog.height() >= 700
    show(dialog)
    assert dialog.width() >= 1000
    assert dialog.height() >= 700


def test_分隔条可见且有宽度(app: QApplication) -> None:
    dialog = show(build(courses(5)))
    splitter = dialog.splitter
    assert splitter.handleWidth() >= 4
    handle = splitter.handle(1)
    assert handle is not None
    assert handle.isVisible()
    assert handle.width() == splitter.handleWidth()


def test_分隔条肉眼可见_颜色与背景拉开(app: QApplication) -> None:
    """§4c：只说"控件在、几何对"不算数 —— 分隔条与背景的**通道差**要量出来。"""
    dialog = show(build(courses(5)))
    handle = dialog.splitter.handle(1)
    image = handle.grab().toImage()
    assert not image.isNull()
    colour = image.pixelColor(handle.width() // 2, image.height() // 2)
    background = dialog.palette().color(dialog.backgroundRole())
    diff = max(
        abs(colour.red() - background.red()),
        abs(colour.green() - background.green()),
        abs(colour.blue() - background.blue()),
    )
    assert diff >= 20, (
        f"分隔条颜色 {colour.name()} 与背景 {background.name()} 太接近（差 {diff}），看不见"
    )
    assert dialog.splitter.handleWidth() >= 4


def test_两侧都不许被拖塌成0(app: QApplication) -> None:
    dialog = show(build(courses(5)))
    splitter = dialog.splitter
    assert splitter.childrenCollapsible() is False
    splitter.moveSplitter(-10_000, 1)
    QApplication.processEvents()
    left = splitter.sizes()[0]
    splitter.moveSplitter(10_000, 1)
    QApplication.processEvents()
    right = splitter.sizes()[1]
    assert left >= rd._MIN_PANE_WIDTHS[0]
    assert right >= rd._MIN_PANE_WIDTHS[1]


# ---------------------------------------------------------------------------
# 二、拖动：即时生效 + 生命周期内保持
# ---------------------------------------------------------------------------


def test_moveSplitter立即改变实际尺寸(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    before = list(dialog.splitter.sizes())
    dialog.splitter.moveSplitter(before[0] - 200, 1)
    QApplication.processEvents()
    after = list(dialog.splitter.sizes())
    assert after != before
    assert after[0] < before[0]
    assert dialog.table.width() == after[0]
    assert dialog.hint_scroll.width() == after[1]


def test_真实鼠标拖动抓手(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    splitter = dialog.splitter
    before = list(splitter.sizes())
    handle = splitter.handle(1)
    start = QPoint(handle.width() // 2, handle.height() // 2)
    target = QPoint(start.x() - 150, start.y())
    QTest.mousePress(
        handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start
    )
    QTest.mouseMove(handle, target)
    QTest.mouseRelease(
        handle, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target
    )
    QApplication.processEvents()
    after = list(splitter.sizes())
    assert after != before, "鼠标拖了抓手，尺寸却没变"
    assert after[0] < before[0]
    assert dialog.table.width() == after[0]


def test_拖动后比例在生命周期内保持(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    dialog.splitter.moveSplitter(350, 1)
    QApplication.processEvents()
    dragged = ratio(dialog)
    assert dragged < 0.6

    dialog.resize(1400, 800)
    QApplication.processEvents()
    dialog.layout().activate()
    dialog.table.repaint()
    dialog.repaint()
    QApplication.processEvents()
    assert abs(ratio(dialog) - dragged) < 0.02, "窗口一动，拖过的比例就跑了"

    dialog.resize(1000, 700)
    QApplication.processEvents()
    assert abs(ratio(dialog) - dragged) < 0.02


def test_拖动会起去抖保存定时器(app: QApplication, state_file: Path) -> None:
    dialog = show(build(courses(5)))
    assert dialog._save_timer.isActive() is False
    dialog.splitter.moveSplitter(300, 1)
    assert dialog._save_timer.isActive() is True
    assert not state_file.exists() or SPLITTER_KEY.split("/")[0] in state_file.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 三、提示区：自己滚，绝不挤表格
# ---------------------------------------------------------------------------


def test_提示区是QScrollArea(app: QApplication) -> None:
    dialog = show(build(courses(5)))
    assert isinstance(dialog.hint_scroll, QScrollArea)
    assert dialog.hint_scroll.widgetResizable() is True
    assert dialog.splitter.widget(1) is dialog.hint_scroll


def test_提示不多时不该出现滚动条(app: QApplication) -> None:
    dialog = show(build(courses(5), warnings=["提示一", "提示二"]))
    assert dialog.hint_scroll.verticalScrollBar().maximum() == 0


def test_提示很多时自己滚且表格不被挤扁(app: QApplication) -> None:
    dialog = show(
        build(
            courses(40, risky_every=3),
            extra_alerts=[f"锚点第 {i} 行学分对不上" for i in range(40)],
            score_alerts=[f"第 {i} 行第 3 列 = 101" for i in range(40)],
            warnings=[f"提示{i}" for i in range(8)],
        )
    )
    bar = dialog.hint_scroll.verticalScrollBar()
    assert bar.maximum() > 0, "提示区内容远超一屏，却滚不起来"
    bar.setValue(bar.maximum())
    QApplication.processEvents()
    assert bar.value() == bar.maximum()

    # ① 表格没被挤扁：宽度不小于下限，比例还在 0.6 以上
    assert dialog.table.width() >= rd._MIN_PANE_WIDTHS[0]
    assert ratio(dialog) > 0.6
    # ② 列宽也没被压
    widths = [dialog.table.columnWidth(c) for c in range(dialog.table.columnCount())]
    assert min(widths) >= MIN_COLUMN_WIDTH


def test_提示区横向不溢出_换行生效(app: QApplication) -> None:
    dialog = show(
        build(courses(10), extra_alerts=["很长的锚点校验说明" * 8 for _ in range(20)])
    )
    scroll = dialog.hint_scroll
    assert scroll.widget().width() <= scroll.viewport().width()
    assert scroll.horizontalScrollBar().maximum() == 0


def test_没有提示时有兜底文案(app: QApplication) -> None:
    dialog = show(build(courses(3)))
    texts = labels_of(dialog.hint_scroll.widget())
    assert "提示与校验" in texts
    assert any("没有需要额外注意的提示" in text for text in texts)


# ---------------------------------------------------------------------------
# 四、表格：表头 / 行号 / 列宽下限 / 两个方向都能滚
# ---------------------------------------------------------------------------


def test_表头常驻可见而且是交互式(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    header = dialog.table.horizontalHeader()
    assert header.isVisible()
    assert header.sectionsMovable() is False
    assert header.minimumSectionSize() == MIN_COLUMN_WIDTH
    for col in range(dialog.table.columnCount()):
        assert header.sectionResizeMode(col) == QHeaderView.ResizeMode.Interactive


def test_行号常驻可见(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    vheader = dialog.table.verticalHeader()
    assert vheader.isVisible()
    assert vheader.sectionResizeMode(0) == QHeaderView.ResizeMode.Fixed
    model = dialog.table.model()
    first = model.headerData(0, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole)
    last = model.headerData(29, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole)
    assert str(first) == "1"
    assert str(last) == "30"


def test_列宽不低于最小值_程序也压不扁(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    table = dialog.table
    widths = [table.columnWidth(c) for c in range(table.columnCount())]
    assert min(widths) >= MIN_COLUMN_WIDTH
    # 表头自己也不许把列调到下限以下
    table.setColumnWidth(2, 5)
    QApplication.processEvents()
    assert table.columnWidth(2) >= MIN_COLUMN_WIDTH
    assert table.horizontalHeader().minimumSectionSize() == MIN_COLUMN_WIDTH


def test_30门课两个方向都能滚(app: QApplication) -> None:
    dialog = show(build(courses(30)))
    table = dialog.table
    assert table.rowCount() == 30
    assert table.horizontalScrollBar().maximum() > 0
    assert table.verticalScrollBar().maximum() > 0
    assert (
        table.horizontalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert table.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_表头文字与列数没变(app: QApplication) -> None:
    dialog = show(build(courses(3)))
    assert dialog.table.columnCount() == 6
    headers = [
        dialog.table.horizontalHeaderItem(c).text()
        for c in range(dialog.table.columnCount())
    ]
    assert headers == ["#", "课程名", "类别", "学分", "学分来源", "类别来源"]
    assert dialog.table.item(0, 1).text() == "课程0"
    assert dialog.table.item(0, 3).text() == "3"
    assert dialog.table.item(0, 4).text() == "header:regex:3"


def test_风险行标红_正常行不标(app: QApplication) -> None:
    dialog = show(build([course(0), course(1, risky=True), course(2)]))
    flagged = dialog.table.item(1, 1).background().color().name()
    plain = dialog.table.item(0, 1).background().color().name()
    assert flagged.lower() == "#fde8e8"
    assert plain.lower() != "#fde8e8"


def test_空课程表也能开(app: QApplication) -> None:
    dialog = show(build([]))
    assert dialog.table.rowCount() == 0
    assert 0.6 < ratio(dialog) < 0.8


# ---------------------------------------------------------------------------
# 五、既有功能与返回值语义：一个字都没改
# ---------------------------------------------------------------------------


def test_确认返回True(app: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ReviewDialog, "exec", lambda self, *a, **k: QDialog.DialogCode.Accepted
    )
    assert confirm_courses(None, courses(3)) is True


def test_取消返回False(app: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ReviewDialog, "exec", lambda self, *a, **k: QDialog.DialogCode.Rejected
    )
    assert confirm_courses(None, courses(3)) is False


def test_点确认按钮result是Accepted(app: QApplication) -> None:
    dialog = show(build(courses(3)))
    click_ok(dialog)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_点返回修正result是Rejected(app: QApplication) -> None:
    dialog = show(build(courses(3)))
    QTest.mouseClick(dialog.buttons[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_关窗口等于返回修正(app: QApplication) -> None:
    dialog = show(build(courses(3)))
    dialog.close()
    QApplication.processEvents()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_按钮文案与默认键没变(app: QApplication) -> None:
    dialog = build(courses(3))
    back, ok = dialog.buttons
    assert back.text() == "返回修正"
    assert ok.text() == "确认无误，开始计算"
    assert ok.isDefault() is True
    assert "确认无误才会计算" in " ".join(
        label.text() for label in dialog.findChildren(QLabel)
    )


def test_风险门数的说法没变(app: QApplication) -> None:
    dialog = show(build([course(0, risky=True), course(1, risky=True), course(2)]))
    texts = labels_of(dialog.hint_scroll.widget())
    assert any("有 2 门课" in text and "标红" in text for text in texts)


def test_锚点校验与越界成绩分栏没变(app: QApplication) -> None:
    dialog = show(
        build(
            courses(3),
            extra_alerts=["第 7 行 所得学分 对不上"],
            score_alerts=["第 12 行 第 4 列 = 101"],
            warnings=["列数少了 1 列"],
        )
    )
    texts = labels_of(dialog.hint_scroll.widget())
    anchor = next(t for t in texts if t.startswith("锚点校验："))
    score = next(t for t in texts if t.startswith("⚠ 成绩超出 0~100"))
    notes = next(t for t in texts if t.startswith("解析提示："))
    assert "第 7 行 所得学分 对不上" in anchor
    assert "第 12 行 第 4 列 = 101" in score
    assert "列数少了 1 列" in notes


def test_解析提示仍然只显示前六条(app: QApplication) -> None:
    dialog = show(build(courses(3), warnings=[f"提示{i}" for i in range(9)]))
    notes = next(
        t for t in labels_of(dialog.hint_scroll.widget()) if t.startswith("解析提示：")
    )
    assert notes.count("·") == 6


def test_窗口默认标题与模态没变(app: QApplication) -> None:
    dialog = build(courses(3))
    assert dialog.windowTitle() == "请核对提取结果"
    assert dialog.isModal() is True
    other = build(courses(3), title="请核对提取结果（德育分）")
    assert other.windowTitle() == "请核对提取结果（德育分）"


def test_界面文案没有Markdown记号(app: QApplication) -> None:
    dialog = show(
        build(
            courses(5, risky_every=2),
            extra_alerts=["锚点对不上"],
            score_alerts=["101 分"],
            warnings=["提示一"],
        )
    )
    for text in all_texts(dialog):
        for token in MARKDOWN_TOKENS:
            assert token not in text, f"界面文案出现 Markdown 记号 {token!r}：{text}"


def test_界面文案没有激活二字(app: QApplication) -> None:
    dialog = show(build(courses(5), warnings=["提示一"]))
    for text in all_texts(dialog):
        assert "激活" not in text


# ---------------------------------------------------------------------------
# 六、记住上次位置：ini 文件，绝不进注册表
# ---------------------------------------------------------------------------


def test_键名就是任务书那个() -> None:
    assert SPLITTER_KEY == "grade_import/splitter_state"


def test_状态文件是显式ini而不是注册表(
    app: QApplication, state_file: Path
) -> None:
    settings = rd._settings()
    assert settings.format() == QSettings.Format.IniFormat
    assert Path(settings.fileName()) == state_file
    lowered = str(settings.fileName()).lower()
    assert "hkey" not in lowered and "registry" not in lowered


def test_默认状态文件在数据目录里(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真 ``state_path()`` 落在 ``%LOCALAPPDATA%\\SGMS`` —— 一个普通 ini，不是注册表。"""
    monkeypatch.delenv(rd.STATE_ENV, raising=False)
    path = REAL_STATE_PATH()
    assert path.name == "ui_state.ini"
    assert path.parent.name == "SGMS"
    assert path.is_absolute()
    assert "HKEY" not in str(path)


def test_环境变量可以覆盖状态文件路径(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "green" / "state.ini"
    monkeypatch.setenv(rd.STATE_ENV, str(target))
    assert REAL_STATE_PATH() == target


def test_写入后磁盘上真有ini文件(app: QApplication, state_file: Path) -> None:
    dialog = show(build(courses(5)))
    dialog.splitter.moveSplitter(300, 1)
    QApplication.processEvents()
    dialog.remember_split()

    assert state_file.exists()
    text = state_file.read_text(encoding="utf-8")
    assert "[grade_import]" in text
    assert "splitter_state" in text

    stored = QSettings(str(state_file), QSettings.Format.IniFormat).value(SPLITTER_KEY)
    assert isinstance(stored, QByteArray)


def test_读回上次拖到的位置(app: QApplication, state_file: Path) -> None:
    first = show(build(courses(30)))
    first.splitter.moveSplitter(350, 1)
    QApplication.processEvents()
    dragged = first.splitter.sizes()
    first.remember_split()
    first.close()

    second = show(build(courses(30)))
    assert abs(second.splitter.sizes()[0] - dragged[0]) <= 4
    assert abs(ratio(second) - dragged[0] / sum(dragged)) < 0.02


def test_关窗时会把最后位置记下来(app: QApplication, state_file: Path) -> None:
    dialog = show(build(courses(30)))
    dialog.splitter.moveSplitter(320, 1)
    QApplication.processEvents()
    moved = list(dialog.splitter.sizes())
    dialog.reject()
    assert state_file.exists()
    again = show(build(courses(30)))
    assert abs(again.splitter.sizes()[0] - moved[0]) <= 4


def test_损坏的字节值静默降级(app: QApplication, state_file: Path) -> None:
    state_file.write_text(
        "[grade_import]\nsplitter_state=garbage!!\n", encoding="utf-8"
    )
    dialog = show(build(courses(30)))
    assert abs(ratio(dialog) - 0.7) < 0.03


def test_非字节垃圾值静默降级(app: QApplication, state_file: Path) -> None:
    state_file.write_text("[grade_import]\nsplitter_state=12345\n", encoding="utf-8")
    dialog = show(build(courses(30)))
    assert abs(ratio(dialog) - 0.7) < 0.03


def test_空文件静默降级(app: QApplication, state_file: Path) -> None:
    state_file.write_text("", encoding="utf-8")
    dialog = show(build(courses(30)))
    assert abs(ratio(dialog) - 0.7) < 0.03


def test_写入点其实是目录也不抛(
    app: QApplication, state_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(rd, "state_path", lambda: blocker / "sub" / "ui_state.ini")

    dialog = show(build(courses(5)))
    dialog.remember_split()  # 不许抛
    assert abs(ratio(dialog) - 0.7) < 0.03


class _FakeSplitter:
    """只实现 ``_restore_split`` 用到的那三个方法，用来钉住坏比例降级分支。"""

    def __init__(self, sizes: list[int], ok: bool = True) -> None:
        self._sizes = list(sizes)
        self._ok = ok
        self.sets: list[list[int]] = []

    def restoreState(self, blob: Any) -> bool:
        return self._ok

    def sizes(self) -> list[int]:
        return list(self._sizes)

    def setSizes(self, values: Any) -> None:
        self.sets.append(list(values))
        self._sizes = list(values)


def test_坏比例被挡回默认(app: QApplication, state_file: Path) -> None:
    write_state(state_file)
    dialog = build(courses(3))
    fake = _FakeSplitter([990, 10])  # 提示区只剩 1%
    dialog.splitter = fake  # type: ignore[assignment]
    dialog._restore_split()
    assert fake.sets and fake.sets[0] == list(rd._DEFAULT_SIZES)


def test_零宽度被挡回默认(app: QApplication, state_file: Path) -> None:
    write_state(state_file)
    dialog = build(courses(3))
    fake = _FakeSplitter([0, 954])
    dialog.splitter = fake  # type: ignore[assignment]
    dialog._restore_split()
    assert fake.sets and fake.sets[0] == list(rd._DEFAULT_SIZES)


def test_好状态原样保留(app: QApplication, state_file: Path) -> None:
    write_state(state_file)
    dialog = build(courses(3))
    fake = _FakeSplitter([620, 340])
    dialog.splitter = fake  # type: ignore[assignment]
    dialog._restore_split()
    assert fake.sets == []


def test_restoreState说读不懂就不动尺寸(app: QApplication, state_file: Path) -> None:
    write_state(state_file)
    dialog = build(courses(3))
    fake = _FakeSplitter([620, 340], ok=False)
    dialog.splitter = fake  # type: ignore[assignment]
    dialog._restore_split()
    assert fake.sets == []


# ---------------------------------------------------------------------------
# 七、和真解析器联调：30 门课那张表真能开出来
# ---------------------------------------------------------------------------


def test_真解析器30门课也能开(app: QApplication) -> None:
    headers = ["学号", "姓名"] + [f"课程{i}(3学分)必修" for i in range(30)]
    row: list[Any] = ["2023001", "张三"] + [80 + (i % 10) for i in range(30)]
    parsed = parse_dataframe(pd.DataFrame([headers, row]))
    assert len(parsed.courses) == 30

    dialog = show(build(parsed.courses))
    assert dialog.table.rowCount() == 30
    assert dialog.table.item(0, 1).text() == "课程0"
    assert 0.6 < ratio(dialog) < 0.8
    assert min(
        dialog.table.columnWidth(c) for c in range(dialog.table.columnCount())
    ) >= MIN_COLUMN_WIDTH
