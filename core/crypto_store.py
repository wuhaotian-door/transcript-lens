"""本地加密存储：PBKDF2 派生 + 密钥包裹 + Fernet 加密落盘（Spec 第 5 章 + 红线 7、11）。

落盘布局（**与 exe 物理分离**，打包时不可能夹带用户数据）::

    C:\\Users\\<系统用户>\\AppData\\Local\\SGMS\\
    ├─ users.json                 ← uid + 用户名 + salt + 校验值 + **被包裹的库密钥**
    └─ u_<uid 前 8 位>\\
       ├─ grades.enc              ← 加密后的成绩库（纵向档案）
       ├─ tags.json.enc           ← 意愿标签（加密）
       └─ sessions.json.enc       ← 学期导入记录（加密）

上面那套是**按用户**的：解密要先有 ``UserSession``。但有一类数据在**任何用户登录之前**
就要能读 —— 最典型的是「这台机器激活到哪个版本了」。所以本模块额外提供一对
**应用级**函数，只认路径、不认用户：

    app.salt                      ← 应用级随机盐（明文，盐本来就不是秘密）
    activation.enc                ← 激活状态（加密，只有 {"active_version", "activated_year"}）

.. code-block:: python

    save_encrypted({"active_version": 1}, path)   # 磁盘上只出现密文
    load_encrypted(path)                          # 文件不存在 → {}；解不开 → 抛异常

**必须说清楚的威胁模型**：应用级这层的密码材料是代码里的常量，盐就在旁边那个文件里，
所以**能读到源码 + 拿到盐文件的人可以自己把密钥推出来**。它挡的不是这种攻击，它挡的是：

* 用记事本打开 ``activation.enc`` 把 ``active_version`` 改成 99 —— **改不动**
  （Fernet 带 HMAC，改一个字节就 ``InvalidToken``）
* 文件被截断/半截写入 —— 解不开，而不是解出一个看似合法的半截状态

换成真正的"本机绑定"要上 Windows DPAPI（``CryptProtectData``），那是后续阶段的活。

密钥结构：**库密钥不随密码变**
--------------------------------

这是本模块最关键的一个设计决定。

* 注册时随机生成一把 **库密钥**（library key），载荷全部用它加密。
* 库密钥本身**不落盘**，而是被"包裹"（wrap）成密文：``wrapped_key``
  用 ``PBKDF2(密码)`` 包起来，登录时解开。
* 于是**改密码只需要重新包裹一次**，几十毫秒，不用把整个成绩库重新加密一遍。

  反面教材（第一版就是这么写的，写完才发现）：让库密钥直接由密码 PBKDF2 派生。
  那样改密码就等于换库密钥 → 必须把所有载荷重新加密，而且**改到一半失败就是
  半个库加密、半个库没加密**，没有事务能兜住。

* 代价（必须说清楚）：改密码**不会**更换库密钥。它的作用是
  「**旧密码再也登不进来**」，而不是「把已经泄漏的库密钥作废」。
  对"辅导员自己电脑上的单机工具"这个场景，这个取舍是对的。

密码忘了怎么办（**本阶段：没有办法**）
--------------------------------------

`2026-09` 阶段按产品决定**删除了恢复码**，并且**尚未**接入邮箱找回。所以现在：

* 本地不存密码、不存库密钥明文、也不存任何能反推出它们的东西。
* **忘了密码 = 库密钥永远解不开 = 成绩数据永久打不开。** 没有后门，没有救兵。
  界面必须明确告诉用户这一点（登录页与注册页各有一处提示）。

> 预留：邮箱找回的**接口**在 :mod:`core.email_recovery`，本阶段只有壳、不发信。
> 注意它**不是**"服务器帮你重置密码" —— 库密钥只在本地，任何外部服务都拿不到它。
> 真要能找回，恢复材料必须在**注册时**（库密钥还在手里的时候）就生成并寄出。

**升级兼容**：老版本写过的 ``users.json`` 里可能还留着 ``"recovery"`` 键。
:meth:`UserRecord.from_dict` 用 ``data.get()`` 读，所以那种文件**照常能加载**，
那个键会被静默忽略、不进任何数据结构；等到下一次写盘（注册新用户 / 改密码 /
改名）就自然消失了。**它不会被读、也不会被用** —— 但请注意它仍在文件里躺着，
所以"删掉恢复码功能"并不等于"抹掉历史文件里的那份包裹"。

三条不許违反的规矩
------------------

1. **磁盘上永远只有密文。** 成绩数据在内存里是 dict → ``json.dumps`` → Fernet →
   一次性写字节。**没有"先写明文 JSON 再加密"的中间态**，连临时文件都不会出现明文。
   ``tests`` 里有一条测试会把写出来的文件按字节扫一遍，确认找不到已知明文。

2. **解不开就报错，绝不返回空。** 密码错 / 文件被改坏时，``load`` 抛异常。
   如果这里"优雅降级"成返回 ``{}``，用户看到的就是"我的成绩库空了"，
   然后很可能重新导入一次把真数据覆盖掉 —— 那才是真正的数据丢失。

3. **密码忘了就是打不开了。** 本地不存密码、不存库密钥明文、也不存任何能反推出
   它们的东西。本阶段**不存在任何恢复路径**，界面必须明确告诉用户这一点。

本模块**不联网、不调大模型、不写注册表**。
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core.version import (
    CURRENT_VERSION,
    LEGACY_VAULT_VERSION_KEY,
    VAULT_VERSION_KEY,
    VAULT_VERSION_KEYS,
    stamp_vault_version,
)

__all__ = [
    "APP_DIR_NAME",
    "ENC_SUFFIX",
    "CryptoError",
    "CryptoStore",
    "FILE_GRADES",
    "FILE_SESSIONS",
    "FILE_TAGS",
    "PAYLOAD_FILES",
    "PBKDF2_ITERATIONS",
    "SALT_BYTES",
    "SALT_FILE",
    "StoreCorruptError",
    "UnknownUserError",
    "USER_DIR_PREFIX",
    "USERS_FILE",
    "UserExistsError",
    "UserIndex",
    "UserRecord",
    "UserSession",
    "WrongPasswordError",
    "app_fernet",
    "app_salt_path",
    "default_root",
    "derive_material",
    "hash_verifier",
    "load_app_salt",
    "load_encrypted",
    "new_salt",
    "save_encrypted",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 数据根目录名（Spec 第 5 章：AppData/Local/SGMS/）
APP_DIR_NAME = "SGMS"

#: users.json 的文件名
USERS_FILE = "users.json"

#: 每个用户的目录前缀（Spec：u_<uid 前 8 位>）
USER_DIR_PREFIX = "u_"

#: PBKDF2-HMAC-SHA256 迭代次数。OWASP 当前对 PBKDF2-SHA256 的下限是 600,000；
#: 实测本机约 263 ms/次 —— 登录时感知不到，值得。
PBKDF2_ITERATIONS = 600_000

#: 每个用户独立的随机盐长度
SALT_BYTES = 16

#: PBKDF2 输出长度：前 32 字节当**包裹密钥**，后 32 字节当密码校验值。
#: 拆成两半是为了"能校验密码"和"能包裹"用两份独立的材料 —— 不给任何一方反推另一方。
_DERIVED_BYTES = 64

#: 允许读写的载荷文件名（白名单）。不在表里的名字一律拒绝，
#: 免得界面上一旦拼错或被人塞进 ``..\\..\\`` 就写到别处去了。
FILE_GRADES = "grades.enc"
FILE_TAGS = "tags.json.enc"
FILE_SESSIONS = "sessions.json.enc"
PAYLOAD_FILES: tuple[str, ...] = (FILE_GRADES, FILE_TAGS, FILE_SESSIONS)

#: 索引文件的结构版本，将来改格式时用来迁移
USERS_VERSION = 1

#: **应用级**（不属于任何用户）的东西：盐文件 + 只允许的密文后缀
SALT_FILE = "app.salt"

#: 应用级载荷只许用这个后缀。"磁盘上永远只有 .enc" 不是口号，是这里挡下来的：
#: 谁手滑把 ``activation.json`` 传进来会直接 ValueError，而不是写出一个明文谜题。
ENC_SUFFIX = ".enc"

#: 应用级 PBKDF2 的密码材料。**它不是秘密**（仓库是公开的），
#: 公开它是为了让"盐文件 + 源码 = 密钥"这件事一眼可算，而不是假装它安全。
#: 它真正提供的是**完整性**：Fernet 的 HMAC 让本地文件改不动。
_APP_KDF_PASSPHRASE = b"transcript-lens/app-level-store/v1"


def default_root() -> Path:
    """默认数据根：``C:\\Users\\<系统用户>\\AppData\\Local\\SGMS``。

    只返回路径，**不创建**。真正建目录发生在第一次写入时。
    """
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 应用级密钥：单独一个盐文件，不进仓库
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, blob: bytes) -> None:
    """先写同目录下的临时文件，再 ``os.replace`` 原子替换。

    临时文件里放的也是**密文**，所以磁盘上任何时刻都不会出现明文成绩。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def app_salt_path(root: str | Path | None = None) -> Path:
    """应用级盐文件的路径（默认在 AppData 下，**不进仓库**）。"""
    base = Path(root) if root is not None else default_root()
    return base / SALT_FILE


def load_app_salt(root: str | Path | None = None, *, create: bool = True) -> bytes:
    """读应用级盐；没有就生成一份并落盘。

    盐是**明文**存的 —— 盐按定义就是公开的，它的作用是让"同一密码在两台机器上
    派生出不同密钥"，不是保密。

    ★ 删掉这个文件 = 所有应用级 ``.enc`` 再也解不开（会派生出另一把密钥）。
    这是设计如此，不是 bug：所以它在 AppData 里，和密文放一起，一起备份。
    """
    path = app_salt_path(root)
    if path.exists():
        try:
            raw = base64.b64decode(path.read_bytes().strip(), validate=True)
        except (ValueError, OSError) as exc:
            raise StoreCorruptError(
                f"盐文件 {path} 读不出来或不是合法 base64：{exc}。"
                "它被改坏之后，同目录下所有应用级 .enc 都解不开了。"
            ) from exc
        if len(raw) < 8:
            raise StoreCorruptError(f"盐文件 {path} 太短（{len(raw)} 字节），不像本程序写的")
        return raw

    if not create:
        return b""
    salt = new_salt()
    _atomic_write_bytes(path, base64.b64encode(salt))
    return salt


def app_fernet(root: str | Path | None = None) -> Fernet:
    """应用级 Fernet（用于激活状态这类"还没有用户"的数据）。"""
    salt = load_app_salt(root)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(_APP_KDF_PASSPHRASE)))



# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class CryptoError(Exception):
    """本模块所有异常的基类。"""


class UserExistsError(CryptoError):
    """用户名已被占用。"""


class UnknownUserError(CryptoError):
    """没有这个用户名。"""


class WrongPasswordError(CryptoError):
    """密码不对。"""


class StoreCorruptError(CryptoError):
    """文件坏了 / 被改过 / 不是本程序写的。

    和"密码错"是两码事：能走到解密这一步，说明密码校验已经先过了，
    所以解不开只能是文件本身出了问题。分开报是为了给出准确的处置建议。
    """


# ---------------------------------------------------------------------------
# 密钥派生
# ---------------------------------------------------------------------------


def new_salt(nbytes: int = SALT_BYTES) -> bytes:
    """每个用户一份的随机盐。"""
    return os.urandom(nbytes)


def derive_material(
    password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS
) -> tuple[bytes, bytes]:
    """由密码派生出 ``(包裹密钥, 密码校验值)`` 两份材料。

    ``PBKDF2(password, salt, iterations, dklen=64)``，前 32 字节当**包裹密钥**
    （ursafe base64 后就是一个合法的 Fernet 密钥，用来包裹库密钥），
    后 32 字节当校验值（base64）。

    注意：这里派出来的**不是数据密钥**，而是一把"能解开库密钥的钥匙"。
    库密钥本身随机生成、不随密码变 —— 见模块开头的说明。
    """
    if not isinstance(password, str):
        raise TypeError("密码必须是 str")
    if not salt:
        raise ValueError("盐不能为空")
    if iterations < 1:
        raise ValueError("迭代次数必须 >= 1")

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_DERIVED_BYTES,
        salt=salt,
        iterations=iterations,
    )
    derived = kdf.derive(password.encode("utf-8"))

    wrap_key = base64.urlsafe_b64encode(derived[:32])
    verifier = base64.b64encode(derived[32:])
    return wrap_key, verifier


def hash_verifier(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    """只要校验值（注册时用）。"""
    _, verifier = derive_material(password, salt, iterations)
    return verifier.decode("ascii")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRecord:
    """一个用户的公开信息。**没有密码，也没有库密钥明文。**

    ``wrapped_key`` 是用 ``PBKDF2(密码)`` 包起来的库密钥密文。
    它是库密钥**唯一**的一份包裹 —— 本阶段没有任何第二种解开方式。
    """

    uid: str
    username: str
    salt: str
    verifier: str
    #: 库密钥被 KDF(密码) 包裹后的密文
    wrapped_key: str = ""
    iterations: int = PBKDF2_ITERATIONS
    created_at: str = ""

    @property
    def short_id(self) -> str:
        """目录名用的前 8 位（Spec：``u_<uid 前 8 位>``）。"""
        return self.uid.replace("-", "")[:8]

    @property
    def salt_bytes(self) -> bytes:
        return base64.b64decode(self.salt.encode("ascii"))

    def to_dict(self) -> dict[str, Any]:
        # 注意：这里**只写** uid/用户名/salt/校验值/被包裹的密钥。
        # 旧版本还会写一个 "recovery" 键（恢复码那份包裹）；已随恢复码一并删除。
        # from_dict 用 data.get() 读，所以带 recovery 的老文件仍然能正常加载。
        return {
            "uid": self.uid,
            "username": self.username,
            "salt": self.salt,
            "verifier": self.verifier,
            "wrapped_key": self.wrapped_key,
            "iterations": self.iterations,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserRecord":
        return cls(
            uid=str(data.get("uid", "")),
            username=str(data.get("username", "")),
            salt=str(data.get("salt", "")),
            verifier=str(data.get("verifier", "")),
            wrapped_key=str(data.get("wrapped_key", "")),
            iterations=int(data.get("iterations", PBKDF2_ITERATIONS)),
            created_at=str(data.get("created_at", "")),
        )


@dataclass(frozen=True)
class UserIndex:
    """``users.json`` 的内容。有盐、校验值和被包裹的库密钥，**没有任何明文密码**。

    ``version`` 和 ``vault_version`` 是**两件事**，别合并：

    ==================  ==========  ============================================
    字段                 类型        含义
    ==================  ==========  ============================================
    ``version``          ``int``     **这个文件自己的结构版本**（``USERS_VERSION``）
    ``vault_version``    ``str``     **数据版本**，如 ``"1.7"``（charter 8.1）
    ==================  ==========  ============================================

    数据版本空串 = 老库（v1.6 及以前没写过这个标记）→ 迁移那边按 ``1.0`` 处理。
    """

    users: tuple[UserRecord, ...] = ()
    version: int = USERS_VERSION
    #: 数据版本标记，写进 JSON 时叫 ``__vault_version__``（charter 8.1）
    vault_version: str = ""

    def __len__(self) -> int:
        return len(self.users)

    def __iter__(self):
        return iter(self.users)

    def find(self, username: str) -> UserRecord | None:
        """按用户名找。大小写不敏感、去首尾空格。"""
        wanted = (username or "").strip().casefold()
        for record in self.users:
            if record.username.casefold() == wanted:
                return record
        return None

    def by_uid(self, uid: str) -> UserRecord | None:
        for record in self.users:
            if record.uid == uid:
                return record
        return None

    def with_user(self, record: UserRecord) -> "UserIndex":
        """返回**新**索引（不改自己）。同 uid 覆盖，同名不同 uid 追加。"""
        kept = tuple(u for u in self.users if u.uid != record.uid)
        return replace(self, users=kept + (record,))

    def without_user(self, uid: str) -> "UserIndex":
        return replace(self, users=tuple(u for u in self.users if u.uid != uid))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "users": [u.to_dict() for u in self.users],
        }
        if self.vault_version:
            # ★ 两个键名都写（值相同）—— 见 core.version.VAULT_VERSION_KEYS。
            #   写盘走同一个助手函数，保证"两个键打架"不可能发生。
            stamp_vault_version(data, self.vault_version)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "UserIndex":
        if not isinstance(data, dict):
            raise StoreCorruptError("users.json 的结构不对（顶层不是对象）")
        raw = data.get("users", [])
        if not isinstance(raw, list):
            raise StoreCorruptError("users.json 的 users 字段不是数组")
        # 兼容读：charter 8.1 的键优先，任务书 2.1 那个旧键名也认
        vault_version = ""
        for key in VAULT_VERSION_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                vault_version = value.strip()
                break
        return cls(
            users=tuple(UserRecord.from_dict(item) for item in raw if isinstance(item, dict)),
            version=int(data.get("version", USERS_VERSION)),
            vault_version=vault_version,
        )


@dataclass(frozen=True)
class UserSession:
    """登录成功后拿到的东西：身份 + **库密钥**。

    ``key`` 用 ``field(repr=False)``：**任何日志、异常、调试打印都不该带出密钥**。
    """

    record: UserRecord
    key: bytes = field(repr=False, default=b"")

    @property
    def uid(self) -> str:
        return self.record.uid

    @property
    def username(self) -> str:
        return self.record.username

    def fernet(self) -> Fernet:
        return Fernet(self.key)

    def __repr__(self) -> str:  # pragma: no cover - 防御性
        return f"UserSession(username={self.record.username!r}, key=<hidden>)"


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class CryptoStore:
    """本地加密存储。

    典型用法::

        store = CryptoStore()                 # 默认 AppData/Local/SGMS
        session = store.register("张老师", "密码")   # 或 store.login(...)
        store.save(session, FILE_GRADES, archive.as_dict())
        data = store.load(session, FILE_GRADES)     # 解不开会抛异常，不返回 {}
    """

    def __init__(self, root: str | Path | None = None,
                 iterations: int = PBKDF2_ITERATIONS) -> None:
        self._root = Path(root) if root is not None else default_root()
        self._iterations = iterations

    # -- 路径 ---------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def users_path(self) -> Path:
        return self._root / USERS_FILE

    def user_dir(self, uid: str) -> Path:
        return self._root / f"{USER_DIR_PREFIX}{uid.replace('-', '')[:8]}"

    def payload_path(self, uid: str, filename: str) -> Path:
        self._check_filename(filename)
        return self.user_dir(uid) / filename

    @staticmethod
    def _check_filename(filename: str) -> None:
        if filename not in PAYLOAD_FILES:
            raise ValueError(
                f"不接受这个载荷文件名：{filename!r}；只允许 {list(PAYLOAD_FILES)}"
            )

    # -- 用户索引 -----------------------------------------------------------
    def load_users(self) -> UserIndex:
        """读 ``users.json``。文件不存在 → 空索引（首次运行，正常）。

        文件存在但坏了 → 抛 :class:`StoreCorruptError`。
        **绝不静默重置** —— 重置会让所有已加密的成绩库变成永远打不开的孤儿。
        """
        path = self.users_path
        if not path.exists():
            return UserIndex()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreCorruptError(
                f"{path} 读不出来或不是合法 JSON：{exc}。"
                "这个文件里只有用户名、校验值和被包裹的密钥；如果确实损坏，"
                "删掉它只影响「能不能登录」，u_* 目录里的加密成绩库仍在。"
            ) from exc
        return UserIndex.from_dict(data)

    def save_users(self, index: UserIndex) -> Path:
        """写 ``users.json``。

        这个文件本身**不是密文**（Spec 第 5 章就是这样设计的），所以额外加一道自检：
        写之前递归确认结构里真的没有密码字段。

        注意检查的是**字段名**而不是整段文本 —— 否则一个叫 "password" 的用户名
        就会被自己误伤（踩过这种"检查写太宽"的坑）。
        """
        payload = index.to_dict()
        offenders = sorted(_find_forbidden_keys(payload))
        if offenders:
            raise CryptoError(
                f"拒绝写入 users.json：出现了 {offenders} 字段 —— "
                "这个文件只允许存 uid / 用户名 / salt / 校验值 / 被包裹的密钥密文"
            )
        blob = json.dumps(payload, ensure_ascii=False, indent=2)
        self._atomic_write_text(self.users_path, blob)
        return self.users_path

    # -- 注册 / 登录 --------------------------------------------------------
    def init_vault(self, index: UserIndex | None = None) -> UserIndex:
        """给加密库盖**数据版本**标记（charter v1.7 8.1）。返回（可能是新的）索引。

        ★ **只在没有标记时盖**，绝不覆盖已有的标记：
          库是 v1.6、程序是 v1.7 的时候，直接盖成 1.7 会让
          "检测到旧版数据 → 提示迁移"这一步**永远发现不了旧数据**，
          版本保护就白做了。**升级标记是** :func:`core.migrator.migrate_store`
          的事，不是这里的。
        """
        current = self.load_users() if index is None else index
        if current.vault_version:
            return current
        return replace(current, vault_version=CURRENT_VERSION)

    def register(self, username: str, password: str) -> UserSession:
        """建一个新用户，返回可直接用的 session（顺带省掉两次派生）。

        库密钥是随机生成的，只被 ``PBKDF2(密码)`` 包裹这一次。**没有第二种
        解开方式** —— 密码忘了，库里所有东西就再也打不开了。调用方（注册页）
        有责任把这句话明确告诉用户。

        ★ 建第一个用户 = **初始化这个加密库**，所以顺手盖数据版本标记
          （见 :meth:`init_vault`）。
        """
        name = (username or "").strip()
        if not name:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("密码不能为空")

        index = self.load_users()
        if index.find(name) is not None:
            raise UserExistsError(f"用户名已存在：{name}")

        salt = new_salt()
        wrap_key, verifier = derive_material(password, salt, self._iterations)

        # 库密钥：随机，不随密码变（改密码时只需重新包裹一次，不用重新加密整个库）
        library_key = Fernet.generate_key()
        wrapped_key = Fernet(wrap_key).encrypt(library_key).decode("ascii")

        record = UserRecord(
            uid=self._fresh_uid(index),
            username=name,
            salt=base64.b64encode(salt).decode("ascii"),
            verifier=verifier.decode("ascii"),
            wrapped_key=wrapped_key,
            iterations=self._iterations,
            created_at=_now(),
        )
        self.save_users(self.init_vault(index.with_user(record)))
        self.user_dir(record.uid).mkdir(parents=True, exist_ok=True)
        return UserSession(record=record, key=library_key)

    def _fresh_uid(self, index: UserIndex) -> str:
        """生成一个短目录名不撞车的 uid。

        目录名只取 uid 前 8 位十六进制（Spec 第 5 章）。这是 32 位空间，
        两个用户撞名的概率极低但**不是零**；一旦撞上，两个人会**静默共用**
        同一个 u_xxxxxxxx 目录，彼此覆盖对方的成绩库。所以这里显式避让。
        """
        taken = {self.user_dir(u.uid) for u in index.users}
        for _ in range(100):
            uid = str(uuid.uuid4())
            if self.user_dir(uid) not in taken:
                return uid
        raise CryptoError("连续 100 次生成的 uid 都撞上了已有目录名，这不正常")

    def login(self, username: str, password: str) -> UserSession:
        """校验密码并解出库密钥。"""
        index = self.load_users()
        record = index.find(username)
        if record is None:
            raise UnknownUserError(f"没有这个用户名：{(username or '').strip()}")
        if not self.check_password(record, password):
            raise WrongPasswordError("密码不对")
        return UserSession(record=record, key=self._unwrap_with_password(record, password))

    @staticmethod
    def check_password(record: UserRecord, password: str) -> bool:
        """常量时间比较，避免时序侧信道。"""
        if not isinstance(password, str) or not password:
            return False
        _, verifier = derive_material(password, record.salt_bytes, record.iterations)
        return hmac.compare_digest(verifier.decode("ascii"), record.verifier)

    def _unwrap_with_password(self, record: UserRecord, password: str) -> bytes:
        wrap_key, _ = derive_material(password, record.salt_bytes, record.iterations)
        if not record.wrapped_key:
            raise StoreCorruptError(
                f"{record.username} 的记录里没有 wrapped_key，无法解出库密钥。"
            )
        try:
            return Fernet(wrap_key).decrypt(record.wrapped_key.encode("ascii"))
        except InvalidToken as exc:
            raise StoreCorruptError(
                f"{record.username} 的库密钥解不开（users.json 里的 wrapped_key 被改过）"
            ) from exc

    # -- 改密码 / 改名 ------------------------------------------------------
    def change_password(
        self, session: UserSession, old_password: str, new_password: str
    ) -> UserSession:
        """改密码 = **只重新包裹一次库密钥**。

        因为库密钥不随密码变，所以不用把整个成绩库重新加密 —— 快，而且不存在
        "改一半失败、半个库加密半个库没加密"的风险。

        它的作用是「旧密码再也登不进来」。它**不会**作废已经泄漏出去的库密钥 ——
        这个取舍在模块开头写清楚了。
        """
        if not self.check_password(session.record, old_password):
            raise WrongPasswordError("原密码不对")
        return self._set_password(session, new_password)

    def _set_password(self, session: UserSession, new_password: str) -> UserSession:
        if not new_password:
            raise ValueError("新密码不能为空")
        salt = new_salt()
        wrap_key, verifier = derive_material(new_password, salt, self._iterations)
        record = replace(
            session.record,
            salt=base64.b64encode(salt).decode("ascii"),
            verifier=verifier.decode("ascii"),
            wrapped_key=Fernet(wrap_key).encrypt(session.key).decode("ascii"),
            iterations=self._iterations,
        )
        self.save_users(self.load_users().with_user(record))
        return UserSession(record=record, key=session.key)

    def rename_user(self, session: UserSession, new_username: str) -> UserSession:
        """改用户名（不动密钥、不动数据）。"""
        name = (new_username or "").strip()
        if not name:
            raise ValueError("用户名不能为空")
        index = self.load_users()
        existing = index.find(name)
        if existing is not None and existing.uid != session.uid:
            raise UserExistsError(f"用户名已存在：{name}")
        record = replace(session.record, username=name)
        self.save_users(index.with_user(record))
        return UserSession(record=record, key=session.key)

    # -- 载荷读写 -----------------------------------------------------------
    def has(self, uid: str, filename: str) -> bool:
        return self.payload_path(uid, filename).exists()

    def save(self, session: UserSession, filename: str, payload: dict[str, Any]) -> Path:
        """把 dict 加密后落盘。磁盘上只会出现密文。"""
        self._check_filename(filename)
        if not isinstance(payload, dict):
            raise TypeError("载荷必须是 dict")
        blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        token = session.fernet().encrypt(blob)
        path = self.payload_path(session.uid, filename)
        self._atomic_write_bytes(path, token)
        return path

    def load(self, session: UserSession, filename: str) -> dict[str, Any]:
        """解密读回。

        * 文件不存在 → ``{}``（"还没存过"，正常）
        * 解不开 → :class:`StoreCorruptError`
        * 解出来不是 dict → :class:`StoreCorruptError`

        「文件不存在」和「解不开」必须分开：前者是空库，后者是**数据出事了**，
        绝不能都变成 ``{}``。
        """
        self._check_filename(filename)
        path = self.payload_path(session.uid, filename)
        if not path.exists():
            return {}
        try:
            token = path.read_bytes()
        except OSError as exc:
            raise StoreCorruptError(f"读不了 {path}：{exc}") from exc
        if not token:
            raise StoreCorruptError(f"{path} 是空文件（0 字节），不是正常的加密载荷")

        try:
            blob = session.fernet().decrypt(token)
        except InvalidToken as exc:
            raise StoreCorruptError(
                f"{path} 解不开。密码是对的（能走到这一步说明校验已通过），"
                "所以是文件本身被改过或损坏了。"
            ) from exc
        try:
            data = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreCorruptError(f"{path} 解出来的内容不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise StoreCorruptError(f"{path} 解出来的顶层结构不是对象")
        return data

    # -- 清库（红线 7：只有用户主动要求才做，且必须明说删了什么）----------
    def inventory(self, uid: str) -> list[dict[str, Any]]:
        """列出这个用户当前**实际存在**的载荷文件（路径 + 字节数 + 修改时间）。

        给"清库"做二次确认用：先把要删的东西原样摆在用户面前。
        """
        items: list[dict[str, Any]] = []
        for filename in PAYLOAD_FILES:
            path = self.payload_path(uid, filename)
            if not path.exists():
                continue
            stat = path.stat()
            items.append(
                {
                    "filename": filename,
                    "path": str(path),
                    "bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
        return items

    def delete_payloads(self, uid: str, filenames: Iterable[str] | None = None) -> list[str]:
        """删掉载荷文件，返回**被删掉的路径**。

        ``filenames`` 省略时删全部。调用方负责二次确认；
        这个函数自己**不做任何确认**，也绝不自动调用（红线 7）。
        """
        targets = list(filenames) if filenames is not None else list(PAYLOAD_FILES)
        removed: list[str] = []
        for filename in targets:
            path = self.payload_path(uid, filename)
            if path.exists():
                path.unlink()
                removed.append(str(path))
        return removed

    def delete_user(self, username: str) -> list[str]:
        """删掉一个用户：``users.json`` 里的记录 + 整个 ``u_xxxxxxxx`` 目录。

        返回被删掉的所有路径，供界面原样展示。**不删别的用户、不碰别的东西。**
        """
        index = self.load_users()
        record = index.find(username)
        if record is None:
            raise UnknownUserError(f"没有这个用户名：{(username or '').strip()}")

        removed = self.delete_payloads(record.uid)
        directory = self.user_dir(record.uid)
        if directory.exists():
            # 目录里可能还有不认识的东西，一并如实报出来
            for leftover in sorted(directory.iterdir()):
                if leftover.is_file():
                    leftover.unlink()
                    removed.append(str(leftover))
            directory.rmdir()
            removed.append(str(directory))
        self.save_users(index.without_user(record.uid))
        removed.append(f"{self.users_path}（仅移除该用户的记录，文件本身保留）")
        return removed

    # -- 原子写 -------------------------------------------------------------
    def _atomic_write_bytes(self, path: Path, blob: bytes) -> None:
        """见模块级 :func:`_atomic_write_bytes`（应用级函数共用同一份实现）。"""
        _atomic_write_bytes(path, blob)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        self._atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 应用级载荷读写：save_encrypted / load_encrypted
# ---------------------------------------------------------------------------


def save_encrypted(
    data: dict[str, Any], path: str | Path, *, root: str | Path | None = None
) -> None:
    """把 dict 加密写到 ``path``。**磁盘上只会出现密文。**

    和 :meth:`CryptoStore.save` 的分工：

    ==================  ==========================  ============================
    用哪个              密钥来自                    什么时候用
    ==================  ==========================  ============================
    ``CryptoStore``     ``UserSession``（密码解出） 成绩库、标签、学期记录
    ``save_encrypted``  应用级盐文件（``app.salt``）  **还没有任何用户登录**的数据
    ==================  ==========================  ============================

    激活状态属于右边那一列：它是一台机器的属性，不是某个用户的属性，
    在登录之前就要能读能写。

    Raises:
        TypeError: ``data`` 不是 dict。
        ValueError: ``path`` 不是 ``.enc`` 后缀 —— 这条是故意的，
            「磁盘上永远只有密文」必须由代码挡，不能靠自觉。
    """
    if not isinstance(data, dict):
        raise TypeError("载荷必须是 dict")
    target = Path(path)
    if target.suffix.lower() != ENC_SUFFIX:
        raise ValueError(
            f"拒绝写入 {target.name!r}：应用级载荷只允许 {ENC_SUFFIX} 后缀。"
            "明文 .json 一律不许落盘（Spec 红线 11）"
        )
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = app_fernet(root).encrypt(blob)
    _atomic_write_bytes(target, token)


def load_encrypted(path: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """解密读回。**解不开就抛异常，绝不返回空。**

    * 文件不存在 → ``{}``（"还没写过"，正常状态；激活状态靠它表示"从未激活"）
    * 解不开 / 不是 dict → :class:`StoreCorruptError`

    「不存在」和「解不开」必须分开，理由和 :meth:`CryptoStore.load` 一样：
    把损坏降级成空值，用户看到的就是"我的激活状态没了"，然后可能去重新激活 ——
    真正该做的是报错、让人来看一眼。
    """
    target = Path(path)
    if not target.exists():
        return {}

    # 记下"读之前盐在不在"。盐要是被删了，下面会现生成一份新的，
    # 于是必然解不开 —— 这时给一句准确的话，别让用户去猜。
    salt_existed = app_salt_path(root).exists()

    try:
        token = target.read_bytes()
    except OSError as exc:
        raise StoreCorruptError(f"读不了 {target}：{exc}") from exc
    if not token:
        raise StoreCorruptError(f"{target} 是空文件（0 字节），不是正常的加密载荷")

    try:
        blob = app_fernet(root).decrypt(token)
    except InvalidToken as exc:
        if salt_existed:
            why = "盐文件对不上，或者这个文件被改过/损坏了"
        else:
            why = (
                f"盐文件 {app_salt_path(root)} 不见了（刚重新生成了一份），"
                "用新盐推出来的密钥打不开旧密文"
            )
        raise StoreCorruptError(f"{target} 解不开：{why}") from exc

    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreCorruptError(f"{target} 解出来的内容不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise StoreCorruptError(f"{target} 解出来的顶层结构不是对象")
    return data


# ---------------------------------------------------------------------------
# users.json 的字段自检
# ---------------------------------------------------------------------------

#: 这些词出现在**字段名**里就拒绝写盘
_FORBIDDEN_KEY_PARTS = ("password", "passwd", "secret", "plaintext")

_FORBIDDEN_KEY_RE = re.compile("|".join(_FORBIDDEN_KEY_PARTS), re.IGNORECASE)


def _find_forbidden_keys(payload: Any, prefix: str = "") -> set[str]:
    """递归找出结构里所有"看起来像在存密码"的字段名。

    递归是必要的：字段值可能是嵌套结构（比如未来的邮箱绑定信息），
    只查顶层会漏掉里面。
    """
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _FORBIDDEN_KEY_RE.search(str(key)):
                found.add(path)
            found |= _find_forbidden_keys(value, path)
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found |= _find_forbidden_keys(item, f"{prefix}[{index}]")
    return found
