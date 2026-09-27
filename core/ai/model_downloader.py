"""AI 模型下载器（charter v1.8 第八节）—— 骨架 + 预留。

★ 一个刻意的架构选择：**这里一行 ``urllib`` 都没有**
=================================================

charter v1.8 第四节把"AI 模型下载（``urllib.request`` → 网盘直链）"加进了
白名单。但 ``main._network_policy_report()`` 里有一条更严的闸门：
**``core/ai/*.py`` 里出现任何 HTTP import 就 FAIL** —— 那条闸门是 v1.6 立的，
本意是"AI 骨架期不许偷偷联网"，它现在依然有价值（防止有人往
``cloud_adapter`` 里塞一个 requests 让它自己发请求）。

所以这里的做法是：**下载的字节流复用 :func:`core.updater.download_exe`**，
本模块只负责"下到哪、叫什么名字、装好了没有"。

好处是两条规则**都不用让步**：

* ``core/ai/`` 仍然"零 HTTP import"（自检原样通过，不需要放宽扫描范围）；
* 模型下载仍然是 ``urllib.request``（在 ``core.updater`` 里），
  符合 charter v1.8 第四节。

★ 下载到哪
=========

``%LOCALAPPDATA%/SGMS/models/<模型名>/<文件名>``

**不是仓库里的 ``models/``。** 理由：

1. 一个 GGUF 是 1~3GB，进仓库就是灾难（红线：大文件不入库）；
2. 打包成 exe 之后，"程序目录"常常是只读的（Program Files），
   往那儿写会直接失败；
3. 数据目录本来就已经是"这台机器上属于本程序的东西"的家
   （``users.json`` / ``app.salt`` / ``u_*/``），模型和它们同级最好解释。

★ 落盘顺序：先下到 ``models/temp/``，验完再搬进 ``models/<名字>/``
================================================================

和 :func:`core.updater.download_exe` 的 ``.part`` 是同一个道理 ——
**没有"下了一半的模型"这种中间状态被当成已安装**。
``installed_models()`` 只认"目录存在且里面有一个非空的模型文件"，
所以下载中途断电也不会让设置页显示一个装好的假模型。

★ 只支持用户主动触发
=================

这个类**没有任何定时器、没有 import 期副作用、没有"启动时顺手检查"**。
``download()`` 必须有人显式调用（设置页上点「下载新模型」→ 选一个 → 确定）。
charter v1.8 第四节 ❌ 一栏里"自动更新/静默下载"依然在。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from core.updater import DownloadCancelled, DownloadError, download_exe, verify_checksum

__all__ = [
    "MODEL_CATALOG",
    "MODEL_SUFFIXES",
    "ModelDownloader",
    "ModelInfo",
    "catalog_from_release",
    "default_models_dir",
    "delete_model",
    "install_staged_model",
    "installed_models",
    "model_dir",
    "models_temp_dir",
    "stage_model",
]

#: 认哪些扩展名算"模型文件"。目前只有 GGUF（charter v1.8 第八节）。
MODEL_SUFFIXES = (".gguf",)

#: 一个模型文件至少该有多大。低于这个数说明下回来的是网盘错误页。
MIN_MODEL_BYTES = 1024 * 1024

#: 兜底的可用模型清单（Release 描述里没写 AI 模型时用这份）。
#:
#: ★ 名字和体积都是**说明性**的：真正下载链接必须来自 Release 描述
#:   （``parse_release_body``），因为只有发版的人知道文件放在哪。
#:   这里给的是"界面上至少要能列出这几档"，不是可下载的地址。
MODEL_CATALOG: tuple[dict[str, str], ...] = (
    {"name": "1.5B", "label": "AI 学习助手模块 - 1.5B", "size": "~1.2GB",
     "note": "体积小、启动快，适合普通办公机"},
    {"name": "3B", "label": "AI 学习助手模块 - 3B", "size": "~2.8GB",
     "note": "效果更好，需要更多内存"},
)


@dataclass(frozen=True)
class ModelInfo:
    """一个模型的现状（已装 / 可下）。

    ==========  ==========================================================
    字段         说明
    ==========  ==========================================================
    ``name``    模型名（``"1.5B"``）—— 也是磁盘上的目录名
    ``label``   界面上的中文名
    ``size``    体积文字（``"~1.2GB"``），**说明性**的
    ``url``     下载直链；空串 = 这个模型还没有可用地址
    ``path``    已安装时是文件路径，否则 ``None``
    ==========  ==========================================================
    """

    name: str
    label: str = ""
    size: str = ""
    url: str = ""
    note: str = ""
    path: Path | None = None

    @property
    def installed(self) -> bool:
        return self.path is not None

    @property
    def display(self) -> str:
        if self.installed:
            return f"{self.label or self.name}（已下载）"
        if self.size:
            return f"{self.label or self.name}（{self.size}）"
        return self.label or self.name


def default_models_dir() -> Path:
    """``%LOCALAPPDATA%/SGMS/models/``（跟着数据根目录走）。"""
    from core.crypto_store import default_root

    return default_root() / "models"


def models_temp_dir(dest_dir: str | Path | None = None) -> Path:
    """下载中转目录 ``models/temp/``。"""
    base = Path(dest_dir) if dest_dir is not None else default_models_dir()
    return base / "temp"


def model_dir(model_name: str, dest_dir: str | Path | None = None) -> Path:
    """某个模型的安装目录 ``models/<模型名>/``。"""
    base = Path(dest_dir) if dest_dir is not None else default_models_dir()
    return base / _safe_name(model_name)


def _safe_name(name: str) -> str:
    """把模型名清洗成安全的目录名（**防目录穿越**）。

    ``download(url, "../../evil")`` 这种输入不该写到 models/ 外面去。
    只保留字母数字和 ``._-``，其余换成 ``_``。
    """
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(name or "")
    ).strip("._")
    return cleaned or "model"


def _find_model_file(folder: Path) -> Path | None:
    """在模型目录里找那个模型文件（非空、扩展名对得上）。"""
    if not folder.is_dir():
        return None
    candidates = sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES
    )
    for path in candidates:
        try:
            if path.stat().st_size > 0:
                return path
        except OSError:  # pragma: no cover
            continue
    return None


def installed_models(dest_dir: str | Path | None = None) -> list[ModelInfo]:
    """扫一遍 ``models/`` 下**真的装好了**的模型。

    ★ 判据是"目录里有一个非空的模型文件"，不是"目录存在" ——
      下载中途断电会留下一个空目录，那个不算装好。
    """
    base = Path(dest_dir) if dest_dir is not None else default_models_dir()
    found: list[ModelInfo] = []
    if not base.is_dir():
        return found
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.name == "temp":
            continue
        model_file = _find_model_file(folder)
        if model_file is None:
            continue
        found.append(
            ModelInfo(name=folder.name, label=folder.name, path=model_file)
        )
    return found


def delete_model(model_name: str, dest_dir: str | Path | None = None) -> bool:
    """删掉一个已安装的模型。返回"真的删掉了没有"。

    ★ 只删 ``models/<清洗过的名字>/`` 这一个目录。
      名字先过 :func:`_safe_name`，所以 ``"../users.json"`` 删不到别处去。
    """
    folder = model_dir(model_name, dest_dir)
    base = Path(dest_dir) if dest_dir is not None else default_models_dir()
    # 双保险：解析之后必须仍在 models/ 里面
    try:
        if base.resolve() not in folder.resolve().parents:
            return False
    except OSError:  # pragma: no cover
        return False
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def catalog_from_release(
    body: Any, dest_dir: str | Path | None = None
) -> list[ModelInfo]:
    """把 Release 描述里的 AI 模型链接 + 本地已装的模型合成一张清单。

    Args:
        body: Release 描述原文（直接喂给 :func:`core.updater.parse_release_body`）。
        dest_dir: models 目录（测试用）。

    Returns:
        :class:`ModelInfo` 列表。**已装的排在前面**，同一名字只出现一次
        （以"已装"为准 —— 界面上不该同时出现"3B（已下载）"和"3B（~2.8GB）"）。
    """
    from core.updater import parse_release_body

    parsed = parse_release_body(body)
    urls: dict[str, str] = dict(parsed.get("ai_models") or {})
    sizes: dict[str, str] = dict(parsed.get("sizes") or {})

    available: list[ModelInfo] = []
    seen: set[str] = set()
    for name, url in urls.items():
        seen.add(name)
        available.append(
            ModelInfo(
                name=name,
                label=f"AI 学习助手模块 - {name}",
                size=sizes.get(name, ""),
                url=url,
            )
        )
    for entry in MODEL_CATALOG:
        if entry["name"] in seen:
            continue
        seen.add(entry["name"])
        available.append(
            ModelInfo(
                name=entry["name"],
                label=entry["label"],
                size=entry["size"],
                note=entry.get("note", ""),
            )
        )

    installed = installed_models(dest_dir)
    installed_names = {info.name for info in installed}
    merged = installed + [info for info in available if info.name not in installed_names]
    # 已装的补上体积文字，免得界面上一栏空着
    return [
        info
        if not (info.installed and not info.size)
        else ModelInfo(
            name=info.name, label=info.label, size=_human_size(info.path),
            note=info.note, path=info.path,
        )
        for info in merged
    ]


def _human_size(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        size = float(path.stat().st_size)
    except OSError:  # pragma: no cover
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return ""  # pragma: no cover


class _DownloadSignals(QObject):
    """``QRunnable`` 不是 ``QObject``，发不了信号 —— 信号挂在这个小助手身上。"""

    progress = Signal(int)
    finished = Signal(str)
    error = Signal(str)


class _DownloadTask(QRunnable):
    """后台线程里跑的那一趟下载。"""

    def __init__(
        self,
        url: str,
        model_name: str,
        base: Path,
        signals: _DownloadSignals,
        cancel: Callable[[], bool],
        expected_sha256: str,
        on_bytes: Callable[[int, int | None], None],
    ) -> None:
        super().__init__()
        self._url = url
        self._model_name = model_name
        self._base = base
        self._signals = signals
        self._cancel = cancel
        self._sha = expected_sha256
        self._on_bytes = on_bytes
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover - 在后台线程里跑
        try:
            def _progress(done: int, total: int | None) -> None:
                self._on_bytes(done, total)
                self._signals.progress.emit(_percent(done, total))

            final = stage_model(
                self._url,
                self._model_name,
                self._base,
                expected_sha256=self._sha,
                progress=_progress,
                cancel=self._cancel,
            )
            self._signals.progress.emit(100)
            self._signals.finished.emit(str(final))
        except DownloadCancelled:
            # ★ 取消**不是错误**。单独发一个 Finished 语义的信号会让界面
            #   以为装好了，所以走 error 通道，但文案是中性的。
            self._signals.error.emit("已取消下载。")
        except (DownloadError, OSError, ValueError) as exc:
            self._signals.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 后台异常必须传回主线程
            self._signals.error.emit(f"{type(exc).__name__}: {exc}")


def _percent(done: int, total: int | None) -> int:
    """进度百分比。总大小未知时返回 ``-1``（界面切不确定态）。"""
    if not total or total <= 0:
        return -1
    return max(0, min(100, int(done * 100 / total)))


def install_staged_model(
    staged: str | Path, model_name: str, dest_dir: str | Path | None = None
) -> Path:
    """把已经下好、已经校验过的模型文件搬进 ``models/<模型名>/``。

    ★ 用 ``os.replace``（同盘原子改名），失败时**源文件还在** ——
      所以"搬一半"不会产生一个装了一半的模型。

    Returns:
        最终的文件路径。
    """
    source = Path(staged)
    final_dir = model_dir(model_name, dest_dir)
    final_dir.mkdir(parents=True, exist_ok=True)
    final = final_dir / source.name
    os.replace(source, final)
    return final


def stage_model(
    url: str,
    model_name: str,
    dest_dir: str | Path | None = None,
    *,
    expected_sha256: str = "",
    progress: Callable[[int, int | None], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """**同步**下载一个模型并装好，返回最终路径。

    供两类调用方共用，避免"异步类里一套、向导里又一套"：

    * :class:`ModelDownloader`（后台线程，发信号）；
    * 更新向导里那条阻塞式流程（``QEventLoop`` + 模态进度条）。

    先下到 ``models/temp/``，校验通过再 :func:`install_staged_model` 搬进
    ``models/<名字>/`` —— 中途失败绝不会留下一个"看起来装好了"的半截模型。
    """
    base = Path(dest_dir) if dest_dir is not None else default_models_dir()
    temp = models_temp_dir(base)
    temp.mkdir(parents=True, exist_ok=True)
    file_name = _file_name_from_url(url, model_name)
    staged = temp / f"{_safe_name(model_name)}__{file_name}"

    path = download_exe(
        url,
        staged,
        progress,
        cancel=cancel,
        expected_sha256=expected_sha256,
        min_bytes=MIN_MODEL_BYTES,
    )
    return install_staged_model(path, model_name, base)


def _file_name_from_url(url: str, model_name: str) -> str:
    """从直链里取文件名；取不到就用模型名造一个。"""
    tail = str(url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    tail = "".join(ch for ch in tail if ch.isalnum() or ch in "._-")
    if tail.lower().endswith(MODEL_SUFFIXES):
        return tail
    return f"{_safe_name(model_name)}.gguf"


class ModelDownloader(QObject):
    """下载 AI 模型到 ``models/`` 目录（charter v1.8 第八节）。

    信号（任务书 4.1 指定的三个，一个不多一个不少）：

    ==============  ==========================================================
    信号             参数与含义
    ==============  ==========================================================
    ``progress``    ``int`` —— 百分比 0~100；**总大小未知时发 -1**
    ``finished``    ``str`` —— 装好的模型文件**完整路径**
    ``error``       ``str`` —— 可以直接显示给用户的一句话
    ==============  ==========================================================

    ★ 信号从后台线程发出来，Qt 会以**队列连接**投递到主线程 ——
      所以槽函数里可以安全地碰控件（和 ``ui.workers.run_async`` 同一个道理）。

    ★ **不会自动下载任何东西。** 只有显式调用 :meth:`download` 才会发请求。
    """

    progress = Signal(int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, dest_dir: str | Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base = Path(dest_dir) if dest_dir is not None else default_models_dir()
        self._signals = _DownloadSignals()
        self._signals.progress.connect(self.progress)
        self._signals.finished.connect(self.finished)
        self._signals.error.connect(self.error)
        self._cancelled = False
        self._busy = False
        self._pool = QThreadPool.globalInstance()
        #: 最近一次下载的字节数（进度条旁边那行"12.3MB / 1.2GB"用它）
        self.downloaded_bytes = 0
        self.total_bytes: int | None = None
        self.model_name = ""

    # ------------------------------------------------------------- 查询
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def dest_dir(self) -> Path:
        return self._base

    def list_available_models(self, release_body: Any = "") -> list[ModelInfo]:
        """可用 / 已下载的模型清单（任务书 4.1）。

        Args:
            release_body: Release 描述原文。给了就从中解析下载直链；
                没给就只列本地已装的 + :data:`MODEL_CATALOG` 那几档
                （**没有直链**，界面会显示"暂无下载地址"）。

        Returns:
            :class:`ModelInfo` 列表。
        """
        return catalog_from_release(release_body, self._base)

    def list_installed(self) -> list[ModelInfo]:
        """只列**已下载**的模型。"""
        return installed_models(self._base)

    # ------------------------------------------------------------- 动作
    def download(
        self,
        url: str,
        model_name: str,
        dest_dir: str | Path | None = None,
        *,
        expected_sha256: str = "",
    ) -> bool:
        """开始下载一个模型（异步，立刻返回）。

        Args:
            url: 直链。空串直接报错（没什么可下的）。
            model_name: 模型名，也是磁盘上的目录名。
            dest_dir: 覆盖默认目录。
            expected_sha256: 期望哈希；空串 = 不校验。

        Returns:
            ``True`` = 已经在下了；``False`` = 没开始（参数不对或正在下）。
            真正的结果从 ``finished`` / ``error`` 信号出来。
        """
        if self._busy:
            self.error.emit("已经有一个下载在进行中了。")
            return False
        if not str(url or "").strip():
            self.error.emit(
                f"「{model_name}」还没有可用的下载地址 —— "
                "请到发行版页面查看最新的下载说明。"
            )
            return False
        if not str(model_name or "").strip():
            self.error.emit("没有指定模型名字。")
            return False

        base = Path(dest_dir) if dest_dir is not None else self._base
        self._cancelled = False
        self._busy = True
        self.downloaded_bytes = 0
        self.total_bytes = None
        self.model_name = str(model_name)

        task = _DownloadTask(
            str(url),
            str(model_name),
            base,
            self._signals,
            lambda: self._cancelled,
            expected_sha256,
            self._remember_bytes,
        )
        self._signals.finished.connect(self._clear_busy)
        self._signals.error.connect(self._clear_busy)
        self._pool.start(task)
        return True

    def cancel(self) -> None:
        """请求取消。后台线程在**下一个块边界**退出。"""
        self._cancelled = True

    def delete(self, model_name: str, dest_dir: str | Path | None = None) -> bool:
        """删掉一个已下载的模型。"""
        return delete_model(model_name, dest_dir if dest_dir is not None else self._base)

    # ------------------------------------------------------------- 内部
    def _remember_bytes(self, done: int, total: int | None) -> None:
        self.downloaded_bytes = int(done)
        self.total_bytes = total

    def _clear_busy(self, *_args: Any) -> None:
        self._busy = False

    # ------------------------------------------------------------- 纯函数
    @staticmethod
    def is_installed(model_name: str, dest_dir: str | Path | None = None) -> bool:
        return _find_model_file(model_dir(model_name, dest_dir)) is not None

    @staticmethod
    def verify(file_path: str | Path, expected_sha256: str) -> bool:
        return verify_checksum(file_path, expected_sha256)

    @staticmethod
    def human_size(path: str | Path | None) -> str:
        return _human_size(Path(path) if path is not None else None)


def describe_status(dest_dir: str | Path | None = None) -> str:
    """给设置页用的一句话现状（纯文本，不写 Markdown —— 铁律 9）。"""
    models = installed_models(dest_dir)
    if not models:
        return "还没有下载任何本地模型。"
    names = "、".join(info.name for info in models)
    return f"已下载 {len(models)} 个本地模型：{names}"


def model_names(models: Iterable[ModelInfo]) -> list[str]:
    """辅助：把清单里的名字取出来（测试/界面都用得上）。"""
    return [info.name for info in models]
