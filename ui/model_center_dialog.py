"""「模型中心」对话框 —— 一个窗口看全清单、勾选、串行下载。

这个对话框回答三个问题：

1. **有哪些模型能用** —— 从 Gitee 上的清单（``update/ai_models.json``）取，
   再补上"本机已经有、清单里没写"的那些；
2. **哪些这台机器跑得动** —— 内存不够的行直接禁掉勾选，并在状态列写明原因，
   而不是让用户下完 1 GB 才发现跑不起来；
3. **下到哪儿、下完算不算数** —— 落到 ``models/<id>/<文件>.gguf``，
   下完校大小和 SHA256，过了才记账。

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

★ 失败之后怎么办
==============

自动**重试 1 次**（``download_exe`` 的 ``resume=True`` 会接着 ``.part``
往下下，不会从头再来）→ 还是失败就把那一行标红、写明原因，
并留一个「复制链接」给用户手动下 —— 网络的事谁也说不准，
但至少别让人卡在一个只能重试的按钮上。
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

from core.ai.model_downloader import MIN_MODEL_BYTES, model_dir
from core.ai.model_registry import (
    SIZE_TOLERANCE,
    STATE_INSTALLED,
    ModelChoice,
    RemoteModel,
    build_choices,
    describe_download_plan,
    fetch_model_list,
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
    "SHA256 校验一遍。"
)

#: 失败行标红用的颜色（和分析页的警示色保持一致）。
DANGER_COLOR = "#DC2626"

#: 一个模型最多尝试几次（首次 + 自动重试 1 次）。
MAX_ATTEMPTS = 2


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

    #: 列表的三列：名称 / 大小 / 状态。
    COLUMNS = ("名称", "大小", "状态")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(DIALOG_TITLE)
        self.setModal(True)
        self.resize(760, 560)

        #: 清单里的全部模型（含"本机已有"补进来的那些）
        self._models: list[RemoteModel] = []
        #: 上面那批合成出来的界面条目
        self._choices: list[ModelChoice] = []
        #: 用户勾上的 id / 在列表里出现过的 id（用来在刷新后保住用户的选择）
        self._checked_ids: set[str] = set()
        self._seen_ids: set[str] = set()
        #: 这一轮真正装好的 id —— 调用方读 :attr:`downloaded`
        self._downloaded: list[str] = []

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
        self._tree.setColumnCount(len(self.COLUMNS))
        self._tree.setHeaderLabels(list(self.COLUMNS))
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
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
        self._choices = build_choices(self._models)
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
            for group_name, items in group_choices(self._choices):
                top = QTreeWidgetItem([group_name, "", ""])
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

    def _append_row(self, top: QTreeWidgetItem, choice: ModelChoice) -> None:
        model = choice.model
        row = QTreeWidgetItem(
            [model.display, model.size_text, choice.status_text]
        )
        # ★ id 存在 UserRole 里，绝不靠显示文字反查 —— 显示文字是可以随便改的
        row.setData(0, Qt.ItemDataRole.UserRole, model.id)

        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
        if choice.selectable and model.url:
            flags |= Qt.ItemFlag.ItemIsEnabled
        # 内存不够 / 没有下载地址的行去掉 ItemIsEnabled：
        # 它还是看得见（用户得知道有这个东西、为什么不能用），但勾不动。
        row.setFlags(flags)

        if model.note:
            row.setToolTip(0, model.note)
        if model.url and choice.memory_note:
            row.setToolTip(2, choice.memory_note)
        if self._checked_now(choice):
            row.setCheckState(0, Qt.CheckState.Checked)
        else:
            row.setCheckState(0, Qt.CheckState.Unchecked)
        top.addChild(row)

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
        if self._populating or column != 0:
            return
        model_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if not model_id:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._checked_ids.add(model_id)
        else:
            self._checked_ids.discard(model_id)
        self._sync_checked()
        self._update_plan()

    def _sync_checked(self) -> None:
        """把树上的勾选状态写回 :attr:`_choices`，好让计划文案跟着变。"""
        states: dict[str, bool] = {}
        for row in self._iter_rows():
            model_id = str(row.data(0, Qt.ItemDataRole.UserRole) or "")
            if model_id:
                states[model_id] = row.checkState(0) == Qt.CheckState.Checked
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
            model_id = str(row.data(0, Qt.ItemDataRole.UserRole) or "")
            if model_id in reasons:
                row.setText(2, f"下载失败：{reasons[model_id]}")
                row.setForeground(2, brush)
                row.setToolTip(2, reasons[model_id])

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
        self._choices = build_choices(self._models)
        self._populate()

    # ------------------------------------------------------------------
    # 复制链接
    # ------------------------------------------------------------------

    def _on_copy_link(self) -> None:
        item = self._tree.currentItem()
        model_id = (
            str(item.data(0, Qt.ItemDataRole.UserRole) or "")
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
        QMessageBox.information(
            self, "已复制", "已复制到剪贴板，可在浏览器里手动下载。"
        )

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def _on_download(self) -> None:
        if self._busy:
            return
        self._sync_checked()
        picked = [c for c in self._choices if c.checked and c.model.url]
        if not picked:
            QMessageBox.information(
                self,
                "还没选模型",
                "还没有勾选任何模型。先在列表里勾上要下载的，再点「下载选中」。",
            )
            return

        todo = [c for c in picked if c.state != STATE_INSTALLED]
        already = [c for c in picked if c.state == STATE_INSTALLED]

        lines = [f"{describe_download_plan(picked)}。"]
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

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_btn.setEnabled(not busy)
        self._download_btn.setEnabled(not busy)

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
                "选中失败的那一行再点「复制链接」，就能到浏览器里手动下载。"
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
