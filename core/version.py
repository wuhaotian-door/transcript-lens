"""版本号的**唯一真源**（charter v1.7 第八节 / v1.8 第三节铁律 10）。

三样东西名字很像，别搞混：

======================  =========================  ================================
名字                    值                         含义
======================  =========================  ================================
``CURRENT_VERSION``     ``"1.8"``                   **程序**版本（这个 exe 是哪一版）
``VAULT_VERSION_KEY``   ``"__vault_version__"``     写进 ``users.json`` 的那个键名
``DEFAULT_VAULT_VERSION`` ``"1.0"``                 老库**没有标记**时按 1.0 算
======================  =========================  ================================

★ v1.8：界面上的版本号**一律从这里读**
--------------------------------------

charter v1.8 第五节把"版本号硬编码在 UI 里"列为必须修正的旧 bug ——
实测确实有三处写死了 ``"v1.0"``：

* ``ui/login_window.py`` 的 ``APP_VERSION``
* ``ui/splash.py`` 的开屏版本号默认参数
* ``ui/pages/account_page.py`` 的"关于"表

三处都不报错，只是**一直在说假话**（程序是 1.8、界面写着 1.0）。
``main._version_source_report()`` 里有一条闸门：用 AST 扫 ``ui/`` 下所有
**字符串字面量**，出现纯版本号字面量（``"v1.0"`` / ``"1.8"``）就 FAIL。
注释和文档字符串不算 —— 那里写 "charter v1.7 第七节" 是**引用**，不是版本显示。

★ 为什么程序版本号只写在这里一份
--------------------------------

`core/updater.py` 也要用它（"当前已是最新版本 (v1.7)"这句要靠它比大小），
`core/migrator.py` 也要用它（判断要不要迁移），设置页还要显示它。
**四个地方各写一份字符串，迟早出现"设置页写着 1.7、更新检查却按 1.6 比"**
这种事 —— 而且没人会报错，只是行为不对。

所以：``core.updater`` 里的 ``CURRENT_VERSION`` 是**从这里 import 过去的**
（名字仍然可用，只是定义点只有一个）。

★ 关于键名的两个来源
------------------

charter 8.1 写的是数据里的键 ``__vault_version__``；
任务书 2.1 写的是 Python 常量名 ``VAULT_VERSION_KEY = "vault_version"``。
两处其实说的不是一回事（一个是常量名、一个是键值），这里按 charter 取键值，
并把任务书那个键名当成**兼容读取**的别名 —— 万一有中间版本写过它，也读得出来。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = [
    "CURRENT_VERSION",
    "DEFAULT_VAULT_VERSION",
    "LEGACY_VAULT_VERSION_KEY",
    "VAULT_VERSION_KEYS",
    "VAULT_VERSION_KEY",
    "compare_versions",
    "is_newer",
    "normalize_version",
    "parse_version",
    "stamp_vault_version",
    "vault_version_of",
]

#: 程序版本。**改版本号只改这一行。**
CURRENT_VERSION = "1.8"

#: 加密库里存数据版本用的键（charter 8.1）
VAULT_VERSION_KEY = "__vault_version__"

#: 兼容读取：任务书 2.1 里写的那个键名，万一有版本写过它就还能读出来
LEGACY_VAULT_VERSION_KEY = "vault_version"

#: ★ 写盘时**两个键都写**，读取时**哪个有值认哪个**。
#:
#: 这不是洁癖，是因为"用哪个键名"这件事在文档之间对不上：
#: charter 8.1 写 ``__vault_version__``，任务书（v1.7 2.1 和 v1.8 1.1 两次）
#: 写 ``vault_version``。两边都是权威文档，让实现去猜哪边赢，风险是
#: **某次真迁移把标记写到一个启动检测读不到的键上** —— 而那正是版本保护
#: 唯一要防的事。写两份的成本是两个 JSON 字段，收益是"谁读都对"。
#: 两个键的值**永远是同一个**（:func:`stamp_vault_version` 是唯一写入点），
#: 自检里有一条断言它们相等。
VAULT_VERSION_KEYS: tuple[str, ...] = (VAULT_VERSION_KEY, LEGACY_VAULT_VERSION_KEY)

#: 没有版本标记的老库一律按这个版本算（任务书 2.2）
DEFAULT_VAULT_VERSION = "1.0"

#: `v1.7` / `1.7.0` / `release-1.7` 都能认出来
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def normalize_version(text: Any) -> str:
    """把各种写法收敛成 ``"1.7"`` 这种点分数字串。认不出来返回空串。

    >>> normalize_version("v1.7"), normalize_version(" 1.6.2 "), normalize_version("?")
    ('1.7', '1.6.2', '')

    ★ **不要去末尾的 0**（第一版这么干过，踩了坑）：
      ``"1.0"`` 会被削成 ``"1"``，于是

      * 备份目录名变成 ``backup_v1_...``（charter 8.2 写的是 ``v{old}``）；
      * 迁移链按 ``"1.0"`` 查表查不到，直接报"没有从 v1 升级的路径"。

      ``"1.7.0"`` 和 ``"1.7"`` 该不该算同一个版本，
      **是 :func:`compare_versions` 的事**（缺的位按 0 补），不是这里的事。
      规范化只管"把 v 前缀/空白/噪音去掉"。
    """
    if text is None:
        return ""
    match = _VERSION_RE.search(str(text))
    if match is None:
        return ""
    return match.group(1)


def parse_version(text: Any) -> tuple[int, ...]:
    """变成可比较的整数元组。认不出来 → ``()``（比任何真版本都小）。

    >>> parse_version("v1.7"), parse_version("1.6.2"), parse_version("乱写")
    ((1, 7), (1, 6, 2), ())
    """
    normalized = normalize_version(text)
    if not normalized:
        return ()
    try:
        return tuple(int(part) for part in normalized.split("."))
    except ValueError:  # pragma: no cover - normalize 已经保证是数字
        return ()


def compare_versions(left: Any, right: Any) -> int:
    """``left`` 比 ``right``：新 → 1，同 → 0，旧 → -1。

    ★ 用**数值**比而不是字符串比：``"1.10" > "1.9"`` 字符串比会得出 False
      （'1' < '9'），而 1.10 其实是更新的版本。这个坑等真出 1.10 那天才会暴露。
    """
    a, b = parse_version(left), parse_version(right)
    for index in range(max(len(a), len(b))):
        x = a[index] if index < len(a) else 0
        y = b[index] if index < len(b) else 0
        if x != y:
            return 1 if x > y else -1
    return 0


def is_newer(candidate: Any, current: Any = CURRENT_VERSION) -> bool:
    """``candidate`` 是否比 ``current`` 新。认不出来的 candidate 一律算"不新"。

    ★ 认不出来时**不能**说"有新版本" —— 那会让用户去下载一个根本解析不出
      版本号的东西。宁可说"已是最新"，也不要误导。
    """
    if not parse_version(candidate):
        return False
    return compare_versions(candidate, current) > 0


def vault_version_of(vault_data: Mapping[str, Any] | None) -> str:
    """从加密库数据里读数据版本；没有标记 → :data:`DEFAULT_VAULT_VERSION`。"""
    if not isinstance(vault_data, Mapping):
        return DEFAULT_VAULT_VERSION
    for key in VAULT_VERSION_KEYS:
        value = normalize_version(vault_data.get(key))
        if value:
            return value
    return DEFAULT_VAULT_VERSION


def stamp_vault_version(data: Any, version: str = CURRENT_VERSION) -> Any:
    """把数据版本标记写进一份 ``users.json`` 字典（**唯一写入点**）。

    ★ 两个键名**同时写**，值永远相同（原因见 :data:`VAULT_VERSION_KEYS`）。
      写之前会把旧键名里可能残留的、和 canonical 不一致的值**覆盖掉**，
      所以"两个键打架"这种状态从写入这一侧就不可能产生。

    Args:
        data: ``users.json`` 反序列化出来的 dict（**原地改**）。
        version: 要盖的版本号。

    Returns:
        同一个 dict（方便链式用）。
    """
    for key in VAULT_VERSION_KEYS:
        data[key] = version
    return data
