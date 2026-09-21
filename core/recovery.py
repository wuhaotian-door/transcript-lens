"""密码找回：邮箱验证码 + 恢复码包裹 + 用恢复码改密码。

两件事，别混
============

1. **邮箱验证码**（:func:`issue_email_code` / :func:`check_email_code`）
   用来在**注册时**确认"这个邮箱真的是你的"。它是个短时效的一次性凭据
   （6 位数字、10 分钟、最多试 5 次），**和成绩库没有任何关系** ——
   它只证明邮箱可达。

2. **恢复码**（:func:`enable_recovery` / :func:`reset_password_recovery`）
   这才是真正的第二把钥匙：注册成功时生成一串高熵随机码，
   本地存一份"用恢复码包起来的库密钥"，同时把码寄到邮箱。
   忘密码时凭它解出库密钥、设新密码。

★ 为什么恢复码必须**注册时就生成**
----------------------------------

库密钥只在内存里、只被密码包裹。忘密码那天它已经解不开了 ——
没有任何东西可以拿去"发"给用户。所以顺序只能是：

    注册成功（库密钥还在手里）
      → 生成恢复码 S
      → 本地存 Fernet(PBKDF2(S)).encrypt(库密钥)
      → 把 S 寄到邮箱
    忘密码时
      → 用户从邮箱翻出 S → 粘回来 → 解出库密钥 → 设新密码

★ 恢复码包裹存在哪、为什么不更安全一点
--------------------------------------

存在 ``u_<uid>/recovery.json.enc``，用**应用级**密钥加密。

为什么不用户密码派生？因为**忘密码时恰恰没有密码** —— 那种加密方式下
这份包裹根本读不出来（盐和校验值都要先读到才能验码），等于没存。

所以这份包裹的真实保护来自**恢复码本身的熵**：
31 个字符的字母表取 24 位 ≈ 118 bit，全宇宙的算力也撞不出来。
文件那层加密只负责"改不动"（Fernet 带 HMAC，改一个字节就 InvalidKey），
不负责"看不见" —— 这点必须说清楚，不能假装它更安全。

★ 恢复码字母表故意去掉 0/O、1/I/L：老师手抄或从邮件里复制粘贴时不会认错。

本模块不联网（发信在 :mod:`core.email_recovery`）、不调大模型。
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from core.crypto_store import (
    CryptoError,
    CryptoStore,
    UserRecord,
    UserSession,
    default_root,
    derive_material,
    load_encrypted,
    new_salt,
    save_encrypted,
)

__all__ = [
    "EMAIL_CODE_LENGTH",
    "EMAIL_CODE_MAX_ATTEMPTS",
    "EMAIL_CODE_TTL_MINUTES",
    "RECOVERY_CODE_LENGTH",
    "RESEND_COOLDOWN_SECONDS",
    "EmailCodeError",
    "RecoveryError",
    "check_email_code",
    "check_recovery_code",
    "enable_recovery",
    "generate_recovery_code",
    "issue_email_code",
    "load_recovery_email",
    "normalize_email",
    "normalize_recovery_code",
    "recovery_info",
    "reset_password_recovery",
]

#: 新包裹用的 PBKDF2 轮数。正常情况下从 ``UserRecord.iterations`` 读，
#: 读不到时（理论上不会）兜底用这个 —— 和 CryptoStore 的默认值保持一致。
PBKDF2_FALLBACK = 600_000


class RecoveryError(CryptoError):
    """找回流程里的问题（码不对、过期、没绑定邮箱……）。"""


class EmailCodeError(RecoveryError):
    """邮箱验证码相关的问题。"""


# ---------------------------------------------------------------------------
# 邮箱验证码
# ---------------------------------------------------------------------------

#: 验证码位数
EMAIL_CODE_LENGTH = 6
#: 有效期（分钟）
EMAIL_CODE_TTL_MINUTES = 10
#: 最多试几次，超了作废（防暴力）
EMAIL_CODE_MAX_ATTEMPTS = 5
#: 两次发送之间的冷却（秒）。防手抖连点把邮箱打爆，也防被人当短信轰炸机用
RESEND_COOLDOWN_SECONDS = 60

#: 验证码状态文件（应用级密文，放在数据根下）
EMAIL_CODE_FILE = "email_code.json.enc"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def normalize_email(text: Any) -> str:
    """归一化邮箱：去空格、转小写。**格式不对就抛**，不静默接受。"""
    value = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    if not _EMAIL_RE.match(value):
        raise EmailCodeError(f"邮箱格式不对：{text!r}")
    return value


def _code_path(root: str | Path | None) -> Path:
    base = Path(root) if root is not None else default_root()
    return base / EMAIL_CODE_FILE


def _now() -> datetime:
    return datetime.now()


def issue_email_code(
    email: str,
    *,
    purpose: str = "注册",
    root: str | Path | None = None,
    now: datetime | None = None,
) -> str:
    """生成一个验证码并**记到本地**（加密），返回码本身。

    调用方负责把返回的码交给 :func:`core.email_recovery.send_verification_mail`
    发出去 —— 本模块不碰网络，这样测试时可以不发信。

    Raises:
        EmailCodeError: 邮箱格式不对，或者距上次发送不到冷却时间。
    """
    target = normalize_email(email)
    issued = now or _now()

    previous = load_encrypted(_code_path(root), root=root) if _code_path(root).exists() else {}
    last = previous.get("issued") if isinstance(previous, dict) else None
    if last:
        try:
            gap = (issued - datetime.fromisoformat(str(last))).total_seconds()
        except (TypeError, ValueError):
            gap = RESEND_COOLDOWN_SECONDS
        if 0 <= gap < RESEND_COOLDOWN_SECONDS:
            raise EmailCodeError(
                f"刚发过，请等 {int(RESEND_COOLDOWN_SECONDS - gap)} 秒再试。"
            )

    code = "".join(secrets.choice("0123456789") for _ in range(EMAIL_CODE_LENGTH))
    payload = {
        "email": target,
        "code": code,
        "purpose": str(purpose),
        "issued": issued.isoformat(timespec="seconds"),
        "expires": (issued + timedelta(minutes=EMAIL_CODE_TTL_MINUTES)).isoformat(
            timespec="seconds"
        ),
        "attempts": 0,
    }
    save_encrypted(payload, _code_path(root), root=root)
    return code


def check_email_code(
    email: str,
    code: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> None:
    """校验验证码。**不对就抛异常**，对了就把它作废（一次性）。

    Raises:
        EmailCodeError: 没发过 / 过期 / 不对 / 试太多次。
    """
    target = normalize_email(email)
    path = _code_path(root)
    if not path.exists():
        raise EmailCodeError("还没有给这个邮箱发过验证码，请先点「发送验证码」。")

    try:
        payload = load_encrypted(path, root=root)
    except CryptoError as exc:
        raise EmailCodeError("验证码记录坏了，请重新发送。") from exc

    if str(payload.get("email", "")) != target:
        raise EmailCodeError("这个验证码不是发给这个邮箱的，请重新发送。")

    current = now or _now()
    try:
        expires = datetime.fromisoformat(str(payload.get("expires", "")))
    except (TypeError, ValueError) as exc:
        raise EmailCodeError("验证码记录坏了，请重新发送。") from exc
    if expires <= current:
        path.unlink(missing_ok=True)
        raise EmailCodeError(
            f"验证码已过期（{EMAIL_CODE_TTL_MINUTES} 分钟内有效），请重新发送。"
        )

    attempts = int(payload.get("attempts", 0))
    if attempts >= EMAIL_CODE_MAX_ATTEMPTS:
        # ★ 这里**故意不删文件**：删了的话下一次调用会报"还没有给这个邮箱发过
        #   验证码"，把"试太多次作废"这个真实原因盖掉，用户看不懂。
        #   保留计数就能一直给出准确的原因；重发会直接覆盖成新记录。
        raise EmailCodeError(
            f"错误次数太多（{EMAIL_CODE_MAX_ATTEMPTS} 次），这个验证码已作废，"
            "请重新发送。"
        )

    entered = unicodedata.normalize("NFKC", str(code or "")).strip()
    if not hmac.compare_digest(entered, str(payload.get("code", ""))):
        # 失败次数要落盘，否则重启程序就能无限试
        payload["attempts"] = attempts + 1
        save_encrypted(payload, path, root=root)
        left = EMAIL_CODE_MAX_ATTEMPTS - payload["attempts"]
        raise EmailCodeError(f"验证码不对，还可以试 {max(left, 0)} 次。")

    # 一次性：验过就删，防止同一个码反复用
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 恢复码
# ---------------------------------------------------------------------------

#: 恢复码字母表：**故意去掉 0/O、1/I/L**，手抄或复制时不会认错
RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
#: 分组数与每组位数 → 4×6 = 24 位 ≈ 118 bit
RECOVERY_GROUP_SIZE = 4
RECOVERY_GROUPS = 6
RECOVERY_CODE_LENGTH = RECOVERY_GROUP_SIZE * RECOVERY_GROUPS

#: 恢复码包裹的文件名（放在该用户目录下）
RECOVERY_FILE = "recovery.json.enc"


def generate_recovery_code() -> str:
    """生成一串形如 ``A7K2-9PQR-M3XY-...`` 的恢复码。"""
    chars = [secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH)]
    return "-".join(
        "".join(chars[i : i + RECOVERY_GROUP_SIZE])
        for i in range(0, len(chars), RECOVERY_GROUP_SIZE)
    )


def normalize_recovery_code(text: Any) -> str:
    """归一化用户粘进来的恢复码：全角转半角、转大写、只留字母表内的字符。

    这样 ``a7k2 9pqr``、``A7K2-9PQR``、``Ａ７Ｋ２－９ＰＱＲ``（中文输入法
    默认全角）都会被认成同一个码。NFKC 那一步不能省 —— 少了它，用户明明
    看着自己粘得一模一样，程序却说"格式不对"。
    """
    if text is None:
        return ""
    raw = unicodedata.normalize("NFKC", str(text)).upper()
    return "".join(ch for ch in raw if ch in RECOVERY_ALPHABET)


def _looks_like_code(text: str) -> bool:
    return len(text) == RECOVERY_CODE_LENGTH


def recovery_path(store: CryptoStore, username: str) -> Path | None:
    """这个用户的恢复码包裹路径。账户不存在 → ``None``。"""
    record = store.load_users().find(username)
    if record is None:
        return None
    return store.user_dir(record.uid) / RECOVERY_FILE


def _load_blob(store: CryptoStore, username: str) -> dict[str, Any] | None:
    path = recovery_path(store, username)
    if path is None or not path.exists():
        return None
    try:
        payload = load_encrypted(path, root=store.root)
    except CryptoError:
        return None
    return payload if isinstance(payload, dict) else None


def load_recovery_email(store: CryptoStore, username: str) -> str:
    """这个用户绑定的找回邮箱。没绑 → 空串。"""
    blob = _load_blob(store, username)
    return str(blob.get("email", "")) if blob else ""


def recovery_info(store: CryptoStore, username: str) -> dict[str, Any]:
    """给界面看的只读快照（**不含任何密文材料**）。"""
    blob = _load_blob(store, username)
    if not blob:
        return {"enabled": False, "email": "", "created_at": ""}
    return {
        "enabled": True,
        "email": str(blob.get("email", "")),
        "created_at": str(blob.get("created_at", "")),
    }


def enable_recovery(session: UserSession, email: str, *, store: CryptoStore) -> str:
    """生成（或重新生成）恢复码，**返回那串码**。

    调用方必须立刻把它交给 :func:`core.email_recovery.send_recovery_mail`
    寄出去 —— 本地**不留明文**，这次不寄就永远拿不到了。

    ★ 旧码会被直接覆盖作废（同一个文件只有一个包裹）。

    Raises:
        RecoveryError: 库密钥为空（说明 session 不是登录得来的）。
    """
    if not session.key:
        raise RecoveryError("这个会话没有库密钥，无法生成恢复码（请重新登录）")

    target = normalize_email(email)
    code = generate_recovery_code()
    canonical = normalize_recovery_code(code)
    salt = new_salt()
    wrap_key, verifier = derive_material(canonical, salt, PBKDF2_FALLBACK)
    payload = {
        "email": target,
        "salt": base64.b64encode(salt).decode("ascii"),
        "verifier": verifier.decode("ascii"),
        "wrapped_key": Fernet(wrap_key).encrypt(session.key).decode("ascii"),
        "iterations": PBKDF2_FALLBACK,
        "created_at": _now().isoformat(timespec="seconds"),
    }
    path = recovery_path(store, session.username)
    if path is None:  # pragma: no cover - session 是从 users.json 来的，不该发生
        raise RecoveryError(f"本机没有账户「{session.username}」")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_encrypted(payload, path, root=store.root)
    return code


def check_recovery_code(store: CryptoStore, username: str, code: str) -> bool:
    """这个恢复码对不对。**只校验，不改任何东西。**"""
    blob = _load_blob(store, username)
    if not blob:
        return False
    normalized = normalize_recovery_code(code)
    if not _looks_like_code(normalized):
        return False
    _, verifier = derive_material(
        normalized, base64.b64decode(blob["salt"]), int(blob.get("iterations", PBKDF2_FALLBACK))
    )
    return hmac.compare_digest(verifier.decode("ascii"), str(blob.get("verifier", "")))


def reset_password_recovery(
    store: CryptoStore, username: str, code: str, new_password: str
) -> UserSession:
    """**忘密码时的完整恢复流程**：验恢复码 → 解出库密钥 → 换新密码。

    返回可以直接用的 :class:`UserSession`（库密钥已经解出来了）。
    **成绩一条都不会丢** —— 换的只是"包裹库密钥的那层壳"。

    ★ 恢复码本身**不作废**：老师可能只留着那一封邮件，
      重置完还能再用一次。想换新的就在个人中心"重新生成"。

    Raises:
        RecoveryError: 没绑定邮箱 / 恢复码格式不对 / 码不对。
    """
    record = store.load_users().find(username)
    if record is None:
        raise RecoveryError(f"本机没有账户「{(username or '').strip()}」")

    blob = _load_blob(store, username)
    if not blob:
        raise RecoveryError(
            f"「{record.username}」没有可用的找回码。\n"
            "找回码是注册时寄到邮箱的那一封；没有它，密码忘了就打不开了。"
        )

    normalized = normalize_recovery_code(code)
    if not _looks_like_code(normalized):
        raise RecoveryError(
            f"找回码格式不对：应该是 {RECOVERY_CODE_LENGTH} 位"
            "（字母表里没有 0/O 和 1/I/L，横杠可以带也可以不带）"
        )
    if not check_recovery_code(store, username, normalized):
        raise RecoveryError("找回码不对，请核对邮件里那一串。")
    if not new_password:
        raise RecoveryError("新密码不能为空")

    iterations = int(blob.get("iterations", PBKDF2_FALLBACK))
    try:
        key = Fernet(
            derive_material(normalized, base64.b64decode(blob["salt"]), iterations)[0]
        ).decrypt(str(blob["wrapped_key"]).encode("ascii"))
    except (InvalidToken, KeyError, ValueError) as exc:
        raise RecoveryError("找回码记录被改过，解不开库密钥。") from exc

    # 用**公开 API** 重新包裹库密钥 —— 和 CryptoStore._set_password 做的事一样，
    # 但这里不需要旧密码（我们本来就是因为忘了它才走这条路）。
    salt = new_salt()
    wrap_key, verifier = derive_material(new_password, salt, record.iterations)
    updated: UserRecord = dataclasses.replace(
        record,
        salt=base64.b64encode(salt).decode("ascii"),
        verifier=verifier.decode("ascii"),
        wrapped_key=Fernet(wrap_key).encrypt(key).decode("ascii"),
        iterations=record.iterations,
    )
    store.save_users(store.load_users().with_user(updated))
    return UserSession(record=updated, key=key)
