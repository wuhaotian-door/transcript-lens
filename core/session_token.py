"""「7 天内自动登录」的会话令牌。

存哪儿
------
``AppData/Local/SGMS/session.enc`` —— 应用级密文（走 ``crypto_store.save_encrypted``），
所以**不需要密码就能解开**。

存什么
------
``{"username": ..., "issued": <ISO>, "exp": <ISO>}`` —— 就这三样。

不存什么
--------
密码、密码的派生材料、库密钥、任何能反推以上三者的东西。**一个都没有。**

★ 必须说清楚的代价

这个文件用**应用级**密钥加密（``app.salt``），谁都能解开 —— 这正是"自动登录"
的定义。所以：**能读到这个文件的人，在有效期内可以免密码进入程序。**

换来的是便利，代价就是上面这一条。因此本模块刻意做了三件事：

1. **有效期写死 7 天**（:data:`AUTO_LOGIN_DAYS`），不做"永久记住我"
2. **令牌里不放任何密钥**，它只能证明"上次登录过这个用户名"
3. **一定要有清除入口**（主界面侧边栏底部的「退出登录」），
   否则用户勾了一次就再也取消不掉 —— 那才是真的坑

它**不是**身份认证。真正的认证是 :mod:`core.crypto_store` 的 PBKDF2 口令校验；
本模块只是"要不要再问一次密码"的开关。

本模块不联网、不调大模型、不碰成绩数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.crypto_store import (
    CryptoError,
    CryptoStore,
    default_root,
    load_encrypted,
    save_encrypted,
)

__all__ = [
    "AUTO_LOGIN_DAYS",
    "TOKEN_FILE",
    "clear_token",
    "load_token",
    "save_token",
    "token_info",
    "token_path",
]

#: 令牌文件名（应用级密文）
TOKEN_FILE = "session.enc"

#: ★ 写死 7 天。Spec 之外的自选参数，改这里就是改产品行为，别在调用方传。
AUTO_LOGIN_DAYS = 7

#: 允许出现在令牌里的字段（白名单）。多一个都不行 ——
#: 万一将来有人往里塞 password，写盘前就会被这里拦下。
_ALLOWED_FIELDS = frozenset({"username", "issued", "exp"})


def token_path(root: str | Path | None = None) -> Path:
    """令牌文件路径（默认 ``AppData/Local/SGMS/session.enc``）。"""
    base = Path(root) if root is not None else default_root()
    return base / TOKEN_FILE


def _now() -> datetime:
    return datetime.now()


def expiry_from(now: datetime | None = None) -> datetime:
    """算出到期时间：``now + 7 天``。"""
    return (now or _now()) + timedelta(days=AUTO_LOGIN_DAYS)


def save_token(
    username: str, *, root: str | Path | None = None, now: datetime | None = None
) -> Path:
    """写下自动登录令牌。**只写用户名和两个时间戳。**

    Raises:
        ValueError: 用户名为空，或者载荷里混进了白名单之外的字段。
        CryptoError: 令牌写入失败；调用方应提示后继续本次密码登录。
    """
    name = (username or "").strip()
    if not name:
        raise ValueError("用户名不能为空，不写空令牌")

    issued = now or _now()
    payload = {
        "username": name,
        "issued": issued.isoformat(timespec="seconds"),
        "exp": expiry_from(issued).isoformat(timespec="seconds"),
    }
    extra = set(payload) - _ALLOWED_FIELDS
    if extra:
        raise ValueError(f"令牌里出现了不该有的字段：{sorted(extra)}")

    try:
        save_encrypted(payload, token_path(root), root=root)
    except OSError as exc:
        raise CryptoError(
            "自动登录凭证未能保存，请检查数据目录写入权限、磁盘空间和文件占用。"
        ) from exc
    return token_path(root)


def _parse_exp(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def token_info(root: str | Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """令牌现在什么状态。**不修改任何东西**，给界面显示用。

    Returns:
        ``{"present", "valid", "username", "issued", "exp", "reason"}``

        ``reason`` 取值：``""`` / ``"missing"`` / ``"expired"`` / ``"corrupt"``
        / ``"unknown_user"`` / ``"bad_payload"``
    """
    path = token_path(root)
    info: dict[str, Any] = {
        "present": False,
        "valid": False,
        "username": "",
        "issued": "",
        "exp": "",
        "reason": "missing",
    }
    try:
        info["present"] = path.exists()
    except OSError:
        info["reason"] = "corrupt"
        return info
    if not info["present"]:
        return info

    try:
        payload = load_encrypted(path, root=root)
    except (CryptoError, OSError):
        # 令牌坏了**不等于**出事：最坏结果就是"这次要输密码"，
        # 没有任何数据会因此丢失，所以这里不抛，只如实说原因。
        info["reason"] = "corrupt"
        return info

    if not payload or not isinstance(payload.get("username"), str):
        info["reason"] = "bad_payload"
        return info

    username = payload["username"].strip()
    info["username"] = username
    info["issued"] = str(payload.get("issued", ""))
    info["exp"] = str(payload.get("exp", ""))

    expires = _parse_exp(payload.get("exp"))
    if expires is None:
        info["reason"] = "bad_payload"
        return info
    if expires <= (now or _now()):
        info["reason"] = "expired"
        return info

    # 用户名在本地账户表里还存不存在？被删掉的账户不该还能免密码进来。
    try:
        if CryptoStore(root).load_users().find(username) is None:
            info["reason"] = "unknown_user"
            return info
    except (CryptoError, OSError):
        info["reason"] = "corrupt"
        return info

    info["valid"] = True
    info["reason"] = ""
    return info


def load_token(root: str | Path | None = None, *, now: datetime | None = None) -> str | None:
    """有效就返回用户名，否则返回 ``None``。

    **过期/损坏一律返回 None，绝不抛异常** —— 这条路是程序启动的第一跳，
    它唯一该做的决定就是"要不要弹登录窗"，把启动流程炸掉没有任何好处。
    真实原因去 :func:`token_info` 里取。
    """
    info = token_info(root, now=now)
    return info["username"] if info["valid"] else None


def clear_token(root: str | Path | None = None) -> bool:
    """删掉令牌（退出登录用）。返回是否真的删掉了东西。"""
    path = token_path(root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CryptoError(f"删不掉自动登录令牌 {path}：{exc}") from exc
