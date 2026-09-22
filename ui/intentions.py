"""发展意愿的本地加密存储（侧写辅导第二层）。

存什么
------
``{"intentions": {"sid:2023001": "考研", "name:李四": "就业"}, "updated": "<ISO>"}``
—— **学生标识 → 意愿标签**。就这些，不含成绩。

★ 为什么键是「学号」，而不是姓名
--------------------------------

踩过的坑：一开始拿**姓名**当键。同名学生（历届都有）会**互相覆盖** ——
张三 A 选了"考研"，同班的张三 B 打开侧写页，看到的也是"考研"，
而他自己选的"就业"会把 A 的覆盖掉。这是数据串门，不是小瑕疵。

所以键改成 :func:`student_key` 算出来的稳定标识：

* 有学号 → ``sid:<学号>``（学号是 Spec 第 2 章指定的纵向累计钥匙）
* 没学号 → ``name:<姓名>``（退回姓名，**并且调用方要提示同名风险**）

历史数据（裸姓名键）在 :func:`load_intentions` 里**自动迁移**成 ``name:<姓名>``，
不用用户做任何事。

存哪儿
------
``AppData/Local/SGMS/u_<uid 前 8 位>/intentions.json.enc``（**密文**）。

★ 为什么用**应用级**密钥，而不是用户密码派生的库密钥
----------------------------------------------------

这是一个必须说清楚的取舍，因为它是被架构逼出来的：

* 本程序支持「7 天内自动登录」。而 ``core/session_token.py`` 第 14 行写得很死：
  **令牌里不放库密钥**。所以自动登录之后，我们**只有用户名，没有库密钥**。
* 用户密码派生的那把库密钥，只有真正输过密码的那次登录才拿得到
  （要 PBKDF2(password) 解 ``wrapped_key``）。
* 于是：如果意愿存在"用户库"里，那么**自动登录进来的人根本写不进去** ——
  而 Spec 六又要求"下次打开这个学生还在"。两条直接打架。

结论：意愿走应用级加密（``crypto_store.save_encrypted``），
这样**登录与否都能读写**，行为一致。

代价（不能瞒着）
----------------
应用级密钥的材料是代码里的常量 + 同目录的 ``app.salt``，
**能读到源码又拿到盐的人可以自己把密钥推出来**（``crypto_store`` 开头就写着这条）。
所以这个文件的保护强度是「**防顺手翻看，不防有心人**」。

意愿标签（保研/考研/就业）本身不是成绩，泄漏后果远小于成绩单；
真正敏感的成绩仍然在 ``grades.enc`` 里，由密码派生的库密钥保护。
**这是明知代价的选择，不是疏忽。**

本模块不联网、不调大模型。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.crypto_store import (
    CryptoError,
    CryptoStore,
    load_encrypted,
    save_encrypted,
)

__all__ = [
    "INTENTIONS_FILE",
    "INTENTION_OPTIONS",
    "KEY_PREFIX_NAME",
    "KEY_PREFIX_SID",
    "clear_intention",
    "intention_key_label",
    "intentions_path",
    "load_intentions",
    "normalize_key",
    "save_intention",
    "student_key",
]

#: 文件名。放在该用户自己的目录下，只是加密用的是应用级密钥。
INTENTIONS_FILE = "intentions.json.enc"

#: Spec 六·第二层的六个选项，顺序即界面顺序
INTENTION_OPTIONS: tuple[str, ...] = (
    "保研",
    "考研",
    "就业",
    "考公",
    "留学",
    "暂未确定",
)

#: 键前缀：有学号 / 没学号（退回姓名）
KEY_PREFIX_SID = "sid:"
KEY_PREFIX_NAME = "name:"

_KEY = "intentions"


def student_key(student_id: str = "", student: str = "") -> str:
    """算出一个学生的**稳定标识**，用作意愿的键。

    有学号就用学号（``sid:2023001``），没有才退回姓名（``name:张三``）。
    前后空白一律先剥掉 —— 表里常见的不可见空格会让同一个学生算出两个键。
    """
    sid = (student_id or "").strip()
    if sid:
        return f"{KEY_PREFIX_SID}{sid}"
    return f"{KEY_PREFIX_NAME}{(student or '').strip()}"


def normalize_key(raw: str) -> str:
    """把各种写法的键归一到 ``sid:`` / ``name:`` 两种前缀。

    历史文件里存的是**裸姓名**（``"张三"``），这里给它补上 ``name:`` 前缀。
    这样老文件不用重写也能照常读出来。
    """
    text = (raw or "").strip()
    if text.startswith((KEY_PREFIX_SID, KEY_PREFIX_NAME)):
        return text
    return f"{KEY_PREFIX_NAME}{text}"


def intention_key_label(key: str) -> str:
    """把键还原成人看得懂的标签（报错信息／日志用）。"""
    text = normalize_key(key)
    for prefix, word in ((KEY_PREFIX_SID, "学号"), (KEY_PREFIX_NAME, "姓名")):
        if text.startswith(prefix):
            return f"{word} {text[len(prefix):]}"
    return text  # pragma: no cover - normalize_key 之后不可达


def intentions_path(store: CryptoStore, username: str) -> Path | None:
    """算出这个用户的意愿文件路径。**账户不存在时返回 ``None``**（不抛）。"""
    record = store.load_users().find(username)
    if record is None:
        return None
    return store.user_dir(record.uid) / INTENTIONS_FILE


def load_intentions(store: CryptoStore, username: str) -> dict[str, str]:
    """读回 ``{学生键: 意愿}``。

    读不出来一律当"还没有"（返回空字典）：意愿是**便利功能**，
    它坏掉不该拦住用户看成绩。真实原因不吞，交给调用方的日志。
    """
    path = intentions_path(store, username)
    if path is None or not path.exists():
        return {}
    try:
        payload = load_encrypted(path, root=store.root)
    except (CryptoError, OSError, ValueError):
        return {}
    raw = payload.get(_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        normalize_key(name): str(value)
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, str)
    }


def save_intention(
    store: CryptoStore, username: str, key: str, intention: str
) -> Path:
    """记下/更新一个学生的意愿，**立即落盘**。

    Args:
        key: :func:`student_key` 算出来的标识。传裸姓名也能用
            （:func:`normalize_key` 会补上前缀），但**那样就有同名风险**。

    Returns:
        写出去的路径。

    Raises:
        ValueError: 键或用户名为空。
        CryptoError: 账户不存在，或写盘失败。
    """
    student = normalize_key(key)
    if student == KEY_PREFIX_NAME:
        raise ValueError("学生标识不能为空")
    path = intentions_path(store, username)
    if path is None:
        raise CryptoError(f"本机没有账户「{username}」，意愿没地方存")

    data = load_intentions(store, username)
    value = (intention or "").strip()
    if value:
        data[student] = value
    else:
        data.pop(student, None)

    payload = {_KEY: data, "updated": datetime.now().isoformat(timespec="seconds")}
    save_encrypted(payload, path, root=store.root)
    return path


def clear_intention(store: CryptoStore, username: str, key: str) -> Path:
    """抹掉一个学生的意愿（等于把值设成空）。"""
    return save_intention(store, username, key, "")
