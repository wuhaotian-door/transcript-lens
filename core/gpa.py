"""按用户选择的规则换算 GPA，保留空成绩和缺考零分的区别。"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
import logging
import re
from typing import Any, Iterable, Mapping
from uuid import uuid4

logger = logging.getLogger(__name__)

SCHEME_STANDARD_4 = DEFAULT_SCHEME = "standard4"
STANDARD_4_TABLE = (
    (90, 4.0), (85, 3.7), (82, 3.3), (78, 3.0), (75, 2.7),
    (72, 2.3), (68, 2.0), (64, 1.5), (60, 1.0), (0, 0.0),
)
FULL_SCORE = 100.0
PASS_SCORE = 60.0
BLANK_TOKENS = frozenset({"", "-", "/", "—", "–", "未选", "无", "未修"})
MAX_CUSTOM_SCHEMES = 10
GPA_SETTINGS_FILE = "gpa_settings.json.enc"
GPA_SETTINGS_KEY = "scheme"


def _preset(scheme_id: str, label: str, **definition: Any) -> dict[str, Any]:
    return {
        "id": scheme_id, "label": label, "builtin": True, "available": True,
        "round": 2, "note": "", **definition,
    }


PRESETS = (
    _preset("standard4", "细分 4.0（原方案）", kind="table", rows=STANDARD_4_TABLE),
    _preset("standard4_band", "标准 4.0（四档）", kind="table",
            rows=((90, 4), (80, 3), (70, 2), (60, 1), (0, 0))),
    _preset("standard5_linear", "标准 5.0（线性）", kind="linear",
            a=0.1, b=-5, min_point=0, max_point=5),
    _preset("scale43", "4.3 制", kind="table", rows=((95, 4.3), *STANDARD_4_TABLE)),
    _preset("wes_cn", "WES（中国参考）", kind="table",
            rows=((85, 4), (75, 3), (60, 2), (0, 0))),
    _preset("five_level", "五级制", kind="table",
            rows=((95, 4), (85, 3), (75, 2), (65, 1), (0, 0)),
            grade_map={"优": 95, "优秀": 95, "良": 85, "良好": 85, "中": 75, "中等": 75,
                       "及格": 65, "不及格": 0},
            note="优 95、良 85、中 75、及格 65、不及格 0。"),
)
SCHEMES = PRESETS
_PRESET_BY_ID = {item["id"]: item for item in PRESETS}
_CUSTOM_ID = re.compile(r"custom:[0-9a-f]{6}")


class SchemeError(ValueError):
    """GPA 换算方案无效。"""


def custom_id() -> str:
    return "custom:" + uuid4().hex[:6]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _table_errors(rows: Any) -> list[str]:
    if not isinstance(rows, (list, tuple)) or len(rows) < 2:
        return ["至少需要 2 档（例如 90 和 0）。"]
    errors = []
    prev_floor = prev_point = None
    for index, row in enumerate(rows, 1):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            errors.append(f"第 {index} 行须填写最低分和绩点。")
            prev_floor = prev_point = None
            continue
        floor, point = map(_finite, row)
        if floor is None or not 0 <= floor <= FULL_SCORE or floor != round(floor, 1):
            errors.append(f"第 {index} 行的最低分须在 0 到 100 之间，最多 1 位小数。")
        elif prev_floor is not None and floor >= prev_floor:
            errors.append(f"第 {index} 行的最低分必须小于上一行。")
        if point is None or not 0 <= point <= 5:
            errors.append(f"第 {index} 行的绩点须在 0 到 5 之间。")
        elif prev_point is not None and point > prev_point:
            errors.append(f"第 {index} 行的绩点不能高于上一行。")
        prev_floor, prev_point = floor, point
    if prev_floor != 0:
        errors.append("最后一档的最低分必须是 0。")
    return errors


def validate_scheme(definition: Mapping[str, Any]) -> list[str]:
    """返回可直接显示的方案校验错误。"""
    if not isinstance(definition, Mapping):
        return ["GPA 方案格式无效。"]
    errors = []
    name = definition.get("name", definition.get("label", ""))
    if not isinstance(name, str) or not name.strip():
        errors.append("方案名称不能为空。")
    elif len(name.strip()) > 40:
        errors.append("方案名称不能超过 40 个字符。")
    elif any(mark in name for mark in ("**", "__", "`", "<", ">", "\n", "\r")):
        errors.append("方案名称不能包含 Markdown 或换行符号。")
    precision = definition.get("round", 2)
    if type(precision) is not int or not 0 <= precision <= 6:
        errors.append("显示小数位数须为 0 到 6 之间的整数。")

    kind = definition.get("kind")
    if kind == "table":
        errors.extend(_table_errors(definition.get("rows")))
    elif kind == "linear":
        if any(_finite(definition.get(key)) is None for key in ("a", "b")):
            errors.append("线性换算的系数和截距须为有限数字。")
        lower = _finite(definition.get("min_point"))
        upper = _finite(definition.get("max_point"))
        if lower is None or upper is None or not 0 <= lower <= upper <= 5:
            errors.append("线性换算的绩点上下限须在 0 到 5 之间，且下限不能大于上限。")
    else:
        errors.append("请选择分档或线性换算。")
    grade_map = definition.get("grade_map", {})
    if not isinstance(grade_map, Mapping) or any(
        not isinstance(key, str) or key.strip() != key or key in BLANK_TOKENS
        or _finite(key) is not None or (value := _finite(raw)) is None
        or not 0 <= value <= FULL_SCORE
        for key, raw in grade_map.items()
    ):
        errors.append("等级映射须使用等级文字和 0 到 100 的对应分数。")
    return errors


def _normalize(definition: Mapping[str, Any]) -> dict[str, Any]:
    errors = validate_scheme(definition)
    if errors:
        raise SchemeError(errors[0])
    kind = definition["kind"]
    name = definition.get("name", definition.get("label", "")).strip()
    result = {"id": definition.get("id", ""), "name": name, "kind": kind,
              "round": definition.get("round", 2)}
    if kind == "table":
        result["rows"] = tuple((float(floor), float(point)) for floor, point in definition["rows"])
    else:
        for key in ("a", "b", "min_point", "max_point"):
            result[key] = float(definition[key])
    if definition.get("grade_map"):
        result["grade_map"] = {key: float(value) for key, value in definition["grade_map"].items()}
    return result


def resolve_scheme(
    scheme: Any, custom: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """把方案 ID 或定义解析为可独立使用的方案。"""
    if isinstance(scheme, Mapping):
        return _normalize(scheme)
    key = DEFAULT_SCHEME if scheme is None else scheme
    if isinstance(key, str):
        if key in _PRESET_BY_ID:
            return deepcopy(_PRESET_BY_ID[key])
        for definition in custom:
            if definition.get("id") == key:
                return _normalize(definition)
    raise SchemeError(f"未实现的 GPA 方案：{scheme!r}。")


def scheme_options(custom: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    return [*deepcopy(PRESETS), *(_normalize(item) for item in custom)]


def available_schemes() -> list[str]:
    return list(_PRESET_BY_ID)


def is_available(scheme: Any) -> bool:
    return scheme is None or (isinstance(scheme, str) and scheme in _PRESET_BY_ID)


def scheme_label(scheme: Any, custom: Iterable[Mapping[str, Any]] = ()) -> str:
    try:
        definition = resolve_scheme(scheme, custom)
    except SchemeError:
        return str(scheme)
    return definition.get("label") or definition["name"]


def _field(item: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(item, Mapping):
        return next((item[key] for key in keys if key in item), None)
    return next((getattr(item, key) for key in keys if hasattr(item, key)), None)


def score_of(value: Any, grade_map: Mapping[str, float] | None = None) -> float | None:
    """读取实得成绩，缺失成绩返回 None，实录 0 分保留。"""
    if isinstance(value, str):
        value = value.strip()
        if value in BLANK_TOKENS:
            return None
        if grade_map and value in grade_map:
            return grade_map[value]
    return _finite(value)


def credit_of(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _grade_point(score: Any, definition: Mapping[str, Any]) -> float | None:
    value = score_of(score, definition.get("grade_map"))
    if value is None:
        return None
    if value <= 0:
        return 0.0
    if definition["kind"] == "linear":
        point = definition["a"] * min(value, FULL_SCORE) + definition["b"]
        return min(definition["max_point"], max(definition["min_point"], point))
    return next(point for floor, point in definition["rows"] if value >= floor)


def grade_point(score: Any, scheme: Any = DEFAULT_SCHEME) -> float | None:
    return _grade_point(score, resolve_scheme(scheme))


def gpa_detail(courses: Iterable[Any], scheme: Any = DEFAULT_SCHEME) -> dict[str, Any]:
    """按已选方案换算，以有成绩且学分有效的课程为分母。"""
    definition = resolve_scheme(scheme)
    points = credits = 0.0
    counted = skipped = 0
    for course in courses or ():
        credit = credit_of(_field(course, ("credit", "学分")))
        point = _grade_point(_field(course, ("score", "成绩", "分数")), definition)
        if credit is None or point is None:
            skipped += 1
            continue
        points += point * credit
        credits += credit
        counted += 1
    return {
        "scheme": definition["id"], "gpa": points / credits if credits else None,
        "points": points, "credits": credits, "counted": counted, "skipped": skipped,
    }


def gpa_of(courses: Iterable[Any], scheme: Any = DEFAULT_SCHEME) -> float | None:
    return gpa_detail(courses, scheme)["gpa"]


def _normalize_custom(items: Any, *, strict: bool) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        try:
            scheme_id = item.get("id") if isinstance(item, Mapping) else None
            if not isinstance(scheme_id, str) or not _CUSTOM_ID.fullmatch(scheme_id):
                raise SchemeError("自定义方案的标识不合法。")
            if scheme_id in seen:
                raise SchemeError("自定义方案的标识不能重复。")
            if len(result) >= MAX_CUSTOM_SCHEMES:
                raise SchemeError("自定义方案最多 10 个，请先删除不用的方案。")
            definition = _normalize(item)
        except SchemeError:
            if strict:
                raise
            logger.warning("略过不合法的 GPA 自定义方案")
            continue
        result.append(definition)
        seen.add(scheme_id)
    return result


def _read_settings(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, Mapping) else {}
    items = data.get("custom")
    custom = _normalize_custom(items, strict=False) if isinstance(items, (tuple, list)) else []
    scheme = data.get(GPA_SETTINGS_KEY, DEFAULT_SCHEME)
    if not isinstance(scheme, str) or (
        not is_available(scheme) and all(item["id"] != scheme for item in custom)
    ):
        logger.warning("GPA 设置引用了不可用的方案，使用默认方案")
        scheme = DEFAULT_SCHEME
    return {"version": 1, GPA_SETTINGS_KEY: scheme, "custom": custom}


def load_settings(store: Any = None, username: str = "") -> dict[str, Any]:
    from core.crypto_store import CryptoStore
    from core.user_store import load_user_json

    if not username:
        return _read_settings(None)
    vault = store if store is not None else CryptoStore()
    return _read_settings(load_user_json(vault, username, GPA_SETTINGS_FILE, None))


def save_settings(
    store: Any = None, username: str = "", *, scheme: Any = None, custom: Any = None,
) -> dict[str, Any]:
    from core.crypto_store import CryptoStore
    from core.user_store import load_user_json, save_user_json

    if not username:
        raise SchemeError("请先登录再保存 GPA 设置。")
    vault = store if store is not None else CryptoStore()
    payload = load_user_json(vault, username, GPA_SETTINGS_FILE, None, strict=True)
    current = _read_settings(payload)
    items = current["custom"] if custom is None else _normalize_custom(custom, strict=True)
    if scheme is None:
        selected = current[GPA_SETTINGS_KEY]
        if not is_available(selected) and all(item["id"] != selected for item in items):
            selected = DEFAULT_SCHEME
    elif isinstance(scheme, str):
        selected = resolve_scheme(scheme, items)["id"]
    else:
        raise SchemeError("请选择有效的 GPA 方案。")
    current.update({GPA_SETTINGS_KEY: selected, "custom": items})
    body = dict(payload) if isinstance(payload, Mapping) else {}
    body.update(current)
    save_user_json(vault, username, GPA_SETTINGS_FILE, body)
    return current


def load_scheme(store: Any = None, username: str = "") -> str:
    return load_settings(store, username)[GPA_SETTINGS_KEY]


def save_scheme(store: Any = None, username: str = "", scheme: Any = DEFAULT_SCHEME) -> str:
    selected = DEFAULT_SCHEME if scheme is None else scheme
    return save_settings(store, username, scheme=selected)[GPA_SETTINGS_KEY]
