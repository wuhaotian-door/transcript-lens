"""激活码版本校验 + 本地激活状态落盘（Spec 第 2 章规则表 + 第 10 章 + 红线 7）。

Owner 定死的行为，实现时不许自行发挥：

1. **本地只持久化两个 int**：``active_version`` 和 ``activated_year``。
   用户敲进去的那个字符串**绝不落盘**，连进 :class:`ActivationState` 的资格都没有
   （见 :meth:`ActivationState.to_dict`）；状态本身走 ``core.crypto_store``
   存成 ``activation.enc`` 密文。
2. **校验流程**：split 三段 → 前缀 ``SGMS-`` → **查码表** :data:`_CODE_TABLE`
   → 系统年份 ``> issue_year`` 拒绝（方案 A）→ **严格大于**本地版本才更新生效；
   ``<=`` 当前版本直接拒绝。
3. **方案 A 只拦"新激活"，绝不追杀已激活的老设备**。年度过期只在
   :func:`verify_code` / :func:`try_activate` **这一次动作**里检查；
   :func:`is_import_allowed` **不读系统时间**，只看 ``active_version >= 1``。
   拒绝时状态一字不动（不回滚、不降级）。
4. **生效范围只有"导入按钮"**。没激活 / 版本不够时点导入 → 弹「需要有效激活码」。
   已经导入进内存的数据、已经算出的加权结果、纵向档案 —— **一律照看不锁**。
   本模块因此只对外提供 :func:`import_allowed` / :func:`is_import_allowed`
   两个闸门，没有任何"锁结果页 / 锁画像页 / 锁导出"的接口，红线 7 要求
   「换码只锁导入，不清成绩库」——所以这里连 ``core.archival`` 都没 import。

码表 :data:`_CODE_TABLE` 硬编码在下面的常量里，**不进任何会被 git 追踪的 json
配置文件**。发新码 = 在表里加一行（版本号 + 发售年份）。

本模块**不联网、不调大模型、不写注册表**。它唯一会落盘的东西是
``AppData/Local/SGMS/activation.enc``（密文，两个 int），
走 ``core.crypto_store.save_encrypted`` / ``load_encrypted``。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.crypto_store import default_root, load_encrypted, save_encrypted

__all__ = [
    "ACTIVATION_PREFIX",
    "ACTIVATION_RE",
    "CODE_FAMILY_LETTERS",
    "GATED_FEATURES",
    "INITIAL_ACTIVATION_CODE",
    "INITIAL_ACTIVATION_VERSION",
    "KNOWN_ACTIVATION_CODES",
    "MIN_ACTIVE_VERSION",
    "REASON_EMPTY",
    "REASON_EXPIRED",
    "REASON_MALFORMED",
    "REASON_NOT_ACTIVATED",
    "REASON_NOT_NEWER",
    "REASON_OK",
    "REASON_UNKNOWN",
    "SEGMENT_COUNT",
    "STATE_FIELD",
    "STATE_FILE",
    "UNACTIVATED_VERSION",
    "YEAR_FIELD",
    "ActivationCode",
    "ActivationEngine",
    "ActivationResult",
    "ActivationState",
    "current_year",
    "import_allowed",
    "is_gated",
    "is_import_allowed",
    "load_state",
    "normalize_code",
    "parse_activation_code",
    "require_import",
    "save_state",
    "set_year_override",
    "state_path",
    "try_activate",
    "verify_code",
]

# ---------------------------------------------------------------------------
# 常量（Spec 第 10 章「Owner 已定死、不再讨论的参数」）
# ---------------------------------------------------------------------------

#: 码的固定前缀
ACTIVATION_PREFIX = "SGMS-"

#: split 之后必须是几段
SEGMENT_COUNT = 3

#: 初始激活码与它对应的版本号（Spec 第 10 章）
INITIAL_ACTIVATION_CODE = "SGMS-1ABCD-2026"
INITIAL_ACTIVATION_VERSION = 1

#: ★ **码表**（白名单）：已发行的码 → ``{版本号, 发售年份}``。
#: 硬编码在代码里，**不落任何文件**。发新码 = 在这里加一行，例如::
#:
#:     "SGMS-2ABCD-2027": {"version": 2, "issue_year": 2027},
#:
#: ``issue_year`` 是年度过期（方案 A）的依据：**当前年份严格大于 issue_year**
#: 时，这个码不再能拿来激活。
_CODE_TABLE: dict[str, dict[str, int]] = {
    INITIAL_ACTIVATION_CODE: {
        "version": INITIAL_ACTIVATION_VERSION,
        "issue_year": 2026,
    },
}

#: 兼容视图：码 → 版本号。:data:`_CODE_TABLE` 才是真源，这个只是方便别处查版本。
KNOWN_ACTIVATION_CODES: dict[str, int] = {
    code: entry["version"] for code, entry in _CODE_TABLE.items()
}

#: 已发行的"家族字母段"。**只用来把错误提示写得更准，不再作为放行依据** ——
#: 放行一律查 :data:`_CODE_TABLE`。不这么做的话，``SGMS-1ABCD-2099`` 这种
#: 自己把年份段改掉的码会被当成"家族成员"放行，年度过期就白设了。
#: 说清楚：这**不是密码学防线**（仓库是公开 MIT 的），它只防手滑输错 + 防止
#: 任意字符串蒙混过关；真正的信息防线是红线 11（真实成绩/真实码不入库）。
CODE_FAMILY_LETTERS: frozenset[str] = frozenset({"ABCD"})

#: 本地状态里的字段 —— **就这两个**，没有第三个。
STATE_FIELD = "active_version"
YEAR_FIELD = "activated_year"

#: 本地激活状态的文件名（应用级密文，走 ``crypto_store.save_encrypted``）
STATE_FILE = "activation.enc"

#: 全新安装 / 从未激活
UNACTIVATED_VERSION = 0

#: 低于这个版本就不放行导入
MIN_ACTIVE_VERSION = INITIAL_ACTIVATION_VERSION

#: 被激活码闸门管控的功能 —— **只有导入**（第三刀）。
GATED_FEATURES: tuple[str, ...] = ("import",)

#: 拒绝原因
REASON_OK = "ok"
REASON_EMPTY = "empty"
REASON_MALFORMED = "malformed"
REASON_UNKNOWN = "unknown"
REASON_NOT_NEWER = "not_newer"
REASON_NOT_ACTIVATED = "not_activated"
REASON_EXPIRED = "expired"

#: 中间段 = 前导数字（版本号） + 尾随字母（家族）
ACTIVATION_RE = re.compile(r"^(\d{1,3})([A-Z]{1,8})$")

#: 各种"看起来像横杠"的字符，统一成 ASCII 的 ``-``
_DASHES = {
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2212": "-",  # minus sign
    "\uff0d": "-",  # fullwidth hyphen-minus（NFKC 一般已处理，双保险）
    "\u3000": " ",  # ideographic space
}


# ---------------------------------------------------------------------------
# 规范化 + 解析
# ---------------------------------------------------------------------------


def normalize_code(text: Any) -> str:
    """规范化用户输入：全角转半角、去空白、统一横杠、转大写。

    ``ＳＧＭＳ－１ＡＢＣＤ－２０２６`` / `` sgms-1abcd-2026 `` / ``SGMS—1ABCD—2026``
    都能被认出来（防的是"手滑输错"，不是攻击）。
    """
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    value = "".join(_DASHES.get(ch, ch) for ch in value)
    value = re.sub(r"\s+", "", value)
    return value.upper()


@dataclass(frozen=True)
class ActivationCode:
    """一个**格式合法**的激活码的解析结果。

    ``raw`` 只是为了当场比对，本对象**不会被持久化**。
    """

    raw: str
    version: int
    letters: str
    year: str
    #: 发售年份。在码表里 = 表里写的；不在表里 = 码面上的年份段（反正会被拒）。
    issue_year: int = 0

    @property
    def fingerprint(self) -> str:
        """码的短哈希。要留痕就留这个，别留明文。"""
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()[:8]

    @property
    def known(self) -> bool:
        """是否在已发行的码表里。"""
        return self.raw in _CODE_TABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "letters": self.letters,
            "year": self.year,
            "issue_year": self.issue_year,
            "fingerprint": self.fingerprint,
            "known": self.known,
        }


def parse_activation_code(text: Any) -> ActivationCode | None:
    """按"split 三段"解析激活码。格式不对返回 ``None``。

    三段 = ``SGMS`` / ``<版本号><家族字母>`` / ``<4 位年份>``。

    **只校验格式，不校验是否已发行** —— 已发行的判断在 :func:`verify_code` 里
    （先查 :data:`KNOWN_ACTIVATION_CODES`，再退到 :data:`CODE_FAMILY_LETTERS`）。
    """
    raw = normalize_code(text)
    if not raw:
        return None

    parts = raw.split("-")
    if len(parts) != SEGMENT_COUNT:
        return None

    prefix, body, year = parts
    if f"{prefix}-" != ACTIVATION_PREFIX:
        return None
    if len(year) != 4 or not year.isdigit():
        return None

    matched = ACTIVATION_RE.match(body)
    if matched is None:
        return None

    version = int(matched.group(1))
    if version < 1:
        return None

    letters = matched.group(2)

    # 码表里有的，**一切以码表为准**（版本号和发售年份都从表里取）；
    # 表里写的版本号和码面上的数字对不上 = 表写错了，直接当无效码
    entry = _CODE_TABLE.get(raw)
    if entry is not None:
        if int(entry["version"]) != version:
            return None
        issue_year = int(entry["issue_year"])
    else:
        # 不在码表里：本函数只负责"格式解析"，能不能用由 verify_code 判（一律 unknown）。
        # issue_year 拿码面上的年份段占位，免得下游到处判空。
        issue_year = int(year)

    return ActivationCode(
        raw=raw, version=version, letters=letters, year=year, issue_year=issue_year
    )


# ---------------------------------------------------------------------------
# 系统年份（方案 A 的唯一时间来源）
# ---------------------------------------------------------------------------

#: 仅供测试/演示的年份覆盖。``None`` = 读真实系统时间。
_YEAR_OVERRIDE: int | None = None


def current_year() -> int:
    """当前年份。**方案 A 里唯一读系统时间的地方。**

    单独抽成一个函数，是为了让"把时间拨到 2027 试试"这件事可测、可演示，
    而不是到处散落 ``datetime.now()``，最后只能靠改系统时钟去验证。

    再说一遍：它只服务"本次激活动作"。已激活设备走的是
    :func:`is_import_allowed`，那条路上没有这个函数。
    """
    if _YEAR_OVERRIDE is not None:
        return _YEAR_OVERRIDE
    return datetime.now().year


def set_year_override(year: int | None) -> None:
    """强制系统年份（``None`` = 恢复真实时间）。**只给测试/演示用。**"""
    global _YEAR_OVERRIDE
    _YEAR_OVERRIDE = None if year is None else int(year)


# ---------------------------------------------------------------------------
# 本地状态：两个 int
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivationState:
    """本地激活状态。**整个对象只有两个 int 字段。**

    * ``active_version == 0`` 表示从未激活
    * ``activated_year`` = **哪一年激活成功的**（溯源用，不参与放行判定）

    用户输入的码明文不在这里、不在 :meth:`to_dict` 里，也永远不许被写进任何明文
    json。整份状态走 ``crypto_store.save_encrypted`` 存成 ``activation.enc``。
    """

    active_version: int = UNACTIVATED_VERSION
    #: 激活成功的那一年（0 = 从未激活）
    activated_year: int = 0

    @property
    def activated(self) -> bool:
        return self.active_version >= MIN_ACTIVE_VERSION

    def to_dict(self) -> dict[str, int]:
        """落盘用的载荷 —— 就两个 int，没有第三个。"""
        return {
            STATE_FIELD: int(self.active_version),
            YEAR_FIELD: int(self.activated_year),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ActivationState":
        """从载荷还原。

        看到疑似明文的 ``code`` / ``activation_code`` 字段会**直接报错**：
        说明有人把明文码写进了磁盘状态，这是必须当场炸掉的错误，不是可以忽略的脏数据。
        """
        if not isinstance(data, dict):
            return cls()

        for forbidden in ("code", "activation_code", "raw", "key"):
            if forbidden in data:
                raise ValueError(
                    f"本地激活状态里出现了 {forbidden!r} 字段："
                    "只允许持久化一个 int（active_version），不许存码明文"
                )

        raw = data.get(STATE_FIELD, UNACTIVATED_VERSION)
        try:
            version = int(raw)
        except (TypeError, ValueError):
            return cls()

        raw_year = data.get(YEAR_FIELD, 0)
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            year = 0

        return cls(
            active_version=max(version, UNACTIVATED_VERSION),
            activated_year=max(year, 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            STATE_FIELD: self.active_version,
            YEAR_FIELD: self.activated_year,
            "activated": self.activated,
        }


@dataclass(frozen=True)
class ActivationResult:
    """一次校验/改码的结果。**不含码明文**，只有版本号与短哈希。

    字段语义（别混）：

    * ``version`` —— **这个码自称的**版本号（被拒时也有值，便于提示）
    * ``previous_version`` —— 校验前的本地版本号
    * ``state`` —— **校验之后**的本地状态。被拒时就是原状态（``state.active_version``
      因此可能不等于 ``version``；``ok is True`` 时才等于）
    * ``fingerprint`` —— 码的 sha256 前 8 位，只用来留痕，不是明文
    """

    ok: bool
    reason: str
    message: str
    version: int = UNACTIVATED_VERSION
    previous_version: int = UNACTIVATED_VERSION
    fingerprint: str = ""
    state: ActivationState = field(default_factory=ActivationState)

    @property
    def import_allowed(self) -> bool:
        return import_allowed(self.state)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
            "code_version": self.version,
            "previous_version": self.previous_version,
            "active_version": self.state.active_version,
            "fingerprint": self.fingerprint,
        }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def verify_code(
    code: Any, state: ActivationState | None = None, *, now_year: int | None = None
) -> ActivationResult:
    """校验一个码并算出**如果接受**会变成什么状态。纯函数，不改入参、不落盘。

    ``now_year`` 省略时读 :func:`current_year()`；显式传就用给的那个年份
    （测试/演示"拨到 2027"用，不必真去改系统时钟）。

    判定顺序（别调，第 4 步是方案 A 的年度过期）：

    1. 空 → ``empty``
    2. 格式不对（不是三段 / 前缀不对 / 中间段解析不出 int / 年份不是 4 位）
       → ``malformed``
    3. **不在码表里** → ``unknown``（家族字母只影响提示文案，不影响放行）
    4. **当前年份 > issue_year** → ``expired``（状态原样返回，不追杀已激活设备）
    5. 版本号 ``<=`` 本地版本 → ``not_newer``
    6. 严格大于本地版本 → 接受，新状态 = (这个版本号, 当前年份)
    """
    current = state or ActivationState()

    if not normalize_code(code):
        return ActivationResult(
            ok=False,
            reason=REASON_EMPTY,
            message="请输入激活码",
            previous_version=current.active_version,
            state=current,
        )

    parsed = parse_activation_code(code)
    if parsed is None:
        return ActivationResult(
            ok=False,
            reason=REASON_MALFORMED,
            message="无效码：格式应为 SGMS-<版本号><字母>-<4位年份>，例如 SGMS-1ABCD-2026",
            previous_version=current.active_version,
            state=current,
        )

    # ★ 放行一律查码表。家族字母只用来把话说得更准，**不再当通行证** ——
    #   否则手改年份段（SGMS-1ABCD-2099）就能把下面的年度过期绕过去。
    if not parsed.known:
        message = (
            "无效码：这个码属于已发行家族，但不在码表里（大概抄错了某一位）"
            if parsed.letters in CODE_FAMILY_LETTERS
            else "无效码：这个码不在已发行的码表里"
        )
        return ActivationResult(
            ok=False,
            reason=REASON_UNKNOWN,
            message=message,
            version=parsed.version,
            previous_version=current.active_version,
            fingerprint=parsed.fingerprint,
            state=current,
        )

    today = current_year() if now_year is None else int(now_year)

    # ★ 方案 A：年度过期只拦"这一次激活动作"。
    #   state=current 原样返回 —— 已激活的设备不降级、不清空、不回滚。
    if today > parsed.issue_year:
        return ActivationResult(
            ok=False,
            reason=REASON_EXPIRED,
            message=(
                f"无效或已过期：这个码的发售年份是 {parsed.issue_year}，"
                f"当前年份 {today}，已超出有效期"
            ),
            version=parsed.version,
            previous_version=current.active_version,
            fingerprint=parsed.fingerprint,
            state=current,
        )

    if parsed.version <= current.active_version:
        return ActivationResult(
            ok=False,
            reason=REASON_NOT_NEWER,
            message=(
                f"已是当前或更高版本/无效码："
                f"本地版本 {current.active_version}，这个码是版本 {parsed.version}，无需改码"
            ),
            version=parsed.version,
            previous_version=current.active_version,
            fingerprint=parsed.fingerprint,
            state=current,
        )

    return ActivationResult(
        ok=True,
        reason=REASON_OK,
        message=(
            f"激活成功：版本 {current.active_version} → {parsed.version}，导入功能已解锁"
        ),
        version=parsed.version,
        previous_version=current.active_version,
        fingerprint=parsed.fingerprint,
        state=ActivationState(active_version=parsed.version, activated_year=today),
    )


# ---------------------------------------------------------------------------
# 生效范围：只有导入（第三刀）
# ---------------------------------------------------------------------------


def import_allowed(state: ActivationState | None = None) -> bool:
    """导入按钮是否放行。没激活 / 版本不够 → ``False``。"""
    current = state or ActivationState()
    return current.active_version >= MIN_ACTIVE_VERSION


def require_import(state: ActivationState | None = None) -> ActivationResult:
    """点导入时先问这个。不放行就把 ``message`` 弹给用户。

    注意这里**只问导入**：结果页、画像页、导出、已有内存数据、纵向档案
    全都不经过这个函数，红线 7 要求换码不得影响它们。
    """
    current = state or ActivationState()
    if import_allowed(current):
        return ActivationResult(
            ok=True,
            reason=REASON_OK,
            message="已激活",
            version=current.active_version,
            previous_version=current.active_version,
            state=current,
        )
    return ActivationResult(
        ok=False,
        reason=REASON_NOT_ACTIVATED,
        message="需要有效激活码",
        version=current.active_version,
        previous_version=current.active_version,
        state=current,
    )


def is_gated(feature: str) -> bool:
    """这个功能是否受激活码闸门管控。目前**只有** ``"import"`` 返回 True。"""
    return (feature or "").strip().lower() in GATED_FEATURES


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class ActivationEngine:
    """激活码引擎。状态只在内存里，落盘由调用方（将来 ``crypto_store``）负责。

    典型用法::

        engine = ActivationEngine(ActivationState.from_dict(saved))
        result = engine.activate("SGMS-1ABCD-2026")   # 改码 / 首次激活
        if result.ok:
            persist(engine.to_state_dict())           # 只存 {"active_version": 1}

        if not engine.can_import:                     # 导入按钮的闸门
            show(require_import(engine.state).message)
    """

    def __init__(self, state: ActivationState | None = None) -> None:
        self._state = state or ActivationState()

    # -- 状态 ---------------------------------------------------------------
    @property
    def state(self) -> ActivationState:
        return self._state

    @property
    def active_version(self) -> int:
        return self._state.active_version

    @property
    def activated(self) -> bool:
        return self._state.activated

    @property
    def can_import(self) -> bool:
        return import_allowed(self._state)

    def to_state_dict(self) -> dict[str, int]:
        """交给落盘层的载荷：``{"active_version": N}``，仅此而已。"""
        return self._state.to_dict()

    def load_state_dict(self, data: Any) -> ActivationState:
        self._state = ActivationState.from_dict(data)
        return self._state

    # -- 校验 ---------------------------------------------------------------
    def verify(self, code: Any, *, now_year: int | None = None) -> ActivationResult:
        """只校验，**不改状态**。用于"改码"对话框的即时反馈。"""
        return verify_code(code, self._state, now_year=now_year)

    def activate(self, code: Any, *, now_year: int | None = None) -> ActivationResult:
        """校验并在通过时更新内存状态。返回结果里带新状态。

        绝不触碰成绩库 —— 本模块连 ``core.archival`` 都没 import。
        """
        result = verify_code(code, self._state, now_year=now_year)
        if result.ok:
            self._state = result.state
        return result

    #: 「改码」在语义上就是拿一个更高版本号的码重新激活，走同一条路。
    change = activate

    def require_import(self) -> ActivationResult:
        return require_import(self._state)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ActivationEngine(active_version={self._state.active_version})"


# ---------------------------------------------------------------------------
# 落盘：activation.enc（应用级密文，走 core.crypto_store）
# ---------------------------------------------------------------------------


def state_path(root: str | Path | None = None) -> Path:
    """激活状态文件路径（默认 ``AppData/Local/SGMS/activation.enc``）。"""
    base = Path(root) if root is not None else default_root()
    return base / STATE_FILE


def load_state(root: str | Path | None = None) -> ActivationState:
    """读本地激活状态。

    * 文件不存在 → 从未激活（``active_version=0``），**这是正常状态**
    * 文件解不开 → ``core.crypto_store.StoreCorruptError``

    ★ 损坏时**绝不"就当没激活"糊过去**。静默降级成 0 会让用户以为激活丢了，
    然后跑去重新激活；真正该做的是让人来看一眼那个坏掉的文件。
    """
    return ActivationState.from_dict(load_encrypted(state_path(root), root=root))


def save_state(state: ActivationState, root: str | Path | None = None) -> Path:
    """把状态加密写到 ``activation.enc``。磁盘上只有密文，**不存码明文**。"""
    path = state_path(root)
    save_encrypted(state.to_dict(), path, root=root)
    return path


def try_activate(
    code: Any, root: str | Path | None = None, *, now_year: int | None = None
) -> ActivationResult:
    """UI 上那个「验证」按钮调的就是这个：读状态 → 校验 → **通过了才落盘**。

    没通过时一个字节都不写（已激活设备的文件保持原样）。
    """
    result = verify_code(code, load_state(root), now_year=now_year)
    if result.ok:
        save_state(result.state, root)
    return result


def is_import_allowed(root: str | Path | None = None) -> bool:
    """导入按钮放行吗？``active_version >= 1`` 就放行。

    ★ **这条路径不读系统时间。** 方案 A 的年度过期只作用于"新激活"那一次动作；
    已经激活的机器哪怕系统时间到了 2099，导入照样放行。
    想改这个行为之前，先回去读模块开头第 3 条。
    """
    return import_allowed(load_state(root))
