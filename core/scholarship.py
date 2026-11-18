"""奖学金测算（规则见 charter v1.6 第 4 章 —— **已定死，不许改**）。

    final = 智育加权(必修＋限选) × 0.7 + 德育分 × 0.3

* 班级内按 final 降序排名；
* 前 X 名一等、接下来 Y 名二等、再接下来 Z 名三等（X/Y/Z 每班独立配置）；
* **特等奖**：final 进入前 X 名 **且** 每门科目原始成绩 ≥ 90，
  不占名额，默认不与其他奖项叠加。

学年智育复用 ``core.calculator`` 的必修＋限选口径
  算好的，这里只做「乘以 0.7、加上德育×0.3、排序、截名额」这几步
  —— 那不是加权算术，是奖项规则的算术，charter 第 4 章明确给了公式。

★ 空 ≠ 0（红线 2）：特等奖的「每门 ≥ 90」只检查**这个学生真考了的课**
  （``score is not None``）。没选的课不参与判定，也不会被当成 0 分拖累他。
  缺考（数字 0）**算考了**，0 < 90，所以拿不到特等 —— 这是对的。

★ 学生身份一律用 ``key``（``sid:<学号>``，没学号才退回 ``name:<姓名>``），
  德育分也按这个键存，同名不会串（charter 铁律 4）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.calculator import CalculationScope, CourseScore, dedupe_courses, weighted_average
from core.crypto_store import CryptoStore
from core.parser import Category
from core.user_store import ROSTER_WRITE_LOCK, list_user_json, load_user_json, save_user_json

__all__ = [
    "AWARD_FIRST",
    "AWARD_NONE",
    "AWARD_SPECIAL",
    "ACADEMIC_SCOPE",
    "ACADEMIC_WEIGHT",
    "ACADEMIC_YEARS",
    "CLASS_PREFIX",
    "MORAL_WEIGHT",
    "SPECIAL_MIN_SCORE",
    "ClassRecord",
    "Quota",
    "ScholarshipRow",
    "class_filename",
    "compute_scholarship",
    "list_classes",
    "list_moral_scopes",
    "load_class",
    "load_moral",
    "load_quota",
    "load_year_class",
    "moral_file_scope",
    "moral_filename",
    "quota_filename",
    "save_class",
    "save_moral",
    "save_quota",
    "validate_class_name",
    "validate_school_year",
]

#: 班级号前缀 —— 一个班一个文件：``class_{班级号}.json.enc``
CLASS_PREFIX = "class_"

#: 智育 / 德育权重（charter 4.2，**不许改**）
ACADEMIC_WEIGHT = 0.7
MORAL_WEIGHT = 0.3

#: 特等奖的每门课最低分（charter 4.4，**不许放宽**）
SPECIAL_MIN_SCORE = 90

#: 全年智育与奖项只计必修和限选。
ACADEMIC_SCOPE = CalculationScope.REQUIRED_LIMITED

#: 奖项名
AWARD_SPECIAL = "特等"
AWARD_FIRST = "一等"
AWARD_SECOND = "二等"
AWARD_THIRD = "三等"
AWARD_NONE = ""

#: 班级号：非空、纯字母数字或短横线（charter 4.1）
_CLASS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")

#: 从 ``list_classes`` 里认出一个班级文件的严格模式。
#: ★ 必须排除 ``_moral`` / ``_quota`` 后缀，否则"德育分文件"会被当成一个班。
_CLASS_FILE_RE = re.compile(
    r"^class_(?P<name>[A-Za-z0-9][A-Za-z0-9-]{0,31})(?:_y[1-4]_(?:first|second|full))?\.json\.enc$"
)
ACADEMIC_YEARS = ("大一", "大二", "大三", "大四")
_MORAL_FILE_RE = re.compile(
    r"^class_(?P<name>[A-Za-z0-9][A-Za-z0-9-]{0,31})(?:_y(?P<year>[1-4]))?_moral\.json\.enc$"
)


def validate_class_name(name: str) -> str:
    """校验班级号，返回规范化后的值（去空白）。

    Raises:
        ValueError: 空 / 含非法字符 / 太长。
    """
    value = (name or "").strip()
    if not value:
        raise ValueError("班级号不能为空。")
    if not _CLASS_RE.match(value):
        raise ValueError(
            "班级号只能是字母、数字和短横线（1~32 位），且以字母或数字开头。"
        )
    return value


def class_filename(class_name: str) -> str:
    return f"{CLASS_PREFIX}{validate_class_name(class_name)}.json.enc"


def _class_year_stem(class_name: str, year: str) -> str:
    stem = f"{CLASS_PREFIX}{validate_class_name(class_name)}"
    return f"{stem}_y{ACADEMIC_YEARS.index(year) + 1}" if year else stem


def moral_filename(class_name: str, year: str = "") -> str:
    return f"{_class_year_stem(class_name, year)}_moral.json.enc"


def quota_filename(class_name: str, year: str = "") -> str:
    return f"{_class_year_stem(class_name, year)}_quota.json.enc"


def moral_file_scope(filename: str) -> tuple[str, str] | None:
    match = _MORAL_FILE_RE.fullmatch(filename)
    if match is None:
        return None
    year = match["year"]
    return match["name"], ACADEMIC_YEARS[int(year) - 1] if year else ""


def list_moral_scopes(store: CryptoStore, username: str) -> list[tuple[str, str]]:
    return sorted(scope for name in list_user_json(store, username, "_moral.json.enc")
                  if (scope := moral_file_scope(name)) is not None)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ClassRecord:
    """一个班的成绩快照（从导入的成绩表里抽出来，加密落盘）。

    ``students`` 里每一项都是**算好的**：``academic`` 是必修与限选的加权均分，
    ``courses`` 是这门课他真考了多少分。以后测算时**不需要重新解析 Excel**，
    也就不会出现"两次解析结果不一致"。
    """

    class_name: str
    source: str = ""
    term: str = ""
    students: list[dict[str, Any]] = field(default_factory=list)
    school_year: str = ""
    annual_complete: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_name": self.class_name,
            "source": self.source,
            "term": self.term,
            "students": list(self.students),
            "school_year": self.school_year,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ClassRecord | None":
        if not isinstance(data, dict):
            return None
        name = str(data.get("class_name") or "").strip()
        if not name:
            return None
        students = data.get("students")
        if not isinstance(students, list):
            students = []
        return cls(
            class_name=name,
            source=str(data.get("source") or ""),
            term=str(data.get("term") or ""),
            students=[s for s in students if isinstance(s, dict)],
            school_year=str(data.get("school_year") or ""),
        )

    @property
    def size(self) -> int:
        return len(self.students)


@dataclass
class Quota:
    """一个班的名额配置。"""

    first: int = 0
    second: int = 0
    third: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"first": self.first, "second": self.second, "third": self.third}

    @classmethod
    def from_dict(cls, data: Any) -> "Quota":
        if not isinstance(data, dict):
            return cls()
        def _int(key: str) -> int:
            try:
                return max(0, int(data.get(key) or 0))
            except (TypeError, ValueError):
                return 0
        return cls(first=_int("first"), second=_int("second"), third=_int("third"))

    @property
    def total(self) -> int:
        return self.first + self.second + self.third


@dataclass
class ScholarshipRow:
    """结果表格的一行。"""

    rank: int | None
    student: str
    student_id: str
    key: str
    academic: float | None
    moral: float | None
    final: float | None
    award: str
    note: str = ""

    @property
    def academic_text(self) -> str:
        return "—" if self.academic is None else f"{self.academic:.2f}"

    @property
    def final_text(self) -> str:
        return "—" if self.final is None else f"{self.final:.2f}"

    @property
    def moral_text(self) -> str:
        return "—" if self.moral is None else str(self.moral).removesuffix(".0")


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def save_class(store: CryptoStore, username: str, record: ClassRecord) -> Any:
    """在指定学年范围保存成绩。无学年标签的调用仍使用原班级文件。"""
    with ROSTER_WRITE_LOCK:
        year, half = record.term[:2], record.term[2:]
        filename = class_filename(record.class_name)
        if year in ACADEMIC_YEARS and half in ("", "上", "下"):
            validate_school_year(store, username, record.class_name, year, record.school_year)
            filename = _year_grade_filename(record.class_name, year, half)
        return save_user_json(store, username, filename, record.as_dict())


def load_class(store: CryptoStore, username: str, class_name: str) -> ClassRecord | None:
    name = validate_class_name(class_name)
    filename = _latest_class_files(store, username).get(name)
    return ClassRecord.from_dict(load_user_json(store, username, filename)) if filename else None


def _year_grade_filename(class_name: str, year: str, half: str) -> str:
    coverage = {"": "full", "上": "first", "下": "second"}[half]
    return f"{_class_year_stem(class_name, year)}_{coverage}.json.enc"


def validate_school_year(
    store: CryptoStore, username: str, class_name: str, year: str, school_year: str,
) -> None:
    """同一班级的公历学年只对应一个年级，原表明确标注时检查两者是否一致。"""
    if not school_year:
        return
    files = set(list_user_json(store, username))
    for previous_year in ACADEMIC_YEARS:
        for half in ("", "上", "下"):
            filename = _year_grade_filename(class_name, previous_year, half)
            if filename not in files:
                continue
            previous = ClassRecord.from_dict(load_user_json(store, username, filename, strict=True))
            if previous is None or not previous.school_year:
                continue
            if previous_year == year and previous.school_year != school_year:
                raise ValueError(f"{class_name} 班{year}已登记为 {previous.school_year} 学年，"
                                 f"原表是 {school_year} 学年。")
            if previous_year != year and previous.school_year == school_year:
                raise ValueError(f"{class_name} 班的 {school_year} 学年已归入{previous_year}。")


def load_year_class(
    store: CryptoStore, username: str, class_name: str, year: str,
) -> ClassRecord | None:
    """读取单个班级学年。完整学年表优先，其余情况按上下两份合并。"""
    if year not in ACADEMIC_YEARS:
        raise ValueError("请选择大一至大四中的一个学年。")

    def read(half: str) -> ClassRecord | None:
        filename = _year_grade_filename(class_name, year, half)
        return ClassRecord.from_dict(load_user_json(store, username, filename, strict=True))

    full = read("")
    if full is not None:
        full.annual_complete = True
        full.students = [_annual_student(student, student) for student in full.students]
        return full
    first, second = read("上"), read("下")
    if first is None or second is None:
        partial = first or second
        if partial is not None:
            partial.annual_complete = False
            partial.students = [_annual_student(student, student) for student in partial.students]
        return partial
    if first.school_year and second.school_year and first.school_year != second.school_year:
        raise ValueError(f"{year}的上下半学年来自不同学年，请检查两次导入的归属。")

    first_students = {row["key"]: row for row in first.students if row.get("key")}
    second_students = {row["key"]: row for row in second.students if row.get("key")}
    students = [
        _combine_year_student(first_students.get(key), second_students.get(key))
        for key in dict.fromkeys((*first_students, *second_students))
    ]
    return ClassRecord(
        class_name, " / ".join(filter(None, (first.source, second.source))), year, students,
        first.school_year or second.school_year, True,
    )


def _combine_year_student(
    first: dict[str, Any] | None, second: dict[str, Any] | None,
) -> dict[str, Any]:
    student = dict(second or first or {})
    if first is None or second is None or first.get("academic") is None or second.get("academic") is None:
        return {**student, "academic": None, "incomplete_year": True}

    return _annual_student(student, first, second)


def _annual_student(student: dict[str, Any], *sources: dict[str, Any]) -> dict[str, Any]:
    # 先按课程类别筛选，再处理同名课程，避免选修成绩覆盖同名必修成绩。
    allowed = {category.value for category in ACADEMIC_SCOPE.categories}
    courses = [
        CourseScore(course["name"], Category(course["category"]),
                    float(course["credit"]), course.get("score"))
        for source in sources
        for course in source.get("courses", ())
        if course.get("category") in allowed and course.get("credit") is not None
    ]
    winners = dedupe_courses(courses).course_scores
    return {**student, "academic": weighted_average(winners),
            "courses": [course.as_dict() for course in winners]}


def _latest_class_files(store: CryptoStore, username: str) -> dict[str, str]:
    """扫描班级快照，每班给旧页面提供最近写入的一份。"""
    account = store.load_users().find(username)
    if account is None:
        return {}
    folder = store.user_dir(account.uid)
    if not folder.is_dir():
        return {}
    latest: dict[str, tuple[int, str]] = {}
    for path in folder.iterdir():
        hit = _CLASS_FILE_RE.fullmatch(path.name)
        if not hit or not path.is_file():
            continue
        name = hit["name"]
        try:
            candidate = path.stat().st_mtime_ns, path.name
        except FileNotFoundError:
            continue
        if candidate > latest.get(name, (0, "")):
            latest[name] = candidate
    return {name: latest[name][1] for name in sorted(latest)}


def list_classes(store: CryptoStore, username: str) -> list[ClassRecord]:
    """从加密目录列出班级，每班只出现一次。"""
    return [
        record for filename in _latest_class_files(store, username).values()
        if (record := ClassRecord.from_dict(load_user_json(store, username, filename))) is not None
    ]


def save_moral(store: CryptoStore, username: str, class_name: str,
               scores: dict[str, float], *, year: str = "", school_year: str = "") -> Any:
    """存德育分。键是学生 ``key``（``sid:<学号>``），不是姓名。"""
    clean: dict[str, float] = {}
    for key, value in scores.items():
        if not isinstance(key, str) or not key:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean[key] = max(0, min(100, number))
    with ROSTER_WRITE_LOCK:
        path = moral_filename(class_name, year)
        previous = _read_moral_payload(store, username, class_name, year, school_year) if year else {}
        cycle = school_year or previous.get("school_year", "")
        payload: dict[str, Any] = {"scores": clean}
        if cycle:
            payload["school_year"] = cycle
        return save_user_json(store, username, path, payload)


def _read_moral_payload(store: CryptoStore, username: str, class_name: str,
                        year: str, school_year: str) -> dict[str, Any]:
    raw = load_user_json(store, username, moral_filename(class_name, year), {}, strict=bool(year))
    if not isinstance(raw, dict):
        return {}
    saved_year = raw.get("school_year")
    if school_year and saved_year and school_year != saved_year:
        raise ValueError(f"{class_name} 班{year}的德育分属于 {saved_year} 学年，当前成绩属于 {school_year} 学年。")
    return raw


def load_moral(store: CryptoStore, username: str, class_name: str, *, year: str = "",
               school_year: str = "") -> dict[str, float]:
    raw = _read_moral_payload(store, username, class_name, year, school_year)
    scores = raw.get("scores") if isinstance(raw, dict) else None
    if not isinstance(scores, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in scores.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[str(key)] = max(0, min(100, number))
    return out


def save_quota(store: CryptoStore, username: str, class_name: str, quota: Quota, *, year: str = "") -> Any:
    return save_user_json(store, username, quota_filename(class_name, year), quota.as_dict())


def load_quota(store: CryptoStore, username: str, class_name: str, *, year: str = "") -> Quota:
    return Quota.from_dict(load_user_json(store, username, quota_filename(class_name, year)))


# ---------------------------------------------------------------------------
# 测算
# ---------------------------------------------------------------------------


def all_courses_above(student: dict[str, Any], threshold: float = SPECIAL_MIN_SCORE) -> tuple[bool, list[str]]:
    """这个学生是不是**每一门考过的课**都 ≥ 门槛。

    Returns:
        ``(是否全过, 没过的课程名列表)``

    ★ 空 ≠ 0（红线 2）：``score is None`` 的课**没考/没选**，直接跳过，
      不当成 0 分。缺考（数字 0）会进来，0 < 90，所以过不了 —— 正确。
    """
    below: list[str] = []
    courses = student.get("courses")
    if not isinstance(courses, list):
        return True, []
    for course in courses:
        if not isinstance(course, dict):
            continue
        score = course.get("score")
        if score is None:
            continue
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue
        if value < threshold:
            below.append(str(course.get("name") or "（未命名课程）"))
    return (not below), below


def compute_scholarship(
    record: ClassRecord,
    moral: dict[str, float] | None = None,
    quota: Quota | None = None,
    *,
    stackable: bool = False,
) -> list[ScholarshipRow]:
    """算出一个班的排名与奖项。

    Args:
        record: 班级成绩快照。
        moral: ``{学生 key: 德育分 0-100}``。没填的学生德育分按 **0** 算，
            并在备注里说明 —— 不猜、不给默认满分。
        quota: 名额。默认全 0 = 不分配一/二/三等。
        stackable: 特等能不能同时拿一/二/三等。charter 4.4 默认 **False**。

    Returns:
        按最终成绩降序排列的 :class:`ScholarshipRow` 列表。
    """
    if record.annual_complete is False:
        raise ValueError("请先导入完整学年成绩或同一学年的上下两份成绩。")
    moral = moral or {}
    quota = quota or Quota()

    # --- 1. 算最终成绩 -----------------------------------------------------
    prepared: list[dict[str, Any]] = []
    for student in record.students:
        key = str(student.get("key") or "")
        academic_raw = student.get("academic")
        try:
            academic = None if academic_raw is None else float(academic_raw)
        except (TypeError, ValueError):
            academic = None

        raw_moral = moral.get(key)
        moral_value: float | None
        try:
            moral_value = None if raw_moral is None else float(raw_moral)
        except (TypeError, ValueError):
            moral_value = None

        notes: list[str] = []
        if moral_value is None or not math.isfinite(moral_value):
            # ★ 没录德育分 ≠ 德育满分，也 ≠ 0 分真实值 —— 按 0 参与计算，
            #   但必须在备注里说清楚，否则用户会以为这个人真的德育 0 分。
            moral_value = 0
            notes.append("德育分未录入，按 0 计")
        if academic is None:
            notes.append("个人学年成绩不完整" if student.get("incomplete_year") else "没有可用的智育成绩")
            final = None
        else:
            final = academic * ACADEMIC_WEIGHT + float(moral_value) * MORAL_WEIGHT

        ok90, below = all_courses_above(student)
        prepared.append({
            "student": str(student.get("student") or ""),
            "student_id": str(student.get("student_id") or ""),
            "key": key,
            "academic": academic,
            "moral": moral_value,
            "final": final,
            "all90": ok90,
            "below": below,
            "notes": notes,
        })

    # --- 2. 排名（final 降序；没有成绩的排最后）-----------------------------
    # 并列时的次序：智育分高的在前，再按学号，保证**每次算出来顺序一样**
    # （不然同一份数据两次测算给出不同名次，用户会以为是 bug）。
    prepared.sort(
        key=lambda s: (
            s["final"] is None,
            -(s["final"] if s["final"] is not None else 0.0),
            -(s["academic"] if s["academic"] is not None else 0.0),
            s["student_id"],
            s["student"],
        )
    )
    for index, item in enumerate(prepared, 1):
        item["rank"] = None if record.annual_complete and item["final"] is None else index

    # --- 3. 特等奖：final 进入前 X 名 且 每门 ≥ 90 -------------------------
    first_quota = max(0, quota.first)
    for item in prepared:
        eligible = (
            item["final"] is not None
            and item["rank"] <= first_quota
            and item["all90"]
        )
        item["special"] = bool(eligible)
        if item["final"] is not None and not item["all90"]:
            # 差几门 —— charter 要求备注写"距特等差 N 门 <90"
            item["notes"].append(f"距特等差 {len(item['below'])} 门 <{SPECIAL_MIN_SCORE:g}")

    # --- 4. 名额分配 -------------------------------------------------------
    # 「特等不占名额」= 拿特等的人从名额池里**移出去**，后面的人往前补。
    # 除非用户开了"允许叠加"，那就留在池子里，两个奖都算他的。
    pool = [
        item for item in prepared
        if item["final"] is not None and (stackable or not item["special"])
    ]
    for item in prepared:
        item["award"] = AWARD_NONE

    first_line = quota.first
    second_end = first_line + quota.second
    third_end = second_end + quota.third
    for index, item in enumerate(pool):
        if index < first_line:
            item["award"] = AWARD_FIRST
        elif index < second_end:
            item["award"] = AWARD_SECOND
        elif index < third_end:
            item["award"] = AWARD_THIRD

    def _line_for_rank(rank: int) -> str:
        """按**总名次**看他本来够得上哪一档（给备注用，不影响分配）。"""
        if rank <= first_line:
            return AWARD_FIRST
        if rank <= second_end:
            return AWARD_SECOND
        if rank <= third_end:
            return AWARD_THIRD
        return AWARD_NONE

    for item in prepared:
        if not item["special"]:
            continue
        if stackable:
            # 叠加：两个奖都显示出来，否则用户以为特等把一等顶掉了
            if item["award"]:
                item["award"] = f"{AWARD_SPECIAL}+{item['award']}"
            else:
                item["award"] = AWARD_SPECIAL
        else:
            # 不叠加：只显示特等。他**本来**够得上的那一档写进备注 ——
            # 不说的话，辅导员会以为这个人连一等都没够上。
            would_be = _line_for_rank(item["rank"])
            if would_be:
                item["notes"].append(f"达{would_be}线，以特等计入（不占名额）")
            item["award"] = AWARD_SPECIAL

    # --- 5. 出表 -----------------------------------------------------------
    rows: list[ScholarshipRow] = []
    for item in prepared:
        rows.append(
            ScholarshipRow(
                rank=item["rank"],
                student=item["student"],
                student_id=item["student_id"],
                key=item["key"],
                academic=item["academic"],
                moral=item["moral"],
                final=item["final"],
                award=item["award"],
                note="；".join(item["notes"]),
            )
        )
    return rows


def award_counts(rows: Sequence[ScholarshipRow]) -> dict[str, int]:
    """统计各奖项实际分配了多少个。

    ★ 开了"允许叠加"时奖项可能是 ``特等+一等`` 这种复合值，
      两边**都要计数** —— 只数第一个的话，统计行会少报一个一等奖。
    """
    counts = {AWARD_SPECIAL: 0, AWARD_FIRST: 0, AWARD_SECOND: 0, AWARD_THIRD: 0}
    for row in rows:
        for part in str(row.award or "").split("+"):
            part = part.strip()
            if part in counts:
                counts[part] += 1
    return counts
