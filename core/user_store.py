"""本用户私有 JSON 载荷的读写（**一律应用级加密**）。

这个模块只做一件事：把"某个用户名下的某个 json 文件"安全地读出来/写回去。
:mod:`ui.intentions` 是第一个用它的地方，班级名单、德育分、名额配置、
AI 设置都走同一套。

★ 为什么是**应用级**加密而不是密码级
----------------------------------

加密用的是 ``crypto_store.save_encrypted`` / ``load_encrypted`` ——
密钥来自 AppData 里的应用级 salt，**不需要用户密码**。

理由是这些数据要在**自动登录**（没有密码）时也能读：
自动登录只带一个 session token，库密钥是解不开的。意愿、班级名单这类
"辅导员自己录进去的辅助数据"如果按密码级加密，自动登录进来就是空的。
真正敏感的成绩仍然走密码级（``store.save(session, ...)``）。

> 这条边界很重要：**成绩进密码级，辅助数据进应用级**。
> 别把成绩往这里塞。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from core.crypto_store import CryptoError, CryptoStore, load_encrypted, save_encrypted

__all__ = [
    "STAMP_KEY",
    "delete_user_json",
    "list_user_json",
    "load_user_json",
    "save_user_json",
    "user_file",
]

#: 每次写盘都会盖一个时间戳，方便排查"这份配置是什么时候改的"
STAMP_KEY = "updated"


def user_file(store: CryptoStore, username: str, filename: str) -> Path | None:
    """这个用户名下某个载荷的完整路径。**账户不存在时返回 ``None``**（不抛）。"""
    record = store.load_users().find(username)
    if record is None:
        return None
    return store.user_dir(record.uid) / filename


def load_user_json(
    store: CryptoStore,
    username: str,
    filename: str,
    default: Any = None,
) -> Any:
    """读回一份 JSON 载荷。

    读不出来（没登录 / 文件没有 / 解密失败 / 内容坏了）**一律返回 ``default``**，
    不抛异常 —— 这些都是便利数据，坏掉不该拦住用户看成绩。
    真实原因由调用方按需自己再读一次。
    """
    path = user_file(store, username, filename)
    if path is None or not path.exists():
        return default
    try:
        return load_encrypted(path, root=store.root)
    except (CryptoError, OSError, ValueError):
        return default


def save_user_json(
    store: CryptoStore,
    username: str,
    filename: str,
    payload: dict[str, Any],
) -> Path:
    """把一份 JSON 载荷加密落盘，返回路径。

    Raises:
        CryptoError: 账户不存在，或写盘失败。
        ValueError: 载荷不是 dict。
    """
    if not isinstance(payload, dict):
        raise ValueError("载荷必须是 dict")
    path = user_file(store, username, filename)
    if path is None:
        raise CryptoError(f"本机没有账户「{username}」，数据没地方存")
    body = dict(payload)
    body[STAMP_KEY] = datetime.now().isoformat(timespec="seconds")
    save_encrypted(body, path, root=store.root)
    return path


def delete_user_json(store: CryptoStore, username: str, filename: str) -> bool:
    """删掉一份载荷。删掉了返回 True，本来就没有返回 False。"""
    path = user_file(store, username, filename)
    if path is None or not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def list_user_json(store: CryptoStore, username: str, suffix: str = ".json.enc") -> list[str]:
    """列出这个用户目录下所有以 ``suffix`` 结尾的文件名（不含路径）。

    班级列表就是靠它扫出来的 —— 不需要额外维护一份"有哪些班"的索引，
    也就不会出现"索引和文件对不上"的不一致。
    """
    record = store.load_users().find(username)
    if record is None:
        return []
    folder = store.user_dir(record.uid)
    if not folder.is_dir():
        return []
    return sorted(
        p.name for p in folder.iterdir() if p.is_file() and p.name.endswith(suffix)
    )
