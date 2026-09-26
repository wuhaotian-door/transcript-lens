"""更新检查（charter v1.7 第七节）—— **只在用户点「检查更新」时联网**。

三条设计原则（charter 7.1，一条都不能松）：

1. **用户主动触发**：没有任何自动检查。这个模块里**没有定时器、没有线程、
   没有 import 期副作用** —— 只有人调 :func:`check_latest_version` 才会发请求。
2. **不自动下载**：只告诉用户"有新版本"，然后打开浏览器让他自己去下载。
   写自动替换 exe 的逻辑是被明令禁止的（charter 十节）。
3. **不引入第三方库**：只用标准库 ``urllib.request``（charter 7.3）。

★ 为什么是 ``urllib.request`` 而不是 ``requests``
---------------------------------------------

这是 charter 第三节/第四节里**唯一**被点名的 stdlib 联网方式。
``core/ai/`` 那边仍然是一条 HTTP 客户端都不许有（那条自检的扫描范围就是
``core/ai/*.py``，不包括本模块）。两条规则不冲突：

* ``core/ai/`` —— 骨架期，**禁止**任何网络代码；
* ``core/updater.py`` —— 用户主动点按钮时，**允许**一次只读 GET。

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

★ 超时 5 秒是硬要求
------------------

不设超时或者设得很长，网络不通时用户会盯着一个卡死的按钮。
而且设置页**必须把它丢到后台线程**跑（``ui/workers.run_async``）——
在按钮回调里直接 ``urlopen`` 就是 ``ui/workers.py`` 开头说的那个坑。
本模块只负责"发一次请求、失败返回 None"，**不碰任何 UI**。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.version import CURRENT_VERSION, compare_versions, is_newer, normalize_version

__all__ = [
    "CURRENT_VERSION",
    "DEFAULT_TIMEOUT",
    "GITEE_API",
    "GITEE_RELEASES_API",
    "GITEE_RELEASES_PAGE",
    "NO_RELEASE",
    "STATUS_CURRENT",
    "STATUS_ERROR",
    "STATUS_NEWER",
    "USER_AGENT",
    "UpdateCheck",
    "check_for_update",
    "check_latest_version",
    "latest_release",
]

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
    """

    status: str
    version: str = ""
    detail: str = ""

    @property
    def reachable(self) -> bool:
        """接口到底通没通。``False`` 才叫"检查失败，请手动访问仓库"。"""
        return self.status != STATUS_ERROR

    @property
    def has_newer(self) -> bool:
        return self.status == STATUS_NEWER


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
    timeout: float = DEFAULT_TIMEOUT, *, current: str = CURRENT_VERSION
) -> UpdateCheck:
    """一次完整的更新检查，带**能说清楚**的状态。

    设置页用这个（而不是只用 :func:`check_latest_version`），因为
    "接口通了但仓库里一个发行版都没有"和"根本没连上"必须给用户
    不同的话 —— 前者不是故障，说成"检查失败"是撒谎。

    Args:
        timeout: 秒。
        current: 拿来比的当前版本（测试用；默认 :data:`core.version.CURRENT_VERSION`）。

    Returns:
        :class:`UpdateCheck`。
    """
    latest_status, latest_data = _get_json(GITEE_API, timeout)
    version = ""
    if latest_status == 200 and isinstance(latest_data, dict):
        version = _version_from_release(latest_data)

    if not version:
        # latest 没给出可用版本：区分"还没发过版"和"真失败"
        list_status, list_data = _get_json(GITEE_RELEASES_API, timeout)
        if list_status == 200 and isinstance(list_data, list):
            if not list_data:
                # ★ **列表空**才叫"没有发行版"
                return UpdateCheck(
                    status=NO_RELEASE,
                    detail=(
                        f"当前已是最新版本 (v{current}) —— "
                        "仓库里还没有发布任何发行版，暂时没有可下载的新版本。"
                    ),
                )
            for item in list_data:
                version = _version_from_release(item)
                if version:
                    break
            if not version:
                # ★ 列表**非空**却读不出版本号 —— 这不是"没发行版"，
                #   是"有发行版但我们看不懂"。第一版把这两种混成一种，
                #   于是界面会说"仓库里还没有发布任何发行版"，
                #   而仓库里明明挂着东西 —— 那是句假话。
                return UpdateCheck(
                    status=STATUS_ERROR,
                    detail=(
                        "检查失败：仓库里有发行版，但读不出可识别的版本号。"
                        "请手动访问仓库查看。"
                    ),
                )
        else:
            return UpdateCheck(
                status=STATUS_ERROR,
                detail="检查失败：连不上 Gitee。请手动访问仓库查看是否有新版本。",
            )

    if not version:  # pragma: no cover - 上面两条分支已经覆盖
        return UpdateCheck(
            status=STATUS_ERROR,
            detail="检查失败：接口返回里读不出可识别的版本号。请手动访问仓库。",
        )

    if is_newer(version, current):
        return UpdateCheck(
            status=STATUS_NEWER,
            version=version,
            detail=f"发现新版本 v{version}（当前 v{current}）。",
        )

    relation = compare_versions(version, current)
    if relation < 0:
        # 接口上的版本比本机还旧（比如发行版没跟上代码）—— 不劝人降级，
        # 但也别装作"已是最新"，把这句实话说出来。
        return UpdateCheck(
            status=STATUS_CURRENT,
            version=version,
            detail=(
                f"当前已是最新版本 (v{current})。"
                f"仓库上最新的一条发行版是 v{version}，比本机旧。"
            ),
        )
    return UpdateCheck(
        status=STATUS_CURRENT,
        version=version,
        detail=f"当前已是最新版本 (v{current})。",
    )
