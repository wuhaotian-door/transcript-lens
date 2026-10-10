"""更新检查 + 更新下载（charter v1.8 第四 / 六节）。

三条设计原则（charter 7.1 → v1.8 第六节，一条都不能松）：

1. **用户主动触发**：没有任何自动检查、没有定时器、没有 import 期副作用 ——
   只有人点了「检查更新」才发请求，只有人在弹窗上点了「开始下载」才下载。
2. **不静默下载**：v1.7 时这里只做到"打开浏览器让用户自己下"；
   v1.8 起了变化 —— charter v1.8 第四节把 **更新下载** 也加进了白名单，
   第六节给了完整的下载 → 重启 → 替换流程。但
   **❌ 禁止 一栏里"自动更新/静默下载"仍然在**，所以本模块的边界是：
   每一次下载都必须**先有一个用户点头的弹窗**，代码里没有任何
   "启动时顺便下一下"的路径。
3. **不引入第三方库**：只用标准库 ``urllib.request``（charter 7.3 / v1.8 二节）。

★ 为什么是 ``urllib.request`` 而不是 ``requests``
---------------------------------------------

这是 charter 第四节里被点名的 stdlib 联网方式。``core/ai/`` 那边有它自己的
一条更严的规则（见 :func:`main._network_policy_report`）：除了
``model_downloader`` 这个明确被 charter v1.8 第八节允许的下载器之外，
别的 AI 模块仍然一个 HTTP 客户端都不许有。

★ 实测：``/releases/latest`` 现在是 404
------------------------------------

2026-09-26 实打实跑了一遍:

===========================  ======  ==========================================
请求                          结果    说明
===========================  ======  ==========================================
``/repos/.../transcript-lens``        200     仓库是**公开**的，匿名可读
``/repos/.../releases``       200     ``[]`` —— **一个发行版都还没发**
``/repos/.../releases/latest`` 404    没有 release 时 Gitee 就是回 404
===========================  ======  ==========================================

所以只照 charter 7.2 打 ``/releases/latest`` 的话，**在发布第一个发行版之前，
这个功能永远只能报"检查失败"** —— 而那不是实话（网络是通的，接口也在，
只是仓库里没有东西可下载）。

于是这里做了一层诚实的兜底：``latest`` 拿不到就退一步读 ``/releases`` 列表。
两者都空 → :data:`NO_RELEASE` 状态，界面上说"仓库还没有发布发行版"，
**不说"检查失败"**。等真发了 release，走的是同一条路径，不需要改代码。

★ 超时：检查 5 秒，下载 30 秒/块
------------------

不设超时或者设得很长，网络不通时用户会盯着一个卡死的按钮。
而且设置页**必须把它丢到后台线程**跑（``ui/workers.run_async``）——
在按钮回调里直接 ``urlopen`` 就是 ``ui/workers.py`` 开头说的那个坑。
本模块只负责"发请求 / 收字节"，**不碰任何 UI**。

★ 下载安全：先写 ``.part``，验完再改名
-----------------------------------

:func:`download_exe` 全程只往 ``<dest>.part`` 里写，**成功之后才** ``replace``
成目标文件。所以"下载到一半断网/取消"永远不会留下一个半截的 exe
去覆盖用户手里那个好的 —— 这是任务书 2.4 的硬约束"下载失败不损坏已有 exe"。

★ v1.9：版本号和直链改从 ``update/manifest.json`` 读
------------------------------------------------

v1.8 及之前，"最新版本号"只能从 Gitee 发行版的 ``tag_name`` 猜，"主程序直链 /
体积 / sha256"只能在发行版**正文**里用正则抠（:func:`parse_release_body`）。
正文是给人看的散文，正则抠它等于把发版人的写作习惯当接口 —— 少写一个冒号、
把直链换成网盘页面，更新功能就哑了。

v1.9 起换成一份**结构化的清单**：``update/manifest.json``（仓库里那份就是模板），
:func:`check_for_update` 先读它，拿到 ``app`` 段的 ``version / url / size / sha256``。

* 清单里 **version 比当前新** → :data:`STATUS_NEWER`，并把直链一并带出来，
  更新向导不必再解析正文；
* 清单缺失 / 404 / 读不懂 / **三样关键字段缺任何一样** → 退回老路子
  （读发行版正文），一条都不改老代码的行为。宁可退回老路子，
  **也绝不拿一条没校验的直链去替换用户的程序**（见 :meth:`ManifestApp.usable`）。

老路子的 :func:`parse_release_body` 与 :func:`latest_release` **保留不删**，
但已经标成 deprecated：只在清单不可用时兜底，新发版方式请写清单。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from core.version import CURRENT_VERSION, compare_versions, is_newer, normalize_version

__all__ = [
    "ATTACHMENT_LIMIT_BYTES",
    "CHUNK_SIZE",
    "CURRENT_VERSION",
    "DEFAULT_TIMEOUT",
    "DOWNLOAD_TIMEOUT",
    "DownloadCancelled",
    "DownloadError",
    "GITEE_API",
    "GITEE_RAW_MANIFEST",
    "GITEE_RELEASES_API",
    "GITEE_RELEASES_PAGE",
    "MANIFEST_SCHEMA",
    "MIN_EXE_BYTES",
    "ManifestApp",
    "ManifestPart",
    "NO_RELEASE",
    "SOURCE_MANIFEST",
    "SOURCE_RELEASE",
    "STATUS_CURRENT",
    "STATUS_ERROR",
    "STATUS_NEWER",
    "USER_AGENT",
    "UpdateCheck",
    "check_for_update",
    "check_latest_version",
    "download_exe",
    "download_parts",
    "fetch_manifest",
    "human_size",
    "latest_release",
    "normalize_sha256",
    "parse_manifest",
    "parse_release_body",
    "verify_checksum",
]

#: 更新清单（v1.9 新增）。发版的人把版本号 / 主程序直链 / 体积 / sha256 写在这里。
#:
#: ★ 用的是 Gitee 的 **raw** 地址而不是 API：清单是仓库里的一个普通文件，
#:   不需要 token，也没有"还没发过版就 404"那套麻烦（那是发行版接口的脾气）。
#:   raw 的代价是有缓存，刚推上去可能要等一会儿才刷出来 —— 所以本模块
#:   永远保留"清单读不到就退回发行版正文"的兜底。
GITEE_RAW_MANIFEST = (
    "https://gitee.com/wuhaotiansystem/transcript-lens/raw/master/update/manifest.json"
)

#: 清单格式版本。不认识的版本一律当"读不懂"处理（退回老路子），**不猜**。
MANIFEST_SCHEMA = 1

#: :class:`UpdateCheck.source` 的取值：这次信息是从哪儿来的。
SOURCE_MANIFEST = "manifest"   #: v1.9 的清单
SOURCE_RELEASE = "release"     #: 老的发行版正文（deprecated，仅兜底）

#: Gitee 的"最新发行版"接口（charter 7.2）。只读 GET，不需要 token。
GITEE_API = (
    "https://gitee.com/api/v5/repos/wuhaotiansystem/transcript-lens/releases/latest"
)

#: 发行版**列表**接口 —— ``latest`` 404 时的兜底（见模块开头的实测表）
GITEE_RELEASES_API = (
    "https://gitee.com/api/v5/repos/wuhaotiansystem/transcript-lens/releases"
)

#: 让用户自己去下载的那个页面（不自动下载，只打开浏览器）
GITEE_RELEASES_PAGE = "https://gitee.com/wuhaotiansystem/transcript-lens/releases"

#: 默认超时（秒）。charter 7.2 指定 5s。
DEFAULT_TIMEOUT = 5

#: 带个 UA：有些网关会对没有 UA 的请求直接 403
USER_AGENT = f"transcript-lens-updater/{CURRENT_VERSION}"

#: 响应体最大字节数。防着"对端返回一个超大 JSON 把内存吃光"。
MAX_RESPONSE_BYTES = 256 * 1024

#: 结果状态：有新版本 / 已是最新 / 仓库里还没有发行版 / 检查本身失败
STATUS_NEWER = "newer"
STATUS_CURRENT = "current"
NO_RELEASE = "no-release"
STATUS_ERROR = "error"

# ---------------------------------------------------------------------------
# 下载（charter v1.8 第六节）
# ---------------------------------------------------------------------------

#: 每次 ``read()`` 读多少字节（任务书 2.1 指定 8192）。
CHUNK_SIZE = 8192

#: 下载超时（秒/块）。**不是整个文件的总超时** —— 230MB 按块读，
#: 每块之间给 30 秒，断流就抛。任务书 2.1 指定 30s。
DOWNLOAD_TIMEOUT = 30

#: 一个"主程序 exe"至少该有多大。低于这个数说明下回来的
#: **不是 exe**（多半是网盘的错误页 / 登录页 / 一段 JSON），
#: 拿它去替换用户的程序是灾难。实测打包出来的 exe 是 228MB 量级。
MIN_EXE_BYTES = 1 * 1024 * 1024

#: Gitee **单个附件**的上限（社区版）：100MB。
#:
#: ★ 2026-10-10 查官方帮助确认（"创建 Release"页原文：单个附件不能超过 100M，
#:   GVP 项目 200M）。我们打出来的 onefile exe 是 **240MB**，也就是说
#:   **根本传不上去** —— 所以清单支持 `app.parts`：把 exe 切成几片分别传，
#:   程序自己逐片校验再拼回来。
#:
#: 这条只用来判断"传得上去吗"（见 :attr:`ManifestPart.oversize_on_gitee`），
#: **不是**运行期的下载限制：托管在别处（网盘 / 对象存储）的分片想多大都行。
ATTACHMENT_LIMIT_BYTES = 100 * 1024 * 1024


class DownloadCancelled(Exception):
    """用户点了「取消下载」。

    ★ 单独一个异常类型，是为了让调用方能把它和"网络出错"分开：
      取消**不是错误**，界面上不该弹红色报错框。
    """


class DownloadError(RuntimeError):
    """下载过程中的可预期失败（网络断、文件太小、校验不过）。

    ``str(exc)`` 已经是可以直接显示给用户的一句话。
    """


# ---------------------------------------------------------------------------
# 更新清单 update/manifest.json（v1.9 新增，替代解析发行版正文）
# ---------------------------------------------------------------------------


def _positive_int(value: Any) -> int:
    """把 JSON 里的数字读成 ``>= 0`` 的 int。**认不出来就是 0，不抛异常。**

    ``bool`` 要单独挡掉 —— Python 里 ``True`` 也是 ``int``，不挡的话
    ``"size": true`` 会变成 1 字节，然后下载校验拿 1 去比 228MB。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value == value and value > 0:
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        if text.isdigit():
            return int(text)
    return 0


def human_size(size: Any) -> str:
    """把字节数写成人话（``"223.5 MB"`` / ``"1.07 GB"``）；取不到就给空串。

    更新向导里"主程序（223.5 MB）"这句话用的就是它。
    1024 进制，B 不带小数。
    """
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{int(value)} B"


@dataclass(frozen=True)
class ManifestPart:
    """清单里 ``app.parts`` 的一片（v1.8.1 新增）。

    为什么要有它：Gitee 社区版**单个附件上限 100MB**，而我们的 onefile exe
    是 240MB 量级 —— 传不上去。于是发版时把 exe 切成几片分别当附件传，
    程序逐片校验、拼回原样、再核对**整体** sha256。

    ==============  =======================================================
    字段             说明
    ==============  =======================================================
    ``url``         这一片的直链
    ``size``        这一片的字节数
    ``sha256``      这一片的 sha256（下完就校验，坏片不会混进拼接）
    ==============  =======================================================

    ★ **片内的 sha256 与整体的 sha256 都要有**：只看整体哈希的话，一个坏片
      要等全部下完才被发现；只看片哈希的话，片与片之间的**顺序与缺失**
      没人管。两个都要，见 :attr:`ManifestApp.usable`。
    """

    url: str = ""
    size: int = 0
    sha256: str = ""

    @property
    def usable(self) -> bool:
        """直链、体积、哈希三样齐全才敢用。"""
        return bool(self.url) and self.size > 0 and bool(self.sha256)

    @property
    def oversize_on_gitee(self) -> bool:
        """托管在 Gitee 上、却超过了单附件上限 —— 传不上去的那种片。

        ★ 只管 gitee.com 的直链：托管在网盘 / 对象存储上的片不受这条限制。
        """
        return "gitee.com" in self.url and self.size > ATTACHMENT_LIMIT_BYTES

    @property
    def size_text(self) -> str:
        """给人看的体积（``"76.3 MB"``）。"""
        return human_size(self.size)


@dataclass(frozen=True)
class ManifestApp:
    """``update/manifest.json`` 里 ``app`` 段的内容。

    ==============  =======================================================
    字段             说明
    ==============  =======================================================
    ``version``     这次发布的版本号（``"1.8.1"``）
    ``url``         主程序 exe 的直链（**单文件**时用；多段时留空）
    ``size``        字节数（``0`` = 清单里没写）
    ``sha256``      小写 64 位十六进制（空串 = 清单里没写）
    ``notes``       发版说明（纯文本，**不许写 Markdown** —— 总纲 §三 铁律 9）
    ``published``   发布日期文字，只用于显示
    ``parts``       多段下载的分片（v1.8.1 新增；空 = 走单文件那套）
    ==============  =======================================================

    ★ ``size`` / ``sha256`` 在两种模式下都指**整个 exe**：单文件时它就是那个
      文件的体积与哈希；多段时它是**拼回来之后**的体积与哈希
      （每一片自己的体积/哈希在 :class:`ManifestPart` 里）。
    """

    version: str = ""
    url: str = ""
    size: int = 0
    sha256: str = ""
    notes: str = ""
    published: str = ""
    parts: tuple[ManifestPart, ...] = ()

    @property
    def multi_part(self) -> bool:
        """这份清单是不是"切成几片"的发布。"""
        return bool(self.parts)

    @property
    def usable(self) -> bool:
        """够不够格拿来**自动更新**。

        单文件：直链、体积、哈希三样缺一不可（v1.9 起就是这样）。

        多段：每一片三样齐全、**片的体积之和等于整体体积**、整体哈希有写，
        并且**托管在 Gitee 上的片不许超过 100MB 上限**（传不上去的清单
        等于死的，宁可不认）。

        ★ 少任何一样都宁可退回老路子。拿一个"不知道多大、也没法校验"的文件
          去覆盖用户手里的程序，不是勇敢，是不负责任。
        """
        if self.parts:
            if self.size <= 0 or not self.sha256:
                return False
            if sum(p.size for p in self.parts) != self.size:
                return False
            return all(p.usable and not p.oversize_on_gitee for p in self.parts)
        return bool(self.url) and self.size > 0 and bool(self.sha256)

    @property
    def size_text(self) -> str:
        """给人看的体积（``"223.5 MB"``）；没写体积时给空串。"""
        return human_size(self.size)

    @property
    def parts_text(self) -> str:
        """给界面用的一句"分几片"（单文件时给空串）。"""
        if not self.parts:
            return ""
        return f"{len(self.parts)} 片"


def parse_manifest(payload: Any) -> tuple[ManifestApp | None, str]:
    """解析一份清单。**永不抛异常**，返回 ``(app, 说明)``。

    三种可能的返回：

    * ``(None, 原因)`` —— 压根没解析出 app（不是 JSON 对象 / 没有 app 段 /
      读不出版本号 / ``schema`` 是本程序不认识的版本）；
    * ``(app, 原因)`` —— app 解析出来了但**不完整**（缺 url / size / sha256），
      调用方必须看 :meth:`ManifestApp.usable` 再决定用不用；
    * ``(app, "")`` —— 完整可用。

    ``schema`` 的处理是**不猜**：写了别的版本号就当读不懂，
    免得以后改了格式，老程序拿新清单瞎解析出一堆错值。
    """
    if not isinstance(payload, dict):
        return None, "清单不是一个 JSON 对象"
    schema = payload.get("schema")
    if schema is not None and schema != MANIFEST_SCHEMA:
        return None, f"清单格式版本是 {schema!r}，本程序只认 {MANIFEST_SCHEMA}"
    app = payload.get("app")
    if not isinstance(app, dict):
        return None, "清单里没有 app 段"
    # 版本号两种写法都认：写在 ``app`` 里（"这次发布的程序是哪个版本"），
    # 或写在顶层（"这份清单描述哪个版本"）。发版的人两种都会写，没必要挑。
    version = normalize_version(app.get("version")) or normalize_version(
        payload.get("version")
    )
    if not version:
        return None, "清单里没有可识别的版本号"
    # 发版说明同理：优先 app.notes，其次顶层 notes。
    notes = str(app.get("notes") or payload.get("notes") or "").strip()

    # 分片（v1.8.1 新增）：`app.parts` 是一个列表，每项 url / size / sha256。
    # ★ 结构不对的项**照样收进来**（宁可让它 usable=False 明确报错，
    #   也不要静默丢掉一片 —— 丢一片就是拼出半个 exe 去替换用户的程序）。
    raw_parts = app.get("parts")
    parts: list[ManifestPart] = []
    if isinstance(raw_parts, list):
        for item in raw_parts:
            if not isinstance(item, dict):
                parts.append(ManifestPart())
                continue
            parts.append(
                ManifestPart(
                    url=str(item.get("url") or "").strip(),
                    size=_positive_int(item.get("size")),
                    sha256=normalize_sha256(item.get("sha256")),
                )
            )
    elif raw_parts is not None:
        return None, "清单里 app.parts 不是一个列表"

    manifest = ManifestApp(
        version=version,
        url=str(app.get("url") or "").strip(),
        size=_positive_int(app.get("size")),
        sha256=normalize_sha256(app.get("sha256")),
        notes=notes,
        published=str(payload.get("published") or "").strip(),
        parts=tuple(parts),
    )
    if manifest.usable:
        return manifest, ""
    if manifest.multi_part:
        if any(not p.usable for p in manifest.parts):
            return manifest, "清单里的分片有缺 url / size / sha256 的"
        if any(p.oversize_on_gitee for p in manifest.parts):
            return manifest, (
                f"有分片超过 Gitee 单附件上限（{human_size(ATTACHMENT_LIMIT_BYTES)}）"
                "，传不上去"
            )
        if sum(p.size for p in manifest.parts) != manifest.size:
            return manifest, "分片体积之和与 app.size 对不上"
        return manifest, "清单里 app.size / app.sha256 没写全"
    return manifest, "清单里 app.url / app.size / app.sha256 没写全"


def fetch_manifest(
    url: str = GITEE_RAW_MANIFEST, timeout: float = DEFAULT_TIMEOUT
) -> tuple[ManifestApp | None, str]:
    """GET 一次清单。**永不抛异常**，返回 ``(app, 说明)``。

    说明的三种常见情况（都**不是故障**，只是"退回老路子"）：

    * ``404`` —— 仓库里还没有 ``update/manifest.json``；
    * 连不上 / 超时 —— 断网，或者 Gitee 抽风；
    * 拿到了但不是合法 JSON —— 多半被网关换成了 HTML 错误页。
    """
    status, payload = _get_json(url, timeout)
    if status is None:
        return None, "连不上服务器（断网或 Gitee 抽风）"
    if status == 404:
        return None, "远端还没有 update/manifest.json（404 不是故障）"
    if status != 200:
        return None, f"服务器返回 HTTP {status}"
    if payload is None:
        return None, "清单不是合法 JSON（可能被网关换成了 HTML 错误页）"
    return parse_manifest(payload)


@dataclass(frozen=True)
class UpdateCheck:
    """一次更新检查的结果。

    ``status`` 是下面四个之一：

    * :data:`STATUS_NEWER` —— 有新版本，``version`` 是它
    * :data:`STATUS_CURRENT` —— 已是最新（接口报的版本 <= 当前版本）
    * :data:`NO_RELEASE` —— 接口通了，但仓库里一个发行版都没有
    * :data:`STATUS_ERROR` —— 超时 / 断网 / 接口不通

    ``detail`` 是给状态行直接用的一句话（**纯文本，不写 Markdown** ——
    QLabel 不渲染，charter 铁律 9）。

    ``body`` 是发行版描述原文（v1.8 新增）。更新向导要拿它解析出
    "主程序直链 / 各 AI 模块直链 / sha256 / 体积"（:func:`parse_release_body`）。
    ★ 只有 :data:`STATUS_NEWER` 时才会填 —— 没有新版本就没必要
      把整段描述带着满世界传。

    ``url`` / ``size`` / ``sha256`` / ``notes`` 是**清单路线**带出来的直链信息
    （v1.9 新增）。走了清单时 ``body`` 是空串，更新向导直接用这三个字段，
    不用再解析正文。``source`` 说明这次信息是从哪儿来的
    （:data:`SOURCE_MANIFEST` / :data:`SOURCE_RELEASE`），给日志和测试看。
    """

    status: str
    version: str = ""
    detail: str = ""
    body: str = ""
    url: str = ""
    size: int = 0
    sha256: str = ""
    notes: str = ""
    source: str = ""
    parts: tuple[ManifestPart, ...] = ()

    @property
    def reachable(self) -> bool:
        """接口到底通没通。``False`` 才叫"检查失败，请手动访问仓库"。"""
        return self.status != STATUS_ERROR

    @property
    def has_newer(self) -> bool:
        return self.status == STATUS_NEWER

    @property
    def multi_part(self) -> bool:
        """这次要下的是**切成几片**的主程序（v1.8.1 新增）。"""
        return bool(self.parts)

    @property
    def has_direct_link(self) -> bool:
        """带出了**能校验**的直链，可以走自动下载。

        单文件：``url`` + ``size`` + ``sha256`` 三样齐全；
        多段（v1.8.1）：有分片、整体 ``size``/``sha256`` 齐全，
        且每一片自己也是三样齐全。

        ★ 少一样就 ``False`` —— 界面这时只能给"打开浏览器自己下"。
        """
        if self.parts:
            return (
                self.size > 0
                and bool(self.sha256)
                and all(p.usable for p in self.parts)
                and sum(p.size for p in self.parts) == self.size
            )
        return bool(self.url) and self.size > 0 and bool(self.sha256)

    @property
    def parts_text(self) -> str:
        """给界面用的一句"分几片"（单文件时给空串）。"""
        if not self.parts:
            return ""
        return f"{len(self.parts)} 片"

    @property
    def size_text(self) -> str:
        """清单给的主程序体积（``"223.5 MB"``）；没带直链体积时给空串。"""
        return human_size(self.size)


def _get_json(url: str, timeout: float) -> tuple[int | None, Any]:
    """GET 一次并解析 JSON。**不抛异常**，返回 ``(HTTP 状态码, 解析结果)``。

    状态码 ``None`` = 连都没连上（超时 / DNS / 断网）。
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(getattr(response, "status", 0) or 0)
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        # HTTPError 是 URLError 的子类，必须**先**接住才有状态码可看
        return int(exc.code), None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def _version_from_release(release: Any) -> str:
    """从一条 release 记录里抠出版本号。

    charter 7.2 说解析 ``tag_name``。实际接口里 ``tag_name`` / ``name``
    可能只有其一（发版时手滑少填一个不算罕见），所以多试几个字段，
    **哪个能解析出版本号就用哪个**。
    """
    if not isinstance(release, dict):
        return ""
    for key in ("tag_name", "name", "tag", "title"):
        version = normalize_version(release.get(key))
        if version:
            return version
    return ""


def latest_release(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """拿一条 release 记录（``latest`` 拿不到就退一步读列表）。

    ★ **deprecated（v1.9 起）**：版本号与直链请走 ``update/manifest.json``
      （:func:`fetch_manifest`）。本函数只在清单不可用时兜底。

    **任何异常都返回 ``None``**，绝不往外抛 —— 调用方是设置页的一个按钮，
    用户点一下不该看到 traceback（charter 7.2："超时/失败返回 None，不弹错"）。

    Returns:
        release 的 dict；连不上 / 没发布过 / 格式不对一律 ``None``。
    """
    status, data = _get_json(GITEE_API, timeout)
    if status == 200 and isinstance(data, dict):
        return data

    # ---- 兜底：latest 404（"还没有发行版"）时读列表 ----------------------
    status, data = _get_json(GITEE_RELEASES_API, timeout)
    if status == 200 and isinstance(data, list):
        # Gitee 列表按创建时间倒序，第一条就是最新的
        for item in data:
            if isinstance(item, dict):
                return item
    return None


def check_latest_version(timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """最新版本号（``"1.7"`` 这种）；拿不到 / 没发布过返回 ``None``。

    charter 7.2 指定的就是这个函数。**它的契约一个字没改**：
    成功给版本号字符串，失败给 ``None``。

    想知道"失败"到底是哪一种（断网？还是仓库里根本没发过版本？），
    用 :func:`check_for_update` —— 它多给一个状态。
    """
    version = _version_from_release(latest_release(timeout=timeout))
    return version or None


def check_for_update(
    timeout: float = DEFAULT_TIMEOUT,
    *,
    current: str = CURRENT_VERSION,
    manifest_url: str = GITEE_RAW_MANIFEST,
) -> UpdateCheck:
    """一次完整的更新检查，带**能说清楚**的状态。

    设置页用这个（而不是只用 :func:`check_latest_version`），因为
    "接口通了但仓库里一个发行版都没有"和"根本没连上"必须给用户
    不同的话 —— 前者不是故障，说成"检查失败"是撒谎。

    v1.9 的顺序是**先清单、后正文**：

    1. 读 ``update/manifest.json``（:func:`fetch_manifest`）。拿到一份
       :meth:`ManifestApp.usable` 的清单就照它给结果 ——
       版本号来自 ``app.version``，直链/体积/哈希一并带回去；
    2. 清单拿不到、读不懂、**或者三样关键字段没写全** → 退回老的发行版路线
       （:func:`_check_from_release`，deprecated）。**行为与 v1.8 完全一致**，
       所以清单还没发布的那段时间，这个功能不会退化。

    Args:
        timeout: 秒。
        current: 拿来比的当前版本（测试用；默认 :data:`core.version.CURRENT_VERSION`）。
        manifest_url: 清单地址（测试用）。

    Returns:
        :class:`UpdateCheck`。
    """
    app, _problem = fetch_manifest(manifest_url, timeout)
    if app is not None and app.usable:
        return _from_manifest(app, current)
    return _check_from_release(timeout, current)


def _from_manifest(app: ManifestApp, current: str) -> UpdateCheck:
    """清单路线：版本号 / 直链 / 体积 / 哈希全部来自 ``update/manifest.json``。

    三种版本关系的话术与老路线**逐字一致**（"发现新版本"、
    "已是最新"、"清单上写的比本机旧"），免得用户在不同路线上看到两套说法。
    """
    if is_newer(app.version, current):
        return UpdateCheck(
            status=STATUS_NEWER,
            version=app.version,
            detail=f"发现新版本 v{app.version}（当前 v{current}）。",
            url=app.url,
            size=app.size,
            sha256=app.sha256,
            notes=app.notes,
            source=SOURCE_MANIFEST,
            parts=app.parts,
        )
    if compare_versions(app.version, current) < 0:
        # 清单上的版本比本机还旧（比如发版人忘了更新清单）——
        # 不劝人降级，但也别装作"已是最新"，把这句实话说出来。
        return UpdateCheck(
            status=STATUS_CURRENT,
            version=app.version,
            detail=(
                f"当前已是最新版本 (v{current})。"
                f"清单上写的是 v{app.version}，比本机旧。"
            ),
            source=SOURCE_MANIFEST,
        )
    return UpdateCheck(
        status=STATUS_CURRENT,
        version=app.version,
        detail=f"当前已是最新版本 (v{current})。",
        source=SOURCE_MANIFEST,
    )


def _check_from_release(timeout: float, current: str) -> UpdateCheck:
    """**deprecated**：v1.8 及之前的老路子 —— 从 Gitee 发行版抠版本号和正文。

    ★ 保留不删（任务书要求），但只在清单不可用时兜底。
      新发版方式请写 ``update/manifest.json``，别再依赖正文正则。
    """
    latest_status, latest_data = _get_json(GITEE_API, timeout)
    version = ""
    release: dict[str, Any] | None = None
    if latest_status == 200 and isinstance(latest_data, dict):
        version = _version_from_release(latest_data)
        release = latest_data

    if not version:
        # latest 没给出可用版本：区分"还没发过版"和"真失败"
        list_status, list_data = _get_json(GITEE_RELEASES_API, timeout)
        if list_status == 200 and isinstance(list_data, list):
            if not list_data:
                # ★ **列表空**才叫"没有发行版"
                return UpdateCheck(
                    status=NO_RELEASE,
                    source=SOURCE_RELEASE,
                    detail=(
                        f"当前已是最新版本 (v{current}) —— "
                        "仓库里还没有发布任何发行版，暂时没有可下载的新版本。"
                    ),
                )
            for item in list_data:
                version = _version_from_release(item)
                if version:
                    release = item
                    break
            if not version:
                # ★ 列表**非空**却读不出版本号 —— 这不是"没发行版"，
                #   是"有发行版但我们看不懂"。第一版把这两种混成一种，
                #   于是界面会说"仓库里还没有发布任何发行版"，
                #   而仓库里明明挂着东西 —— 那是句假话。
                return UpdateCheck(
                    status=STATUS_ERROR,
                    source=SOURCE_RELEASE,
                    detail=(
                        "检查失败：仓库里有发行版，但读不出可识别的版本号。"
                        "请手动访问仓库查看。"
                    ),
                )
        else:
            return UpdateCheck(
                status=STATUS_ERROR,
                source=SOURCE_RELEASE,
                detail="检查失败：连不上 Gitee。请手动访问仓库查看是否有新版本。",
            )

    if not version:  # pragma: no cover - 上面两条分支已经覆盖
        return UpdateCheck(
            status=STATUS_ERROR,
            source=SOURCE_RELEASE,
            detail="检查失败：接口返回里读不出可识别的版本号。请手动访问仓库。",
        )

    if is_newer(version, current):
        body = ""
        if isinstance(release, dict):
            body = str(release.get("body") or release.get("description") or "")
        return UpdateCheck(
            status=STATUS_NEWER,
            version=version,
            detail=f"发现新版本 v{version}（当前 v{current}）。",
            body=body,
            source=SOURCE_RELEASE,
        )

    relation = compare_versions(version, current)
    if relation < 0:
        # 接口上的版本比本机还旧（比如发行版没跟上代码）—— 不劝人降级，
        # 但也别装作"已是最新"，把这句实话说出来。
        return UpdateCheck(
            status=STATUS_CURRENT,
            version=version,
            source=SOURCE_RELEASE,
            detail=(
                f"当前已是最新版本 (v{current})。"
                f"仓库上最新的一条发行版是 v{version}，比本机旧。"
            ),
        )
    return UpdateCheck(
        status=STATUS_CURRENT,
        version=version,
        source=SOURCE_RELEASE,
        detail=f"当前已是最新版本 (v{current})。",
    )


# ---------------------------------------------------------------------------
# Release 描述解析（任务书 2.1）
# ---------------------------------------------------------------------------

#: 认 URL。中文括号、引号、``<>`` 都排除掉，免得把描述里的标点吃进来。
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`,，。）】]+")

#: ``sha256: <64 hex>`` / ``SHA256 = <64 hex>``
_SHA_LABELED_RE = re.compile(r"sha-?256\s*[:=]\s*([0-9a-fA-F]{64})")

#: 裸的 64 位十六进制串（描述里直接贴一行哈希）
_SHA_BARE_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")

#: ``大小: 230MB`` / ``size: 1.2 GB``
_SIZE_RE = re.compile(r"(?:大小|size|体积)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?\s*[KMGTP]?i?B)", re.I)

#: ``AI模型-1.5B`` / ``AI 模型 - 3B`` / ``AI模型：Qwen-1.5B``
_AI_HEADER_RE = re.compile(r"^AI\s*模型\s*[-–—:：]?\s*(.+)$", re.I)

#: ``主程序`` 那一节的开头（可以带"更新""（必须）"之类的后缀）
_MAIN_HEADER_RE = re.compile(r"^主程序\b.*$")

#: 解析结果里代表主程序的键
KEY_MAIN = "main"


def _strip_markup(line: str) -> str:
    """去掉 Markdown 的行首记号（``##`` / ``**`` / ``-`` / ``>``）和行尾星号。

    这样 ``## 主程序``、``**主程序**``、``- 主程序`` 都能被认成同一件事 ——
    发版的人不会每次都写成一样。
    """
    text = line.strip()
    text = text.lstrip("#>*-•· \t")
    text = text.strip()
    if text.endswith("**") or text.endswith("__"):
        text = text[:-2]
    return text.strip().strip("*_`").strip()


def parse_release_body(body: Any) -> dict[str, Any]:
    """从 Release 描述里解析出下载链接（任务书 2.1）。

    ★ **deprecated（v1.9 起）**：新发版方式是在 ``update/manifest.json`` 里
      写结构化字段（:func:`parse_manifest`），不用再让程序拿正则去抠散文。
      本函数**保留不删**，只在清单不可用时兜底（见 :func:`check_for_update`）。

    约定格式（``docs/DISTRIBUTION.md`` 的发版 SOP 里写的就是这个）::

        主程序
        https://pan.example.com/transcript-lens-v1.8.exe
        sha256: 3f2a...（64 位）
        大小: 230MB

        AI模型-1.5B
        https://pan.example.com/qwen-1.5b.gguf
        sha256: ...

        AI模型-3B
        https://pan.example.com/qwen-3b.gguf

    也认这些等价写法（发版的人手滑不算罕见，没必要让功能因此失效）：

    * ``## 主程序`` / ``**主程序**`` / ``- 主程序``
    * ``主程序: https://...``（标题和链接同一行）
    * ``AI 模型 - 3B`` / ``AI模型：3B``
    * 直接给一段 JSON：``{"main": "...", "ai_models": {"1.5B": "..."}}``
    * ``sha256`` 写成 ``SHA256 = ...`` 或直接把 64 位哈希裸贴一行

    ★ 一个刻意的取舍：描述里**一个能认的 URL 都没有**时返回空字典结构
      （``main`` 为空串），而不是抛异常 —— 调用方（设置页）要能说
      "这个发行版没有提供直链，请点「打开下载页」"，那比一个 traceback 有用。

    Args:
        body: Release 描述原文。``None`` / 非字符串一律当成空串。

    Returns:
        ``{"main": str, "ai_models": {名字: url}, "checksums": {键: sha256},
        "sizes": {键: 文字}}``。

        ``checksums`` / ``sizes`` 的键是 ``"main"`` 或模型名。
        ★ 任务书 2.1 只点名了前两个键；后两个是**附加**信息，
          用于 :func:`verify_checksum` 和弹窗里的"~230MB"。
    """
    result: dict[str, Any] = {
        KEY_MAIN: "",
        "ai_models": {},
        "checksums": {},
        "sizes": {},
    }
    text = body if isinstance(body, str) else ""
    if not text.strip():
        return result

    # --- 先试 JSON（有人喜欢把描述写成一段 JSON）--------------------------
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and ("main" in data or "ai_models" in data):
            result[KEY_MAIN] = str(data.get("main") or "").strip()
            models = data.get("ai_models")
            if isinstance(models, dict):
                for name, url in models.items():
                    if url:
                        result["ai_models"][str(name).strip()] = str(url).strip()
            checksums = data.get("checksums")
            if isinstance(checksums, dict):
                for key, value in checksums.items():
                    if value:
                        result["checksums"][str(key)] = str(value).strip().lower()
            return result

    # --- 按行扫 -----------------------------------------------------------
    section = ""            # "" / "main" / 模型名
    urls_seen: list[str] = []

    for raw_line in text.splitlines():
        line = _strip_markup(raw_line)
        if not line:
            continue

        # 1) 这一行是不是小标题？
        ai_match = _AI_HEADER_RE.match(line)
        if ai_match:
            name = ai_match.group(1).strip().strip("*_`").strip()
            # `AI模型-3B: https://...` —— 标题和链接同一行
            inline = _URL_RE.search(name)
            if inline:
                name = name[: inline.start()].strip().rstrip(":：-–—").strip()
                section = name or "model"
                result["ai_models"].setdefault(section, inline.group(0))
                urls_seen.append(inline.group(0))
            else:
                section = name or "model"
            continue
        if _MAIN_HEADER_RE.match(line):
            section = KEY_MAIN
            # `主程序: https://...`
            inline = _URL_RE.search(line)
            if inline:
                result[KEY_MAIN] = result[KEY_MAIN] or inline.group(0)
                urls_seen.append(inline.group(0))
            continue

        # 2) 链接
        found = _URL_RE.findall(line)
        for url in found:
            urls_seen.append(url)
            if section == KEY_MAIN:
                if not result[KEY_MAIN]:
                    result[KEY_MAIN] = url
            elif section:
                result["ai_models"].setdefault(section, url)

        # 3) 校验和（优先带 label 的；没有 label 才认裸哈希）
        sha = _SHA_LABELED_RE.search(line)
        if sha is None:
            bare = _SHA_BARE_RE.search(line)
            # ★ 裸哈希只在"这一行没有 URL"时认 —— 否则 URL 里恰好出现
            #   一段 64 位十六进制（网盘直链很常见）会被误当成哈希。
            sha = bare if (bare and not found) else None
        if sha is not None and section:
            key = KEY_MAIN if section == KEY_MAIN else section
            result["checksums"].setdefault(key, sha.group(1).lower())

        # 4) 体积
        size = _SIZE_RE.search(line)
        if size is not None and section:
            key = KEY_MAIN if section == KEY_MAIN else section
            result["sizes"].setdefault(key, " ".join(size.group(1).split()))

    # --- 兜底：一个标题都没认出来、但整段只有一个 URL → 那就是主程序 -------
    if not result[KEY_MAIN] and not result["ai_models"] and len(urls_seen) == 1:
        result[KEY_MAIN] = urls_seen[0]

    return result


# ---------------------------------------------------------------------------
# 校验和（任务书 2.1）
# ---------------------------------------------------------------------------


def normalize_sha256(text: Any) -> str:
    """把 ``sha256: <hex>`` / 大写 / 带空格 的写法收敛成 64 位小写十六进制。

    认不出来返回空串。
    """
    if text is None:
        return ""
    candidate = str(text).strip().lower()
    candidate = candidate.removeprefix("sha256").lstrip(":：= ").strip()
    match = re.search(r"\b([0-9a-f]{64})\b", candidate)
    return match.group(1) if match else ""


def verify_checksum(file_path: str | Path, expected_sha256: str) -> bool:
    """SHA256 校验（任务书 2.1）。**一致返回 True，其余一律 False。**

    ★ 空期望值返回 **False**，不是 True。
      "没给校验和"和"校验通过"是**两件事** —— 让空值通过等于把这道闸门
      变成默认敞开。调用方想跳过校验就自己判断
      ``if expected and not verify_checksum(...)``，让"跳过"这个决定
      留在调用点上、写在代码里，而不是藏在这个函数的默认行为里。

    Args:
        file_path: 待校验的文件。
        expected_sha256: 期望的哈希（``sha256:`` 前缀 / 大写 / 空白都能认）。

    Returns:
        一致 → ``True``；不一致 / 期望为空 / 认不出 / 文件读不了 → ``False``。
    """
    expected = normalize_sha256(expected_sha256)
    if not expected:
        return False
    digest = hashlib.sha256()
    try:
        with open(file_path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return False
    import hmac

    # 定时安全比较：哈希不是密钥，但没有理由在这里用 ==（也免得哪天被当成
    # "可以拿它做旁路"的坏样板抄走）。
    return hmac.compare_digest(digest.hexdigest(), expected)


# ---------------------------------------------------------------------------
# 下载（任务书 2.1）
# ---------------------------------------------------------------------------


def _content_length(response: Any, offset: int) -> int | None:
    """算出"这个文件一共多大"。拿不到返回 ``None``（界面上显示"未知"）。"""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    def _int(name: str) -> int | None:
        try:
            raw = headers.get(name)
        except Exception:  # noqa: BLE001 - headers 不是 dict 的假实现
            return None
        if raw is None:
            return None
        text = str(raw).strip()
        if not text.isdigit():
            return None
        return int(text)

    # 断点续传时 Content-Length 只是**这一段**的长度，总量在 Content-Range 里：
    #   Content-Range: bytes 1024-9999/10000
    content_range = ""
    try:
        content_range = str(headers.get("Content-Range") or "")
    except Exception:  # noqa: BLE001
        content_range = ""
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)

    length = _int("Content-Length")
    if length is None:
        return None
    return length + offset


def download_exe(
    download_url: str,
    dest_path: str | Path,
    progress_callback: Callable[[int, int | None], None] | None = None,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    resume: bool = True,
    cancel: Callable[[], bool] | None = None,
    expected_sha256: str = "",
    min_bytes: int = MIN_EXE_BYTES,
) -> Path:
    """流式下载到 ``dest_path``（任务书 2.1）。**只用 stdlib**。

    流程与保证：

    1. 全程只往 ``<dest>.part`` 写，**成功之后才** ``os.replace`` 成目标文件
       —— 所以中途失败/取消**绝不会**损坏已经存在的那个 exe
       （任务书 2.4："下载失败不损坏已有 exe"）。
    2. ``resume=True`` 且 ``.part`` 已存在 → 带 ``Range: bytes=<已有>-`` 续传。
       服务器**忽略 Range**（返回 200 而不是 206）时**自动从头重下**，
       不会把新内容追加到旧字节后面拼出一个坏文件。
    3. ``cancel()`` 返回真 → 抛 :class:`DownloadCancelled`，
       ``.part`` **留在原地**（下次续传用得上），目标文件不动。
    4. ``expected_sha256`` 非空 → 校验；不一致抛 :class:`DownloadError`，
       目标文件不动。
    5. ``min_bytes`` → 下回来的东西太小（网盘错误页 / 登录页）直接判失败。

    Args:
        download_url: 直链。
        dest_path: 目标文件路径。
        progress_callback: ``callback(已下载字节, 总字节或 None)``。
            **在调用线程里同步调用** —— 调用方自己负责把它丢到后台。
        timeout: 每块之间的超时（秒）。
        resume: 允不允许断点续传。
        cancel: 无参可调用对象，返回真表示用户要取消。
        expected_sha256: 期望哈希；空串 = 不校验。
        min_bytes: 最小可接受字节数。

    Returns:
        真正的目标路径（:class:`pathlib.Path`）。

    Raises:
        DownloadCancelled: 用户取消。
        DownloadError: 网络失败 / 太小 / 校验不过。
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    offset = 0
    if resume and part.exists():
        try:
            offset = part.stat().st_size
        except OSError:
            offset = 0
    elif part.exists():
        try:
            part.unlink()
        except OSError:
            pass

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/octet-stream",
    }
    if offset:
        headers["Range"] = f"bytes={offset}-"

    request = urllib.request.Request(download_url, headers=headers, method="GET")

    try:
        response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"下载失败：服务器返回 HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"下载失败：连不上服务器（{exc.reason}）。") from exc
    except (OSError, ValueError) as exc:
        raise DownloadError(f"下载失败：{exc}") from exc

    mode = "ab"
    with response:
        status = int(getattr(response, "status", 200) or 200)
        if offset and status != 206:
            # ★ 服务器不支持/忽略了 Range：它给的是**整个文件**，
            #   必须从头写，否则旧的那截会留在前面拼成垃圾。
            offset = 0
            mode = "wb"
        total = _content_length(response, offset)

        written = offset
        try:
            with open(part, mode) as sink:
                while True:
                    if cancel is not None and cancel():
                        raise DownloadCancelled("用户取消了下载")
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sink.write(chunk)
                    written += len(chunk)
                    if progress_callback is not None:
                        progress_callback(written, total)
                sink.flush()
                os.fsync(sink.fileno())
        except DownloadCancelled:
            raise
        except (OSError, ValueError) as exc:
            raise DownloadError(
                f"下载中断：{exc}。已下载的部分留在 {part.name}，"
                "下次点「开始下载」会从断点继续。"
            ) from exc

    if min_bytes and written < min_bytes:
        raise DownloadError(
            f"下载到的文件只有 {written} 字节，明显不是程序本体"
            f"（至少应有 {min_bytes} 字节）。可能是网盘返回了错误页 —— "
            "请到发行版页面手动下载。"
        )

    if expected_sha256 and not verify_checksum(part, expected_sha256):
        raise DownloadError(
            "下载完成但 SHA256 校验不一致 —— 文件可能在传输中损坏，"
            "或与发行版声明的不符。已丢弃，请重试或到页面手动下载。"
        )

    os.replace(part, dest)
    if progress_callback is not None:
        progress_callback(written, total or written)
    return dest


def download_parts(
    parts: Sequence[ManifestPart],
    dest_path: str | Path,
    progress_callback: Callable[[int, int | None], None] | None = None,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    resume: bool = True,
    cancel: Callable[[], bool] | None = None,
    expected_sha256: str = "",
    expected_size: int = 0,
    min_bytes: int = MIN_EXE_BYTES,
) -> Path:
    """把**切成几片**的主程序下回来、拼好、校验（v1.8.1 新增）。

    为什么会有这个函数：Gitee 社区版单个附件上限 100MB，而 onefile exe 是
    240MB 量级 —— 只能切片上传。切片带来的风险必须由这一段代码兜住，
    所以它的保证比 :func:`download_exe` 只多不少：

    1. **逐片校验**：每一片下完先核 ``part.sha256``，坏片立刻丢弃重下，
       绝不混进拼接（否则要等 240MB 全下完才发现白干）；
    2. **顺序固定**：拼接顺序就是清单里 ``app.parts`` 的顺序，不看文件名；
    3. **整体再校验**：拼完核 ``expected_sha256``（整个 exe 的哈希）
       与 ``expected_size``，任何一条不过就**不写目标文件**；
    4. **全程只写 ``<dest>.part``**，成功之后才 ``os.replace`` ——
       与 :func:`download_exe` 同一条红线：失败/取消**绝不损坏已有 exe**；
    5. **可续传**：每片缓存在 ``<dest>.parts/partNN``，取消/断网后再点一次，
       已经校验过的片**直接跳过**（连请求都不发）；
    6. 全部成功后才清掉 ``<dest>.parts/``。

    Args:
        parts: 分片清单（顺序即拼接顺序）。
        dest_path: 最终目标文件。
        progress_callback: ``callback(已下载字节, 总字节或 None)`` ——
            **整条链的累计进度**，不是某一片的。
        timeout: 每块之间的超时（秒）。
        resume: 允不允许续传（片级 + 整体）。
        cancel: 无参可调用对象，返回真表示用户要取消。
        expected_sha256: **拼好后**整个文件的 sha256；空串 = 不校验。
        expected_size: **拼好后**整个文件的字节数；``0`` = 不校验。
        min_bytes: 拼好后小于这个数判失败（防"下回来一个错误页"）。

    Returns:
        真正的目标路径（:class:`pathlib.Path`）。

    Raises:
        DownloadCancelled: 用户取消。
        DownloadError: 网络失败 / 片校验不过 / 体积对不上 / 整体校验不过。
    """
    if not parts:
        raise DownloadError("没有任何分片可下 —— 清单里的 app.parts 是空的。")

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    whole = dest.with_name(dest.name + ".part")
    pool = dest.with_name(dest.name + ".parts")
    pool.mkdir(parents=True, exist_ok=True)

    total: int | None = expected_size or sum(p.size for p in parts) or None
    done_before = 0

    # --- 1. 逐片下载 ---------------------------------------------------
    for index, piece in enumerate(parts, start=1):
        cache = pool / f"part{index:02d}"
        if resume and _looks_complete(cache, piece.size, piece.sha256):
            done_before += piece.size
            if progress_callback is not None:
                progress_callback(done_before, total)
            continue

        def _relay(written: int, _inner: int | None, _base: int = done_before) -> None:
            if progress_callback is not None:
                progress_callback(_base + written, total)

        download_exe(
            piece.url,
            cache,
            _relay,
            timeout=timeout,
            resume=resume,
            cancel=cancel,
            expected_sha256=piece.sha256,
            min_bytes=min(piece.size, MIN_EXE_BYTES),
        )
        done_before += piece.size

    # --- 2. 按清单顺序拼接 ---------------------------------------------
    written = 0
    try:
        with open(whole, "wb") as sink:
            for index in range(1, len(parts) + 1):
                with open(pool / f"part{index:02d}", "rb") as source:
                    while True:
                        block = source.read(CHUNK_SIZE)
                        if not block:
                            break
                        sink.write(block)
                        written += len(block)
            sink.flush()
            os.fsync(sink.fileno())
    except OSError as exc:
        raise DownloadError(
            f"拼接分片失败：{exc}。已下好的分片留在 {pool.name}\\ 里，重试不用重下。"
        ) from exc

    # --- 3. 整体校验（先体积、后哈希）----------------------------------
    if expected_size and written != expected_size:
        raise DownloadError(
            f"拼出来的文件是 {written} 字节，清单声明 {expected_size} 字节 —— "
            "分片可能不完整。已丢弃，请重试或到发行版页面手动下载。"
        )
    if min_bytes and written < min_bytes:
        raise DownloadError(
            f"拼出来的文件只有 {written} 字节，明显不是程序本体"
            f"（至少应有 {min_bytes} 字节）。请到发行版页面手动下载。"
        )
    if expected_sha256 and not verify_checksum(whole, expected_sha256):
        raise DownloadError(
            "分片全部下完，但拼起来的整体 SHA256 校验不一致 —— "
            "文件可能在传输中损坏，或与发行版声明的不符。已丢弃，请重试。"
        )

    os.replace(whole, dest)
    shutil.rmtree(pool, ignore_errors=True)
    if progress_callback is not None:
        progress_callback(written, total or written)
    return dest


def _looks_complete(path: Path, size: int, sha256: str) -> bool:
    """``path`` 是不是一片**已经下好且校验过**的分片（续传时用它跳过请求）。"""
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
    except OSError:
        return False
    return verify_checksum(path, sha256)

