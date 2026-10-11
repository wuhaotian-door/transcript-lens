"""「模型中心」对话框 —— 一个窗口看全清单、勾选、串行下载。

这个对话框回答四个问题：

1. **有哪些模型能用** —— 从 Gitee 上的清单（``update/ai_models.json``）取，
   再补上"本机已经有、清单里没写"的那些；
2. **哪些这台机器跑得动** —— 两套判据，**别搞混**（``AGENTS.md`` 第 31 节）：
   内存闸门（只看 ``minRamGb``）不够的行没有 ``ItemIsEnabled``、整行不可选；
   负载指示（内存 + 显存，三档「轻松 / 勉强 / 困难」）只决定默认勾不勾、
   以及「困难」时下载按钮是否置灰；
3. **下到哪儿、下完算不算数** —— 落到 ``models/<id>/<文件>.gguf``，
   下完校大小和 SHA256，过了才记账；
4. **怎么把它弄走** —— 已装的行有「卸载」按钮（删目录 + 记账），
   下载两条命都失败的行有「复制链接」兜底。

★ 绝不自动下载
============

``AGENTS.md`` 的规矩：任何一次下载都必须由用户**明确点过**。
所以这里没有"打开就开始下"的路径 —— 打开对话框只读一个几 KB 的清单
（只读 GET，不碰模型文件），真正下载要用户先勾、再点「下载选中」、
再在确认框里点"是"。三道手，缺一不可。

★ 为什么下载在后台线程里
======================

一个 GGUF 是 1~3 GB，走 urllib 同步读就是几分钟。这中间如果占着主线程，
整个界面会变成一块白板 —— 用户连"取消"都点不动。
所以：**下载 + 校验都在 ``ui.workers.run_async`` 的工作线程里跑**，
进度通过 :class:`_ProgressRelay` 的信号回到主线程，
校验（要读完整个文件算哈希）同样不在主线程做。

★ 失败之后怎么办（B5）
===================

自动**重试 1 次**（``download_exe`` 的 ``resume=True`` 会接着 ``.part``
往下下，不会从头再来）→ 还是失败就把那一行标红、写明原因，
并在该行的操作列放一个「复制链接」：把直链送进剪贴板，提示用户
"请浏览器下载后放入 models/ 目录"。网络的事谁也说不准，
但至少别让人卡在一个只能重试的按钮上。

★ 界面文案一律纯文本（B4）
========================

负载那一列是**彩色圆点 + 中文**（``●`` 字符 + ``color``，圆点用 QSS 的
``border-radius``）—— **不用 emoji**（``AGENTS.md`` 4l：开屏标题里一个
emoji 值 241.7ms；而且 emoji 在不同机器上字形差别很大）。
所有 ``QLabel`` / tooltip 里不许出现 ``**`` / ``__`` / 反引号（铁律 9）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ai.model_downloader import MIN_MODEL_BYTES, delete_model, model_dir
from core.ai.model_registry import (
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

__all__ = ["ModelCenterDialog", "ModelVerifyError", "open_model_center"]

#: 对话框标题。
DIALOG_TITLE = "模型中心"

#: 顶部那行说明。★ 不许出现 Markdown 记号（``AGENTS.md`` 有文案闸门）。
INTRO_TEXT = (
    "模型都在这台电脑上跑，下载完就不需要联网了。"
    "下载地址来自 Gitee 上的模型清单，支持断点续传，下完会按清单里的 "
    "SHA256 校验一遍。负载那一列是按这台电脑的内存和显存算的："
    "轻松 = 够用，勉强 = 能跑但会慢，困难 = 可能带不动。"
)

#: 失败行标红用的颜色（和分析页的警示色保持一致）。
DANGER_COLOR = "#DC2626"

#: 「困难」那一行置灰用的颜色（比正文浅一档，但仍看得清字）。
MUTED_COLOR = "#9CA3AF"

#: 一个模型最多尝试几次（首次 + 自动重试 1 次）。
MAX_ATTEMPTS = 2

#: ★ 卸载确认框的文案 —— **一个字都不许改**（施工单逐字指定）。
UNINSTALL_CONFIRM_TEXT = "卸载后将无法使用 AI 功能，确定？"

#: 「困难」那一行的下载按钮气泡。
HARD_LOAD_TOOLTIP = "当前电脑配置可能无法流畅运行此模型"

#: 复制链接成功的提示（B5 的兜底路径就靠这一句告诉用户把文件放哪）。
COPY_LINK_HINT = "已复制到剪贴板，请浏览器下载后放入 models/ 目录。"

#: 操作列里两个按钮的宽度（``setCellWidget`` 放进去的控件**不参与**
#: ``ResizeToContents`` —— 所以这一列必须写死宽度，``AGENTS.md`` 4c 第 2 条）。
COPY_BUTTON_WIDTH = 88
UNINSTALL_BUTTON_WIDTH = 72
ACTION_COLUMN_WIDTH = 182

#: 表格的六个列（顺序即列下标，别手写元组 —— ``AGENTS.md`` 第 9 节第 3 条）。
COLUMN_TITLES: tuple[str, ...] = ("名称", "大小", "适配场景", "负载", "状态", "操作")
COLUMN_NAME = 0
COLUMN_SIZE = 1
COLUMN_SCENE = 2
COLUMN_LOAD = 3
COLUMN_STATUS = 4
COLUMN_ACTION = 5


class ModelVerifyError(RuntimeError):
    """下回来的东西没通过校验（大小对不上 / SHA256 不对）。"""


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


def _target_path(model: RemoteModel) -> Path:
    """这个模型该落到哪儿：``models/<清洗过的 id>/<文件名>``。

    ★ 文件名必须以 ``.gguf`` 结尾 —— ``installed_models()`` 只认这个后缀，
      下回来一个 ``xxx.bin`` 在界面上等于"没装"。
      清单里的 ``file`` 来自网络，所以先取 ``Path(...).name`` 掐掉任何路径。
    """
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


class ModelCenterDialog(QDialog):
    """模型中心：看清单、挑模型、串行下载。

    调用方一般直接用 :func:`open_model_center`，不用自己 new。

    Signals:
        downloadFinished(list): 一轮下载结束时发出，参数是**这次真正装好**的
            模型 id 列表（可能为空）。设置页可以连它来刷新"本地模型"那块。
    """

    downloadFinished = Signal(list)

    #: 列表的六列（= :data:`COLUMN_TITLES`，保留这个类属性只是为了兼容老写法）。
    COLUMNS = COLUMN_TITLES

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(DIALOG_TITLE)
        self.setModal(True)
        self.resize(980, 600)

        #: 清单里的全部模型（含"本机已有"补进来的那些）
        self._models: list[RemoteModel] = []
        #: 上面那批合成出来的界面条目
        self._choices: list[ModelChoice] = []
        #: 用户勾上的 id / 在列表里出现过的 id（用来在刷新后保住用户的选择）
        self._checked_ids: set[str] = set()
        self._seen_ids: set[str] = set()
        #: 这一轮真正装好的 id —— 调用方读 :attr:`downloaded`
        self._downloaded: list[str] = []

        #: 本机配置的读数，只探一次（``system_info.snapshot()`` 自己也带缓存）。
        #: ``None`` 表示"还没探过"，和"探过但没读到"用 :attr:`_machine_read` 区分。
        self._machine_ram: float | None = None
        self._machine_vram: float | None = None
        self._machine_read = False

        #: 每行的操作列控件：``{模型 id: [按钮, ...]}`` —— 忙的时候一起禁用。
        self._row_buttons: dict[str, list[QWidget]] = {}

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

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(COLUMN_TITLES))
        self._tree.setHeaderLabels(list(COLUMN_TITLES))
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        header = self._tree.header()
        # 名称吃掉剩下的宽度；大小 / 适配场景 / 负载 / 状态按内容收紧。
        header.setSectionResizeMode(COLUMN_NAME, QHeaderView.ResizeMode.Stretch)
        for column in (COLUMN_SIZE, COLUMN_SCENE, COLUMN_LOAD, COLUMN_STATUS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # ★ 操作列必须写死宽度：列里放的是 setCellWidget 进去的按钮，
        #   ResizeToContents 不会把它们算进来（AGENTS.md 4c 第 2 条），
        #   不写死的话按钮会被裁掉半个字。
        # ★ 还要先关掉 setStretchLastSection —— QHeaderView 默认就是开着它的，
        #   最后一列会被强行拉到填满视口，Fixed 模式和 setColumnWidth 全被它盖掉。
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COLUMN_ACTION, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COLUMN_ACTION, ACTION_COLUMN_WIDTH)
        self._tree.itemChanged.connect(self._on_item_changed)
        outer.addWidget(self._tree, 1)

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

        self._download_btn = QPushButton("下载选中")
        self._download_btn.setObjectName("Primary")
        self._download_btn.setMinimumHeight(36)
        self._download_btn.setMinimumWidth(130)
        self._download_btn.clicked.connect(self._on_download)

        self._copy_btn = QPushButton("复制链接")
        self._copy_btn.setObjectName("Ghost")
        self._copy_btn.setMinimumHeight(36)
        self._copy_btn.setMinimumWidth(110)
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

    # ------------------------------------------------------------------
    # 本机配置（只探一次）
    # ------------------------------------------------------------------

    def _machine_kwargs(self) -> dict[str, float | None]:
        """本机内存 / 显存，**整个对话框只探一次**。

        ★ 为什么要把读数记下来、而不是每次重建列表都让
          :func:`build_choices` 自己探：同一台机器的读数在一轮会话里看不出变化，
          探两次只会让"刚才还轻松、刷新一下变困难"这种事有机会发生
          （``nvidia-smi`` 抽一下就是 ``None``）。探一次、存下来、两次共用。
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
        return {"ram_gb": self._machine_ram, "vram_gb": self._machine_vram}

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

        self._models = merged_with_local(models)
        self._choices = build_choices(self._models, **self._machine_kwargs())
        self._populate()

        text = status
        if warnings:
            text = f"{status} 有 {len(warnings)} 条没能用上：" + " ".join(warnings[:5])
            if len(warnings) > 5:
                text += f"（还有 {len(warnings) - 5} 条同类的）"
        self._status.setText(text)

    def _on_manifest_error(self, exc: BaseException) -> None:
        self._refresh_btn.setEnabled(True)
        self._status.setText(f"读取清单失败：{type(exc).__name__}，稍后再试。")

    # ------------------------------------------------------------------
    # 画列表
    # ------------------------------------------------------------------

    def _populate(self) -> None:
        """按分组重画整棵树。"""
        self._populating = True
        try:
            self._tree.clear()
            self._row_buttons.clear()
            for group_name, items in group_choices(self._choices):
                top = QTreeWidgetItem([group_name, "", "", "", "", ""])
                top.setFirstColumnSpanned(True)
                # 组名只是个标题，别让用户选中它
                top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                font = top.font(0)
                font.setBold(True)
                top.setFont(0, font)
                self._tree.addTopLevelItem(top)

                for choice in items:
                    self._append_row(top, choice)
        finally:
            self._populating = False

        self._seen_ids |= {choice.model.id for choice in self._choices}
        self._sync_checked()
        self._apply_failure_marks()
        self._update_plan()

    def _load_widget(self, choice: ModelChoice) -> QWidget:
        """负载那一格：一个彩色圆点 + 中文等级（**不用 emoji**）。

        ★ 圆点用 QSS 的 ``border-radius`` 把一个 10x10 的小方块磨成圆，
          不用 ``●`` 字符、更不用 emoji —— 一个 emoji 要现场做彩色字形合成
          （``AGENTS.md`` 4l 实测 241.7ms），而且不同机器上图 shape 差得远。
        """
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        color = LOAD_COLORS.get(choice.load_level, MUTED_COLOR)
        dot = QLabel("")
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(
            f"background-color: {color}; border-radius: 5px; border: none;"
        )

        text = QLabel(choice.load_text)
        text.setStyleSheet(f"color: {color}; border: none;")

        layout.addWidget(dot)
        layout.addWidget(text)
        layout.addStretch(1)
        holder.setToolTip(choice.load_note)
        return holder

    def _action_widget(self, choice: ModelChoice) -> QWidget:
        """操作那一格。

        三条规则各管各的，**别混**：

        * **还没装** → 「下载」：只下这一条（不必先去左边打勾）。
          这一档也是「困难」的落点 —— 负载判成困难时这个按钮
          ``setEnabled(False)`` + :data:`HARD_LOAD_TOOLTIP`；
          内存闸门没过（整行没有 ``ItemIsEnabled``）时同样置灰，
          但气泡说的是内存那句。

        ★ **两条判断的先后是有讲究的**：「困难」是"内存也不够"的**子集** ——
          困难要求 ``内存 < minRamGb × RAM_TIGHT_RATIO``，而内存闸门要求
          ``内存 ≥ minRamGb``，所以困难的行一定同时没过内存闸门（见
          :func:`core.ai.model_registry._load_level_of`）。既然两条都成立，
          先判谁就决定了用户看到哪一句。任务书点名困难行要显示
          :data:`HARD_LOAD_TOOLTIP`，所以 **困难先判**；
          「勉强但内存不够」（``0.7 × minRamGb ≤ 内存 < minRamGb``）那一档
          仍然拿内存那句 —— 那才是真正拦住它的原因。
        * **已装** → 「卸载」：删 ``models/<id>/`` 再销账（B2）。
        * **下载两次都失败** → 「复制链接」：把直链送进剪贴板，让用户自己下（B5）。
          （顶部那个「复制链接」按钮是同一个动作的"按选中行"版本。）

        ★ 按钮宽度一律写死：``setCellWidget`` 放进去的控件不参与
          ``ResizeToContents``，不写死就会被裁掉半个字（``AGENTS.md`` 4c 第 2 条）。
        """
        model = choice.model
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        buttons: list[QWidget] = []
        installed = choice.state == STATE_INSTALLED
        failed = model.id in dict(self._failures)

        if not installed and model.url:
            download = QPushButton("下载")
            download.setObjectName("Ghost")
            download.setMinimumHeight(26)
            download.setFixedWidth(UNINSTALL_BUTTON_WIDTH)
            if choice.load_level == LOAD_HARD:
                download.setEnabled(False)
                download.setToolTip(HARD_LOAD_TOOLTIP)
            elif not choice.selectable:
                download.setEnabled(False)
                download.setToolTip(choice.memory_note or "这台电脑的内存不够跑这个模型。")
            else:
                download.setToolTip("只下载这一条模型。")
            download.clicked.connect(
                lambda _checked=False, mid=model.id: self._on_row_download(mid)
            )
            layout.addWidget(download)
            buttons.append(download)

        if installed and model.url:
            uninstall = QPushButton("卸载")
            uninstall.setObjectName("Ghost")
            uninstall.setMinimumHeight(26)
            uninstall.setFixedWidth(UNINSTALL_BUTTON_WIDTH)
            uninstall.setToolTip(
                "把已经下好的模型文件从硬盘上删掉，需要时可以重新下载。"
            )
            uninstall.clicked.connect(
                lambda _checked=False, mid=model.id: self._on_uninstall(mid)
            )
            layout.addWidget(uninstall)
            buttons.append(uninstall)

        if failed and model.url:
            copy = QPushButton("复制链接")
            copy.setObjectName("Ghost")
            copy.setMinimumHeight(26)
            copy.setFixedWidth(COPY_BUTTON_WIDTH)
            copy.setToolTip("把这一条的下载地址复制到剪贴板，自己用浏览器下载。")
            copy.clicked.connect(
                lambda _checked=False, mid=model.id: self._on_copy_link(mid)
            )
            layout.addWidget(copy)
            buttons.append(copy)

        layout.addStretch(1)
        if buttons:
            self._row_buttons[model.id] = buttons
            holder.setMinimumHeight(
                max(btn.minimumSizeHint().height() for btn in buttons) + 8
            )
            if self._busy:
                for button in buttons:
                    button.setEnabled(False)
        return holder

    def _append_row(self, top: QTreeWidgetItem, choice: ModelChoice) -> None:
        model = choice.model
        row = QTreeWidgetItem(
            [
                model.display,
                model.size_text,
                choice.load_note,
                "",
                choice.status_text,
                "",
            ]
        )
        # ★ id 存在 UserRole 里，绝不靠显示文字反查 —— 显示文字是可以随便改的
        row.setData(COLUMN_NAME, Qt.ItemDataRole.UserRole, model.id)

        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
        if choice.selectable and model.url:
            flags |= Qt.ItemFlag.ItemIsEnabled
        # 内存不够 / 没有下载地址的行去掉 ItemIsEnabled：
        # 它还是看得见（用户得知道有这个东西、为什么不能用），但勾不动。
        # ★ 这一条只看 minRamGb（内存闸门），和"负载"那三档是两套判据。
        row.setFlags(flags)

        # 名称那一格的悬停提示：清单里的说明 + （困难时）再补一句
        # "这台机器可能带不动"。两个都是纯文本，不许 Markdown 记号。
        name_parts = [model.note] if model.note else []
        if model.url and choice.load_level == LOAD_HARD:
            name_parts.append(HARD_LOAD_TOOLTIP)
        if name_parts:
            row.setToolTip(COLUMN_NAME, "；".join(name_parts))

        if model.url:
            row.setToolTip(COLUMN_SCENE, choice.load_note)
            row.setToolTip(
                COLUMN_LOAD,
                f"{choice.load_text}：{choice.load_note}"
                if choice.load_note
                else choice.load_text,
            )
        if self._checked_now(choice):
            row.setCheckState(COLUMN_NAME, Qt.CheckState.Checked)
        else:
            row.setCheckState(COLUMN_NAME, Qt.CheckState.Unchecked)
        top.addChild(row)
        tree = top.treeWidget()
        tree.setItemWidget(row, COLUMN_LOAD, self._load_widget(choice))
        tree.setItemWidget(row, COLUMN_ACTION, self._action_widget(choice))

    def _checked_now(self, choice: ModelChoice) -> bool:
        """这一行此刻该不该处于勾选状态。

        ★ 已经出现过的模型听用户的（``_checked_ids``），
          第一次出现的新模型听清单的（``choice.checked``）——
          不然"刷新清单"会把用户刚取消的勾又打回来。
        """
        if choice.model.id in self._seen_ids:
            return choice.model.id in self._checked_ids
        return bool(choice.checked)

    def _iter_rows(self) -> Iterator[QTreeWidgetItem]:
        for index in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(index)
            for child_index in range(top.childCount()):
                yield top.child(child_index)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating or column != COLUMN_NAME:
            return
        model_id = str(item.data(COLUMN_NAME, Qt.ItemDataRole.UserRole) or "")
        if not model_id:
            return
        if item.checkState(COLUMN_NAME) == Qt.CheckState.Checked:
            self._checked_ids.add(model_id)
        else:
            self._checked_ids.discard(model_id)
        self._sync_checked()
        self._update_plan()

    def _sync_checked(self) -> None:
        """把树上的勾选状态写回 :attr:`_choices`，好让计划文案跟着变。"""
        states: dict[str, bool] = {}
        for row in self._iter_rows():
            model_id = str(row.data(COLUMN_NAME, Qt.ItemDataRole.UserRole) or "")
            if model_id:
                states[model_id] = (
                    row.checkState(COLUMN_NAME) == Qt.CheckState.Checked
                )
        for choice in self._choices:
            if choice.model.id in states:
                choice.checked = states[choice.model.id]

    def _apply_failure_marks(self) -> None:
        """把上一轮失败的行标红 + 写明原因（重画之后要重新标）。"""
        reasons = dict(self._failures)
        if not reasons:
            return
        brush = QBrush(QColor(DANGER_COLOR))
        for row in self._iter_rows():
            model_id = str(row.data(COLUMN_NAME, Qt.ItemDataRole.UserRole) or "")
            if model_id in reasons:
                row.setText(COLUMN_STATUS, f"下载失败：{reasons[model_id]}")
                row.setForeground(COLUMN_STATUS, brush)
                row.setToolTip(COLUMN_STATUS, reasons[model_id])

    def _update_plan(self) -> None:
        if self._busy:
            return
        self._plan.setText(describe_download_plan(self._choices))

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
        """
        self._choices = build_choices(self._models, **self._machine_kwargs())
        self._populate()

    # ------------------------------------------------------------------
    # 复制链接（B5 的兜底路径）
    # ------------------------------------------------------------------

    def _on_copy_link(self, model_id: str = "") -> None:
        """把某一条的下载地址复制到剪贴板。

        Args:
            model_id: 要复制哪一条；留空表示"用列表里选中的那一行"
                （顶部那个「复制链接」按钮就是这么调的）。
        """
        if not model_id:
            item = self._tree.currentItem()
            model_id = (
                str(item.data(COLUMN_NAME, Qt.ItemDataRole.UserRole) or "")
                if item is not None
                else ""
            )
        if not model_id:
            QMessageBox.information(
                self, "先选一行", "先在列表里点一行模型，再点「复制链接」。"
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
            forget_installed(model.id)
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
        self._sync_checked()
        picked = [
            c for c in self._choices if c.checked and c.model.url and c.runnable
        ]
        blocked = [
            c for c in self._choices if c.checked and c.model.url and not c.runnable
        ]
        if not picked:
            QMessageBox.information(
                self,
                "还没选模型",
                "还没有勾选任何能下载的模型。先在列表里勾上要下载的，再点「下载选中」。",
            )
            return

        todo = [c for c in picked if c.state != STATE_INSTALLED]
        already = [c for c in picked if c.state == STATE_INSTALLED]

        lines = [f"{describe_download_plan(picked)}。"]
        if blocked:
            names = "、".join(c.model.display for c in blocked[:3])
            lines.append(
                f"另外勾了 {len(blocked)} 个这台电脑跑不动的（{names}），这次会跳过。"
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
        self._skipped = [c.model.id for c in already]
        self._queue = list(todo)
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

    def _on_row_download(self, model_id: str) -> None:
        """操作列那个「下载」按钮：只下这一条。

        ★ 这里是"逐行下载"的入口，所以负载判成「困难」时**不靠按钮置灰兜底就算完**：
          用户完全可能用键盘（Tab + 空格）摸到这个按钮，或者旧版本留下的
          ``_failures`` 让按钮又活过来。真正的闸门放在这句判断里 ——
          置灰只是提示，拦得住的是这一段。

        ★ 「困难」先判（和 :meth:`_action_widget` 里的气泡同一个顺序）：
          困难的行一定同时没过内存闸门，两条都成立时先说的那句才是用户看到的。
        """
        if self._busy:
            return
        choice = next((c for c in self._choices if c.model.id == model_id), None)
        if choice is None or not choice.model.url:
            return
        if choice.load_level == LOAD_HARD:
            QMessageBox.information(
                self,
                "这台电脑可能带不动",
                f"{choice.model.display}：{choice.load_note}。"
                "真要下也可以用下面的「下载选中」，但很可能跑不流畅。",
            )
            return
        if not choice.selectable:
            QMessageBox.information(
                self,
                "这台电脑跑不动",
                f"{choice.model.display} 需要 {choice.model.min_ram_gb:g} GB 内存，"
                "这台电脑的内存不够，先别下了。",
            )
            return

        # 只下这一条：把别的勾全部取消、只留这一条，再走同一条确认 + 队列流程。
        # ★ 直接改复选框、不走 _checked_ids —— 让 _on_item_changed 自己去同步，
        #   否则 _on_download 里的 _sync_checked() 会用旧状态把它盖回去。
        for other in self._iter_rows():
            other_id = str(other.data(COLUMN_NAME, Qt.ItemDataRole.UserRole) or "")
            state = (
                Qt.CheckState.Checked
                if other_id == model_id
                else Qt.CheckState.Unchecked
            )
            other.setCheckState(COLUMN_NAME, state)
        self._on_download()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_btn.setEnabled(not busy)
        self._download_btn.setEnabled(not busy)
        self._copy_btn.setEnabled(not busy)
        # 每行操作列里的按钮跟着一起禁用：下载中不该再点得动「卸载」，
        # 也不该在下载没结束时又发一次下载。
        # ★ 放开时只"打开"当初是启用的那些 —— 「困难」/ 内存不够的行
        #   在功能上本来就不该能点，别在这里顺手把它们点活。
        for model_id, buttons in self._row_buttons.items():
            usable = self._button_usable(model_id, buttons)
            for button in buttons:
                button.setEnabled(busy is False and usable)

    def _button_usable(self, model_id: str, buttons: list[QWidget]) -> bool:
        """这一行的按钮"功能上"可不可用（与忙不忙无关）。

        判据只有一条：「下载」按钮在负载判成困难、或内存闸门没过时不可用。
        卸载 / 复制链接两个按钮任何时候都能点。
        """
        choice = next((c for c in self._choices if c.model.id == model_id), None)
        if choice is None:
            return True
        if not any(btn.text() == "下载" for btn in buttons):
            return True
        return bool(choice.selectable and choice.load_level != LOAD_HARD)

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
        target = _target_path(choice.model)

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
                "失败的那一行有一个「复制链接」按钮，点它会把下载地址放进剪贴板，"
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
