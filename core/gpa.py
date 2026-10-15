"""GPA 换算（**方案 1 · 标准 4.0**）—— v1.9.1 新增。

这个模块是**纯函数**（除了文末"方案选择的落盘"那一节）：

* 不 import Qt（``pytest`` 直接可用，不需要 ``QT_QPA_PLATFORM=offscreen``）
* 不 import 任何联网库
* ★ **不 import :mod:`core.calculator`** —— 那是铁律一封板的文件（AGENTS §2 /
  §27f）。GPA 只吃"课程名 / 学分 / 成绩"这种**已经算好、已经在库里**的结构，
  自己一行加权都不重算（铁律五：加权算术的真源只有那一个）。
* ★ :func:`load_scheme` / :func:`save_scheme` 是唯一的 I/O（读写
  ``gpa_settings.json.enc``，应用级加密），这里只存一个方案 id。

★ 空 ≠ 0（红线 2）
------------------

* **没有成绩**（``None`` / 空串 / ``-`` / ``/`` / ``未选``）＝ 这门课**没进分母**，
  连学分都不算 —— 不按 0 分惩罚学生。
* **数字 ``0``** ＝ 缺考：**学分进分母**，绩点按 0 算。
* 一门课**学分为空**时无法承担权重，同样不进分母（数学上等价于权重 0）。
* 一份**全都没有成绩**的名单 → :func:`gpa_of` 返回 ``None``（"暂无数据"），
  **不是** ``0.0``。

★ 四个方案里只实现了方案 1
--------------------------

换算表是模块级常量 :data:`STANDARD_4_TABLE`，方案清单是 :data:`SCHEMES`。
方案 2 / 3 / 4 是**预留位**（``available=False``）—— 界面上列出来但禁用、
注明"待定"；:func:`scheme_options` 就是给那个下拉框吃的。
把某个方案填成 ``available=True`` 之前，**先在这里把换算表写清楚**。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    "BLANK_TOKENS",
    "DEFAULT_SCHEME",
    "FULL_SCORE",
    "GPA_SETTINGS_FILE",
    "GPA_SETTINGS_KEY",
    "PASS_SCORE",
    "SCHEMES",
    "SCHEME_STANDARD_4",
    "STANDARD_4_TABLE",
    "available_schemes",
    "credit_of",
    "grade_point",
    "gpa_detail",
    "gpa_of",
    "is_available",
    "load_scheme",
    "save_scheme",
    "scheme_label",
    "scheme_options",
    "score_of",
]

# ---------------------------------------------------------------------------
# 方案 1：标准 4.0
# ---------------------------------------------------------------------------

#: 方案 1 的稳定标识（存进应用级设置的就是它）
SCHEME_STANDARD_4 = "standard4"

#: 标准 4.0 换算表：``(最低分（含）, 绩点)``，**从高到低**排。
#: 查表 = 从头扫，第一个 ``score >= 最低分`` 就是结果；分数带小数时按这个边界判
#: （89.5 落在 85 档 → 3.7）。
STANDARD_4_TABLE: tuple[tuple[float, float], ...] = (
    (90.0, 4.0),
    (85.0, 3.7),
    (82.0, 3.3),
    (78.0, 3.0),
    (75.0, 2.7),
    (72.0, 2.3),
    (68.0, 2.0),
    (64.0, 1.5),
    (60.0, 1.0),
    (0.0, 0.0),
)

#: 默认方案（界面下拉框的初始选项）
DEFAULT_SCHEME = SCHEME_STANDARD_4

#: 满分 / 及格线（只用于说明文案与边界说明，不参与计算）
FULL_SCORE = 100.0
PASS_SCORE = 60.0

#: 这些写法都表示"这门课没选 / 没考 / 没录" —— **空 ≠ 0**，一律不进分母
BLANK_TOKENS: frozenset[str] = frozenset({"", "-", "/", "—", "–", "未选", "无", "未修"})

#: 四个方案的清单（**界面下拉框的唯一真源**）。
#:
#: ★ ``label`` / ``note`` 会原样显示在界面上 —— 不许出现 Markdown 记号
#:   （AGENTS §24：界面文案里的 ``**`` 会原样显示成两个星号）。
SCHEMES: tuple[dict[str, Any], ...] = (
    {
        "id": SCHEME_STANDARD_4,
        "label": "方案 1 · 标准 4.0",
        "note": (
            "≥90→4.0，85-89→3.7，82-84→3.3，78-81→3.0，75-77→2.7，"
            "72-74→2.3，68-71→2.0，64-67→1.5，60-63→1.0，<60→0；"
            "GPA = Σ(绩点 × 学分) ÷ Σ学分。没有成绩的课不进分母。"
        ),
        "available": True,
    },
    {
        "id": "scheme2",
        "label": "方案 2（待定）",
        "note": "换算表尚未确定，暂不可选。",
        "available": False,
    },
    {
        "id": "scheme3",
        "label": "方案 3（待定）",
        "note": "换算表尚未确定，暂不可选。",
        "available": False,
    },
    {
        "id": "scheme4",
        "label": "方案 4（待定）",
        "note": "换算表尚未确定，暂不可选。",
        "available": False,
    },
)

#: 字段别名：库里 / 解析器里叫法不完全一样，这里一次性收口
_SCORE_KEYS = ("score", "成绩", "分数")
_CREDIT_KEYS = ("credit", "学分")
_NAME_KEYS = ("course_name", "课程名", "course", "name")


# ---------------------------------------------------------------------------
# 方案清单（给界面用）
# ---------------------------------------------------------------------------


def scheme_options() -> list[dict[str, Any]]:
    """方案清单的**副本**（改它不会影响 :data:`SCHEMES`）。"""
    return [dict(item) for item in SCHEMES]


def available_schemes() -> list[str]:
    """已实现的方案 id（目前只有方案 1）。"""
    return [str(item["id"]) for item in SCHEMES if item.get("available")]


def is_available(scheme: Any) -> bool:
    """这个方案实现了没有（未知 id / None → ``False``，**不抛**）。"""
    return _scheme_of(scheme) is not None


def scheme_label(scheme: Any) -> str:
    """方案的中文名；**还没实现的也有名字**（"方案 2（待定）"），完全不认识的 id 原样返回。

    ★ 查的是整张 :data:`SCHEMES`（含 ``available=False`` 的）：下拉框里那个禁用的
      选项、以及用户以前存过、现在被停用的方案，界面上都要有名字可显示 ——
      显示成 ``scheme2`` 只有写代码的人看得懂。
    """
    key = DEFAULT_SCHEME if scheme is None else str(scheme)
    for item in SCHEMES:
        if str(item["id"]) == key:
            return str(item["label"])
    return "" if scheme is None else key


def _scheme_of(scheme: Any) -> dict[str, Any] | None:
    key = DEFAULT_SCHEME if scheme is None else str(scheme)
    for item in SCHEMES:
        if str(item["id"]) == key and item.get("available"):
            return item
    return None


def _require_scheme(scheme: Any) -> dict[str, Any]:
    item = _scheme_of(scheme)
    if item is None:
        raise ValueError(
            f"未实现的 GPA 方案：{scheme!r}；已实现的只有 {available_schemes()}"
        )
    return item


# ---------------------------------------------------------------------------
# 取值（空 ≠ 0）
# ---------------------------------------------------------------------------


def _field(item: Any, keys: tuple[str, ...]) -> Any:
    """从 dict / 对象里取一个字段。"""
    if isinstance(item, Mapping):
        for key in keys:
            if key in item:
                return item[key]
        return None
    for key in keys:
        if hasattr(item, key):
            return getattr(item, key)
    return None


def score_of(value: Any) -> float | None:
    """成绩 → ``float``；"没有成绩" → ``None``（**绝不返回 0**）。

    ``0`` 是**有效的**成绩（缺考）—— 只有 ``None`` / 空串 / ``-`` / ``/`` /
    ``未选`` 这类"没选这门课"的写法才返回 ``None``；解析不出数字的字符串
    也按"没有成绩"处理（宁可不算，也不替学生记 0 分）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in BLANK_TOKENS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def credit_of(value: Any) -> float | None:
    """学分 → ``float``；空 / 不是数字 → ``None``（进不了分母）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if text in BLANK_TOKENS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if number > 0 else None


# ---------------------------------------------------------------------------
# 换算
# ---------------------------------------------------------------------------


def grade_point(score: Any, scheme: Any = DEFAULT_SCHEME) -> float | None:
    """单门课的绩点。

    * 没有成绩（空 / ``-`` / ``未选``…）→ ``None``（**不是 0**）
    * ``0`` 分（缺考）→ ``0.0``
    * 分数大于 100（``core.calculator`` 那边会把它挤出加权的历史遗留，AGENTS §25b）
      在这里按"≥90"给 4.0 —— 区间外的分数在界面层已经由强制确认弹窗拦过一道。
    """
    _require_scheme(scheme)
    value = score_of(score)
    if value is None:
        return None
    for floor, point in STANDARD_4_TABLE:
        if value >= floor:
            return point
    return 0.0                      # 负分这种脏数据按 0 算，不给负绩点


def gpa_detail(courses: Iterable[Any], scheme: Any = DEFAULT_SCHEME) -> dict[str, Any]:
    """把一门门课汇总成明细（界面要显示"算了几门、几门没成绩"时用它）。

    Args:
        courses: 一串课程。每项可以是 ``dict``，也可以是带同名属性的对象；
            认得的字段名：``score`` / ``成绩`` / ``分数``、``credit`` / ``学分``、
            ``course_name`` / ``课程名``。
        scheme: 方案 id，缺省方案 1。

    Returns:
        ``{"scheme", "gpa", "points", "credits", "counted", "skipped"}``

        * ``gpa``：``Σ(绩点×学分) / Σ学分``；**一门都没进分母时是 ``None``**（空 ≠ 0）
        * ``points`` / ``credits``：分子 / 分母
        * ``counted``：进分母的课数；``skipped``：因为"没有成绩"或"没有学分"略过的课数
    """
    item = _require_scheme(scheme)

    points = 0.0
    credits = 0.0
    counted = 0
    skipped = 0

    for course in courses or ():
        if course is None:
            skipped += 1
            continue
        credit = credit_of(_field(course, _CREDIT_KEYS))
        point = grade_point(_field(course, _SCORE_KEYS), item["id"])
        if credit is None or point is None:
            skipped += 1
            continue
        points += point * credit
        credits += credit
        counted += 1

    gpa = (points / credits) if credits > 0 else None
    return {
        "scheme": str(item["id"]),
        "gpa": gpa,
        "points": points,
        "credits": credits,
        "counted": counted,
        "skipped": skipped,
    }


def gpa_of(courses: Iterable[Any], scheme: Any = DEFAULT_SCHEME) -> float | None:
    """``Σ(绩点 × 学分) / Σ学分``；**没有可算的课就返回 ``None``**。

    ``None`` 和 ``0.0`` 是两件事：前者是"还没有数据"，后者是"平均绩点真的是 0"
    —— 界面必须分开显示（红线 2 的同一条道理）。
    """
    return gpa_detail(courses, scheme)["gpa"]


# ---------------------------------------------------------------------------
# 方案选择的落盘（**这一节有 I/O，上面全是纯函数**）
# ---------------------------------------------------------------------------
#
# ★ 走**应用级**加密（``core.user_store``，不需要密码）—— 自动登录时也能读回来，
#   和班级快照 / 归档 / AI 设置同一层（AGENTS §16）。
# ★ 这里只存一个方案 id（"standard4" 这种），**不含任何成绩数据**。

#: 应用级设置的文件名（**必须以 .enc 结尾**）
GPA_SETTINGS_FILE = "gpa_settings.json.enc"
#: 载荷里存方案 id 的键
GPA_SETTINGS_KEY = "scheme"


def load_scheme(store: Any = None, username: str = "") -> str:
    """读回用户上次选的方案 id。

    没存过 / 读不出来 / 存的值已经不认识了（比如方案被删掉）**一律回默认方案**
    —— 界面下拉框永远有一个合法选中项。
    """
    from core.crypto_store import CryptoStore
    from core.user_store import load_user_json

    if not username:
        return DEFAULT_SCHEME
    vault = store if store is not None else CryptoStore()
    payload = load_user_json(vault, username, GPA_SETTINGS_FILE, None)
    if not isinstance(payload, Mapping):
        return DEFAULT_SCHEME
    stored = payload.get(GPA_SETTINGS_KEY)
    return str(stored) if is_available(stored) else DEFAULT_SCHEME


def save_scheme(store: Any = None, username: str = "", scheme: Any = DEFAULT_SCHEME) -> str:
    """把方案 id 存进应用级设置，返回真正存下去的值。

    Raises:
        ValueError: 方案没实现（"待定"的那三个不许被选中存盘）。
        CryptoError: 本机没有这个账户（``core.user_store`` 抛的）。
    """
    from core.crypto_store import CryptoStore
    from core.user_store import load_user_json, save_user_json

    item = _require_scheme(scheme)
    if not username:
        raise ValueError("还没登录，GPA 设置没地方存")
    vault = store if store is not None else CryptoStore()
    payload = load_user_json(vault, username, GPA_SETTINGS_FILE, None)
    body = dict(payload) if isinstance(payload, Mapping) else {}
    body[GPA_SETTINGS_KEY] = str(item["id"])
    save_user_json(vault, username, GPA_SETTINGS_FILE, body)
    return str(item["id"])
