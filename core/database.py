"""成绩历史与个人档案的**逻辑表 API**（v1.9 第二阶段 D 任务）。

这个模块对外是"两张表"，对内**没有任何数据库引擎** ——
它是纯 JSON 载荷，落到 :mod:`core.user_store` 的**应用级加密存储**上
（文件仍然是 ``*.json.enc``）：

===================  =================================  ==============================
表                   文件名                             主键
===================  =================================  ==============================
``grade_history``    ``grade_history.json.enc``         (student_id, semester, course_name)
``student_profiles`` ``student_profiles.json.enc``       student_id
``grade_audit``      ``audit/grade_changes.json.enc``   追加序号（只追加，不改不删）
===================  =================================  ==============================

★ 审计日志（``grade_audit``）
-----------------------------

成绩的每一次变动都**自动**留一行：操作时间 / 操作类型（``insert`` /
``overwrite`` / ``delete``）/ 学生 ID / 学期 / 课程名 / 这一次动了几条。

* ★ **不记分数** —— 它是审计日志，不是第二份成绩库（记上分数就等于把成绩
  又多抄了一份到磁盘上）。
* ★ 日志写不进去 / 读不出来**都不许把主流程搞崩**：写失败降级成一条警告
  （成绩本身已经存好了），读失败当作"没有日志"返回空列表。
* ★ 清空成绩库**不删审计日志** —— 删了就等于给"删数据"这件事销赃。

★ 为什么是应用级加密，而不是 SQLite 明文库
-------------------------------------------

铁律：**明文成绩绝不落盘**。SQLite 会自己建一个明文文件（还有 ``-wal`` /
``-journal`` 两个兄弟文件），那正是这条铁律要挡的东西。所以这里只做
"逻辑表 API"，真正的字节由 :func:`core.user_store.save_user_json` 写 ——
它走 ``crypto_store.save_encrypted``，**文件名不是 ``.enc`` 会被当场拒绝**。

★ 为什么应用级而不是密码级

自动登录（只有 session token、没有密码）时也要能读这份历史：
"这条成绩是哪一批导进来的、这个学生有没有被覆盖过" 属于辅导员自己录进去的
辅助数据，和班级快照、德育分、名额同一层级（AGENTS §16）。
真正**给别人看的成绩本身**仍然走密码级，另有其库。

★ 学生身份一律 ``sid:<学号>``

同名学生历届都有，姓名当键必然串号，而且不报错（AGENTS §9）。
``student_id`` 字段存的是 :func:`ui.intentions.student_key` 的输出
（``sid:<学号>``，没有学号才退回 ``name:<姓名>``）—— 本模块**不解析、
不改写**这个值，原样存取。

★ 这个模块零 Qt
----------------

pytest 可以直接 import、直接用，不需要 ``QT_QPA_PLATFORM=offscreen``。

★ 这里一行加权算术都没有

平均分 / 加权均分 / GPA / 排名一概不在这里算（铁律五）。
真源是 :mod:`core.calculator`（导入时算好、存进班级快照）和
:mod:`core.archival`（纵向字段）。本模块只做"按主键存、按主键取、按主键删"。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from core.crypto_store import CryptoError, CryptoStore, StoreCorruptError
from core.archival import semester_rank
from core.user_store import list_user_json, load_user_json, save_user_json, user_file

#: 审计日志写不进去时用它留痕（**不抛**，成绩已经存好了）
_LOGGER = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_PAYLOAD_FILE",
    "AUDIT_DIR",
    "AUDIT_FILE",
    "AUDIT_MAX_ENTRIES",
    "AUDIT_OPERATIONS",
    "CLEAR_KEEP_FILES",
    "CLEAR_NEVER_TOUCH_SUFFIXES",
    "GRADE_HISTORY_FILE",
    "GRADE_HISTORY_FIELDS",
    "GRADE_HISTORY_PK",
    "LEGACY_GRADE_FILES",
    "OP_DELETE",
    "OP_INSERT",
    "OP_OVERWRITE",
    "PROFILE_FILE",
    "TABLE_GRADE_AUDIT",
    "TABLE_GRADE_HISTORY",
    "TABLE_STUDENT_PROFILES",
    "USE_OVERWRITE",
    "USE_SKIP",
    "ConflictChooser",
    "ConflictQuestion",
    "STUDENTS_FILE",
    "STUDENT_FIELDS",
    "STUDENT_OPS",
    "STUDENT_SOURCES",
    "AUDIT_DETAIL_MAX",
    "TABLE_STUDENTS",
    "DuplicateStudentError",
    "GradeAuditLog",
    "GradeHistoryDB",
    "ProfileStore",
    "StudentMigrationReport",
    "StudentStore",
    "clear_library",
    "library_inventory",
    "migrate_student_id",
    "resolve_uid",
]

# ---------------------------------------------------------------------------
# 逻辑表定义（照任务书的表名 / 字段 / 主键，一个字不改）
# ---------------------------------------------------------------------------

#: 成绩历史表名
TABLE_GRADE_HISTORY = "grade_history"
#: 个人档案表名
TABLE_STUDENT_PROFILES = "student_profiles"

#: 落盘文件名（**必须以 ``.enc`` 结尾**，否则 save_encrypted 拒绝）
GRADE_HISTORY_FILE = "grade_history.json.enc"
PROFILE_FILE = "student_profiles.json.enc"

#: 学生主档表名（v1.9.2 新增）—— 一行 = 一个学生，唯一键是**学号**
TABLE_STUDENTS = "students"
#: 学生主档的文件名（应用级加密，**必须以 ``.enc`` 结尾**）
STUDENTS_FILE = "students.json.enc"
#: 学生主档的字段（顺序即落盘顺序）
STUDENT_FIELDS = (
    "student_id",
    "name",
    "class_name",
    "note",
    "archived",
    "source",
    "created_at",
    "updated_at",
)

#: 学生主档允许的 ``source`` 取值（manual = 手工建档 / import = 导入时补档）
STUDENT_SOURCES = ("manual", "import")

#: 审计日志表名
TABLE_GRADE_AUDIT = "grade_audit"
#: 审计日志所在子目录（相对用户目录）
AUDIT_DIR = "audit"
#: 审计日志文件名 —— **带子目录**：``<user_dir>/audit/grade_changes.json.enc``
AUDIT_FILE = f"{AUDIT_DIR}/grade_changes.json.enc"
#: 每个用户最多留多少条（超了从**最旧的**开始丢，日志不能无限长大）
AUDIT_MAX_ENTRIES = 2000
#: 审计行 ``detail`` 最多留多少字（只写操作说明，**绝不写分数**）
AUDIT_DETAIL_MAX = 200

#: 审计日志的操作类型（任务书：新增 insert / 覆盖 overwrite / 删除 delete）
OP_INSERT = "insert"
OP_OVERWRITE = "overwrite"
OP_DELETE = "delete"
#: 学生主档的审计操作（v1.9.2；同样**只记变动、不记分数**）
OP_STUDENT_ADD = "student_add"
OP_STUDENT_EDIT = "student_edit"
OP_STUDENT_ARCHIVE = "student_archive"
OP_STUDENT_RESTORE = "student_restore"
OP_STUDENT_DELETE = "student_delete"
OP_STUDENT_MIGRATE = "student_migrate"
#: 学生主档相关的全部操作类型
STUDENT_OPS = (
    OP_STUDENT_ADD,
    OP_STUDENT_EDIT,
    OP_STUDENT_ARCHIVE,
    OP_STUDENT_RESTORE,
    OP_STUDENT_DELETE,
    OP_STUDENT_MIGRATE,
)
#: 允许出现在日志里的操作类型（别的一律丢弃，防日志被写脏）
AUDIT_OPERATIONS = (OP_INSERT, OP_OVERWRITE, OP_DELETE) + STUDENT_OPS

#: grade_history 的字段（顺序即落盘顺序，方便人肉看密文结构）
GRADE_HISTORY_FIELDS = (
    "student_id",
    "semester",
    "course_name",
    "score",
    "credit",
    "import_batch_id",
    "imported_at",
)
#: grade_history 的主键
GRADE_HISTORY_PK = ("student_id", "semester", "course_name")

#: ``apply()`` 的两条策略
USE_OVERWRITE = "overwrite"
USE_SKIP = "skip"

#: 冲突回调：给一串冲突，回一个 (student_id -> "overwrite" | "skip")
ConflictChooser = Callable[["list[ConflictQuestion]"], "dict[str, str]"]


class ConflictQuestion:
    """一条"这个学生这个学期已经有成绩了，怎么办？"的问询。

    只带**界面要显示的东西**（姓名 + 学号 + 学期 + 已有几条），
    不替界面做判断 —— 策略由回调返回。
    """

    __slots__ = ("student_id", "student", "semester", "existing")

    def __init__(self, student_id: str, student: str, semester: str, existing: int) -> None:
        self.student_id = student_id
        self.student = student
        self.semester = semester
        self.existing = existing

    def __repr__(self) -> str:  # pragma: no cover - 只为排查时好看
        return (
            f"ConflictQuestion(student_id={self.student_id!r}, student={self.student!r}, "
            f"semester={self.semester!r}, existing={self.existing})"
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def resolve_uid(store: CryptoStore, username: str | None) -> str | None:
    """用户名 → uid。查不到（没登录 / 账户不存在）返回 ``None``（**不抛**）。"""
    if not username:
        return None
    try:
        record = store.load_users().find(username)
    except (CryptoError, StoreCorruptError, OSError, ValueError):
        return None
    return None if record is None else record.uid


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _detail_text(value: Any) -> str:
    """审计 ``detail`` 的清洗：去首尾空白、最多 :data:`AUDIT_DETAIL_MAX` 字。

    ★ 它只是给人看的操作说明（"学号 2023001 → 2023002；成绩 3 条"），
      **绝不写分数** —— 审计日志不是第二份成绩库。
    """
    return _clean_text(value)[:AUDIT_DETAIL_MAX]


def _optional_number(value: Any) -> float | None:
    """``None`` / 空串 → ``None``（**空 ≠ 0**，红线 2）；数字 → float。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rest_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _clean_text(row.get("student_id")),
        _clean_text(row.get("semester")),
        _clean_text(row.get("course_name")),
    )


def _count_of(value: Any) -> int:
    """日志里的"动了几条"：读不出来就是 **1**（一次操作至少动一条），绝不给 0/负数。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return number if number > 0 else 1


# ---------------------------------------------------------------------------
# 成绩历史表
# ---------------------------------------------------------------------------


class GradeHistoryDB:
    """``grade_history`` 表：一行 = 一个学生一个学期的一门课。

    落盘位置：``<user_dir>/grade_history.json.enc``（应用级加密）。
    """

    #: 表名
    table = TABLE_GRADE_HISTORY
    #: 载荷文件名
    filename = GRADE_HISTORY_FILE

    def __init__(self, *, username: str | None = None, uid: str | None = None,
                 store: CryptoStore | None = None) -> None:
        """``store`` 缺省 = 当前登录用户所在的库；``username`` 缺省 = 当前登录用户。

        ★ ``uid`` 现在只是**记录下来的身份快照**（:func:`resolve_uid` 的结果），
        真正的读写一律按 ``username`` 走 :func:`core.user_store.user_file` ——
        后者自己会用 ``users.json`` 里的 uid 解析路径。测试里换个 stdlib 之外的
        store 也不会走错目录。
        """
        self._store = store if store is not None else CryptoStore()
        self._username = username or current_username()
        self._uid = uid if uid is not None else resolve_uid(self._store, self._username)

    # -- 身份 ---------------------------------------------------------------
    @property
    def store(self) -> CryptoStore:
        return self._store

    @property
    def username(self) -> str:
        return self._username

    @property
    def uid(self) -> str | None:
        return self._uid

    # -- 读 -----------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        """整张表，按 ``(学期顺序 → 学号 → 课程名)`` 排好。"""
        stored = self._payload().get("rows")
        rows: list[dict[str, Any]] = []
        if not isinstance(stored, list):
            return rows
        for row in stored:
            if not isinstance(row, dict):
                continue
            if not all(_rest_key(row)):
                continue
            clean = {field: row.get(field) for field in GRADE_HISTORY_FIELDS}
            clean["score"] = _optional_number(clean.get("score"))
            clean["credit"] = _optional_number(clean.get("credit"))
            clean["student_id"] = _clean_text(clean.get("student_id"))
            clean["semester"] = _clean_text(clean.get("semester"))
            clean["course_name"] = _clean_text(clean.get("course_name"))
            clean["import_batch_id"] = _clean_text(clean.get("import_batch_id"))
            clean["imported_at"] = _clean_text(clean.get("imported_at"))
            rows.append(clean)
        rows.sort(
            key=lambda r: (
                semester_rank(r["semester"]),
                r["semester"],
                r["student_id"],
                r["course_name"],
            )
        )
        return rows

    def semester_rows(self, student_id: str, semester: str) -> list[dict[str, Any]]:
        """某个学生某个学期的明细（按课程名排序）。"""
        want_id = _clean_text(student_id)
        want_term = _clean_text(semester)
        return [
            row
            for row in self.rows()
            if row["student_id"] == want_id and row["semester"] == want_term
        ]

    def has_semester(self, student_id: str, semester: str) -> bool:
        """这个学生这个学期**有没有任何一条**记录。"""
        return bool(self.semester_rows(student_id, semester))

    def students_in(self, semester: str) -> list[str]:
        """这个学期有哪些学生（去重、已排序）。"""
        want_term = _clean_text(semester)
        found = {row["student_id"] for row in self.rows() if row["semester"] == want_term}
        return sorted(found)

    def count(self) -> int:
        """表里有几行（**只数不排序**）。

        ★ 「清空成绩库」的探测就走它 —— 判据是**表里真有没有记录**，
          不是"磁盘上有没有那个文件"（空表也会留下一个 0 行的 ``.enc``）。
        """
        stored = self._payload().get("rows")
        if not isinstance(stored, list):
            return 0
        return sum(1 for row in stored
                   if isinstance(row, dict) and all(_rest_key(row)))

    def is_empty(self) -> bool:
        """这张表是不是空的（等价于 ``count() == 0``）。"""
        return self.count() == 0

    # -- 写 -----------------------------------------------------------------
    def apply(self, records: list[dict[str, Any]], *, semester: str,
              batch_id: str, policy: str = USE_OVERWRITE,
              on_conflict: ConflictChooser | None = None) -> dict[str, int]:
        """把一批记录写进表里，并**如实汇报**每条的去向。

        参数：
            records: 每项至少要有 ``student_id`` / ``course_name``；
                ``score`` / ``credit`` 允许缺省或为 ``None``（**空 ≠ 0**）；
                ``student_name`` 可选 —— 只用来把冲突弹窗写成
                「检测到学生 张三 已有 …」，**不落盘**（表里没有这一列）
            semester: 这一批属于哪个学期（写进每行的 ``semester``）
            batch_id: 导入批次号，调用方传入，形如 ``20261027-1``
            policy: ``"overwrite"``（删旧写新）或 ``"skip"``（保留旧记录、
                这个学生这一批一行都不写）；**缺省就是 overwrite**
            on_conflict: 冲突抉择回调。给了就**逐学生问一次**，以它为准
                （``policy`` 退化成"没有回调时的缺省"）；回调漏答的学生
                作为**跳过**（最保守）；回调抛异常 → 整批**跳过**，不写盘。

        返回：
            ``{"overwritten": n, "inserted": m, "skipped": k}`` —— 单位是**行**：
            * ``overwritten``：删掉的旧行数（覆盖成功才计）
            * ``inserted``：真正写进去的新行数
            * ``skipped``：因为策略是跳过而**没写**的行数

        ★ 本方法**不弹任何界面**，也不做任何算术 —— 覆盖/跳过是唯一两种可能，
        都由调用方决定。
        """
        term = _clean_text(semester)
        if not term:
            raise ValueError("semester 不能为空")
        batch = _clean_text(batch_id)
        if not batch:
            raise ValueError("batch_id 不能为空")
        if policy not in (USE_OVERWRITE, USE_SKIP):
            raise ValueError(f"未知的覆盖策略：{policy!r}")

        cleaned = self._clean_records(records, term, batch)

        # 姓名映射：**只喂给冲突弹窗**（表里没有这一列，不进磁盘）
        names: dict[str, str] = {}
        for record in records or ():
            if not isinstance(record, dict):
                continue
            sid = _clean_text(record.get("student_id"))
            nm = _clean_text(record.get("student_name")) or _clean_text(record.get("student"))
            if sid and nm and sid not in names:
                names[sid] = nm

        stored = self._payload().get("rows")
        rows: list[dict[str, Any]] = [r for r in stored if isinstance(r, dict)] \
            if isinstance(stored, list) else []

        index: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = _rest_key(row)
            if all(key) and key[1] == term:
                index[key] = row

        # 这批涉及的学生，按第一次出现的顺序（界面问询的顺序 = 表里的顺序）
        order: list[str] = []
        for record in cleaned:
            sid = record["student_id"]
            if sid not in order:
                order.append(sid)
        per_student: dict[str, list[dict[str, Any]]] = {sid: [] for sid in order}
        for record in cleaned:
            per_student[record["student_id"]].append(record)

        conflicted = [sid for sid in order if any((sid, term, r["course_name"]) in index
                                                 for r in per_student[sid])]

        # 每个冲突学生**这个学期已经存了几条** —— 弹窗上要显示的数字。
        # 必须是"全部已有行"而不是"这一批撞上的那几行"：后者会让
        # "我上学期的成绩还剩几条"看不出来，而覆盖删的正是全部同键旧行。
        existing_counts = {
            sid: sum(1 for row in rows
                     if all(_rest_key(row)) and _rest_key(row)[0] == sid
                     and _rest_key(row)[1] == term)
            for sid in conflicted
        }

        decisions = self._decide(conflicted, per_student, term, policy, on_conflict,
                                 names, existing_counts)

        overwritten = 0
        inserted = 0
        skipped = 0
        removed: set[int] = set()
        #: 逐学生的计数 —— 审计日志要按「学生 + 学期」记，而不是只记一个总数
        per_overwritten: dict[str, int] = {}
        per_inserted: dict[str, int] = {}

        def drop(key: tuple[str, str, str]) -> bool:
            """把同键的旧行标记为**删掉**（覆盖时才用）—— 删旧写新。"""
            row = index.pop(key, None)
            if row is None:
                return False
            removed.add(id(row))
            return True

        def has(key: tuple[str, str, str]) -> bool:
            """同键有没有旧行（**只看不删**）。

            ★ 跳过分支必须是这个，**不能**复用 ``drop``：第一版两处都调 ``drop``，
              于是"跳过"把旧行也一并从 kept 里剔掉了 —— 界面上表现为
              "选了跳过之后这个学生这一学期的成绩全没了"，而那正是跳过要避免的事
              （跳过 = 保留旧记录）。只有真数据能暴露它，返回值（skipped=1）
              看起来完全正常。
            """
            return key in index

        for sid in order:
            # ★ 缺省必须是 policy 而不是 USE_SKIP：**没有冲突的学生**根本不进
            #   decisions（没什么可问的），他们本来就该按 policy 正常入库。
            #   第一版这里写成 USE_SKIP，于是"无冲突的一次导入"一行都写不进去，
            #   而且返回全 0 —— 只有真数据才能发现，靠读代码看不出来。
            if decisions.get(sid, policy) == USE_OVERWRITE:
                for record in per_student[sid]:
                    if drop((sid, term, record["course_name"])):
                        overwritten += 1
                        per_overwritten[sid] = per_overwritten.get(sid, 0) + 1
                for record in per_student[sid]:
                    rows.append(dict(record))
                    inserted += 1
                    per_inserted[sid] = per_inserted.get(sid, 0) + 1
            else:
                # ★ 跳过 = "保留旧记录、该行不导入"。作用范围是**这一行（键）**，
                #   不是"这个学生整批作废"：同一批里**没有旧记录的新课**照常写进去，
                #   否则补考/重修那种"老课带一门新课"的表会被整批丢掉 —— 那才是真丢数据。
                for record in per_student[sid]:
                    if has((sid, term, record["course_name"])):
                        skipped += 1
                    else:
                        rows.append(dict(record))
                        inserted += 1
                        per_inserted[sid] = per_inserted.get(sid, 0) + 1

        kept = rows if not removed else [row for row in rows if id(row) not in removed]
        # 写盘失败（没登录 / 库坏了）时**绝不能撒谎说写成功了**：
        # 让 CryptoError 如实抛给调用方，界面那边只记日志、不打断用户看成绩。
        self._write(kept)
        # ★ 成绩**先落地**，再记日志：写盘抛了就不该留下"我改过"的记录。
        self._log_changes(order, term, per_overwritten, per_inserted)
        return {"overwritten": overwritten, "inserted": inserted, "skipped": skipped}

    def clear(self) -> int:
        """清空这张表（**只动 grade_history**，班级快照 / 归档一个字不碰）。

        Returns:
            删掉的行数（本来空着就是 ``0`` —— 界面靠它区分"清完了"和"本来就是空的"）。
        """
        removed = self.count()
        self._write([])
        if removed:
            self._audit_record(OP_DELETE, count=removed)
        return removed

    def delete_record(self, student_id: str, semester: str, course_name: str) -> int:
        """删掉**一条**成绩（主键三元组）。

        Returns:
            真删掉的行数（``0`` = 本来就没有这一条，不算错）。
        """
        sid = _clean_text(student_id)
        term = _clean_text(semester)
        course = _clean_text(course_name)
        if not (sid and term and course):
            raise ValueError("student_id / semester / course_name 都不能为空")
        rows = self._raw_rows()
        kept = [row for row in rows if _rest_key(row) != (sid, term, course)]
        removed = len(rows) - len(kept)
        if removed:
            self._write(kept)
            self._audit_record(OP_DELETE, student_id=sid, semester=term,
                               course_name=course, count=removed)
        return removed

    def delete_semester(self, student_id: str, semester: str) -> int:
        """删掉一个学生**一个学期**的全部记录（别的学期一条不动）。

        Returns:
            真删掉的行数（``0`` = 本来就没有，不算错）。
        """
        sid = _clean_text(student_id)
        term = _clean_text(semester)
        if not (sid and term):
            raise ValueError("student_id / semester 都不能为空")
        rows = self._raw_rows()
        kept = [row for row in rows
                if not (_rest_key(row)[0] == sid and _rest_key(row)[1] == term)]
        removed = len(rows) - len(kept)
        if removed:
            self._write(kept)
            self._audit_record(OP_DELETE, student_id=sid, semester=term, count=removed)
        return removed

    def rekey_student(self, old_id: str, new_id: str) -> tuple[int, int]:
        """把一个人的全部成绩行改挂到新学号上（v1.9.2「改学号」迁移的成绩部分）。

        ★ 只管 ``grade_history``；侧写 / 学期侧写 / 聊天历史 / 德育分 / 意愿标签
          由 :func:`migrate_student_id` 统一处理 —— 少迁一样就是静默丢数据。

        ★ 撞主键时的取舍：新学号上**已经有** ``(学期, 课程名)`` 那一行时，保留
          新学号原有的那条、丢弃旧学号的重复记录（不让旧数据覆盖新数据），
          并计入第二个返回值。**绝不把同一条成绩存两份。**

        Returns:
            ``(改挂过去的行数, 因主键重复而丢弃的行数)``
        """
        old = _clean_text(old_id)
        new = _clean_text(new_id)
        if not (old and new):
            raise ValueError("old_id / new_id 都不能为空")
        if old == new:
            return (0, 0)
        rows = self._raw_rows()
        existing = {_rest_key(row) for row in rows
                    if _rest_key(row)[0] == new}
        moved = 0
        dropped = 0
        kept: list[dict[str, Any]] = []
        for row in rows:
            if _rest_key(row)[0] != old:
                kept.append(row)
                continue
            key = (new, _rest_key(row)[1], _rest_key(row)[2])
            if key in existing:
                dropped += 1
                continue
            rewritten = dict(row)
            rewritten["student_id"] = new
            kept.append(rewritten)
            existing.add(key)
            moved += 1
        if moved or dropped:
            self._write(kept)
            self._audit_record(OP_STUDENT_MIGRATE, student_id=new, count=moved,
                               detail=f"学号 {old} → {new}；成绩 {moved} 条"
                                      + (f"，撞键丢弃 {dropped} 条" if dropped else ""))
        return (moved, dropped)

    def rename_student(self, old_id: str, new_id: str) -> int:
        """把某人的成绩行改挂到新学号（任务书 v1.9.2 的公开名字）。

        :meth:`rekey_student` 的薄封装，只返回实际改挂的行数（撞主键被丢弃的不算）。

        Args:
            old_id: 旧学号（``2023001`` / ``sid:2023001`` 都行）
            new_id: 新学号（同上）

        Returns:
            实际改挂到新学号下的行数；旧学号本来没有成绩 → ``0``。

        Raises:
            ValueError: 新旧学号有空串时。
        """
        moved, _dropped = self.rekey_student(old_id, new_id)
        return moved

    # -- 审计日志 -----------------------------------------------------------
    def audit(self) -> "GradeAuditLog":
        """这张表的审计日志（同一个用户）。只读查询走它。"""
        return GradeAuditLog(username=self._username, uid=self._uid, store=self._store)

    def changes(self, student_id: str | None = None, semester: str | None = None,
                limit: int | None = None) -> list[dict[str, Any]]:
        """审计日志的只读查询（等价于 ``self.audit().changes(...)``）。"""
        return self.audit().changes(student_id=student_id, semester=semester, limit=limit)

    def _audit_record(self, operation: str, *, student_id: str = "", semester: str = "",
                      course_name: str = "", count: int = 1,
                      detail: str = "") -> None:
        """记一条变更日志（**失败只警告，绝不把写入带崩**）。"""
        self.audit().record(operation, student_id=student_id, semester=semester,
                            course_name=course_name, count=count, detail=detail)

    def _log_changes(self, order: list[str], term: str,
                     per_overwritten: dict[str, int],
                     per_inserted: dict[str, int]) -> None:
        """把这一批的动向按学生记成日志（**一次写盘**，不按学生反复读写）。"""
        entries: list[dict[str, Any]] = []
        for sid in order:
            over = per_overwritten.get(sid, 0)
            new = per_inserted.get(sid, 0)
            if over:
                entries.append({"operation": OP_OVERWRITE, "student_id": sid,
                                "semester": term, "count": over})
            if new:
                entries.append({"operation": OP_INSERT, "student_id": sid,
                                "semester": term, "count": new})
        if entries:
            self.audit().record_many(entries)

    def _raw_rows(self) -> list[dict[str, Any]]:
        """磁盘上那份原始行（**不做键校验、不排序**）。

        ★ 删除必须基于原始行：一行如果键不全（脏数据），``rows()`` 会把它藏起来，
          但删的时候要是看不见它，就会"删了 3 条、磁盘上还剩 1 条"。
        """
        stored = self._payload().get("rows")
        if not isinstance(stored, list):
            return []
        return [row for row in stored if isinstance(row, dict)]

    # -- 内部 ---------------------------------------------------------------
    def _clean_records(self, records: list[dict[str, Any]], term: str,
                       batch: str) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for record in records or []:
            if not isinstance(record, dict):
                continue
            sid = _clean_text(record.get("student_id"))
            course = _clean_text(record.get("course_name"))
            if not sid or not course:
                continue
            # ★ 一批**只能属于一个学期**：以参数 semester 为准盖进每一行
            #   （同一次导入里本来就只有一个学期，见 ui/pages/analysis_page）。
            cleaned.append({
                "student_id": sid,
                "semester": term,
                "course_name": course,
                "score": _optional_number(record.get("score")),
                "credit": _optional_number(record.get("credit")),
                "import_batch_id": batch,
                "imported_at": _clean_text(record.get("imported_at")) or _now(),
            })
        return cleaned

    def _decide(self, conflicted: list[str], per_student: dict[str, list[dict[str, Any]]],
                term: str, policy: str, on_conflict: ConflictChooser | None,
                names: dict[str, str] | None = None,
                existing_counts: dict[str, int] | None = None,
                ) -> dict[str, str]:
        if not conflicted:
            return {}
        if on_conflict is None:
            return {sid: policy for sid in conflicted}
        shown = names or {}
        counts = existing_counts or {}
        try:
            answer = on_conflict([
                ConflictQuestion(
                    sid,
                    shown.get(sid) or sid,
                    term,
                    # 已有条数：优先用"这个学期全部已有行"，
                    # 没有那份账时退回"这一批撞上的门数"（信息量小一点但不会撒谎）
                    counts.get(sid, len(per_student[sid])),
                )
                for sid in conflicted
            ])
        except Exception:  # 回调是界面代码，任何异常都不能把数据写坏
            return {sid: USE_SKIP for sid in conflicted}
        if not isinstance(answer, dict):
            return {sid: USE_SKIP for sid in conflicted}
        decisions: dict[str, str] = {}
        for sid in conflicted:
            choice = answer.get(sid, USE_SKIP)
            decisions[sid] = USE_OVERWRITE if choice == USE_OVERWRITE else USE_SKIP
        return decisions

    def _payload(self) -> dict[str, Any]:
        payload = load_user_json(self._store, self._username, self.filename, None)
        if isinstance(payload, dict):
            return payload
        return {"table": self.table, "version": 1, "rows": []}

    def _write(self, rows: list[dict[str, Any]]) -> None:
        save_user_json(self._store, self._username, self.filename, {
            "table": self.table,
            "version": 1,
            "rows": rows,
        })


# ---------------------------------------------------------------------------
# 成绩变更审计日志
# ---------------------------------------------------------------------------


class GradeAuditLog:
    """``grade_audit`` 表：一行 = 一次成绩变动（**只追加**）。

    落盘位置：``<user_dir>/audit/grade_changes.json.enc``（应用级加密）。

    ★ **只记变动本身，不记分数**：操作时间 / 操作类型 / 学生 ID / 学期 /
      课程名 / 动了几个。分数一概不写 —— 审计日志不是第二份成绩库。

    ★ **日志坏了不许拦住主流程**：

    * 写不进去（没登录 / 库坏了 / 文件权限）→ 记一条 :mod:`logging` 警告，
      ``record()`` 返回 ``None``，**不抛**（成绩本身已经存好了，
      为了记日志把用户那次导入整批作废才是真的坏事）；
    * 读不出来（文件损坏 / 解不开）→ :meth:`changes` 返回空列表，**不抛**。
    """

    #: 表名
    table = TABLE_GRADE_AUDIT
    #: 载荷文件名（带子目录）
    filename = AUDIT_FILE

    def __init__(self, *, username: str | None = None, uid: str | None = None,
                 store: CryptoStore | None = None) -> None:
        self._store = store if store is not None else CryptoStore()
        self._username = username or current_username()
        self._uid = uid if uid is not None else resolve_uid(self._store, self._username)
        #: 最近一次失败的原因（"降级成警告"之后，排查时还看得到）
        self._last_error = ""

    # -- 身份 ---------------------------------------------------------------
    @property
    def store(self) -> CryptoStore:
        return self._store

    @property
    def username(self) -> str:
        return self._username

    @property
    def uid(self) -> str | None:
        return self._uid

    @property
    def last_error(self) -> str:
        """最近一次写盘失败的原因（成功过就是空串）。"""
        return self._last_error

    # -- 写 -----------------------------------------------------------------
    def record(self, operation: str, *, student_id: str = "", semester: str = "",
               course_name: str = "", count: int = 1,
               detail: str = "") -> dict[str, Any] | None:
        """记一条。返回真正写进去的那一条；写不进去返回 ``None``（**不抛**）。

        ``detail`` 是给人看的补充说明（v1.9.2 起，例如改学号时的旧学号与
        迁移了几类数据）。★ **绝不写分数** —— 它只是操作说明。
        """
        written = self.record_many([{
            "operation": operation,
            "student_id": student_id,
            "semester": semester,
            "course_name": course_name,
            "count": count,
            "detail": detail,
        }])
        return written[0] if written else None

    def record_student(self, operation: str, *, student_id: str = "",
                       detail: str = "", count: int = 1) -> dict[str, Any] | None:
        """记一条**学生主档**操作（内部走 :meth:`record_many`）。

        Args:
            operation: 只认 :data:`STUDENT_OPS`（student_add / student_edit /
                student_archive / student_restore / student_delete / student_migrate）
            student_id: 学生键（``sid:<学号>``）
            detail: 操作说明（最多 :data:`AUDIT_DETAIL_MAX` 字，**严禁写分数**）
            count: 这一次动了几条

        Returns:
            写进去的那一条；类型不认识或写盘失败 → ``None``（**不抛**）。
        """
        if _clean_text(operation) not in STUDENT_OPS:
            _LOGGER.warning("学生审计操作类型不认识（已忽略）：%s", operation)
            return None
        return self.record(operation, student_id=student_id, detail=detail,
                           count=count)

    def record_many(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """一次写盘记多条（一批导入几百个学生时别反复读写同一个文件）。

        非法条目（操作类型不认识）**直接丢掉**，其余照记。
        """
        rows: list[dict[str, Any]] = []
        for entry in entries or ():
            if not isinstance(entry, dict):
                continue
            operation = _clean_text(entry.get("operation"))
            if operation not in AUDIT_OPERATIONS:
                continue
            rows.append({
                "at": _clean_text(entry.get("at")) or _now(),
                "operation": operation,
                "student_id": _clean_text(entry.get("student_id")),
                "semester": _clean_text(entry.get("semester")),
                "course_name": _clean_text(entry.get("course_name")),
                "count": _count_of(entry.get("count")),
                "detail": _detail_text(entry.get("detail")),
            })
        if not rows:
            return []
        try:
            stored = self._stored_rows()
            stored.extend(rows)
            if len(stored) > AUDIT_MAX_ENTRIES:
                # 只丢最旧的，绝不因为"太长了"整份丢掉
                stored = stored[-AUDIT_MAX_ENTRIES:]
            self._write(stored)
        except Exception as exc:  # noqa: BLE001 - 日志失败不许影响成绩写入
            self._last_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning("成绩审计日志没写进去（已忽略）：%s", self._last_error)
            return []
        self._last_error = ""
        return rows

    # -- 读 -----------------------------------------------------------------
    def changes(self, student_id: str | None = None, semester: str | None = None,
                limit: int | None = None) -> list[dict[str, Any]]:
        """只读查询，**最新的在前**。

        Args:
            student_id: 只看这个学生（``sid:<学号>``）；``None`` = 不过滤
            semester: 只看这个学期；``None`` = 不过滤
            limit: 最多返回几条；``None`` = 全部

        ★ 日志文件损坏 / 读不到 → 返回 ``[]``（不抛）—— 界面上"没有日志"和
          "日志坏了"都不该把成绩页带崩。
        """
        rows = self._stored_rows()
        want_id = None if student_id is None else _clean_text(student_id)
        want_term = None if semester is None else _clean_text(semester)
        out: list[dict[str, Any]] = []
        for row in rows:
            if want_id is not None and row["student_id"] != want_id:
                continue
            if want_term is not None and row["semester"] != want_term:
                continue
            out.append(row)
        out.reverse()                       # 追加序 → 最新在前
        if limit is not None:
            try:
                cap = int(limit)
            except (TypeError, ValueError):
                cap = 0
            out = out[:max(0, cap)]
        return out

    def count(self) -> int:
        """日志有几条（读不到就是 0）。"""
        return len(self._stored_rows())

    # -- 内部 ---------------------------------------------------------------
    def _stored_rows(self) -> list[dict[str, Any]]:
        """磁盘上那份原始行（**坏的条目跳过**，读不到就是空列表）。"""
        payload = load_user_json(self._store, self._username, self.filename, None)
        if not isinstance(payload, dict):
            return []
        stored = payload.get("rows")
        if not isinstance(stored, list):
            return []
        rows: list[dict[str, Any]] = []
        for entry in stored:
            if not isinstance(entry, dict):
                continue
            operation = _clean_text(entry.get("operation"))
            if operation not in AUDIT_OPERATIONS:
                continue
            rows.append({
                "at": _clean_text(entry.get("at")),
                "operation": operation,
                "student_id": _clean_text(entry.get("student_id")),
                "semester": _clean_text(entry.get("semester")),
                "course_name": _clean_text(entry.get("course_name")),
                "count": _count_of(entry.get("count")),
                "detail": _detail_text(entry.get("detail")),
            })
        return rows

    def _write(self, rows: list[dict[str, Any]]) -> None:
        save_user_json(self._store, self._username, self.filename, {
            "table": self.table,
            "version": 1,
            "rows": rows,
        })


# ---------------------------------------------------------------------------
# 个人档案表（AI 侧写正文）
# ---------------------------------------------------------------------------


class ProfileStore:
    """``student_profiles`` 表：一行 = 一个学生，目前只放 AI 侧写的正文。

    落盘位置：``<user_dir>/student_profiles.json.enc``（应用级加密）。
    """

    #: 表名
    table = TABLE_STUDENT_PROFILES
    #: 载荷文件名
    filename = PROFILE_FILE

    def __init__(self, *, username: str | None = None, uid: str | None = None,
                 store: CryptoStore | None = None) -> None:
        self._store = store if store is not None else CryptoStore()
        self._username = username or current_username()
        self._uid = uid if uid is not None else resolve_uid(self._store, self._username)
        self._cache: dict[str, dict[str, Any]] = self._load()

    # -- 身份 ---------------------------------------------------------------
    @property
    def store(self) -> CryptoStore:
        return self._store

    @property
    def username(self) -> str:
        return self._username

    @property
    def uid(self) -> str | None:
        return self._uid

    # -- 读 -----------------------------------------------------------------
    def side_text(self, student_id: str) -> str:
        """侧写正文；没有存过（或存的是空串）就返回 ``""``。"""
        row = self._cache.get(_clean_text(student_id))
        if not isinstance(row, dict):
            return ""
        text = row.get("side_text")
        return "" if text is None else str(text)

    def all_profiles(self) -> dict[str, dict[str, Any]]:
        """整张表的副本（改它不会影响库里的内容）。"""
        return {sid: dict(row) for sid, row in self._cache.items() if isinstance(row, dict)}

    def count(self) -> int:
        """表里有几行（**侧写正文为空的也算一行** —— 磁盘上确实有这条学生记录）。"""
        return len(self._cache)

    def has_any(self) -> bool:
        """这张表里有没有学生（等价于 ``count() > 0``）。"""
        return bool(self._cache)

    # -- 写 -----------------------------------------------------------------
    def save_side_text(self, student_id: str, text: str) -> None:
        """写一个学生的侧写正文（**先写盘、成功了才更新内存**，避免假成功）。"""
        sid = _clean_text(student_id)
        if not sid:
            raise ValueError("student_id 不能为空")
        rows = self._rows_copy()
        row = rows.get(sid) or {}
        row.update({
            "student_id": sid,
            "side_text": "" if text is None else str(text),
            "updated_at": _now(),
        })
        rows[sid] = row
        self._write(rows)

    def clear_side_text(self, student_id: str) -> bool:
        """抹掉一个学生的侧写正文，**保留这一行**（他在档案里仍然存在）。

        Returns:
            ``True`` 表示真抹掉了非空正文；``False`` 表示本来就没有正文可抹。
        """
        sid = _clean_text(student_id)
        if not sid:
            raise ValueError("student_id 不能为空")
        row = self._cache.get(sid)
        if not isinstance(row, dict) or not str(row.get("side_text") or ""):
            return False
        rows = self._rows_copy()
        row = dict(rows[sid])
        row["side_text"] = ""
        row["updated_at"] = _now()
        rows[sid] = row
        self._write(rows)
        return True

    # -- 学期侧写（v1.9.2） ---------------------------------------------------
    def semester_sides(self, student_id: str) -> dict[str, str]:
        """一个学生的**按学期**侧写（``{学期: 正文}``；没有就是空 dict）。"""
        row = self._cache.get(_clean_text(student_id))
        if not isinstance(row, dict):
            return {}
        stored = row.get("semester_side")
        if not isinstance(stored, dict):
            return {}
        return {str(term): "" if text is None else str(text)
                for term, text in stored.items() if str(text or "")}

    def semester_side_text(self, student_id: str, semester: str) -> str:
        """该生某学期的侧写正文；没有存过就是 ``""``。"""
        term = _clean_text(semester)
        if not term:
            return ""
        return self.semester_sides(student_id).get(term, "")

    def save_semester_side_text(self, student_id: str, semester: str, text: str) -> None:
        """写该生某学期的侧写正文（先写盘、成功了才更新内存）。"""
        sid = _clean_text(student_id)
        term = _clean_text(semester)
        if not sid:
            raise ValueError("student_id 不能为空")
        if not term:
            raise ValueError("semester 不能为空")
        rows = self._rows_copy()
        row = dict(rows.get(sid) or {})
        stored = dict(row.get("semester_side") or {}) if isinstance(
            row.get("semester_side"), dict) else {}
        stored[term] = "" if text is None else str(text)
        row.update({
            "student_id": sid,
            "semester_side": stored,
            "updated_at": _now(),
        })
        rows[sid] = row
        self._write(rows)

    def move_student(self, old_id: str, new_id: str) -> bool:
        """把一个人的侧写（含学期侧写）改挂到新学号上（v1.9.2 改学号迁移）。

        ★ 新学号上**已经有侧写正文**时保留新的、不覆盖（合并时只补空的那一半）。

        Returns:
            ``True`` 表示真动过；``False`` 表示旧学号本来就没有侧写。
        """
        old = _clean_text(old_id)
        new = _clean_text(new_id)
        if not (old and new) or old == new:
            return False
        source = self._cache.get(old)
        if not isinstance(source, dict):
            return False
        rows = self._rows_copy()
        target = dict(rows.get(new) or {})
        if not str(target.get("side_text") or ""):
            target["side_text"] = str(source.get("side_text") or "")
        merged = dict(target.get("semester_side") or {}) if isinstance(
            target.get("semester_side"), dict) else {}
        for term, text in (source.get("semester_side") or {}).items():
            if isinstance(source.get("semester_side"), dict) and not merged.get(term):
                merged[str(term)] = str(text or "")
        if merged:
            target["semester_side"] = merged
        target["student_id"] = new
        target["updated_at"] = _now()
        rows[new] = target
        rows.pop(old, None)
        self._write(rows)
        return True

    def rename_student(self, old_id: str, new_id: str) -> bool:
        """把某个人的侧写（含学期侧写）改挂到新学号（任务书 v1.9.2 的公开名字）。

        :meth:`move_student` 的薄封装；新学号已有侧写正文时保留新的那份。

        Returns:
            ``True`` = 旧学号本来有侧写、已处理；``False`` = 旧学号没有侧写。
        """
        return self.move_student(old_id, new_id)

    def remove(self, student_id: str) -> bool:
        """删掉一个学生的整行侧写（含学期侧写）。彻底删除学生时用。"""
        sid = _clean_text(student_id)
        if not sid or sid not in self._cache:
            return False
        rows = self._rows_copy()
        rows.pop(sid, None)
        self._write(rows)
        return True

    def clear_all(self) -> int:
        """清空整张表（**这是"删除成绩库"里那个可选勾选的落点** —— 删用户数据必须由用户点过）。

        Returns:
            抹掉了几个学生（``0`` = 本来就没有侧写数据）。
        """
        removed = len(self._cache)
        if removed:
            self._write({})
        return removed

    # -- 内部 ---------------------------------------------------------------
    def _rows_copy(self) -> dict[str, dict[str, Any]]:
        """内存表的可写副本（写盘前在这里改，成功了才换上去）。"""
        return {key: dict(row) for key, row in self._cache.items() if isinstance(row, dict)}

    def _write(self, rows: dict[str, dict[str, Any]]) -> None:
        save_user_json(self._store, self._username, self.filename, {
            "table": self.table,
            "version": 1,
            "profiles": rows,
        })
        self._cache = rows

    def _load(self) -> dict[str, dict[str, Any]]:
        payload = load_user_json(self._store, self._username, self.filename, None)
        if not isinstance(payload, dict):
            return {}
        stored = payload.get("profiles")
        if not isinstance(stored, dict):
            return {}
        return {str(sid): dict(row) for sid, row in stored.items() if isinstance(row, dict)}


# ---------------------------------------------------------------------------
# 「删除成绩库」：先盘点，再清（数据层）
# ---------------------------------------------------------------------------

#: 归档快照的文件名。★ 真源是 ``ui.pages.analysis_page.ARCHIVE_FILE`` ——
#: 但 core **不许 import ui**（依赖方向反了），所以这里只能是同一个字符串。
#: 两个地方一起改；``tests/test_grade_delete.py`` 里有一条断言盯着它俩相等。
ARCHIVE_PAYLOAD_FILE = "archive.json.enc"

#: v1.9 之前的**密码级**成绩载荷：现在的成绩已经进了
#: :class:`GradeHistoryDB`（``grade_history.json.enc``），这两个只剩老库残留要清。
LEGACY_GRADE_FILES = ("grades.enc", "tags.json.enc")

#: 清库时**故意留下**的文件。
#:
#: ``sessions.json.enc`` 是"记住我"的会话令牌 —— v1.9 之前那个实现会顺手把它删掉，
#: 于是"清空成绩库"的副作用是**把用户踢下线**。清成绩不该动登录态，所以留下它。
CLEAR_KEEP_FILES = ("sessions.json.enc",)

#: 侧写/生成物里**不属于成绩库**、清库时绝不碰的：
#: 德育分和名额是用户自己一条条录进去的（不是导入成绩的附带产物），
#: 审计日志更是**这次删除的记录本身**，删掉它等于毁尸灭迹。
CLEAR_NEVER_TOUCH_SUFFIXES = ("_moral.json.enc", "_quota.json.enc", "grade_changes.json.enc")


def _class_payload_names(store: CryptoStore, username: str) -> list[str]:
    """用户目录下的班级快照文件名。

    ★ 排除规则是 AGENTS §15 点名的那条：``_moral`` / ``_quota`` **不是班级文件**
      （班级号只允许字母数字和短横线，所以"以 ``_moral`` 结尾"不会误伤真班级）。
    """
    return [
        name
        for name in list_user_json(store, username)
        if name.startswith("class_")
        and name.endswith(".json.enc")
        and not name.endswith("_moral.json.enc")
        and not name.endswith("_quota.json.enc")
    ]


#: AI 谈话记录的子目录（相对用户目录）—— 按学生一个文件
CHAT_CACHE_DIR = "cache"
#: 聊天缓存文件名前缀（``chat_history_<学号>.json.enc``）
CHAT_CACHE_PREFIX = "chat_history_"


def _chat_cache_names(store: CryptoStore, username: str) -> list[str]:
    """该用户目录下 ``cache/chat_history_*.json.enc`` 的**相对文件名**列表。

    ★ 这些文件不在 ``list_user_json`` 的扫描面上（那个只看顶层），但它们是
      "这个学生的谈话记录"，**删成绩库时必须一起清掉**（v1.9.2 遗留修复 ④）：
      否则界面上成绩没了、AI 辅导里还留着整段谈话。
    """
    base = _user_dir(store, username)
    if base is None:
        return []
    folder = base / CHAT_CACHE_DIR
    if not folder.is_dir():
        return []
    try:
        paths = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.name.startswith(CHAT_CACHE_PREFIX)
                       and p.name.endswith(".json.enc"))
    except OSError:                          # 目录读不了就当没有
        return []
    return [f"{CHAT_CACHE_DIR}/{p.name}" for p in paths]


def library_inventory(store: CryptoStore | None = None, username: str = "") -> dict[str, Any]:
    """**只读**盘点：这个账户的"成绩库"里到底有些什么。

    ★ 这是"删除成绩库"那个按钮误报 bug 的正解 —— 判据必须是
      :class:`GradeHistoryDB` **表里真有没有记录**（``grade_rows``），
      而不是"磁盘上有没有某个文件名"：v1.9 起成绩在
      ``grade_history.json.enc`` 里，旧实现只查 ``grades.enc``/``tags.json.enc``，
      所以导入完成绩照样报"库里本来就是空的"。

    Returns:
        ``{"account", "grade_rows", "profile_rows", "legacy_files",
        "class_files", "archive", "audit_rows", "files", "has_data"}``

        * ``account``：账户在不在（不在时其余全是 0 / 空）
        * ``files``：清库时会删掉的文件名（按顺序、去重）
        * ``has_data``：**有没有可删的东西** —— 界面判"没有成绩库"就看它
        * ``audit_rows``：只报数，**永远不删**
    """
    vault = store if store is not None else CryptoStore()
    empty: dict[str, Any] = {
        "account": False,
        "grade_rows": 0,
        "profile_rows": 0,
        "student_rows": 0,
        "chat_files": 0,
        "legacy_files": [],
        "class_files": [],
        "archive": False,
        "audit_rows": 0,
        "files": [],
        "has_data": False,
    }
    if not username:
        return empty

    db = GradeHistoryDB(username=username, store=vault)
    try:
        grade_rows = db.count()
    except Exception as exc:  # noqa: BLE001 - 盘点失败当作 0，不能把按钮打崩
        _LOGGER.warning("盘点成绩库失败（按 0 算）：%s: %s", type(exc).__name__, exc)
        grade_rows = 0
    profiles = ProfileStore(username=username, store=vault)
    profile_rows = profiles.count()
    audit_rows = db.audit().count()

    def present(name: str) -> bool:
        path = user_file(vault, username, name)
        return bool(path is not None and path.exists())

    chat_names = _chat_cache_names(vault, username)
    legacy = [name for name in LEGACY_GRADE_FILES if present(name)]
    classes = [name for name in _class_payload_names(vault, username) if present(name)]
    archive = present(ARCHIVE_PAYLOAD_FILE)

    # ★ 这里**只列真要 unlink 的文件**：``grade_history.json.enc`` /
    #   ``student_profiles.json.enc`` 是"把表清空"而不是"删文件"（清完文件还在、
    #   只是 0 行），把它们列进来会让"下次进来还有数据"永远成立 ——
    #   那正是这次要修的那类 bug（拿文件在不在当判据）。
    files: list[str] = []
    files.extend(name for name in chat_names if name not in files)
    if archive:
        files.append(ARCHIVE_PAYLOAD_FILE)
    files.extend(name for name in classes if name not in files)
    files.extend(name for name in legacy if name not in files)

    return {
        "account": vault.load_users().find(username) is not None,
        "grade_rows": grade_rows,
        "profile_rows": profile_rows,
        # ★ 学生主档**不算"成绩库"**（AGENTS §37：主档是学生本身，成绩库是成绩）：
        #   只报个数给界面显示，clear_library 不碰它。
        "student_rows": _student_rows(vault, username),
        "chat_files": len(chat_names),
        "legacy_files": legacy,
        "class_files": classes,
        "archive": archive,
        "audit_rows": audit_rows,
        "files": files,
        # ★ "有没有成绩库" = 表里有记录 / 有侧写 / 有生成物与老库残留。
        #   注意**不算 audit_rows**：审计日志自己不是成绩库，否则删干净之后
        #   下一次进来又会说"还有数据"。
        "has_data": bool(grade_rows or profile_rows or files),
    }


def clear_library(store: CryptoStore | None = None, username: str = "",
                  *, with_profiles: bool = True,
                  with_generated: bool = True) -> dict[str, Any]:
    """清掉这个账户的**成绩库**（数据层）。

    ★ 界面负责"问过用户了"；这个函数只管删，而且**返回删了什么**。

    删除范围：

    * ``grade_history`` 全部记录（:meth:`GradeHistoryDB.clear`，会落审计日志）
    * ``student_profiles`` 侧写（``with_profiles=True`` 时；这是一个**勾选项**）
    * ``archive.json.enc`` 归档快照 + ``class_*.json.enc`` 班级快照
      （``with_generated=True`` 时）—— 它们是导入成绩的**生成物**，
      不删的话档案页 / 智育页会继续显示一份"已经删掉的成绩"
    * 老库残留 ``grades.enc`` / ``tags.json.enc``

    **不动**：德育分、名额配置（用户自己录的）、``sessions.json.enc``（登录态）、
    以及 ``audit/grade_changes.json.enc``（这次删除的记录本身）。

    Returns:
        ``{"found", "grade_rows", "profile_rows", "files", "failed", "inventory"}``

        * ``found``：清之前**有没有**数据 —— ``False`` 表示"本无数据"、
          什么都没删（界面靠它区分"清完了"和"本来就没有"）
        * ``failed``：没删掉的（``"文件名（原因）"``），一个都没失败就是空列表
    """
    vault = store if store is not None else CryptoStore()
    before = library_inventory(vault, username)
    report: dict[str, Any] = {
        "found": before["has_data"],
        "grade_rows": 0,
        "profile_rows": 0,
        "files": [],
        "failed": [],
        "inventory": before,
    }
    if not before["account"] or not before["has_data"]:
        return report                       # 本无数据：一个字节都不动

    db = GradeHistoryDB(username=username, store=vault)
    report["grade_rows"] = db.clear()       # ★ 表里真有的记录（含审计日志）

    if with_profiles:
        report["profile_rows"] = ProfileStore(username=username, store=vault).clear_all()

    targets: list[str] = []
    for name in before["files"]:
        if name in (GRADE_HISTORY_FILE, PROFILE_FILE):
            continue                        # 上面已经按表清过了（清在表上，不在文件上）
        if name.startswith(f"{CHAT_CACHE_DIR}/"):
            # 谈话记录属于"侧写那类辅助数据"：勾了清侧写才清它
            if not with_profiles:
                continue
        elif name == ARCHIVE_PAYLOAD_FILE or name.startswith("class_"):
            if not with_generated:
                continue
        targets.append(name)

    for name in targets:
        path = user_file(vault, username, name)
        if path is None or not path.exists():
            continue
        try:
            path.unlink()
            report["files"].append(name)
        except OSError as exc:
            report["failed"].append(f"{name}（{exc}）")

    # 审计：有成绩行时 clear() 自己已经记过一条；这里补齐"没有成绩行、
    # 但确实删掉了侧写 / 生成物 / 老库残留"的情况 —— 一次真删除不该在日志里失踪。
    if report["grade_rows"] == 0 and (report["profile_rows"] or report["files"]):
        db.audit().record(OP_DELETE,
                          count=report["profile_rows"] + len(report["files"]))

    # ★ 报告里的 inventory 是"删完之后"的盘点（键名与 library_inventory 完全一致，
    #   grade_rows 在这里变成 0）—— 界面拿它再判一次"还有没有数据"才不会误报。
    try:
        report["inventory"] = library_inventory(vault, username)
    except Exception as exc:  # noqa: BLE001 - 盘点失败不许让"已删除"变成异常
        _LOGGER.warning("清库后的盘点失败（沿用删除前的快照）：%s: %s",
                        type(exc).__name__, exc)
    return report


# ---------------------------------------------------------------------------
# 学生主档（v1.9.2）
# ---------------------------------------------------------------------------


class DuplicateStudentError(ValueError):
    """学号重复（主档的唯一键是学号，不许两个学生共用一个学号）。"""


def _student_id_of(value: Any) -> str:
    """把 ``sid:2023001`` / ``2023001`` / ``name:张三`` 归一成一个键。

    * 带 ``sid:`` / ``name:`` 前缀的原样保留（那是 :func:`ui.intentions.student_key`
      的约定，本模块不解析它）；
    * 纯数字/字母的学号补上 ``sid:`` 前缀 —— 档案页、聊天文件名都按这个约定存。
    """
    text = _clean_text(value)
    if not text:
        return ""
    if text.startswith(("sid:", "name:")):
        return text
    return f"sid:{text}"


def _bare_student_id(value: Any) -> str:
    """取学号的"裸"部分（聊天文件名用；``name:`` 那种没有学号，返回空串）。"""
    text = _student_id_of(value)
    if text.startswith("sid:"):
        return text[4:].strip()
    return ""


def _user_dir(store: CryptoStore, username: str) -> Any:
    """这个用户的密文目录（``u_<uid>/``）。

    ★ ``user_file()`` 是按文件名算路径的，拿一个不会被用到的名字探出目录 ——
      聊天历史在 ``cache/`` 子目录里，本模块没有别的办法拿到它。
    """
    path = user_file(store, username, "__user_dir__.json.enc")
    return None if path is None else path.parent


def _chat_file(store: CryptoStore, username: str, student_id: str) -> Any:
    """该生的 AI 谈话记录文件（``<user_dir>/cache/chat_history_<学号>.json.enc``）。"""
    bare = _bare_student_id(student_id)
    base = _user_dir(store, username)
    if not bare or base is None:
        return None
    return base / "cache" / f"chat_history_{bare}.json.enc"


def _student_rows(store: CryptoStore, username: str) -> int:
    """主档里有几个学生（**盘点失败当 0，不能把"删除成绩库"按钮打崩**）。"""
    try:
        return StudentStore(username=username, store=store).count()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("盘点学生主档失败（按 0 算）：%s: %s", type(exc).__name__, exc)
        return 0


class StudentStore:
    """``students`` 表：**学生主档**（v1.9.2）。
    落盘位置：``<user_dir>/students.json.enc``（应用级加密）。

    一行 = 一个学生::

        {"student_id": "sid:2023001", "name": "张三", "class_name": "计科2301",
         "note": "", "archived": false, "source": "import", "updated_at": "..."}

    ★ **主档是唯一的学生名单**（AGENTS §37）：成绩历史 / 侧写 / 聊天都按学号挂在这
      里，**不许另立第二份名单**（分析页的名单是导入结果，不是主档）。
    ★ 学号唯一：重复添加抛 :class:`DuplicateStudentError`，绝不静默并入。
    ★ 归档 = 隐藏，不删数据；彻底删除才动关联数据（见 :func:`migrate_student_id`
      与 :meth:`StudentStore.remove` 的说明）。
    """

    #: 表名
    table = TABLE_STUDENTS
    #: 载荷文件名
    filename = STUDENTS_FILE

    def __init__(self, *, username: str | None = None, uid: str | None = None,
                 store: CryptoStore | None = None) -> None:
        self._store = store if store is not None else CryptoStore()
        self._username = username or current_username()
        self._uid = uid if uid is not None else resolve_uid(self._store, self._username)

    # -- 身份 ---------------------------------------------------------------
    @property
    def store(self) -> CryptoStore:
        return self._store

    @property
    def username(self) -> str:
        return self._username

    @property
    def uid(self) -> str | None:
        return self._uid

    # -- 读 -----------------------------------------------------------------
    def all_rows(self) -> list[dict[str, Any]]:
        """整张表（**含归档**），按学号排序。"""
        rows = [dict(row) for row in self._load().values()]
        rows.sort(key=lambda row: (str(row.get("student_id") or ""), str(row.get("name") or "")))
        return rows

    def rows(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        """学生列表，按 ``(archived, class_name, student_id)`` 排好。

        Args:
            include_archived: ``True``（默认）= 连归档的一起返回；
                ``False`` = 只返回在册的（界面常规视图传它）

        Returns:
            行副本列表：在册的在前，归档的排最后、同班的排在一起。
        """
        rows = self.all_rows()
        rows.sort(key=lambda row: (bool(row.get("archived")),
                                   str(row.get("class_name") or ""),
                                   str(row.get("student_id") or "")))
        if include_archived:
            return rows
        return [row for row in rows if not row.get("archived")]

    def get(self, student_id: str) -> dict[str, Any] | None:
        """按学号取一行（**找得到就返回副本**，改它不影响库里的内容）。"""
        key = _student_id_of(student_id)
        row = self._load().get(key)
        return None if row is None else dict(row)

    def has(self, student_id: str) -> bool:
        """这个学号在不在主档里。"""
        return _student_id_of(student_id) in self._load()

    def count(self, *, include_archived: bool = True) -> int:
        """主档里有几个学生。"""
        return len(self.rows(include_archived=include_archived))

    def names(self) -> dict[str, str]:
        """``{学号键: 姓名}``（界面上"这个名字是哪个学号"用）。"""
        return {key: str(row.get("name") or "")
                for key, row in self._load().items() if isinstance(row, dict)}

    def search(self, query: str = "", *, include_archived: bool = False,
               archived_only: bool = False,
               limit: int | None = None) -> list[dict[str, Any]]:
        """搜索学生：姓名**模糊**、学号**前缀**、班级**模糊**，命中任一即算。

        Args:
            query: 关键词；**空串返回全部**（严格按归档过滤）
            include_archived: 是否连归档的一起搜（默认 ``False``）
            archived_only: 只看归档的（传了它就不看 ``include_archived``）
            limit: 最多返回几条；``None`` = 全部

        Returns:
            命中的行（副本），排序同 :meth:`rows`。

        ★ 关键词以「已归档」/「归档」开头时，等价于 ``archived_only=True``，
          并把这几个字从关键词里去掉（可以写成「已归档 张」= 归档里搜张）。
          单独写「已归档」= 列出全部归档的，不会一条都搜不到。
        ★ 大小写不敏感；学号按**前缀**匹配（``2023`` 能搜出 ``2023001``）。
        ★ 归档的学生**默认搜不到** —— 这是"归档 = 从列表拿掉、数据还在"的唯一入口。
        """
        text = _clean_text(query)
        for prefix in ("已归档", "归档"):
            if text.startswith(prefix):
                archived_only = True
                text = text[len(prefix):].strip()
                break
        needle = text.casefold()
        rows = self.rows(include_archived=True)
        if archived_only:
            rows = [row for row in rows if row.get("archived")]
        elif not include_archived:
            rows = [row for row in rows if not row.get("archived")]
        if not needle:
            found = rows
        else:
            found = []
            for row in rows:
                sid = str(row.get("student_id") or "")
                bare = _bare_student_id(sid).casefold()
                name = str(row.get("name") or "").casefold()
                klass = str(row.get("class_name") or "").casefold()
                if (needle in name or bare.startswith(needle)
                        or needle in klass or needle in sid.casefold()):
                    found.append(row)
        if limit is not None:
            found = found[:max(0, int(limit))]
        return found

    def add(self, student_id: str = "", name: str = "", class_name: str = "",
            note: str = "", *, source: str = "manual",
            archived: bool = False) -> dict[str, Any]:
        """新建一个学生（**学号与姓名都必填**），学号重复抛 :class:`DuplicateStudentError`。

        Args:
            student_id: 学号（``2023001`` / ``sid:2023001`` 都行）
            name: 姓名
            class_name: 班级号（可空）
            note: 备注（可空）
            source: 来源，见 :data:`STUDENT_SOURCES`（默认 ``manual``）
            archived: 建档时就归档（默认不归档）

        Returns:
            新建成的那一行（副本）。

        Raises:
            ValueError: 学号或姓名为空（中文提示）。
            DuplicateStudentError: 学号已经在主档里。
        """
        key = _student_id_of(student_id)
        clean_name = _clean_text(name)
        if not key:
            raise ValueError("学号不能为空")
        if not clean_name:
            raise ValueError("姓名不能为空")
        rows = self._load()
        if key in rows:
            existing = rows[key]
            raise DuplicateStudentError(
                f"学号 {_bare_student_id(key) or key} 已经存在"
                f"（{existing.get('name') or '未填姓名'}）")
        stamp = _now()
        row = {
            "student_id": key,
            "name": clean_name,
            "class_name": _clean_text(class_name),
            "note": _clean_text(note),
            "archived": bool(archived),
            "source": _clean_text(source) or "manual",
            "created_at": stamp,
            "updated_at": stamp,
        }
        rows[key] = row
        self._write(rows)
        self._audit(OP_STUDENT_ADD, key, f"新建学生 {clean_name}")
        return dict(row)

    def update(self, sid: str, *, _record: bool = True,
               **changes: Any) -> dict[str, Any]:
        """改一个学生的字段：**只改传进来的那几个**（``None`` 一律视为"没传"）。

        可改：``name`` / ``class_name`` / ``note`` / ``archived`` / ``source``。

        Args:
            student_id: 学号
            _record: 内部用 —— 归档/恢复要记自己的操作类型，别多记一条"修改"
            changes: 要改的字段

        Returns:
            改完的那一行（副本）；一个字段都没变时返回原行（不写盘、不记日志）。

        Raises:
            KeyError: 主档里没有这个学号。
            ValueError: 想借这里改学号（请走 :func:`migrate_student_id`）。
        """
        key = _student_id_of(sid)
        rows = self._load()
        row = rows.get(key)
        if row is None:
            raise KeyError(f"主档里没有学号 {key}")
        if changes.get("student_id") is not None \
                and _student_id_of(changes["student_id"]) != key:
            raise ValueError("改学号请调用 migrate_student_id()，它会把关联数据一起迁走")
        updated = dict(row)
        for field in ("name", "class_name", "note", "source"):
            value = changes.get(field)
            if value is None:
                continue
            text = _clean_text(value)
            if field in ("name", "source") and not text:
                continue                        # 姓名 / 来源不许被空值抹掉
            updated[field] = text
        if changes.get("archived") is not None:
            updated["archived"] = bool(changes["archived"])
        updated["updated_at"] = _now()
        if updated == row:
            return dict(row)
        rows[key] = updated
        self._write(rows)
        if _record:
            self._audit(OP_STUDENT_EDIT, key, "修改学生信息")
        return dict(updated)

    def upsert_import(self, student_id: str = "", name: str = "", class_name: str = "",
                      *, source: str = "import") -> dict[str, Any]:
        """导入成绩时**补档**：没有就建；有就**只补空字段**。

        ★ 不改备注、不改归档状态、不改 ``created_at``；已有非空的姓名 / 班级
          **保留原值**（导入的名单不是主档的权威，手工改过的名字不许被覆盖）。
        ★ ``source`` 只在新建 / 原值为空时写，已有来源保持不变。

        Args:
            student_id: 学号（空 → ``ValueError``）
            name: 姓名
            class_name: 班级号
            source: 新建时的来源（默认 ``import``）

        Returns:
            补档之后的那一行（副本）。

        Raises:
            ValueError: 学号为空。
        """
        key = _student_id_of(student_id)
        if not key:
            raise ValueError("学号不能为空")
        rows = self._load()
        row = rows.get(key)
        if row is None:
            return self.add(student_id=key, name=name, class_name=class_name,
                            source=source)
        updated = dict(row)
        if not _clean_text(updated.get("name")) and _clean_text(name):
            updated["name"] = _clean_text(name)
        if not _clean_text(updated.get("class_name")) and _clean_text(class_name):
            updated["class_name"] = _clean_text(class_name)
        if not _clean_text(updated.get("source")):
            updated["source"] = _clean_text(source) or "import"
        if updated == row:
            return dict(row)
        updated["updated_at"] = _now()
        rows[key] = updated
        self._write(rows)
        return dict(updated)

    def archive(self, student_id: str, archived: bool = True) -> dict[str, Any]:
        """归档 = 从列表里拿掉（**一条数据都不删**）；``archived=False`` = 恢复。

        Args:
            student_id: 学号
            archived: ``True`` 归档 / ``False`` 取消归档

        Returns:
            改完的那一行（副本）。

        Raises:
            KeyError: 主档里没有这个学号。
        """
        row = self.update(student_id, _record=False, archived=bool(archived))
        self._audit(OP_STUDENT_ARCHIVE if archived else OP_STUDENT_RESTORE,
                    row["student_id"], "归档隐藏" if archived else "取消归档")
        return row

    def restore(self, student_id: str) -> dict[str, Any]:
        """把归档的学生放回列表（等价于 ``archive(student_id, archived=False)``）。"""
        return self.archive(student_id, archived=False)

    def rename(self, old_id: str, new_id: str, *, name: str | None = None) -> dict[str, Any]:
        """改主档里的学号（**只动主档这一行**；关联数据见 :func:`migrate_student_id`）。"""
        old = _student_id_of(old_id)
        new = _student_id_of(new_id)
        if not new:
            raise ValueError("新学号不能为空")
        rows = self._load()
        row = rows.get(old)
        if row is None:
            raise KeyError(f"主档里没有学号 {old}")
        if new != old and new in rows:
            raise DuplicateStudentError(f"学号 {_bare_student_id(new) or new} 已经存在")
        updated = dict(row)
        updated["student_id"] = new
        if name is not None and _clean_text(name):
            updated["name"] = _clean_text(name)
        updated["updated_at"] = _now()
        if new != old:
            rows.pop(old, None)
        rows[new] = updated
        self._write(rows)
        return dict(updated)

    def remove(self, student_id: str) -> bool:
        """从主档里删掉一行（**只删这一行**）。

        ★ 界面上的「彻底删除」要**先**清掉这个人的成绩/侧写/聊天/德育分/意愿，
          再调这里 —— 否则就留下一堆没有主人的数据。
        """
        key = _student_id_of(student_id)
        rows = self._load()
        row = rows.pop(key, None)
        if row is None:
            return False
        self._write(rows)
        self._audit(OP_STUDENT_DELETE, key, f"删除学生 {row.get('name') or ''}".strip())
        return True

    def clear(self) -> int:
        """清空主档（**只在"删库"这类用户明确点过的场景用**）。"""
        rows = self._load()
        removed = len(rows)
        if removed:
            self._write({})
        return removed

    # -- 内部 ---------------------------------------------------------------
    def _audit(self, operation: str, student_id: str, detail: str = "") -> None:
        try:
            GradeAuditLog(username=self._username, uid=self._uid,
                          store=self._store).record(
                operation, student_id=student_id, count=1, detail=detail)
        except Exception as exc:  # noqa: BLE001 - 日志失败不许拦住主流程
            _LOGGER.warning("学生主档审计日志没写进去（已忽略）：%s: %s",
                            type(exc).__name__, exc)

    def _load(self) -> dict[str, dict[str, Any]]:
        payload = load_user_json(self._store, self._username, self.filename, None)
        if not isinstance(payload, dict):
            return {}
        stored = payload.get("rows")
        if isinstance(stored, dict):
            return {str(key): dict(row) for key, row in stored.items()
                    if isinstance(row, dict)}
        if isinstance(stored, list):          # 容忍早期写成列表的载荷
            out: dict[str, dict[str, Any]] = {}
            for row in stored:
                if isinstance(row, dict):
                    key = _student_id_of(row.get("student_id"))
                    if key:
                        out[key] = dict(row)
            return out
        return {}

    def _write(self, rows: dict[str, dict[str, Any]]) -> None:
        save_user_json(self._store, self._username, self.filename, {
            "table": self.table,
            "version": 1,
            "rows": rows,
        })


class StudentMigrationReport:
    """改学号迁移的结果（``as_text()`` 直接给界面弹窗用）。

    Attributes:
        student / grades / profiles / chats / moral / intentions: 各类数据迁了几条
            （``student`` 是 0/1，主档只可能迁一份）
        files: 因撞名而**改名保留**的旧文件（``...bak``），不删用户数据
        grade_dropped: 旧成绩与新学号已有记录撞主键而丢弃的条数
    """

    def __init__(self, *, old_id: str, new_id: str, student: int = 0,
                 grades: int = 0, grade_dropped: int = 0, profiles: int = 0,
                 chats: int = 0, moral: int = 0, intentions: int = 0,
                 files: list[str] | None = None) -> None:
        self.old_id = old_id
        self.new_id = new_id
        self.student = int(student)
        self.grades = int(grades)
        self.grade_dropped = int(grade_dropped)
        self.profiles = int(profiles)
        self.chats = int(chats)
        self.moral = int(moral)
        self.intentions = int(intentions)
        self.files = list(files or [])

    def total(self) -> int:
        """一共迁了多少项（成绩按条算，其余按"类"算）。"""
        return self.student + self.grades + self.profiles + self.chats \
            + self.moral + self.intentions

    def as_text(self) -> str:
        """中文一句话（界面弹窗直接用；**不含** Markdown 记号、不含分数）。

        Returns:
            形如「学号 2023001 → 2023099；已迁移：成绩 12 条、侧写 1 份、聊天 1 份、
            德育分 2 个班、意愿 1 条」。
        """
        old = _bare_student_id(self.old_id) or self.old_id
        new = _bare_student_id(self.new_id) or self.new_id
        moved: list[str] = []
        if self.student:
            moved.append("学生主档")
        if self.grades:
            moved.append(f"成绩 {self.grades} 条")
        if self.profiles:
            moved.append("侧写 1 份")
        if self.chats:
            moved.append(f"聊天 {self.chats} 份")
        if self.moral:
            moved.append(f"德育分 {self.moral} 个班")
        if self.intentions:
            moved.append("意愿 1 条")
        text = f"学号 {old} → {new}；已迁移：" + (
            "、".join(moved) if moved else "无关联数据")
        if self.grade_dropped:
            text += f"；另有 {self.grade_dropped} 条旧成绩与新学号重复，已保留新学号那份"
        if self.files:
            text += "；旧文件已改名保留：" + "、".join(self.files)
        return text


def _rename_key(payload: dict[str, Any], old_key: str, new_key: str) -> bool:
    """把 ``{key: value}`` 表里的键改掉（新键已有值时不覆盖，保留新的那份）。"""
    if old_key not in payload:
        return False
    if new_key in payload:
        return False
    payload[new_key] = payload.pop(old_key)
    return True


def _rename_nested(payload: Any, field: str, old_key: str, new_key: str) -> bool:
    """把 ``{field: {key: value}}`` 载荷里那一层的键改掉（德育分 / 意愿都是这个形状）。

    ★ 真源的形状写在 :func:`core.scholarship.save_moral`（``{"scores": …}``）与
      :func:`ui.intentions.load_intentions`（``{"intentions": …}``）里 ——
      **别在顶层找键**，那样永远迁不动，而且不报错。
    """
    if not isinstance(payload, dict):
        return False
    table = payload.get(field)
    if not isinstance(table, dict):
        return False
    if not _rename_key(table, old_key, new_key):
        return False
    payload[field] = table
    return True


def migrate_student_id(old_id: str, new_id: str, *, store: CryptoStore | None = None,
                       username: str = "", uid: str | None = None,
                       migrate_histories: bool = True
                       ) -> StudentMigrationReport:
    """改学号：**主档 + 成绩 + 侧写/学期侧写 + 聊天记录 + 德育分 + 意愿标签**一起迁。

    ★ 少迁一样就是静默丢数据（AGENTS §37）—— 所以这个函数把六处一次做完，
      任何一处失败**只记警告、继续往下迁**，最后用 :meth:`StudentMigrationReport.as_text`
      如实汇报；返回值只反映**真迁成功**的部分。

    ★ 撞键的取舍：新学号上已有的数据**优先保留**，旧的那份丢弃（成绩计入
      ``grade_dropped``），绝不把同一条数据存两份。
    ★ 聊天历史文件撞名时不删旧文件：**改名成 ``<原名>.bak`` 留在同目录**并记进
      ``report.files`` —— 用户的数据一个字节都不许凭空消失。

    Args:
        old_id: 旧学号（``2023001`` / ``sid:2023001`` 都行）
        new_id: 新学号（同上；归一后与旧学号相同 → 什么都不做）
        store: 加密库；``None`` = 默认库
        username: 用户名；空串 = :func:`current_username`
        uid: 用户 uid（省一次查表）；``None`` = 由用户名解析
        migrate_histories: ``False`` 时跳过**成绩历史**；默认 ``True``。
            生产路径不要传 ``False``。

    Returns:
        :class:`StudentMigrationReport`。

    Raises:
        ValueError: 新旧学号有一个是空串。
    """
    vault = store if store is not None else CryptoStore()
    user = username or current_username()
    report = StudentMigrationReport(old_id=_student_id_of(old_id),
                                    new_id=_student_id_of(new_id))
    if not report.old_id or not report.new_id:
        raise ValueError("新旧学号都不能为空")
    if report.old_id == report.new_id:
        return report

    # 1) 学生主档（旧档不存在就跳过；新档已存在则保留新的，不动）
    try:
        students = StudentStore(username=user, uid=uid, store=vault)
        if students.has(report.old_id) and not students.has(report.new_id):
            students.rename(report.old_id, report.new_id)
            report.student = 1
        elif students.has(report.new_id):
            _LOGGER.warning("改学号：主档里已经有 %s，旧档 %s 不合并",
                            report.new_id, report.old_id)
    except Exception as exc:  # noqa: BLE001 - 一步失败不中断后面的步骤
        _LOGGER.warning("改学号：学生主档没迁成（继续）：%s: %s", type(exc).__name__, exc)

    # 2) 成绩历史
    if migrate_histories:
        try:
            moved, dropped = GradeHistoryDB(username=user, uid=uid,
                                            store=vault).rekey_student(
                report.old_id, report.new_id)
            report.grades = moved
            report.grade_dropped = dropped
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("改学号：成绩历史没迁成（继续）：%s: %s",
                            type(exc).__name__, exc)

    # 3) 侧写（含学期侧写）
    try:
        report.profiles = int(ProfileStore(username=user, uid=uid,
                                           store=vault).move_student(
            report.old_id, report.new_id))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("改学号：侧写没迁成（继续）：%s: %s", type(exc).__name__, exc)

    # 4) AI 谈话记录（按学生一个文件，在 <user_dir>/cache/ 下）
    old_chat = _chat_file(vault, user, report.old_id)
    new_chat = _chat_file(vault, user, report.new_id)
    if old_chat is not None and new_chat is not None and old_chat.exists():
        try:
            new_chat.parent.mkdir(parents=True, exist_ok=True)
            if new_chat.exists():
                # ★ 目标已有记录：保留目标，旧文件改名成 .bak 留在同目录（不删数据）
                backup = old_chat.with_name(old_chat.name + ".bak")
                if backup.exists():
                    stamp = _now().replace(":", "").replace(" ", "_")
                    backup = old_chat.with_name(f"{old_chat.name}.{stamp}.bak")
                old_chat.replace(backup)
                report.files.append(backup.name)
            else:
                old_chat.replace(new_chat)
                report.chats = 1
        except OSError as exc:
            _LOGGER.warning("改学号：聊天记录没迁成（继续）：%s", exc)

    # 5) 德育分（键是 sid:<学号>，一个班一个文件；走 scholarship 的公开 API）
    try:
        from core import scholarship  # noqa: PLC0415 - 晚绑定，避免 import 环

        classes: list[str] = []
        for record in scholarship.list_classes(vault, user):
            class_name = str(getattr(record, "class_name", "") or "")
            if class_name and class_name not in classes:
                classes.append(class_name)
        # ★ 兜底：只有德育分文件、没有班级快照的班（list_classes 扫不到）也要迁 ——
        #   否则"这个班的德育分静默不迁"，而它恰恰是用户手录、丢了就没法复原的数据。
        for name in list_user_json(vault, user, "_moral.json.enc"):
            stem = name.rsplit("_moral.json.enc", 1)[0]
            class_name = stem[len("class_"):] if stem.startswith("class_") else ""
            if class_name and class_name not in classes:
                classes.append(class_name)
        for class_name in classes:
            scores = dict(scholarship.load_moral(vault, user, class_name) or {})
            if _rename_key(scores, report.old_id, report.new_id):
                scholarship.save_moral(vault, user, class_name, scores)
                report.moral += 1
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("改学号：德育分没迁成（继续）：%s: %s", type(exc).__name__, exc)

    # 6) 意愿标签（{"intentions": {"sid:<学号>": "考研"}}）
    try:
        payload = load_user_json(vault, user, "intentions.json.enc", None)
        if _rename_nested(payload, "intentions", report.old_id, report.new_id):
            save_user_json(vault, user, "intentions.json.enc", payload)
            report.intentions = 1
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("改学号：意愿标签没迁成（继续）：%s: %s", type(exc).__name__, exc)

    # 7) 审计：改学号本身留一条（detail 只说迁了哪几类，**不写分数**）
    try:
        GradeAuditLog(username=user, uid=uid, store=vault).record_student(
            OP_STUDENT_MIGRATE, student_id=report.new_id,
            detail=report.as_text(), count=report.total())
    except Exception:  # noqa: BLE001 - 日志失败不许拦住迁移结果
        _LOGGER.warning("改学号：审计日志没写进去（已忽略）", exc_info=True)
    return report


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------


def current_username() -> str:
    """当前登录用户名（**取不到就是空串**）。

    ★ 这里**只读**全局会话，不 import 任何界面模块 —— 本模块要保持零 Qt。

    ★ 仓库里目前**没有** ``core.session_state``（登录态是登录窗自己的局部变量，
      见 ``ui/login_window``），所以这个函数在界面路径上通常返回空串 ——
      界面那边一律显式传 ``username=``（``set_user()`` 给的那个），
      不要指望"自己猜得到当前用户"。测试同理：显式传用户名。
    """
    try:
        from core import session_state
    except ImportError:
        return ""
    for name in ("current_username", "get_current_username", "username"):
        getter = getattr(session_state, name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # pragma: no cover - 会话坏了不该拦住取数
                continue
            if value:
                return str(value)
    return ""


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
