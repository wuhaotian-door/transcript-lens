"""学期归档：纵向累计、趋势 Δ、定型/可救分区（Spec 第 2 章 + 第 8 章第一批）。

这一层解决"大一到大四任意次追加"：

* **学号哈希 + 学期标签 append**（Spec 第 2 章规则表）—— 同一个学生在不同学期
  导入多次，成绩一路累加，不去重、不覆盖
* **累计均分用总加权**（红线 8）：``Σ(成绩×学分) / Σ学分`` 跨所有学期一起算，
  **绝不**对各学期均分再取平均
* **跨学期同名课取高**（Spec 第 2 章：同名课程出现两次取成绩高那次，取那一次的学分）

补考在数据上的表现（红线 9）：**补考不覆盖第一次成绩**。所以如果同一门课在
不同学期出现两次且分数不同，那是「重修」——按取高规则合并；如果只是补考，
原成绩本来就还在库里，这里不会去动它。

本模块是**纯内存**的：只负责"接收内存里的成绩记录 → 返回纵向累计的数据结构"。

红线级别的自我约束（Owner 明确要求，阶段二之前）：

* **绝不落盘。** 本模块没有 ``open()``、没有 ``json.dump()``、不碰 ``AppData``。
  唯一与"序列化"沾边的是 :meth:`Archive.as_dict` / :meth:`Archive.from_dict`
  —— 它们返回/接收的是**普通 dict**，落不落盘、落到哪里，由调用方决定。
* **磁盘上永远只有密文。** 真正的落盘属 ``core/crypto_store.py``（PBKDF2 派生 +
  Fernet，后续阶段），届时不经过明文 JSON 中间态。
* **本模块不加密、不联网、不依赖大模型。**
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from core.calculator import CalculationResult, CalculationScope, calculate_student
from core.parser import Category, Course, ExamRow, ParseResult

__all__ = [
    "ARCHIVE_VERSION",
    "ARCHIVE_PATH_TODO",
    "SEMESTER_ORDER",
    "SESSION_LOG_VERSION",
    "TOTAL_TERMS",
    "Archive",
    "CourseSnapshot",
    "SemesterRecord",
    "StudentArchive",
    "append_semester",
    "archive_key",
    "build_archive",
    "list_stored_students",
    "merge_records",
    "partition",
    "semester_rank",
    "session_log",
    "term_average",
    "trend",
]

#: 归档结构版本号（将来改格式时用来做迁移判断）
ARCHIVE_VERSION = 1

#: 导入日志（Spec 第 5 章 ``sessions.json``）的结构版本号
SESSION_LOG_VERSION = 1

#: 学期标签的固定顺序，用于"从早到晚"排序。
#: 含大五，是为了容忍五年制专业；本科四年实际是前 8 个。
SEMESTER_ORDER: tuple[str, ...] = tuple(
    f"{grade}{half}"
    for grade in ("大一", "大二", "大三", "大四", "大五")
    for half in ("上", "下")
)

#: 本科常规学期总数（大一上 → 大四下）。**"还剩几个学期"要按这个算**，
#: 不能用 ``len(SEMESTER_ORDER)``（那是 10，含大五，会把剩余学期数算多 2）。
TOTAL_TERMS = 8

#: ---------------------------------------------------------------------------
#: TODO（阶段二，等 core/crypto_store.py 做完再接）
#:
#: 归档文件的目标位置（Spec 第 5 章 / 第十章「数据目录」）::
#:
#:     C:\Users\<系统用户>\AppData\Local\SGMS\u_<用户名>\archive.enc
#:
#: 三条硬约束，接的时候不许违反：
#:   1. 走 ``crypto_store.save()``，磁盘上**只有密文**，不写明文 archive.json
#:   2. 不自动创建：用户没点「保存档案」就不碰磁盘
#:   3. 清库必须是用户主动操作 + 二次确认 + 明确告知删了什么（红线 7：
#:      换激活码只锁导入，绝不动成绩库）
#:
#: 现阶段刻意**不提供** ``default_archive_path()`` —— 有了函数就迟早有人调用它。
#: ---------------------------------------------------------------------------
ARCHIVE_PATH_TODO = (
    "AppData/Local/SGMS/u_<用户名>/archive.enc  "
    "(经 crypto_store.save()，只写密文；阶段二实现)"
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CourseSnapshot:
    """一门课在某个学期的一份快照。课程既有学分也有类别，是参与加权的必要信息。"""

    name: str
    credit: float
    category: str
    score: float | None

    @property
    def category_enum(self) -> Category:
        try:
            return Category(self.category)
        except ValueError:
            return Category.UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "credit": self.credit,
            "category": self.category,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CourseSnapshot":
        return cls(
            name=str(data.get("name", "")),
            credit=float(data.get("credit", 0.0)),
            category=str(data.get("category", Category.UNKNOWN.value)),
            score=None if data.get("score") is None else float(data["score"]),
        )


@dataclass
class SemesterRecord:
    """一次导入 = 一个学期的记录。"""

    term: str
    student: str
    student_id: str = ""
    courses: list[CourseSnapshot] = field(default_factory=list)
    #: 导入时间（ISO 字符串），仅供展示
    imported_at: str = ""
    #: 来源文件名，仅供展示与溯源
    source: str = ""
    valid: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def total_credit(self) -> float:
        """这个学期计入学分的课（选了且分数合法）。"""
        return sum(
            c.credit for c in self.courses
            if c.score is not None and 0.0 <= c.score <= 100.0
        )

    @property
    def weighted_sum(self) -> float:
        return sum(
            c.score * c.credit for c in self.courses
            if c.score is not None and 0.0 <= c.score <= 100.0
        )

    @property
    def weighted_average(self) -> float | None:
        total = self.total_credit
        return (self.weighted_sum / total) if total > 0 else None

    @property
    def failed_courses(self) -> list[str]:
        return [
            c.name for c in self.courses
            if c.score is not None and 0 < c.score < 60
        ]

    @property
    def absent_courses(self) -> list[str]:
        return [c.name for c in self.courses if c.score == 0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "student": self.student,
            "student_id": self.student_id,
            "courses": [c.as_dict() for c in self.courses],
            "imported_at": self.imported_at,
            "source": self.source,
            "valid": self.valid,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SemesterRecord":
        return cls(
            term=str(data.get("term", "")),
            student=str(data.get("student", "")),
            student_id=str(data.get("student_id", "")),
            courses=[CourseSnapshot.from_dict(c) for c in data.get("courses", [])],
            imported_at=str(data.get("imported_at", "")),
            source=str(data.get("source", "")),
            valid=bool(data.get("valid", True)),
            warnings=list(data.get("warnings", [])),
        )


@dataclass
class StudentArchive:
    """一个学生的纵向档案：多个学期串起来。"""

    key: str
    student: str
    records: list[SemesterRecord] = field(default_factory=list)

    @property
    def student_id(self) -> str:
        return next((r.student_id for r in self.records if r.student_id), "")

    @property
    def terms(self) -> list[str]:
        """按学期先后排好序的标签（可能含重复，表示同标签导入过多次）。"""
        return sorted(
            {r.term for r in self.records},
            key=lambda t: (semester_rank(t), t),
        )

    @property
    def total_credit(self) -> float:
        return sum(r.total_credit for r in self.records)

    @property
    def weighted_sum(self) -> float:
        return sum(r.weighted_sum for r in self.records)

    @property
    def cumulative_average(self) -> float | None:
        """累计均分（红线 8 的落点）。

        ``Σ(成绩×学分) / Σ学分``，跨所有学期一起算；**不等于**各学期均分的平均。
        重复课程**不在这里合并** —— 这个数字回答的是"这四年所有记过的分怎么加权"。
        """
        total = self.total_credit
        return (self.weighted_sum / total) if total > 0 else None

    @property
    def deduped_credit(self) -> float:
        """跨学期同名课取高之后的学分合计（用于"不重复计学分"的口径）。"""
        from core.calculator import dedupe_courses

        return sum(
            entry.credit for entry in self.merged_courses if entry.score is not None
        )

    @property
    def merged_courses(self) -> list[Any]:
        """跨学期同名课取高后的课程列表（返回 :class:`CourseScore` 序列）。"""
        from core.calculator import dedupe_courses

        pairs: list[tuple[Course, float | None]] = []
        for record in self.sorted_records:
            for snapshot in record.courses:
                pairs.append(
                    (
                        Course(
                            name=snapshot.name,
                            category=snapshot.category_enum,
                            credit=snapshot.credit,
                            col_index=-1,
                        ),
                        snapshot.score,
                    )
                )
        report = dedupe_courses(pairs)
        self._last_dedup_warnings = list(report.warnings)
        return report.course_scores

    @property
    def sorted_records(self) -> list[SemesterRecord]:
        """按学期从早到晚排序。"""
        return sorted(self.records, key=lambda r: (semester_rank(r.term), r.term))

    @property
    def failed_total(self) -> int:
        return sum(len(r.failed_courses) for r in self.records)

    @property
    def absent_total(self) -> int:
        return sum(len(r.absent_courses) for r in self.records)

    @property
    def all_warnings(self) -> list[str]:
        return [w for r in self.records for w in r.warnings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "student": self.student,
            "records": [r.as_dict() for r in self.sorted_records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StudentArchive":
        return cls(
            key=str(data.get("key", "")),
            student=str(data.get("student", "")),
            records=[SemesterRecord.from_dict(r) for r in data.get("records", [])],
        )


@dataclass
class Archive:
    """一整库的归档：``key`` 是学号哈希（没有学号时退回姓名）。"""

    version: int = ARCHIVE_VERSION
    students: dict[str, StudentArchive] = field(default_factory=dict)
    #: 已导入过的 (key, term) 组合，用来提示"这个学期导过了"
    imported_terms: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def get(self, key: str) -> StudentArchive | None:
        return self.students.get(key)

    def __len__(self) -> int:
        return len(self.students)

    @property
    def total_records(self) -> int:
        return sum(len(s.records) for s in self.students.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "students": {k: s.as_dict() for k, s in self.students.items()},
            "imported_terms": {k: sorted(v) for k, v in self.imported_terms.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Archive":
        students = {
            key: StudentArchive.from_dict(value)
            for key, value in (data.get("students") or {}).items()
        }
        return cls(
            version=int(data.get("version", ARCHIVE_VERSION)),
            students=students,
            imported_terms={
                key: list(value)
                for key, value in (data.get("imported_terms") or {}).items()
            },
        )


# ---------------------------------------------------------------------------
# key 与排序
# ---------------------------------------------------------------------------


def archive_key(student_id: str = "", student: str = "") -> str:
    """Spec 第 2 章：按"学号哈希 + 学期标签"append。

    有学号 → ``sha256(学号)[:16]``；没有学号 → 退回 ``name:`` 前缀的哈希，
    并在 :func:`build_archive` 里提示同名风险。
    """
    identifier = (student_id or "").strip()
    if identifier:
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
        return f"sid:{digest}"
    name = (student or "").strip()
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"name:{digest}"


def semester_rank(term: str) -> int:
    """学期标签的排序权重；认不出的排在已知学期之后。"""
    text = (term or "").strip()
    if text in SEMESTER_ORDER:
        return SEMESTER_ORDER.index(text)
    return len(SEMESTER_ORDER) + 1


# ---------------------------------------------------------------------------
# 从解析结果建档
# ---------------------------------------------------------------------------


def build_archive(
    parsed: ParseResult,
    term: str,
    *,
    archive: Archive | None = None,
    source: str = "",
    imported_at: str = "",
    scopes: Sequence[CalculationScope] = (CalculationScope.ALL,),
) -> Archive:
    """把一次导入 append 进归档，返回**新的** :class:`Archive`（不改入参）。

    Args:
        parsed: ``core.parser.parse_dataframe`` 的结果。
        term: 学期标签，如 ``"大二下"``。
        archive: 已有归档；省略则新建。
        source: 来源文件名（溯源用）。
        imported_at: 导入时间（展示用）。
        scopes: 每个学期存哪些口径的结算数字（默认只存"含全部"）。

    Returns:
        追加后的新 :class:`Archive`。同一 ``(key, term)`` 重复导入时**保留两份**，
        只在 ``imported_terms`` 里记一笔并给出提示 —— 绝不静默丢掉某次导入。
    """
    from core.calculator import calculate_student
    from core.parser import Course as _Course

    target = archive or Archive()
    # 复制一份，避免就地改入参（调用方可能还要用原对象）
    new_students = dict(target.students)
    new_terms = {k: list(v) for k, v in target.imported_terms.items()}
    warnings = list(target.warnings)

    if not term:
        warnings.append("学期标签为空，纵向累计会把这次导入归到同一个无名学期")

    courses: list[Course] = parsed.courses
    for row in parsed.rows:
        if not row.is_valid:
            continue  # 非法行不进档（红线 10：不参与计算，也不污染历史）

        key = archive_key(row.student_id, row.student)
        existing = new_students.get(key)
        if existing is None:
            existing = StudentArchive(key=key, student=row.student)
            new_students[key] = existing

        snapshots = [
            CourseSnapshot(
                name=course.name,
                credit=float(course.credit),
                category=course.category.value,
                score=row.score_list[index] if index < len(row.score_list) else None,
            )
            for index, course in enumerate(courses)
        ]
        record = SemesterRecord(
            term=term,
            student=row.student,
            student_id=row.student_id,
            courses=snapshots,
            imported_at=imported_at,
            source=source,
            valid=row.is_valid,
            warnings=list(row.warnings),
        )
        existing.records.append(record)

        seen = new_terms.setdefault(key, [])
        if term in seen:
            warnings.append(
                f"「{row.student}」的 {term} 已经导入过，"
                "本次按追加处理（两份都保留），请在时间轴上核对是否重复导入"
            )
        seen.append(term)

    if parsed.missing_student_id:
        warnings.append(
            f"有 {len(parsed.missing_student_id)} 名学生没有学号，"
            "纵向累计只能退回用姓名做标识，同名学生会被合并，请核对"
        )

    return Archive(
        version=target.version,
        students=new_students,
        imported_terms=new_terms,
        warnings=warnings,
    )


def append_semester(
    archive: Archive,
    parsed: ParseResult,
    term: str,
    **kwargs: Any,
) -> Archive:
    """`build_archive` 的别名，语义更直白：往档案里追加一个学期。"""
    return build_archive(parsed, term, archive=archive, **kwargs)


# ---------------------------------------------------------------------------
# 结算：趋势、分区
# ---------------------------------------------------------------------------


def term_average(
    archive: StudentArchive,
    term: str,
    scope: CalculationScope = CalculationScope.REQUIRED_LIMITED,
) -> float | None:
    """某个学生在某个学期的均分（指定口径）。同一学期有多条记录时**合并后**再算。

    合并用的是 ``Σ(成绩×学分)/Σ学分``（红线 8），不是把两次的均分再平均。
    """
    records = [r for r in archive.records if r.term == term]
    if not records:
        return None
    total = 0.0
    weighted = 0.0
    for row in _records_to_rows(records):
        result = calculate_student(row["exam"], row["courses"], scope=scope)
        total += result.total_credit
        weighted += result.weighted_sum
    return (weighted / total) if total > 0 else None


def trend(
    archive: StudentArchive,
    scope: CalculationScope = CalculationScope.REQUIRED_LIMITED,
) -> dict[str, Any]:
    """趋势：``Δ = 最新学期均分 − 最早学期均分``（Spec 第 8 章：趋势 Δ）。

    只有学期数 ≥ 2 时才有 Δ。返回的 ``series`` 是每一段的明细，
    供时间轴直接画。
    """
    terms = archive.terms
    series = [
        {"term": term, "average": term_average(archive, term, scope)}
        for term in terms
    ]
    available = [point for point in series if point["average"] is not None]
    delta = None
    if len(available) >= 2:
        delta = float(available[-1]["average"]) - float(available[0]["average"])
    return {
        "terms": terms,
        "series": series,
        "delta": delta,
        "first_term": available[0]["term"] if available else "",
        "last_term": available[-1]["term"] if available else "",
        "term_count": len(available),
    }


def partition(
    archive: StudentArchive,
    scope: CalculationScope = CalculationScope.REQUIRED_LIMITED,
) -> dict[str, Any]:
    """定型 / 可救 分区（Spec 第 8 章：定型/可救分区）。

    规则（**产品口径，要调改这里**）：

    * **已定型** —— 已经挂科且分数已入历史的课程。补考不覆盖第一次成绩（红线 9），
      所以这些分怎么谈都不会变，谈话提纲应提示"不必再纠结"
    * **缺考** —— 还没考出来的，可以安排补考

    两个"学期数"要分清楚，命名别混：

    * ``terms_imported`` —— 实际导入了几次（可能含重复导入，见 :attr:`SemesterRecord.term`）
    * ``terms_elapsed`` —— 按日历走到第几个学期（``大三下`` → 6）。
      **剩余可冲学期 = :data:`TOTAL_TERMS`(8) − terms_elapsed**，
      这是谈话提纲里"还剩几门课可以冲"的依据
    """
    failed = [
        {"term": r.term, "course": name}
        for r in archive.sorted_records
        for name in r.failed_courses
    ]
    absent = [
        {"term": r.term, "course": name}
        for r in archive.sorted_records
        for name in r.absent_courses
    ]
    known = [
        semester_rank(term) + 1
        for term in archive.terms
        if semester_rank(term) < TOTAL_TERMS
    ]
    elapsed = max(known, default=0)
    remaining = max(0, TOTAL_TERMS - elapsed)
    return {
        "locked": failed,               # 已定型：补考不改分，不必纠结
        "absent": absent,               # 缺考：还能安排
        "terms_imported": len(archive.terms),
        "terms_elapsed": elapsed,       # 日历上走到第几个学期
        "remaining_terms": remaining,   # 后面还剩几个学期可冲
        "latest_term": archive.terms[-1] if archive.terms else "",
    }


# ---------------------------------------------------------------------------
# 跨学期合并
# ---------------------------------------------------------------------------


def merge_records(archive: StudentArchive) -> CalculationResult:
    """把一个学生的所有学期合并成**一个** :class:`CalculationResult`。

    同名课跨学期取高（Spec 第 2 章），学分取那一次的学分；合并后用
    ``weighted_sum / total_credit`` 得到总加权 —— **绝不**对各学期均分再平均（红线 8）。
    """
    merged = archive.merged_courses
    result = CalculationResult(student=archive.student, scope=CalculationScope.ALL)
    for entry in merged:
        result.course_scores.append(entry)
        if entry.score is None:
            result.skipped_blank.append(entry.name)
            continue
        if not (0.0 <= entry.score <= 100.0):
            result.invalid_scores.append(entry.name)
            continue
        entry.counted = True
        result.total_credit += entry.credit
        result.weighted_sum += entry.score * entry.credit
        if entry.score == 0.0:
            result.zero_scores.append(entry.name)
    return result


def _records_to_rows(records: Iterable[SemesterRecord]) -> list[dict[str, Any]]:
    """把若干学期记录转成可算分的行（复用 ``core.calculator``，不重写算法）。"""
    rows: list[dict[str, Any]] = []
    for record in records:
        courses = [
            Course(
                name=snapshot.name,
                category=_to_category(snapshot.category),
                credit=snapshot.credit,
                col_index=-1,
            )
            for snapshot in record.courses
        ]
        exam = ExamRow(
            row_index=-1,
            student=record.student,
            student_id=record.student_id,
            scores={c.name: s.score for c, s in zip(courses, record.courses)},
            score_list=[s.score for s in record.courses],
        )
        rows.append(
            {
                "record": record,
                "courses": courses,
                "exam": exam,
                "result": None,   # 由调用方按口径现算，避免这里猜口径
            }
        )
    return rows


def _to_category(text: str) -> Category:
    try:
        return Category(text)
    except ValueError:
        return Category.UNKNOWN


# ---------------------------------------------------------------------------
# 序列化：只产出/接收普通 dict，不碰磁盘
# ---------------------------------------------------------------------------
#
# 这里**刻意没有** save_archive() / load_archive() / delete_archive_store()。
# 阶段二之前任何往 AppData 写 archive.json 的代码都不该存在，连 open() 都不要有。
# 想存盘时用 Archive.as_dict() 拿到 dict，交给 core/crypto_store.py 加密落盘。


def list_stored_students(archive: Archive) -> list[dict[str, Any]]:
    """给 UI 用的学生清单（按姓名排序）。"""
    items = [
        {
            "key": student.key,
            "student": student.student,
            "student_id": student.student_id,
            "terms": student.terms,
            "term_count": len(student.terms),
            "record_count": len(student.records),
            "cumulative_average": student.cumulative_average,
            "failed": student.failed_total,
            "absent": student.absent_total,
        }
        for student in archive.students.values()
    ]
    return sorted(items, key=lambda item: item["student"])


def session_log(archive: Archive) -> dict[str, Any]:
    """Spec 第 5 章的 ``sessions.json``：**学期导入记录**（哪学期、什么文件、什么时间）。

    写这个文件的意义不是给程序读，而是给**人**读：

    * 老师能回答"这个班我是哪几次、从哪个文件导进来的"
    * 出问题时能追溯是哪一张表格带进来的脏数据
    * 「这个学期到底导没导过」一眼可见，不用靠记忆

    它是从 :class:`Archive` 里**现推**出来的（每条 :class:`SemesterRecord` 本来就带
    ``term`` / ``source`` / ``imported_at``），不是第二份独立维护的数据 ——
    所以不存在"导入日志和成绩库对不上"的可能。

    落盘时需要加密（``core/crypto_store.py`` 的 ``FILE_SESSIONS``）。
    """
    entries: list[dict[str, Any]] = []
    for student in archive.students.values():
        for record in student.records:
            entries.append(
                {
                    "student_key": student.key,
                    "student": record.student or student.student,
                    "student_id": record.student_id or student.student_id,
                    "term": record.term,
                    "term_rank": semester_rank(record.term),
                    "source": record.source,
                    "imported_at": record.imported_at,
                    "course_count": len(record.courses),
                    "credits": round(record.total_credit, 4),
                    "average": (
                        round(record.weighted_sum / record.total_credit, 6)
                        if record.total_credit
                        else None
                    ),
                    "valid": record.valid,
                }
            )

    entries.sort(key=lambda e: (e["term_rank"], e["student"], e["source"]))
    return {
        "version": SESSION_LOG_VERSION,
        "students": len(archive.students),
        "entries": entries,
    }
