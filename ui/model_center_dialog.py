"""「模型中心」对话框 —— 卡片流：一个窗口看全清单、点选、按顺序下载。

这个对话框回答四个问题：

1. **有哪些模型能用** —— 从 Gitee 上的清单（``update/ai_models.json``）取，
   再补上"本机已经有、清单里没写"的那些；
2. **哪些这台机器跑得动** —— 两套判据，**别搞混**（``AGENTS.md`` 第 31 节）：
   内存闸门（只看 ``minRamGb``）不够的卡片**点不动**；
   负载指示（内存 + 显存，三档「轻松 / 勉强 / 困难」）决定初始选中谁、
   以及卡片右下角那个彩色圆点的颜色；「困难」的卡片同样**点不动**
   （见下面 ★ 「困难」卡片不响应选中 那一段）；
3. **下到哪儿、下完算不算数** —— 落到 ``models/<id>/<文件>.gguf``，
   下完校大小和 SHA256，过了才记账；
4. **怎么把它弄走** —— 已装的卡片右下角有「卸载」小按钮（删目录 + 记账），
   下载两条命都失败的那张卡片有一个「复制链接」兜底。

★ 绝不自动下载
============

``AGENTS.md`` 的规矩：任何一次下载都必须由用户**明确点过**。
所以这里没有"打开就开始下"的路径 —— 打开对话框只读一个几 KB 的清单
（只读 GET，不碰模型文件），真正下载要用户先点卡片选中、再点底部的
「下载选中（N）」、再在确认框里点"是"。三道手，缺一不可。

★ 为什么下载在后台线程里
======================

一个 GGUF 是 1~3 GB，走 urllib 同步读就是几分钟。这中间如果占着主线程，
整个界面会变成一块白板 —— 用户连"取消"都点不动。
所以：**下载 + 校验都在 ``ui.workers.run_async`` 的工作线程里跑**，
进度通过 :class:`_ProgressRelay` 的信号回到主线程，
校验（要读完整个文件算哈希）同样不在主线程做。

★ v1.9.1：六列表格 → 卡片流
==========================

排版：``QScrollArea`` + 一个 ``QGridLayout`` 容器，卡片按**固定列数**排
（:data:`GRID_COLUMNS`），每张卡片的宽度由视线宽度平均分。**没有引入任何新库**
（不用 QML / QtQuick，纯 PySide6 控件）。

**选中**是卡片级的：点一下卡片切换选中，选中的卡片边框变成 2px 的
:data:`SELECTED_BORDER_COLOR`；卡片**内部没有下载按钮**，下载统一走底部那个
「下载选中（N）」—— N 就是当前选中的张数，按**卡片在界面上的顺序**依次下。

★★ 一个 widget 只有一个 ``graphicsEffect``（``AGENTS.md`` 4a / 4b）
=================================================================

卡片挂了阴影（:class:`QGraphicsDropShadowEffect`）之后**绝不能再挂**
``QGraphicsOpacityEffect`` —— 两个效果抢的是同一个槽位，后挂的那个会把阴影顶掉；
更糟的是 ``QGraphicsOpacityEffect`` 会缓存源 pixmap，之后卡片内部增删控件
（下载完重画、状态文案变化）就会画出一块空白。

所以「困难」卡片要的"整体透明度降低"**不用效果实现**，改用
**调色板 + 样式表**（:meth:`ModelCard._refresh_visuals`：字号照旧、
文字与背景一起淡化）。这条有 :func:`_assert_no_opacity_effect` 兜底 ——
它是个**运行时**闸门，不是只写在注释里的约定。

★ 「困难」卡片**不响应选中**（v1.9.1 施工单）
==========================================

``ModelChoice.runnable`` 在数据层写的是"困难只是不预勾、下载按钮置灰"，
v1.9.1 的施工单把界面这一侧收得更紧：**困难卡片点了也不选中**
（:meth:`ModelCard.can_select` 里那一条 ``not self._hard()``），
底部「下载选中（N）」自然也就数不到它，鼠标悬浮给一句
:data:`HARD_LOAD_TOOLTIP`。这条是**界面的**决定，数据层一个字没改。

★ 界面文案一律纯文本（B4）
========================

负载那一格是**彩色圆点 + 中文**（圆点用 QSS 的 ``border-radius`` 磨圆）
—— **不用 emoji**（``AGENTS.md`` 4l：开屏标题里一个 emoji 值 241.7ms；
而且 emoji 在不同机器上字形差别很大）。颜色与文案的唯一真源是
:data:`core.ai.model_registry.LOAD_COLORS` / ``LOAD_LABELS``，这里只取用、不另抄。
所有 ``QLabel`` / tooltip 里不许出现 ``**`` / ``__`` / 反引号（铁律 9）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.ai.model_downloader import MIN_MODEL_BYTES, delete_model, model_dir
from core.ai.model_registry import (
    GITEE_RAW_MODELS,
    LOAD_COLORS,
    LOAD_HARD,
    SIZE_TOLERANCE,
    STATE_INSTALLED,
    ModelChoice,
    RemoteModel,
    build_choices,
    describe_download_plan,
    fetch_model_list,
    forget_installed,
    group_choices,
    human_size_bytes,
    merged_with_local,
    record_installed,
)
from core.updater import DownloadCancelled, download_exe, verify_checksum
from ui.workers import run_async

__all__ = ["ModelCenterDialog", "ModelCard", "ModelVerifyError", "open_model_center"]

#: 本模块的日志器。后端组件缺失 / 检查失败**只记日志**，模型中心照常打开。
_log = logging.getLogger(__name__)


def _backend_available() -> bool:
    """本机的推理后端装好没有。

    ★ 契约来自 :mod:`core.ai.runtime_setup`（v1.9.2）。并行开发期间它可能还
      没落地 —— 那种情况下**按"可用"处理**：不插安装卡片，也绝不崩，
      模型中心照常打开（本模块的既有行为一个字不变）。
    """
    try:
        from core.ai.runtime_setup import is_backend_available  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 还没落地：不插卡片，界面照开
        return True
    try:
        return bool(is_backend_available())
    except Exception:  # noqa: BLE001 - 检查失败按可用处理，别误报"未安装"
        _log.warning("检查推理后端是否可用失败，按可用处理", exc_info=True)
        return True

#: 对话框标题。
DIALOG_TITLE = "模型中心"

#: 顶部那行说明。★ 不许出现 Markdown 记号（``AGENTS.md`` 有文案闸门）。
INTRO_TEXT = (
    "模型都在这台电脑上跑，下载完就不需要联网了。"
    "下载地址来自 Gitee 上的模型清单，支持断点续传，下完会按清单里的 "
    "SHA256 校验一遍。负载是按这台电脑的内存和显存算的："
    "轻松 = 够用，勉强 = 能跑但会慢，困难 = 可能带不动。"
    "点一下卡片选中它，再点底部的「下载选中」。"
)

#: 失败卡片标红用的颜色（和分析页的警示色保持一致）。
DANGER_COLOR = "#DC2626"

#: 「困难」卡片置灰用的颜色（比正文浅一档，但仍看得清字）。
MUTED_COLOR = "#9CA3AF"

#: 视觉降级时卡片背景用的淡灰（**不是**透明度效果，见模块头 4a 那一段）。
MUTED_BG_COLOR = "#F1F5F9"

#: 选中卡片的高亮边框色（施工单指定）。
SELECTED_BORDER_COLOR = "#0078D4"

#: 一个模型最多尝试几次（首次 + 自动重试 1 次）。
MAX_ATTEMPTS = 2

#: ★ 卸载确认框的文案 —— **一个字都不许改**（施工单逐字指定）。
UNINSTALL_CONFIRM_TEXT = "卸载后将无法使用 AI 功能，确定？"

#: 「困难」卡片的悬浮气泡。
HARD_LOAD_TOOLTIP = "当前电脑配置可能无法流畅运行此模型"

#: 复制链接成功的提示（B5 的兜底路径就靠这一句告诉用户把文件放哪）。
COPY_LINK_HINT = "已复制到剪贴板，请浏览器下载后放入 models/ 目录。"

#: 卡片上「复制链接」按钮的气泡（不下载任何东西，只是把直链放进剪贴板）。
COPY_LINK_TIP = "把这个模型的下载地址复制到剪贴板，用浏览器下好以后放进 models/ 目录。"

#: 清单读不到时，教用户自己把这份清单下下来（界面照开，只是没有条目）。
MANIFEST_FALLBACK_HINT = "清单读不到。可以先点「复制链接」把清单地址复制下来，用浏览器打开：" + GITEE_RAW_MODELS

# ---------------------------------------------------------------------------
# 卡片流的排版参数
# ---------------------------------------------------------------------------

#: 卡片最小宽度（比它更窄就没法读说明文字了）。
CARD_MIN_WIDTH = 280

#: 卡片最小高度。
CARD_MIN_HEIGHT = 200

#: 一行放几张卡片（施工单要的"固定列数，按宽度算"）。
GRID_COLUMNS = 3

#: 卡片之间的间距。
CARD_SPACING = 12

#: 卡片外那一圈（阴影和选中边框都在这里画）。
CARD_FRAME_MARGIN = 6

#: 卡片内容默认的内边距。
CARD_PADDING = 12

#: 悬浮时内容整体上移的像素数（施工单指定 2px）。
HOVER_LIFT_PX = 2

#: 卡片圆角。
CARD_RADIUS = 10

#: 「已安装」徽章的绿色（和 QSS 的 TagGreen 同一套语感）。
BADGE_BG_COLOR = "#DCFCE7"

#: 「已安装」徽章的文字色。
BADGE_TEXT_COLOR = "#15803D"

# 阴影三档：常态 / 悬浮（阴影"加深"）/ 视觉降级（仍然只挂阴影，绝不换效果）
SHADOW_BLUR_IDLE = 14
SHADOW_BLUR_HOVER = 26
SHADOW_OFFSET_IDLE = 2
SHADOW_OFFSET_HOVER = 6
SHADOW_ALPHA_IDLE = 26
SHADOW_ALPHA_HOVER = 54

#: 卡片本体的样式（用内联样式表是因为 ``ui/styles.qss`` 不在本模块的写范围内，
#: 而卡片的高亮边框要跟着选中状态来回变 —— 内联样式表改起来是一处真源）。
#:
#: ★ 边框宽度**只由选中状态决定**（1px ↔ 2px）：Qt 的 QSS ``border`` 不参与
#:   布局几何计算，所以切边框不会让卡片跳位，也不会把内容顶歪。
CARD_STYLE = f"""
QFrame#ModelCard {{
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: {CARD_RADIUS}px;
}}
QFrame#ModelCard[tlSelected="true"] {{
    border: 2px solid {SELECTED_BORDER_COLOR};
}}
QFrame#ModelCard[tlVisualState="muted"] {{
    background: {MUTED_BG_COLOR};
}}
QLabel {{
    background: transparent;
    border: none;
}}
"""

#: 「已安装」绿色徽章。
BADGE_STYLE = (
    f"background-color: {BADGE_BG_COLOR}; color: {BADGE_TEXT_COLOR};"
    "border-radius: 9px; padding: 2px 10px; font-size: 12px; font-weight: 700;"
)

#: 「卸载」小按钮（文本是 ×，**不用 emoji**）。
UNINSTALL_STYLE = (
    "QPushButton {"
    "background: #FFFFFF; color: #64748B;"
    "border: 1px solid #E2E8F0; border-radius: 6px;"
    "font-size: 13px; font-weight: 700; padding: 0;"
    "}"
    "QPushButton:hover { background: #FEF2F2; color: #EF4444;"
    " border: 1px solid #FCA5A5; }"
    "QPushButton:disabled { color: #CBD5E1; border: 1px solid #E2E8F0; }"
)

#: 卡片上那个「复制链接」兜底按钮（只在前两次下载都失败时出现）。
COPY_LINK_STYLE = (
    "QPushButton {"
    "background: #FFFFFF; color: #2563EB;"
    "border: 1px solid #BFDBFE; border-radius: 6px;"
    "font-size: 12px; padding: 1px 10px;"
    "}"
    "QPushButton:hover { background: #EFF6FF; }"
    "QPushButton:disabled { color: #CBD5E1; border: 1px solid #E2E8F0; }"
)


class ModelVerifyError(RuntimeError):
    """下回来的东西没通过校验（大小对不上 / SHA256 不对）。"""


def has_opacity_effect(widget: QWidget) -> bool:
    """``widget`` 自己身上挂着 ``QGraphicsOpacityEffect`` 吗。

    只查它**自己** —— 阴影挂在卡片自己的槽位上，所以卡片有阴影**不算**冲突；
    这条判据要抓的是"给挂了阴影的卡片又叠一层透明度"。
    """
    return isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect)


def _assert_no_opacity_effect(widget: QWidget) -> None:
    """★★ 运行时闸门：挂了阴影的卡片（或它的任何子控件）**绝不许**再挂透明度效果。

    ``AGENTS.md`` 4a / 4b：一个 widget 只有一个 ``graphicsEffect``；
    阴影被透明度顶掉只是不好看，真正致命的是 ``QGraphicsOpacityEffect``
    会缓存源 pixmap —— 卡片内部一增删控件就是一块空白（静态断言查不出来）。

    所以这条不是注释里的君子协定，是**每次显示卡片时都要走一遍**的检查。
    """
    if has_opacity_effect(widget):
        raise RuntimeError(
            "模型卡片不许挂 QGraphicsOpacityEffect：一个 widget 只能有一个 "
            "graphicsEffect，卡片已经挂了阴影（见 AGENTS.md 4a / 4b）。"
            "需要视觉降级请走样式表 / 调色板。"
        )
    for child in widget.findChildren(QWidget):
        if has_opacity_effect(child):
            raise RuntimeError(
                "模型卡片的子控件不许挂 QGraphicsOpacityEffect —— "
                "会缓存源 pixmap，卡片一变就是一整块空白（AGENTS.md 4b）。"
            )


def make_shadow(**overrides: Any) -> QGraphicsDropShadowEffect:
    """造一层卡片阴影。参数只为测试与悬浮态微调留口子。"""
    blur = int(overrides.get("blur", SHADOW_BLUR_IDLE))
    offset = int(overrides.get("offset", SHADOW_OFFSET_IDLE))
    alpha = int(overrides.get("alpha", SHADOW_ALPHA_IDLE))
    effect = QGraphicsDropShadowEffect()
    effect.setBlurRadius(blur)
    effect.setOffset(0, offset)
    tint = QColor("#0F172A")
    tint.setAlpha(alpha)
    effect.setColor(tint)
    return effect


class _ProgressRelay(QObject):
    """把后台线程里的下载进度送回主线程。

    ★ 跨线程绝不能直接摸控件：``download_exe`` 的 ``progress_callback``
      是在**工作线程**里同步调用的。信号在 QObject 这一侧完成队列化投递，
      槽函数回到主线程执行 —— 这才是安全的做法。
    """

    progress = Signal(int, int)


def _size_close(actual: int, expected: int) -> bool:
    """实测字节数和清单声称的字节数算不算"对得上"。

    ★ 留 1% 余量（:data:`core.ai.model_registry.SIZE_TOLERANCE`）：
      清单里的体积常常是四舍五入过的（"1.12 GB"这种写法），
      要求逐字节相等会把正常文件判死；而网盘错误页只有几 KB，
      1% 的余量照样拦得住。
    """
    if expected <= 0:
        return True
    return abs(actual - expected) <= max(4096, int(expected * SIZE_TOLERANCE))


def _target_path(model: RemoteModel, folder: Path | None = None) -> Path:
    """这个模型该落到哪儿：``models/<清洗过的 id>/<文件名>``。

    ★ 文件名必须以 ``.gguf`` 结尾 —— ``installed_models()`` 只认这个后缀，
      下回来一个 ``xxx.bin`` 在界面上等于"没装"。
      清单里的 ``file`` 来自网络，所以先取 ``Path(...).name`` 掐掉任何路径。

    Args:
        folder: 目标目录（测试注入临时目录用）；``None`` = 走
            :func:`core.ai.model_downloader.model_dir` 的默认位置。
    """
    if folder is None:
        folder = model_dir(model.id)
    name = Path(model.file or "").name
    if not name.lower().endswith(".gguf"):
        stem = "".join(
            ch if (ch.isalnum() or ch in "._-") else "_"
            for ch in (name or model.id)
        ).strip("._") or "model"
        name = f"{stem}.gguf"
    return folder / name


def _verify(model: RemoteModel, path: Path) -> dict[str, Any]:
    """下完之后的校验。**在工作线程里跑**（算哈希要读完整个文件）。

    Returns:
        ``{"path": 文件, "size": 实测字节数, "note": 给人看的一句话}``。

    Raises:
        ModelVerifyError: 没通过。
    """
    actual = int(path.stat().st_size)

    size_note = ""
    if model.size > 0 and not _size_close(actual, model.size):
        if not model.sha256:
            raise ModelVerifyError(
                f"这一条没有 SHA256，只校验了大小，而大小对不上："
                f"下回来 {human_size_bytes(actual)}，清单写的是 {model.size_text}"
            )
        # ★ 有 SHA256 时它才是主判据：体积那句话降级成提示，不判失败。
        #   否则清单里一个四舍五入过的体积就能让一次正常下载白费。
        size_note = (
            f"体积和清单略有出入（实测 {human_size_bytes(actual)}，"
            f"清单 {model.size_text}）"
        )

    if model.sha256:
        if not verify_checksum(path, model.sha256):
            raise ModelVerifyError(
                "SHA256 校验没过 —— 文件可能在传输中损坏，或者清单里的哈希不对"
            )
        checksum_note = "SHA256 校验通过"
    else:
        checksum_note = "这一条没有 SHA256，只校验了大小"

    return {
        "path": path,
        "size": actual,
        "note": "；".join(part for part in (checksum_note, size_note) if part),
    }


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------


class ModelCard(QFrame):
    """一张模型卡片。

    结构（从外到内）::

        ModelCard（QFrame：挂阴影 + 画边框，**唯一的 graphicsEffect 槽位**）
        └── self.content（QWidget：承载全部内容；悬浮时改它的
                           contentsMargins 把内容整体上移 2px）

    ★ 为什么上移要改 ``content`` 的边距、而不是 ``move()`` 整个卡片：
      ``move()`` 改的是卡片在**父容器**里的位置，会让 QGridLayout 里的卡片
      跳一下（施工单点名不许跳位）；改内层容器的上边距只动**卡片内部**的几何，
      卡片自己的位置和尺寸一个像素都不变。上下边距一减一加，总和不变，
      所以 ``sizeHint()`` 也不变 —— 连"卡片被自己的 hover 撑高"都不会发生。
    """

    def __init__(self, dialog: "ModelCenterDialog", choice: ModelChoice) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._choice = choice

        self.setObjectName("ModelCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumSize(CARD_MIN_WIDTH, CARD_MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(CARD_STYLE)
        # ★ 动态属性一律带 tl 前缀（AGENTS.md 4f）：``selected`` / ``visualState``
        #   这种名字有和 QWidget 自带可读属性撞车的风险 —— 撞上了 setProperty
        #   会**静默失败**（不抛异常、返回 False），QSS 里那条 [xxx="true"]
        #   选择器就永远匹配不上：样式没生效，日志干净。写进去之后一律读回来确认。
        self.setProperty("tlSelected", "false")
        self.setProperty("tlVisualState", "normal")

        self._selected = False
        self._built_installed = False

        # ★ 唯一的 graphicsEffect：阴影。全程只有这一个效果，
        #   绝不在这里 setGraphicsEffect(QGraphicsOpacityEffect())。
        self.shadow = make_shadow()
        self.setGraphicsEffect(self.shadow)

        outer = QVBoxLayout(self)
        # 边框画在卡片自己身上，这圈边距是留给阴影和 2px 高亮边框的。
        outer.setContentsMargins(
            CARD_FRAME_MARGIN, CARD_FRAME_MARGIN, CARD_FRAME_MARGIN, CARD_FRAME_MARGIN
        )
        outer.setSpacing(0)

        self.content = QWidget(self)
        self.content.setObjectName("ModelCardContent")
        self.content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.content)

        self._build_content()
        # 常态：内容不偏移（悬浮时才上移 HOVER_LIFT_PX）
        self._set_content_margin(0)

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------

    def _build_content(self) -> None:
        model = self._choice.model

        layout = QGridLayout(self.content)
        layout.setContentsMargins(CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING)
        layout.setVerticalSpacing(6)
        layout.setHorizontalSpacing(8)
        layout.setColumnStretch(0, 1)

        # 第一行：模型名称（大号粗体）。换行的 label 必须给宽度约束，
        # 否则它会把窗口越撑越宽（AGENTS.md 4c 第 1 条）。
        self.name_label = QLabel(model.display, self.content)
        self.name_label.setWordWrap(True)
        self.name_label.setMinimumWidth(160)
        self.name_label.setStyleSheet("font-size: 15px; font-weight: 700; color: #1F2430;")
        layout.addWidget(self.name_label, 0, 0, 1, 2)

        # 第二行：大小 + 负载指示
        size_label = QLabel(model.size_text or "未知大小", self.content)
        size_label.setObjectName("Muted")
        layout.addWidget(size_label, 1, 0)

        load_holder = QWidget(self.content)
        load_layout = QHBoxLayout(load_holder)
        load_layout.setContentsMargins(0, 0, 0, 0)
        load_layout.setSpacing(6)
        # ★ 颜色与文案都取 ModelChoice 上的那三个 property（它们的真源是
        #   core.ai.model_registry 的 LOAD_COLORS / LOAD_LABELS），
        #   这里**不自己抄一份表**、也不自己判等级。
        color = self._choice.load_color
        # ★ 彩色圆点：一个 10x10 的小方块 + border-radius 磨成圆。
        #   不用 "●" 字符、更不用 emoji（4l：一个 emoji 值 241.7ms）。
        self.load_dot = QLabel("", load_holder)
        self.load_dot.setFixedSize(10, 10)
        self.load_dot.setStyleSheet(
            f"background-color: {color}; border-radius: 5px; border: none;"
        )
        # ★ 文案取 LOAD_LABELS（唯一真源），不在这里手写「轻松 / 勉强 / 困难」。
        self.load_text = QLabel(self._choice.load_text, load_holder)
        self.load_text.setStyleSheet(f"color: {color}; border: none;")
        load_layout.addWidget(self.load_dot)
        load_layout.addWidget(self.load_text)
        load_layout.addStretch(1)
        load_holder.setToolTip(self._load_tooltip())
        self.load_holder = load_holder
        # 整张卡片都能悬浮出气泡（施工单：困难卡片要"鼠标悬浮气泡"）
        self.setToolTip(self._card_tooltip())
        layout.addWidget(load_holder, 1, 1)

        # 第三行：清单里的 note（推荐信息）
        note = QLabel(model.note or "清单里没有写这条的说明。", self.content)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setMinimumWidth(160)
        note.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(note, 2, 0, 1, 2)
        self.note_label = note

        # 第四行：状态 + 已安装徽章 + 卸载小按钮
        self.status_label = QLabel("", self.content)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(80)
        layout.addWidget(self.status_label, 3, 0)

        bottom = QWidget(self.content)
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(6)

        self.badge = QLabel("已安装", bottom)
        self.badge.setStyleSheet(BADGE_STYLE)
        self.badge.setToolTip("这个模型的 .gguf 文件已经在硬盘上了。")
        bottom_layout.addWidget(self.badge)

        # 「复制链接」是**失败兜底**，只在下载两次都没成的那张卡片上出现
        # （施工单 B5：重试 1 次仍失败 → 复制链接）。卡片内部只有这一个按钮
        # 跟下载有关，而且它不下载任何东西 —— 只是把直链放进剪贴板。
        self.copy_btn = QPushButton("复制链接", bottom)
        self.copy_btn.setMinimumHeight(22)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setStyleSheet(COPY_LINK_STYLE)
        self.copy_btn.setToolTip(COPY_LINK_TIP)
        self.copy_btn.clicked.connect(
            lambda _checked=False: self._dialog._on_copy_link(self._choice.model.id)
        )
        bottom_layout.addWidget(self.copy_btn)

        self.uninstall_btn = QPushButton("×", bottom)
        self.uninstall_btn.setFixedSize(22, 22)
        self.uninstall_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.uninstall_btn.setStyleSheet(UNINSTALL_STYLE)
        self.uninstall_btn.setToolTip(
            "把已经下好的模型文件从硬盘上删掉，需要时可以重新下载。"
        )
        self.uninstall_btn.clicked.connect(
            lambda _checked=False: self._dialog._on_uninstall(self._choice.model.id)
        )
        bottom_layout.addWidget(self.uninstall_btn)
        layout.addWidget(bottom, 3, 1)

        layout.setRowStretch(2, 1)

    def _set_content_margin(self, lift: int) -> None:
        """设置内容容器的内边距（悬浮上移就靠它）。

        ★ 上下边距**一减一加**（上 -2 / 下 +2），总和恒定 —— 卡片的
          ``sizeHint()`` 因此不变，QGridLayout 不会重排、卡片不会跳位。
        ★ 改的是 ``content`` 这个**内层容器**的边距，不是卡片的 ``move()``：
          移动卡片会改变它在父布局里的位置（施工单点名不许跳位）。
        """
        base = CARD_PADDING - CARD_FRAME_MARGIN
        self.content.setContentsMargins(
            CARD_PADDING, base - lift, CARD_PADDING, base + lift
        )

    # ------------------------------------------------------------------
    # 气泡文案
    # ------------------------------------------------------------------

    def _load_tooltip(self) -> str:
        note = self._choice.load_note or ""
        text = self._choice.load_text
        return f"{text}：{note}" if note else text

    def _card_tooltip(self) -> str:
        """整张卡片的气泡：清单说明 +（困难时）那句"可能带不动"。

        ★ 「困难」的那一句必须是 :data:`HARD_LOAD_TOOLTIP` 原文 ——
          施工单要求鼠标悬浮就看见它（测试按原文断言）。清单里的说明
          拼在它前面，不替换它。
        """
        parts = [self._choice.model.note] if self._choice.model.note else []
        if self._hard():
            parts.append(HARD_LOAD_TOOLTIP)
        return "；".join(parts)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._choice.model.id

    @property
    def choice(self) -> ModelChoice:
        return self._choice

    @property
    def selected(self) -> bool:
        return self._selected

    def _hard(self) -> bool:
        """负载判成「困难」：**不响应选中**（施工单 LOAD_HARD 那一条）。"""
        return self._choice.load_level == LOAD_HARD

    def can_select(self) -> bool:
        """这张卡片能不能被选中。

        三个条件：内存闸门过了（``selectable``）、有下载地址、负载不是「困难」。
        ★ 内存闸门不过的卡片仍然显示（用户得知道有这个东西、为什么不能用），
          只是点不动 —— 和表格版的行没有 ``ItemIsEnabled`` 是同一个语义。
        """
        return bool(
            self._choice.selectable
            and self._choice.model.url
            and not self._hard()
        )

    def _is_installed(self) -> bool:
        return self._choice.state == STATE_INSTALLED

    def set_selected(self, selected: bool) -> bool:
        """切换选中态。不可选的卡片**拒绝**（返回 ``False``，状态不变）。"""
        want = bool(selected) and self.can_select()
        if want == self._selected:
            self._refresh_style()
            return self._selected
        self._selected = want
        self._refresh_style()
        return self._selected

    def toggle_selected(self) -> bool:
        return self.set_selected(not self._selected)

    def refresh(self) -> None:
        """按当前 :class:`ModelChoice` 重画这一张卡片（不改选中态）。"""
        self._refresh_installed()
        self._refresh_visuals()
        self._refresh_status()

    def _refresh_installed(self) -> None:
        installed = self._is_installed()
        self._built_installed = installed
        self.badge.setVisible(installed)
        self.uninstall_btn.setVisible(installed)
        self.uninstall_btn.setEnabled(installed and not self._dialog._busy)

    def _refresh_visuals(self) -> None:
        """视觉降级：用**样式表 / 调色板**，绝不用 ``QGraphicsOpacityEffect``。

        「困难」和「内存不够」的卡片整体淡化一档：背景转成淡灰、名称与说明
        转成 :data:`MUTED_COLOR`（状态那一行本来就是浅灰）。字都还在、
        位置也没变，只是"看起来用不了"。

        ★ 负载圆点与中文**不跟着变灰** —— 那一格说的就是"为什么用不了"，
          把它一起淡化会让用户看不出原因（红点 / 琥珀点留着更有信息量）。
        """
        muted = self._hard() or not self._choice.selectable
        if muted:
            self.name_label.setStyleSheet(
                f"font-size: 15px; font-weight: 700; color: {MUTED_COLOR};"
            )
            self.note_label.setStyleSheet(f"color: {MUTED_COLOR};")
            self.content.setStyleSheet(f"background: {MUTED_BG_COLOR};")
        else:
            self.name_label.setStyleSheet(
                "font-size: 15px; font-weight: 700; color: #1F2430;"
            )
            self.note_label.setStyleSheet("")
            self.content.setStyleSheet("")
        self.setProperty("tlVisualState", "muted" if muted else "normal")
        self._refresh_style()

    def _refresh_status(self) -> None:
        """状态那一行：失败原因优先（标红），否则显示安装状态。

        ★ 「复制链接」按钮的去留**和这一行同源**：只有下载失败的那张卡片
          才长出它（施工单 B5 的兜底路径）。没地址的条目不给 —— 复制一个
          空串没有任何意义。
        """
        reason = self._dialog._failure_reason(self.model_id)
        if reason:
            self.status_label.setText(f"下载失败：{reason}")
            self.status_label.setStyleSheet(
                f"color: {DANGER_COLOR}; font-size: 12px;"
            )
            self.status_label.setToolTip(reason)
            self.copy_btn.setVisible(bool(self._choice.model.url))
            return
        text = self._choice.status_text or ""
        if text == STATE_INSTALLED:
            # 安装状态由右下角的徽章说，状态行不必重复一遍。
            text = ""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {MUTED_COLOR}; font-size: 12px;"
        )
        self.status_label.setToolTip(text)
        self.copy_btn.setVisible(False)

    def _refresh_style(self) -> None:
        """把动态属性落成样式表。

        ★ Qt 的 QSS 属性选择器（``[tlSelected="true"]``）**不会**因为
          ``setProperty`` 自动重新求值 —— 必须 ``style().unpolish/polish``
          走一遍，否则改了属性界面纹丝不动（``AGENTS.md`` 4f 的同一类坑）。
        ★ 属性名带 ``tl`` 前缀、写完读回来确认，并且先用 ``indexOfProperty``
          确认这个名字**不是** Qt 已经声明过的静态属性 —— 那种情况
          ``setProperty`` 是**静默失败**（不抛异常、不生效），
          指望它生效等于把"边框不会高亮"这种事留给截图去发现（§4f）。
          ⚠️ 判据不能看 ``setProperty()`` 的返回值：Qt 官方写明它
          "returns false if the property is not declared as a Q_PROPERTY and
          is not a dynamic property" —— **全新**的动态属性第一次写也返回
          False，拿它当失败信号会把每一张卡片都误杀。
        """
        name = "tlSelected"
        want = "true" if self._selected else "false"
        if self.metaObject().indexOfProperty(name) >= 0:
            raise RuntimeError(  # pragma: no cover - 名字撞车才会走到
                f"ModelCard: 动态属性名 {name} 和 Qt 自带的静态属性撞车了 —— "
                "换一个名字，否则样式表选择器永远匹配不上"
            )
        self.setProperty(name, want)
        current = self.property(name)
        if current != want:  # pragma: no cover - 只有撞车才会走到
            raise RuntimeError(
                f"ModelCard: 动态属性 {name} 写进去又读不回来（读到 {current!r}）"
            )
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    # ------------------------------------------------------------------
    # 悬浮
    # ------------------------------------------------------------------

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        """进入：内容上移 :data:`HOVER_LIFT_PX`，阴影加深。**不动卡片几何。**"""
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        self._set_hover(False)
        super().leaveEvent(event)

    def _set_hover(self, hover: bool) -> None:
        self._set_content_margin(HOVER_LIFT_PX if hover else 0)
        self.shadow.setBlurRadius(
            SHADOW_BLUR_HOVER if hover else SHADOW_BLUR_IDLE
        )
        self.shadow.setOffset(
            0, SHADOW_OFFSET_HOVER if hover else SHADOW_OFFSET_IDLE
        )
        tint = QColor("#0F172A")
        tint.setAlpha(SHADOW_ALPHA_HOVER if hover else SHADOW_ALPHA_IDLE)
        self.shadow.setColor(tint)

    # ------------------------------------------------------------------
    # 点击
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        """单击切换选中。

        ★ 事件过滤器（:meth:`ModelCenterDialog.eventFilter`）把**子控件**上的
          点击也转发到这里 —— 不然点在名称 / 说明这些 ``QLabel`` 上时，
          事件根本到不了卡片自己（它们是卡片的子控件）。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self._dialog._on_card_clicked(self)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 命名
        """空格 / 回车也能切选中。

        ★ 卡片是 ``StrongFocus`` 的可聚焦控件，键盘用户按空格必须有反应 ——
          只做鼠标的话，这个界面在键盘下就是一块点不动的画。
        """
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._dialog._on_card_clicked(self)
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------

    def showEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        """每次显示都过一遍"没有透明度效果"的运行时闸门（4a / 4b）。"""
        _assert_no_opacity_effect(self)
        super().showEvent(event)


class ModelCenterDialog(QDialog):
    """模型中心：卡片流看清单、点选卡片、按顺序下载。

    调用方一般直接用 :func:`open_model_center`，不用自己 new。

    Signals:
        downloadFinished(list): 一轮下载结束时发出，参数是**这次真正装好**的
            模型 id 列表（可能为空）。设置页可以连它来刷新"本地模型"那块。
    """

    downloadFinished = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(DIALOG_TITLE)
        self.setModal(True)
        self.resize(1040, 660)

        #: 清单里的全部模型（含"本机已有"补进来的那些）
        self._models: list[RemoteModel] = []
        #: 上面那批合成出来的界面条目
        self._choices: list[ModelChoice] = []
        #: 用户选中的 id / 在界面上出现过的 id（用来在重画后保住用户的选择）
        self._checked_ids: set[str] = set()
        self._seen_ids: set[str] = set()
        #: 这一轮真正装好的 id —— 调用方读 :attr:`downloaded`
        self._downloaded: list[str] = []

        #: ``{模型 id: 卡片}`` 与界面上卡片的**顺序**（下载按这个顺序来）
        self._card_map: dict[str, ModelCard] = {}
        self._card_order: list[ModelCard] = []
        #: 筛选后的组，供 :meth:`_iter_cards` / 分组标题复现用
        self._groups: list[tuple[str, list[ModelChoice]]] = []

        #: 「复制链接」在这张卡片上点过之后的兜底目标（清单读不到时用得上）
        self._link_target_id = ""

        #: 本机配置的读数，只探一次（``system_info.snapshot()`` 自己也带缓存）。
        #: ``None`` 表示"还没探过"，和"探过但没读到"用 :attr:`_machine_read` 区分。
        self._machine_ram: float | None = None
        self._machine_vram: float | None = None
        self._machine_read = False

        self._busy = False
        self._populating = False
        self._cancel = False
        self._close_when_idle = False
        self._queue: list[ModelChoice] = []
        self._queue_index = 0
        self._skipped: list[str] = []
        self._failures: list[tuple[str, str]] = []
        self._relay: _ProgressRelay | None = None

        self._build_ui()
        self._load_manifest()

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    @property
    def downloaded(self) -> list[str]:
        """这一轮真正装好的模型 id（只读副本）。"""
        return list(self._downloaded)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(10)

        intro = QLabel(INTRO_TEXT)
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # ★ v1.9.2：推理引擎没装时，在说明文字下方、滚动区上方插一张安装卡片
        #   （AGENTS §36：三态卡片是**唯一**入口；这里不复制安装流程）。
        self._backend_card = self._make_backend_card()
        if self._backend_card is not None:
            outer.addWidget(self._backend_card)

        # ---- 卡片流：QScrollArea + QGridLayout -------------------------
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._grid_host = QWidget()
        self._grid_host.setObjectName("ModelCardHost")
        # 卡片自己的背景由 CARD_STYLE 画；容器保持透明（见 ui/styles.qss 开头）。
        self._grid_host.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(2, 2, 2, 12)
        self._grid.setSpacing(CARD_SPACING)
        for column in range(GRID_COLUMNS):
            self._grid.setColumnStretch(column, 1)
        # ★ v1.9.2：**不要**再给第 0 行（组标题行）挂 stretch —— 那会让"视口高度一变，
        #   卡片整体往下顶同样多"，而安装卡片的折行高度在首次显示后会自己差 2px，
        #   于是鼠标一进卡片、卡片自己的 y 就跟着挪 2px（4c：只能量像素才看得见）。
        #   剩下的竖直余量本来就落在最后一行的下面，卡片因此**顶对齐**、位置恒定。
        self._scroll.setWidget(self._grid_host)
        outer.addWidget(self._scroll, 1)

        self._plan = QLabel("")
        self._plan.setObjectName("Muted")
        self._plan.setWordWrap(True)
        outer.addWidget(self._plan)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setMinimumHeight(22)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        row = QHBoxLayout()
        row.setSpacing(8)

        self._refresh_btn = QPushButton("刷新清单")
        self._refresh_btn.setObjectName("Ghost")
        self._refresh_btn.setMinimumHeight(36)
        self._refresh_btn.setMinimumWidth(110)
        self._refresh_btn.clicked.connect(self._load_manifest)

        # ★ 页面底部统一一个下载按钮：卡片内部**没有**下载按钮。
        self._download_btn = QPushButton(self._download_button_text(0))
        self._download_btn.setObjectName("Primary")
        self._download_btn.setMinimumHeight(36)
        self._download_btn.setMinimumWidth(150)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download)

        self._copy_btn = QPushButton("复制链接")
        self._copy_btn.setObjectName("Ghost")
        self._copy_btn.setMinimumHeight(36)
        self._copy_btn.setMinimumWidth(110)
        self._copy_btn.setToolTip(
            "把选中的卡片（没有选中时用清单地址）的下载地址复制到剪贴板。"
        )
        self._copy_btn.clicked.connect(self._on_copy_link)

        self._close_btn = QPushButton("关闭")
        self._close_btn.setObjectName("Ghost")
        self._close_btn.setMinimumHeight(36)
        self._close_btn.setMinimumWidth(90)
        self._close_btn.clicked.connect(self.reject)

        row.addWidget(self._refresh_btn)
        row.addWidget(self._download_btn)
        row.addWidget(self._copy_btn)
        row.addStretch(1)
        row.addWidget(self._close_btn)
        outer.addLayout(row)

        self._placeholder: QLabel | None = None

    # ------------------------------------------------------------------
    # 推理后端安装卡片（v1.9.2）
    # ------------------------------------------------------------------

    def _make_backend_card(self) -> QWidget | None:
        """引擎没装时建一张 ``BackendSetupCard``，返回 ``None`` 表示不插。

        ★ ``ui.widgets.backend_setup`` 与它依赖的 ``core.ai.runtime_setup`` 是
          并行开发的组件：**任何一步缺失都只记 warning**，模型中心照常打开，
          绝不允许 import 失败把整个对话框带崩。
        """
        if _backend_available():
            return None
        try:
            from ui.widgets.backend_setup import BackendSetupCard  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - 组件还没落地：界面照开
            _log.warning("后端安装卡片还没落地，模型中心按原样打开")
            return None
        try:
            card = BackendSetupCard(self)
        except Exception:  # noqa: BLE001 - 建不出来也不许崩
            _log.warning("后端安装卡片创建失败", exc_info=True)
            return None
        card.ready.connect(self._on_backend_ready)
        refresh = getattr(card, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001 - 刷新失败只记日志
                _log.warning("后端安装卡片刷新失败", exc_info=True)
        return card

    def _on_backend_ready(self) -> None:
        """引擎装好了：收起卡片，并把模型状态刷新一次。"""
        card = getattr(self, "_backend_card", None)
        if card is not None:
            try:
                card.setVisible(False)
                card.deleteLater()
            finally:
                self._backend_card = None
        try:
            self._load_manifest()
        except Exception:  # noqa: BLE001 - 刷新失败只记日志
            _log.warning("后端就绪后刷新模型状态失败", exc_info=True)

    @staticmethod
    def _download_button_text(count: int) -> str:
        """底部按钮的文案。``count > 0`` 时带上张数（施工单：下载选中（N））。"""
        if count <= 0:
            return "下载选中"
        return f"下载选中（{count}）"

    # ------------------------------------------------------------------
    # 本机配置（只探一次）
    # ------------------------------------------------------------------

    def _machine_kwargs(self) -> dict[str, Any]:
        """本机内存 / 显存，**整个对话框只探一次**。

        ★ 为什么要把读数记下来、而不是每次重画卡片都让
          :func:`build_choices` 自己探：同一台机器的读数在一轮会话里看不出变化，
          探两次只会让"刚才还轻松、刷新一下变困难"这种事有机会发生
          （``nvidia-smi`` 抽一下就是 ``None``）。探一次、存下来、两次共用。
        ★ 顺带把 ``dest_dir`` 也带上（:meth:`models_root`）：**对话框自己**
          调 ``build_choices`` / ``merged_with_local`` 一律走这里 —— 少传一次
          ``dest_dir``，那一次读的就是默认用户目录（``%LOCALAPPDATA%\\SGMS\\models``），
          于是"本机已有"那条会用真实机器上的文件冒充结果，测试桩也拦不住。
        """
        if not self._machine_read:
            # ★ 用和 core.ai.model_registry 同一个真源（core.system_info），
            #   不在这里另写一份探测 —— 一份数据两个实现迟早对不上。
            try:
                from core.system_info import snapshot  # noqa: PLC0415

                info = snapshot()
            except Exception:  # pragma: no cover - 探测失败就当没读到
                info = None
            if info is not None:
                self._machine_ram = getattr(info, "total_ram_gb", None)
                self._machine_vram = getattr(info, "max_vram_gb", None)
            self._machine_read = True
        return {
            "ram_gb": self._machine_ram,
            "vram_gb": self._machine_vram,
            "dest_dir": self.models_root,
        }

    # ------------------------------------------------------------------
    # 取清单
    # ------------------------------------------------------------------

    def _load_manifest(self) -> None:
        """去远端取一次清单。**只读 JSON，不下载任何模型文件。**"""
        if self._busy:
            return
        self._refresh_btn.setEnabled(False)
        self._status.setText("正在从远端读取模型清单…")
        run_async(
            self,
            fetch_model_list,
            on_done=self._on_manifest,
            on_error=self._on_manifest_error,
        )

    def _on_manifest(self, result: Any) -> None:
        self._refresh_btn.setEnabled(True)
        try:
            models, warnings, status = result
        except (TypeError, ValueError):  # pragma: no cover - 防手滑改坏契约
            self._status.setText("清单的返回格式不对，这次没读成。")
            return

        self._models = merged_with_local(models, dest_dir=self.models_root)
        self._choices = build_choices(self._models, **self._machine_kwargs())
        self._populate()

        text = status
        if warnings:
            text = f"{status} 有 {len(warnings)} 条没能用上：" + " ".join(warnings[:5])
            if len(warnings) > 5:
                text += f"（还有 {len(warnings) - 5} 条同类的）"
        if not self._choices:
            # 清单拉取失败的兜底：界面照开，给一句能自己动手的话。
            text = f"{text} {MANIFEST_FALLBACK_HINT}"
            self._show_placeholder()
        self._status.setText(text)

    def _on_manifest_error(self, exc: BaseException) -> None:
        self._refresh_btn.setEnabled(True)
        self._status.setText(
            f"读取清单失败：{type(exc).__name__}，稍后再试。{MANIFEST_FALLBACK_HINT}"
        )
        self._link_target_id = ""

    # ------------------------------------------------------------------
    # 画卡片流
    # ------------------------------------------------------------------

    def _populate(self) -> None:
        """按分组重画整个卡片流。"""
        self._populating = True
        try:
            self._clear_cards()
            groups = group_choices(self._choices)
            self._groups = groups
            if not groups:
                self._add_placeholder()
            else:
                for group_name, items in groups:
                    self._add_group(group_name)
                    for choice in items:
                        self._add_card(choice)
        finally:
            self._populating = False

        self._seen_ids |= {choice.model.id for choice in self._choices}
        self._enforce_all_states()
        # ★ 把"这一轮真正选中的"记回 _checked_ids：重画（清单刷新 / 那次迟到的
        #   后台回调落地）重建卡片时 _wants_selected 先看 _checked_ids，不看就
        #   全丢 —— 用户选中的几张会被**静默清空**，界面上看不出任何异常
        #   （实测就踩到了；判据 test_重画之后选中不会丢）。
        self._checked_ids |= {card.model_id for card in self._card_order if card.selected}
        self._update_plan()

    def _clear_cards(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._card_map.clear()
        self._card_order.clear()
        self._placeholder = None
        # 行号自己数（QGridLayout 不会自动回收空行）
        self._grid_row = 0

    def _add_placeholder(self) -> None:
        """一条条目都没有时的占位（清单读不到 / 清单是空的）。"""
        label = QLabel(MANIFEST_FALLBACK_HINT)
        label.setObjectName("Muted")
        label.setWordWrap(True)
        label.setMinimumWidth(240)
        self._grid.addWidget(label, self._grid_row, 0, 1, GRID_COLUMNS)
        # ★ 占位也要占掉一行 —— 不然下一次 _clear_cards 之后的卡片会排到
        #   这行文字上面，两张东西叠在一起。
        self._grid_row += 1
        self._placeholder = label

    def _show_placeholder(self) -> None:
        """清单没读成 / 是空的：把兜底提示**放进卡片流**里再说一遍。

        ★ 为什么要放第二处：状态行在窗口最底下，一屏十来张卡片的时候
          用户根本看不到它。清单读不到时卡片流是空的 —— 那句话必须出现在
          用户正在看的那个位置。
        """
        if self._placeholder is None and not self._card_order:
            self._add_placeholder()

    def _add_group(self, title: str) -> None:
        """组标题栏 + 一条分隔线（``group`` 的真实取值见 ``update/ai_models.json``：
        「推荐」/「高级」，运行期还会多一个「本机已有」）。"""
        if self._grid_row > 0:
            # 第一组上面不要空出一截；后面的组各留一行间距。
            spacer = QWidget()
            spacer.setFixedHeight(CARD_SPACING)
            self._grid.addWidget(spacer, self._grid_row, 0, 1, GRID_COLUMNS)
            self._grid_row += 1

        header = QWidget()
        header.setObjectName("ModelGroupHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 0, 2, 0)
        header_layout.setSpacing(8)

        label = QLabel(title)
        label.setStyleSheet(
            "font-size: 14px; font-weight: 700; color: #334155; border: none;"
        )
        header_layout.addWidget(label)

        # 组间那条分隔线：从标题右侧一直拉到卡片流的右边缘。
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("background: #E2E8F0; border: none;")
        header_layout.addWidget(line, 1)
        self._grid.addWidget(header, self._grid_row, 0, 1, GRID_COLUMNS)
        self._grid_row += 1

    def _add_card(self, choice: ModelChoice) -> None:
        card = ModelCard(self, choice)
        model_id = choice.model.id

        # 选中态：出现过的听用户的（_checked_ids），第一次出现的听清单的。
        # ★ 走 set_selected() 而不是直接写 _selected —— 它自带 can_select() 闸门，
        #   「困难」/ 内存不够的卡片即使清单说默认选中也进不来。
        card.set_selected(self._wants_selected(choice))

        # ★ 子控件也要挂事件过滤器：点在名称 / 说明这些 QLabel 上时，
        #   事件到不了卡片自己，必须由对话框转发（见 eventFilter）。
        self._watch_children(card)

        # 卡片按固定列数排；组标题各占一整行，所以行号自己数最稳
        # （别用 self._grid.count()，那里面还混着标题和占位行）。
        index = len(self._card_order)
        if index and index % GRID_COLUMNS == 0:
            self._grid_row += 1
        self._grid.addWidget(card, self._grid_row, index % GRID_COLUMNS)
        self._card_map[model_id] = card
        self._card_order.append(card)

    def _watch_children(self, card: ModelCard) -> None:
        card.installEventFilter(self)
        for child in card.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt 命名
        """把卡片（含其子控件）上的左键点击转发给卡片。

        ★ 为什么非要过滤器：卡片的内容全是 ``QLabel`` —— 它们是卡片的**子控件**，
          鼠标点在它们身上时事件不会冒泡到父控件，卡片自己的 ``mousePressEvent``
          根本不会被调用（``AGENTS.md`` 4h：挂在鼠标上的逻辑必须真走一遍鼠标）。
        """
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and isinstance(obj, QWidget)
        ):
            card = self._card_of(obj)
            if card is not None and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.LeftButton:
                    self._on_card_clicked(card)
                    return True
        return super().eventFilter(obj, event)

    def _card_of(self, widget: QWidget) -> ModelCard | None:
        node: QWidget | None = widget
        while node is not None:
            if isinstance(node, ModelCard):
                return node
            parent = node.parentWidget()
            node = parent
        return None

    # ------------------------------------------------------------------
    # 选中
    # ------------------------------------------------------------------

    def _wants_selected(self, choice: ModelChoice) -> bool:
        """这张卡片此刻该不该处于选中状态。

        ★ 已经出现过的模型听用户的（``_checked_ids``），
          第一次出现的新模型听清单的（``choice.checked``，= 负载「轻松」）——
          不然"刷新清单"会把用户刚取消的选中又打回来。
        """
        if choice.model.id in self._seen_ids:
            return choice.model.id in self._checked_ids
        return bool(choice.checked)

    def selected_ids(self) -> list[str]:
        """当前选中的模型 id（按卡片在界面上的顺序，只读副本）。"""
        return [c.model_id for c in self._card_order if c.selected]

    def _on_card_clicked(self, card: ModelCard) -> None:
        """卡片被点了一下。"""
        if self._busy or self._populating:
            return
        if not card.can_select():
            # 「困难」/ 内存不够的卡片**不响应选中**（施工单 LOAD_HARD 那一条）。
            self._status.setText(
                f"{card.choice.model.display}："
                f"{card.choice.memory_note or HARD_LOAD_TOOLTIP}"
            )
            return
        now = card.toggle_selected()
        model_id = card.model_id
        if now:
            self._checked_ids.add(model_id)
        else:
            self._checked_ids.discard(model_id)
        self._update_plan()

    def _enforce_all_states(self) -> None:
        """把每张卡片的安装态 / 视觉降级 / 选中闸门过一遍。"""
        for card in self._card_order:
            card.refresh()
            if card.selected and not card.can_select():
                card.set_selected(False)
        self._checked_ids &= {c.model_id for c in self._card_order if c.selected}

    def _iter_cards(self) -> Iterator[ModelCard]:
        """按界面顺序遍历卡片（测试与内部逻辑都用它，别去摸 ``_grid``）。"""
        return iter(list(self._card_order))

    def _check_mutable(self, choice: ModelChoice, selected: bool) -> None:
        """把选中写回 ``choice``。

        ★ 只为还读 ``choice.checked`` 的老代码留的兼容路径（``describe_download_plan``
          就是一个）—— 卡片流的唯一真源是卡片自己的 ``selected``。
        """
        try:  # pragma: no cover - 正常路径永远进得来
            choice.checked = bool(selected)
        except Exception:  # pragma: no cover - 只读实现就当没这回事
            pass

    def _update_plan(self) -> None:
        """刷新底部按钮的张数与计划文案。"""
        selected = [c for c in self._card_order if c.selected]
        self._download_btn.setText(self._download_button_text(len(selected)))
        self._download_btn.setEnabled(bool(selected) and not self._busy)
        for choice in self._choices:
            self._check_mutable(choice, choice.model.id in set(self.selected_ids()))
        if not self._busy:
            self._plan.setText(describe_download_plan(self._choices))

    def _failure_reason(self, model_id: str) -> str:
        return dict(self._failures).get(model_id, "")

    def _title_of(self, model_id: str) -> str:
        for choice in self._choices:
            if choice.model.id == model_id:
                return choice.model.display
        return model_id

    # ------------------------------------------------------------------
    # 刷新状态（下载完要让"已装"立刻出现）
    # ------------------------------------------------------------------

    def _reload_choices(self) -> None:
        """按磁盘上现在的样子重建条目，并重画。

        卸载完 / 下载完都走这儿 —— **一次真源是磁盘**：
        ``build_choices`` 会重新调 ``installed_state()``，谁在谁不在以文件为准。

        ★ ``dest_dir`` 走 :meth:`models_root`（``model_dir`` 那一个真源）：
          不传的话这里读的是默认用户目录，测试里桩掉 ``model_dir`` 就不管用了
          —— 那会让"卸载完卡片上的徽章没掉"这类 bug 溜过去。
        """
        self._choices = build_choices(
            self._models, dest_dir=self.models_root, **self._machine_kwargs()
        )
        self._populate()

    @property
    def models_root(self) -> Path:
        """模型装在哪个目录下（唯一真源：``model_dir`` 的默认值）。

        ★ 用 ``model_dir(".").parent`` 反推，而不是自己再写一遍
          ``default_root() / "models"`` —— 第二个实现迟早和第一个不一致。
          点和点号都会被 :func:`core.ai.model_downloader._safe_name` 换成
          ``model``，所以这里丢掉那一段、只留它的父目录。
        ★ 万一 ``model_dir(".")`` 直接就是目录本身（测试把 ``model_dir``
          整个换成了 ``tmp/"models" / name`` 的假函数，它不做名字清洗），
          那就原样用它 —— 硬砍一层会把模型目录指到人家的 ``tmp_path`` 根上。
        """
        override = getattr(self, "_tl_models_dir", None)
        if override is not None:
            return Path(override)
        base = Path(model_dir("."))
        if base == Path(".") or base.name in ("", "."):
            return base
        return base.parent

    def _apply_failure_marks(self) -> None:
        """把上一轮失败的卡片标红 + 写明原因（重画之后要重新标）。"""
        for card in self._card_order:
            card._refresh_status()

    # ------------------------------------------------------------------
    # 复制链接（B5 的兜底路径）
    # ------------------------------------------------------------------

    def _on_copy_link(self, model_id: str = "") -> None:
        """把某一条的下载地址复制到剪贴板。

        Args:
            model_id: 要复制哪一条；留空表示"用选中的卡片，没有就复制清单地址"
                （底部那个「复制链接」按钮就是这么调的）。
        """
        if not model_id:
            selected = self.selected_ids()
            model_id = selected[0] if selected else self._link_target_id
        if not model_id:
            QMessageBox.information(
                self,
                "先选一张卡片",
                "先点一下要下载的卡片，再点「复制链接」。"
                "清单读不到时，这里会复制清单本身的地址。",
            )
            return
        model = next(
            (c.model for c in self._choices if c.model.id == model_id), None
        )
        if model is None or not model.url:
            QMessageBox.information(
                self,
                "没有下载地址",
                "这一条是本机已有的模型，远端清单里没有它的下载地址。",
            )
            return

        clipboard = QApplication.clipboard()
        if clipboard is None:  # pragma: no cover - 只在没有 QApplication 时发生
            QMessageBox.information(self, "复制不了", "剪贴板不可用，请手动复制。")
            return
        clipboard.setText(model.url)
        QMessageBox.information(self, "已复制", COPY_LINK_HINT)

    # ------------------------------------------------------------------
    # 卸载（B2）
    # ------------------------------------------------------------------

    def _on_uninstall(self, model_id: str) -> None:
        """删掉一个已装模型的模型文件，并销账。

        ★ 顺序是"**先问、再删、最后销账**"：

        1. 确认框文案**逐字**为 :data:`UNINSTALL_CONFIRM_TEXT`；
        2. 用户点"否" → **一个字节都不动**、账本也一个字不写，直接返回；
        3. 删目录复用 :func:`core.ai.model_downloader.delete_model`
           （它自己会 ``_safe_name()`` 清洗、再验一次"解析后仍在 models/ 里"
           防目录穿越）—— **不在这里自己拼路径**；
        4. 只在删成功之后才 ``forget_installed``：反过来先销账、
           ``rmtree`` 又失败的话，磁盘上留着一个再也卸不掉的目录。
        """
        if self._busy:
            return
        model = next(
            (c.model for c in self._choices if c.model.id == model_id), None
        )
        if model is None:
            return

        answer = QMessageBox.question(
            self,
            "卸载模型",
            UNINSTALL_CONFIRM_TEXT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        removed = delete_model(model.id)
        if removed:
            forget_installed(model.id, dest_dir=self.models_root)
            self._checked_ids.discard(model.id)
            self._status.setText(f"已卸载 {model.display}。")
        else:
            self._status.setText(
                f"没能删掉 {model.display} 的文件 —— 可能被别的程序占着，"
                "关掉正在用它的程序再试一次。"
            )
        self._reload_choices()
        self._update_plan()

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _on_download(self) -> None:
        if self._busy:
            return
        self._enforce_all_states()
        picked = [
            c
            for c in self._card_order
            if c.selected and c.choice.model.url and c.choice.runnable
        ]
        blocked = [
            c
            for c in self._card_order
            if c.selected and c.choice.model.url and not c.choice.runnable
        ]
        if not picked:
            QMessageBox.information(
                self,
                "还没选模型",
                "还没有选中任何能下载的模型。先点一下卡片选中它，"
                "再点「下载选中」。",
            )
            return

        todo = [c for c in picked if c.choice.state != STATE_INSTALLED]
        already = [c for c in picked if c.choice.state == STATE_INSTALLED]

        lines = [f"{describe_download_plan([c.choice for c in picked])}。"]
        if blocked:
            names = "、".join(c.choice.model.display for c in blocked[:3])
            lines.append(
                f"另外选中了 {len(blocked)} 个这台电脑跑不动的（{names}），这次会跳过。"
            )
        if already:
            lines.append(f"其中 {len(already)} 个已经装好了，这次会跳过。")
        if not todo:
            lines.append("没有需要下载的东西。")
        else:
            lines.append("现在开始下载吗？下载中可以关掉窗口来中断。")
        answer = QMessageBox.question(self, "确认下载", "\n".join(lines))
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._downloaded = []
        self._failures = []
        self._skipped = [c.choice.model.id for c in already]
        # ★ 按卡片在界面上的顺序下载（施工单：按顺序下载全部选中项）。
        self._queue = [c.choice for c in todo]
        self._queue_index = 0
        self._cancel = False

        if not self._queue:
            self._finish()
            return

        self._set_busy(True)
        self._progress.setVisible(True)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._next_in_queue()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_btn.setEnabled(not busy)
        self._copy_btn.setEnabled(not busy)
        self._download_btn.setEnabled(
            (not busy) and any(c.selected for c in self._card_order)
        )
        # 下载中不该再点得动「卸载」，也不该在下载没结束时又发一次下载。
        for card in self._card_order:
            card.uninstall_btn.setEnabled(
                (not busy) and card._is_installed()
            )
            card.setEnabled(not busy)

    def _next_in_queue(self) -> None:
        if self._cancel or self._queue_index >= len(self._queue):
            self._finish()
            return
        self._start_one(self._queue[self._queue_index], attempt=1)

    def _start_one(self, choice: ModelChoice, *, attempt: int) -> None:
        """把第 ``_queue_index`` 个模型丢进后台线程去下。"""
        position = self._queue_index + 1
        total_count = len(self._queue)
        suffix = "" if attempt == 1 else "（第 2 次尝试，接着上次下）"
        self._status.setText(
            f"正在下载 {position}/{total_count}：{choice.model.display}{suffix}"
        )
        self._progress.setRange(0, 100)
        self._progress.setValue(0)

        relay = _ProgressRelay(self)
        relay.progress.connect(self._on_progress)
        self._relay = relay
        target = _target_path(choice.model, model_dir(choice.model.id))

        def job() -> dict[str, Any]:
            download_exe(
                choice.model.url,
                target,
                lambda done, total: relay.progress.emit(
                    int(done), int(total or 0)
                ),
                resume=True,
                cancel=lambda: self._cancel,
                min_bytes=MIN_MODEL_BYTES,
            )
            return _verify(choice.model, target)

        run_async(
            self,
            job,
            on_done=lambda info: self._on_one_done(choice, info),
            on_error=lambda exc: self._on_one_error(choice, attempt, exc),
        )

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(max(0, min(100, int(done * 100 / total))))
            size_text = f"{human_size_bytes(done)} / {human_size_bytes(total)}"
        else:
            # 服务器没给 Content-Length：让进度条自己转，别显示一个假的百分比
            self._progress.setRange(0, 0)
            size_text = human_size_bytes(done)
        position = min(self._queue_index + 1, len(self._queue))
        label = (
            self._queue[self._queue_index].model.display
            if self._queue_index < len(self._queue)
            else ""
        )
        self._status.setText(
            f"正在下载 {position}/{len(self._queue)}：{label}  ({size_text})"
        )

    def _on_one_done(self, choice: ModelChoice, info: Any) -> None:
        record = info if isinstance(info, dict) else {}
        path = Path(record.get("path") or "")
        size = int(record.get("size") or 0)
        record_installed(
            choice.model.id,
            sha256=choice.model.sha256,
            size=size,
            file=path.name,
            label=choice.model.display,
            dest_dir=self.models_root,
        )
        self._downloaded.append(choice.model.id)
        self._status.setText(
            f"「{choice.model.display}」装好了：{record.get('note') or '校验通过'}。"
        )
        self._queue_index += 1
        self._next_in_queue()

    def _on_one_error(
        self, choice: ModelChoice, attempt: int, exc: BaseException
    ) -> None:
        if isinstance(exc, DownloadCancelled):
            self._cancel = True
            self._failures.append((choice.model.id, "用户中断了下载"))
            self._finish()
            return
        if attempt < MAX_ATTEMPTS:
            # ★ 不删 .part：download_exe(resume=True) 会带着 Range 从断点续，
            #   一个 1 GB 的模型不该因为最后 5% 断了就从头再来。
            self._status.setText(
                f"「{choice.model.display}」下载失败，正在重试（已下载的部分会接着下）…"
            )
            self._start_one(choice, attempt=attempt + 1)
            return
        reason = str(exc) or type(exc).__name__
        self._failures.append((choice.model.id, reason))
        self._queue_index += 1
        self._next_in_queue()

    def _finish(self) -> None:
        interrupted = self._cancel
        self._progress.setVisible(False)
        self._set_busy(False)
        self._relay = None
        # 重算一遍状态：刚装好的那些要立刻显示成"已装"
        self._reload_choices()

        if interrupted:
            self._status.setText(
                f"已经中断这次下载。这轮装好了 {len(self._downloaded)} 个。"
            )
        else:
            self._status.setText(
                f"成功 {len(self._downloaded)} 个 / 跳过 {len(self._skipped)} 个 / "
                f"失败 {len(self._failures)} 个"
            )

        if self._downloaded:
            self.downloadFinished.emit(list(self._downloaded))

        if not interrupted:
            self._show_summary()
        if self._close_when_idle:
            self._close_when_idle = False
            self.close()

    def _show_summary(self) -> None:
        lines = [
            f"成功 {len(self._downloaded)} 个 / "
            f"跳过 {len(self._skipped)} 个 / 失败 {len(self._failures)} 个"
        ]
        if self._failures:
            lines.append("")
            for model_id, reason in self._failures:
                lines.append(f"「{self._title_of(model_id)}」：{reason}")
            lines.append("")
            lines.append(
                "失败的那张卡片有一个「复制链接」按钮，点它会把下载地址放进剪贴板，"
                "可以直接在浏览器里下载。"
            )
        QMessageBox.information(self, "下载结束", "\n".join(lines))

    # ------------------------------------------------------------------
    # 关窗
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """下载中关窗 → 先问一句，再走"中断"这条路。

        ★ 不能直接把窗口拆掉：工作线程还在往 ``.part`` 里写，
          而进度回调还要往这个已经死掉的窗口发信号。
          所以这里是"置取消标志 + 等它自己收尾"，不是"立刻销毁"。
        """
        if not self._busy:
            super().closeEvent(event)
            return
        answer = QMessageBox.question(
            self,
            "还在下载",
            "还有模型没下完。现在关掉会中断这次下载"
            "（已经下好的部分会留在本机，下次接着下）。确定要关吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._cancel = True
            self._close_when_idle = True
            self._status.setText("正在中断下载…")
        event.ignore()


def open_model_center(
    parent: QWidget | None = None,
    *,
    on_installed: Callable[[list[str]], None] | None = None,
) -> int:
    """打开模型中心（模态）。返回 ``QDialog.exec()`` 的结果码。

    ★ **对外签名冻结**（v1.9 的调用方就这么调，一个字都不许改）——
      见 ``ui/pages/settings_page.py`` 的 ``_do_open_model_center``。

    Args:
        parent: 父窗口（一般传设置页）。
        on_installed: 可选回调。有模型装好时会带着 id 列表被调一次，
            设置页拿它刷新"本地模型"那块。

    Returns:
        对话框的结果码（``QDialog.Accepted`` / ``QDialog.Rejected``）。
    """
    dialog = ModelCenterDialog(parent)
    if on_installed is not None:
        dialog.downloadFinished.connect(on_installed)
    return dialog.exec()
