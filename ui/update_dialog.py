"""自动更新向导（charter v1.8 第六节）—— 三步：选模块 → 下载 → 重启。

★ 这个文件里**没有一行网络代码**：请求在 :mod:`core.updater`，
  文件交接在 :mod:`core.update_apply`。这里只管"问用户"和"显示进度"。

三步的界面形态（任务书 2.2）：

1. **选模块** —— 版本说明 + 勾选框（主程序默认勾上，AI 模块默认不勾）
2. **下载中** —— 进度条 + 百分比 + 速度（KB/s）+ 可取消
3. **下载完成** —— "✅ 更新已完成（v1.8）／是否立即重启应用完成更新？"
   → [稍后] / [立即重启]

★ ``update.json`` 到底什么时候写（任务书 §2.2 有个自相矛盾的地方）
================================================================

任务书把"写 update.json"列在**点「立即重启」之后**的动作里，
可它同时又写"点「稍后」→ 关闭弹窗 → 下次启动时检测 update.json 并执行替换"。
**"稍后"那条路要成立，update.json 就必须在用户做选择之前已经躺在盘上了** ——
否则下次启动什么也检测不到，那个功能等于没写。

所以这里的次序是：

* **下载完成的那一刻**写 ``update.json``（这时"更新已完成"是真的）；
* 点「立即重启」→ 备份旧 exe → 生成 bat → 拉起 bat → 退出；
* 点「稍后」→ 什么都不做，直接关窗。下次启动
  :func:`core.update_apply.apply_pending_update` 会接上（它**自己也会先备份**）。

两条路都写到了 update.json，两条路都有备份 —— 任务书那两句话的要求
一条都没丢。

★ 源码模式下**不做替换**
======================

``sys.frozen`` 为假时根本没有 exe 可换。那种情况下第三步只显示
"已下载到 …（源码运行，未执行自动替换）"，**不给「立即重启」按钮** ——
一个会去覆盖 ``python.exe`` 的按钮比没有按钮危险得多。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core import update_apply
from core.ai.model_downloader import ModelInfo, catalog_from_release
from core.updater import (
    DownloadCancelled,
    DownloadError,
    UpdateCheck,
    download_exe,
    parse_release_body,
)
from core.version import CURRENT_VERSION

__all__ = [
    "DEFAULT_MAIN_SIZE",
    "DownloadProgressDialog",
    "ModuleChoiceDialog",
    "UpdatePlan",
    "ask_restart",
    "download_release",
    "plan_from_release",
]

#: 描述里没写主程序体积时显示这个（实测打包产物 228MB 量级）
DEFAULT_MAIN_SIZE = "~230MB"

#: 最少的界面刷新间隔（秒）。不节流的话 8KB 一块、230MB 就是 28000 次重绘。
_UI_TICK = 0.08

#: 算速度需要的**最短**时间窗（秒）。窗口太短算出来的是噪声
#: （实测出现过 "4530151 KB/s"）。
_SPEED_MIN_WINDOW = 0.5


@dataclass
class UpdatePlan:
    """"这次要下什么"的一份完整计划（**纯数据，不碰 Qt**）。"""

    version: str
    body: str = ""
    main_url: str = ""
    main_size: str = DEFAULT_MAIN_SIZE
    main_sha256: str = ""
    models: list[ModelInfo] = field(default_factory=list)

    @property
    def has_main(self) -> bool:
        return bool(self.main_url)

    def selected(self, wanted: list[str]) -> list[ModelInfo]:
        """按名字挑出用户勾上的模型（保持清单顺序）。"""
        chosen = set(wanted)
        return [info for info in self.models if info.name in chosen and info.url]


def plan_from_release(check: UpdateCheck, release: Any = None) -> UpdatePlan:
    """把"检查结果 + release 记录"变成一份 :class:`UpdatePlan`。

    v1.9 起**清单路线优先**：``check`` 里带了清单给的三件套
    （:attr:`~core.updater.UpdateCheck.has_direct_link`）时，直链 / 体积 / 哈希
    直接用它们，**不再解析正文**；否则退回解析 Release 正文
    （``core.updater.parse_release_body``，已标 deprecated）。

    Args:
        check: :func:`core.updater.check_for_update` 的结果。
        release: Gitee release 记录（``latest_release()`` 的返回值）。
            为 ``None`` 时只有版本号，没有任何直链。

    Returns:
        :class:`UpdatePlan`。直链为空是**正常情况**（发版的人可能只贴了
        网盘页面地址而不是直链），界面要能说"这个发行版没提供直链"。
    """
    body = ""
    if isinstance(release, dict):
        body = str(release.get("body") or release.get("description") or "")
    parsed = parse_release_body(body)
    sizes = dict(parsed.get("sizes") or {})
    checksums = dict(parsed.get("checksums") or {})

    main_url = str(parsed.get("main") or "")
    main_size = sizes.get("main") or DEFAULT_MAIN_SIZE
    main_sha256 = checksums.get("main", "")

    if check.has_direct_link:
        # 清单路线：三个字段都是**结构化**给的，比正则抠正文靠谱
        main_url = check.url
        main_size = check.size_text or DEFAULT_MAIN_SIZE
        main_sha256 = check.sha256

    return UpdatePlan(
        version=check.version or CURRENT_VERSION,
        body=body or check.notes,
        main_url=main_url,
        main_size=main_size,
        main_sha256=main_sha256,
        models=catalog_from_release(body),
    )


# ---------------------------------------------------------------------------
# 后台下载
# ---------------------------------------------------------------------------


class _DownloadSignals(QObject):
    progress = Signal(int, int)          # 已下载，总大小（0 = 未知）
    done = Signal(str)
    failed = Signal(str)
    cancelled = Signal()


class _DownloadTask(QRunnable):
    def __init__(
        self,
        url: str,
        dest: Path,
        signals: _DownloadSignals,
        expected_sha256: str,
        cancel: Any,
    ) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._signals = signals
        self._sha = expected_sha256
        self._cancel = cancel
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover - 后台线程
        try:
            path = download_exe(
                self._url,
                self._dest,
                lambda done, total: self._signals.progress.emit(done, int(total or 0)),
                cancel=self._cancel,
                expected_sha256=self._sha,
                min_bytes=0 if self._sha else 1024 * 1024,
            )
        except DownloadCancelled:
            self._signals.cancelled.emit()
        except (DownloadError, OSError, ValueError) as exc:
            self._signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self._signals.done.emit(str(path))


class DownloadProgressDialog(QDialog):
    """第二步：进度条 + 百分比 + 速度 + 取消（任务书 2.2）。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("正在下载更新")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._cancelled = False
        self._started = time.monotonic()
        self._last_tick = 0.0
        self._last_bytes = 0
        self._last_time = self._started
        self._speed = 0.0
        self._total = 0
        self._done = 0

        box = QVBoxLayout(self)
        box.setContentsMargins(24, 20, 24, 18)
        box.setSpacing(12)

        self._head = QLabel(title)
        self._head.setObjectName("H3")
        self._head.setWordWrap(True)
        box.addWidget(self._head)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setMinimumHeight(26)
        box.addWidget(self._bar)

        self._detail = QLabel("正在连接…")
        self._detail.setObjectName("Muted")
        self._detail.setWordWrap(True)
        box.addWidget(self._detail)

        row = QHBoxLayout()
        row.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setObjectName("Ghost")
        self._cancel_btn.setMinimumHeight(36)
        self._cancel_btn.clicked.connect(self._on_cancel)
        row.addWidget(self._cancel_btn)
        box.addLayout(row)

    # ------------------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _on_cancel(self) -> None:
        self._cancelled = True
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("正在取消…")
        self._detail.setText("正在停止下载…已下载的部分会保留，下次可以续传。")

    def on_progress(self, done: int, total: int) -> None:
        self._done = done
        self._total = total
        if total > 0:
            self._bar.setRange(0, 100)
            self._bar.setValue(min(100, int(done * 100 / total)))
        else:
            # 总大小未知（服务器没给 Content-Length）→ 不确定态
            self._bar.setRange(0, 0)

        now = time.monotonic()
        if now - self._last_tick < _UI_TICK:
            return
        self._last_tick = now

        # ★ 速度只在**攒够一段可测的时间**之后才算。
        #   第一版没这个门槛：两次回调挨得极近时 elapsed≈0，
        #   算出来的速度是天文数字 —— 截图里实测出现过
        #   "4530151 KB/s"。界面上给一个明显是假的数字，
        #   比暂时显示"计算中…"更糟。
        elapsed = now - self._last_time
        if elapsed >= _SPEED_MIN_WINDOW:
            instant = (done - self._last_bytes) / elapsed
            # 指数平滑：瞬时速度跳得太厉害，看着像坏的
            self._speed = (
                instant if self._speed <= 0 else (0.7 * self._speed + 0.3 * instant)
            )
            self._last_bytes = done
            self._last_time = now

        self._detail.setText(self.describe())

    def describe(self) -> str:
        """进度行文字。**纯文本，不写 Markdown**（铁律 9）。"""
        # 头半秒不报速度 —— 样本太短，报出来的是噪声
        if self._speed > 0 and time.monotonic() - self._started >= _SPEED_MIN_WINDOW:
            speed = _rate(self._speed)
        else:
            speed = "计算中…"
        if self._total > 0:
            pct = self._done * 100 / self._total
            return (
                f"{pct:.1f}%　{_mb(self._done)} / {_mb(self._total)}　{speed}"
            )
        return f"已下载 {_mb(self._done)}（总大小未知）　{speed}"


def _rate(bytes_per_second: float) -> str:
    """把字节/秒写成人话。

    ★ 千兆网/内网下 230MB 能跑到 100MB/s 以上，一律显示 KB/s 就会出现
      ``133866 KB/s`` 这种既难读又像坏了的数字（实测截图里真出现过）。
      超过 1MB/s 就换单位。
    """
    value = float(bytes_per_second)
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB/s"
    if value >= 1024:
        return f"{value / 1024:.0f} KB/s"
    return f"{value:.0f} B/s"

    def mark_done(self) -> None:
        self._bar.setRange(0, 100)
        self._bar.setValue(100)
        self._detail.setText("下载完成，正在校验…")


def _mb(value: float) -> str:
    """人话的体积。小文件用 KB，大的用 MB/GB。"""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"  # pragma: no cover


def download_release(
    parent: QWidget | None,
    url: str,
    dest: Path,
    *,
    label: str,
    expected_sha256: str = "",
) -> str | None:
    """模态下载一个文件，返回**下载好的路径**；取消或失败返回 ``None``。

    ★ 阻塞（跑一个局部事件循环）而不是回调 —— 调用方是一条线性流程
      （选模块 → 下载 → 问重启），用信号把三步拆开会多出一堆状态机。
      Qt 的 ``QEventLoop`` 在这里是合适的：进度条照样刷新、取消按钮照样能点。
    """
    from PySide6.QtCore import QEventLoop

    signals = _DownloadSignals()
    dialog = DownloadProgressDialog(label, parent)
    loop = QEventLoop()
    outcome: dict[str, Any] = {"path": None, "error": None, "cancelled": False}

    signals.progress.connect(dialog.on_progress)

    def _done(path: str) -> None:
        outcome["path"] = path
        dialog.mark_done()
        loop.quit()

    def _failed(message: str) -> None:
        outcome["error"] = message
        loop.quit()

    def _cancelled() -> None:
        outcome["cancelled"] = True
        loop.quit()

    signals.done.connect(_done)
    signals.failed.connect(_failed)
    signals.cancelled.connect(_cancelled)

    task = _DownloadTask(url, Path(dest), signals, expected_sha256, lambda: dialog.cancelled)
    QThreadPool.globalInstance().start(task)

    dialog.show()
    loop.exec()
    dialog.close()

    if outcome["error"]:
        QMessageBox.critical(parent, "下载失败", str(outcome["error"]))
        return None
    if outcome["cancelled"] or outcome["path"] is None:
        return None
    return str(outcome["path"])


# ---------------------------------------------------------------------------
# 第一步：选模块
# ---------------------------------------------------------------------------


class ModuleChoiceDialog(QDialog):
    """第一步：版本说明 + 模块勾选（任务书 2.2）。

    主程序**默认勾上且必须下**（不勾就没意义了，所以把它设成不可取消勾选）；
    AI 模块默认不勾 —— 一个 1.2GB 的模块不该在用户没注意的时候开始下。
    """

    def __init__(self, plan: UpdatePlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("发现新版本")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._plan = plan

        box = QVBoxLayout(self)
        box.setContentsMargins(24, 20, 24, 18)
        box.setSpacing(10)

        title = QLabel(f"发现新版本 v{plan.version}（当前 v{CURRENT_VERSION}）")
        title.setObjectName("H3")
        title.setWordWrap(True)
        box.addWidget(title)

        note = QLabel(
            "勾选要下载的内容。程序不会自动更新 —— 只有您点了「开始下载」才会联网。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        box.addWidget(note)

        # --- 主程序 -------------------------------------------------------
        self._main_box = QCheckBox(
            f"主程序更新（{plan.main_size}）"
        )
        self._main_box.setChecked(plan.has_main)
        self._main_box.setEnabled(False)      # 主程序是必选项，不给取消
        if not plan.has_main:
            self._main_box.setText("主程序更新（这个发行版没有提供直链）")
        box.addWidget(self._main_box)

        # --- AI 模块 ------------------------------------------------------
        self._model_boxes: list[tuple[str, QCheckBox]] = []
        for info in plan.models:
            suffix = f"（{info.size}）" if info.size else ""
            if not info.url:
                suffix = "（这个发行版没有提供直链）"
            elif info.installed:
                suffix = "（已下载，重新下载会覆盖）"
            check = QCheckBox(f"{info.label or info.name}{suffix}")
            check.setChecked(False)
            check.setEnabled(bool(info.url))
            box.addWidget(check)
            self._model_boxes.append((info.name, check))

        # --- 版本说明 -----------------------------------------------------
        if plan.body.strip():
            box.addSpacing(4)
            notes_title = QLabel("版本说明")
            notes_title.setObjectName("H4")
            box.addWidget(notes_title)
            notes = QLabel(_plain_notes(plan.body))
            notes.setObjectName("Muted")
            notes.setWordWrap(True)
            notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            notes.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            # ★ 必须套滚动区，不能只 setMaximumHeight ——
            #   第一版就是只限了高度，结果一段长描述被**从中间截断**，
            #   后面那些行（AI 模型的链接）在界面上根本够不着，
            #   而静态断言全绿、几何也对（AGENTS.md 4c 那一类事故）。
            #   套上 QScrollArea 之后，"看不完"变成"滚一下就能看完"。
            notes_scroll = QScrollArea()
            notes_scroll.setObjectName("NotesScroll")
            notes_scroll.setWidgetResizable(True)
            notes_scroll.setFrameShape(QFrame.Shape.NoFrame)
            notes_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            notes_scroll.setWidget(notes)
            notes_scroll.setMinimumHeight(96)
            notes_scroll.setMaximumHeight(180)
            box.addWidget(notes_scroll)

        buttons = QDialogButtonBox()
        self._start_btn = buttons.addButton("开始下载", QDialogButtonBox.ButtonRole.AcceptRole)
        self._start_btn.setObjectName("Primary")
        self._start_btn.setMinimumHeight(38)
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setMinimumHeight(38)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def chosen_models(self) -> list[str]:
        """用户勾上的模型名。"""
        return [name for name, box in self._model_boxes if box.isChecked()]

    def wants_main(self) -> bool:
        return self._main_box.isChecked()


def _plain_notes(body: str) -> str:
    """把 Release 描述压成一段**能放进 QLabel** 的纯文本。

    ★ QLabel 不渲染 Markdown（铁律 9），所以 ``##`` / ``**`` 会**原样显示**。
      这里不是要"渲染"它，而是把记号去掉 —— 用户看到的是干净的文字，
      而不是一堆星号。长描述截断，避免弹窗被撑爆。
    """
    lines: list[str] = []
    for raw in str(body).splitlines():
        text = raw.strip()
        if not text:
            continue
        text = text.lstrip("#>").strip()
        text = text.replace("**", "").replace("__", "").replace("`", "")
        if text:
            lines.append(text)
    joined = "\n".join(lines)
    if len(joined) > 700:
        joined = joined[:697] + "…"
    return joined


# ---------------------------------------------------------------------------
# 第三步：问重启
# ---------------------------------------------------------------------------


def ask_restart(
    parent: QWidget | None,
    plan: UpdatePlan,
    downloaded: Path,
    *,
    frozen: bool | None = None,
    root: str | Path | None = None,
) -> bool:
    """第三步：`✅ 更新已完成（vX）／是否立即重启应用完成更新？` [稍后]/[立即重启]。

    ★ ``update.json`` 在**进这个函数之前**就该写好了（见模块开头
      "update.json 到底什么时候写"），所以「稍后」这条路什么都不用做。

    Returns:
        ``True`` = 已经交接给替换脚本，**调用方必须马上退出程序**；
        ``False`` = 用户选了「稍后」，或者设备/环境不支持自动替换。
    """
    running_frozen = update_apply.is_frozen() if frozen is None else bool(frozen)

    if not running_frozen:
        QMessageBox.information(
            parent,
            "更新已下载",
            f"新版本已下载到：\n{downloaded}\n\n"
            "当前是以源码方式运行的，没有 exe 可以自动替换 —— "
            "所以没有执行替换。打包版才会自动完成这一步。",
        )
        return False

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("更新已完成")
    box.setText(
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ 更新已完成（v{plan.version}）\n\n"
        "是否立即重启应用完成更新？\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    box.setInformativeText(
        "点「立即重启」会把当前版本备份一份，然后自动替换并重新打开。"
    )
    later = box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
    now = box.addButton("立即重启", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(now)
    box.exec()

    if box.clickedButton() is not now:
        # ★ 「稍后」：什么都不做。update.json 已经在盘上，
        #   下次启动 core.update_apply.apply_pending_update 会接上。
        del later
        return False

    return _handoff(parent, plan, downloaded, root=root)


def _handoff(
    parent: QWidget | None,
    plan: UpdatePlan,
    downloaded: Path,
    *,
    root: str | Path | None = None,
) -> bool:
    """备份 → 写 bat → 拉起 → 告诉调用方退出。**任何一步失败都拒绝替换。**"""
    target = update_apply.current_exe()
    if target is None:  # pragma: no cover - 上面已经判过 frozen
        QMessageBox.warning(parent, "无法自动替换", "找不到当前程序文件。")
        return False

    # 1) 备份（**失败就拒绝替换** —— 任务书 2.4）
    try:
        saved = update_apply.backup_current_exe(
            target, old_version=CURRENT_VERSION, root=root
        )
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(
            parent,
            "备份失败，已取消更新",
            f"没能备份当前版本：\n{exc}\n\n"
            "为安全起见**没有**执行替换，当前程序原样可用。",
        )
        return False

    # 2) 生成替换脚本并拉起
    json_path = update_apply.update_json_path()
    log_path = update_apply.update_dir() / update_apply.APPLY_LOG_NAME
    try:
        script = update_apply.write_apply_bat(
            downloaded, target, json_path, log_path=log_path
        )
        update_apply.launch_apply(
            script, downloaded, target, json_path, log_path=log_path
        )
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(
            parent,
            "准备替换脚本失败",
            f"{exc}\n\n当前程序照常运行；旧版本已备份在：\n{saved}",
        )
        return False

    QMessageBox.information(
        parent,
        "正在更新",
        f"旧版本已备份到：\n{saved}\n\n"
        "程序即将关闭并自动替换为新版本，稍后会自动重新打开。\n"
        "如果没有自动打开，请手动双击程序图标。",
    )
    return True
