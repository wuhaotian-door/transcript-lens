"""成绩历史与个人档案的**逻辑表 API**（v1.9 第二阶段 D 任务）。

这个模块对外是"两张表"，对内**没有任何数据库引擎** ——
它是纯 JSON 载荷，落到 :mod:`core.user_store` 的**应用级加密存储**上
（文件仍然是 ``*.json.enc``）：

===================  ==========================  ==============================
表                   文件名                      主键
===================  ==========================  ==============================
``grade_history``    ``grade_history.json.enc``  (student_id, semester, course_name)
``student_profiles`` ``student_profiles.json.enc``  student_id
===================  ==========================  ==============================

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

from typing import Any, Callable

from core.crypto_store import CryptoError, CryptoStore, StoreCorruptError
from core.archival import semester_rank
from core.user_store import load_user_json, save_user_json

__all__ = [
    "GRADE_HISTORY_FILE",
    "GRADE_HISTORY_FIELDS",
    "GRADE_HISTORY_PK",
    "PROFILE_FILE",
    "TABLE_GRADE_HISTORY",
    "TABLE_STUDENT_PROFILES",
    "USE_OVERWRITE",
    "USE_SKIP",
    "ConflictChooser",
    "ConflictQuestion",
    "GradeHistoryDB",
    "ProfileStore",
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
                for record in per_student[sid]:
                    rows.append(dict(record))
                    inserted += 1
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

        kept = rows if not removed else [row for row in rows if id(row) not in removed]
        # 写盘失败（没登录 / 库坏了）时**绝不能撒谎说写成功了**：
        # 让 CryptoError 如实抛给调用方，界面那边只记日志、不打断用户看成绩。
        self._write(kept)
        return {"overwritten": overwritten, "inserted": inserted, "skipped": skipped}

    def clear(self) -> None:
        """清空这张表（'''只动 grade_history'''，班级快照 / 归档一个字不碰）。"""
        self._write([])

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

    # -- 写 -----------------------------------------------------------------
    def save_side_text(self, student_id: str, text: str) -> None:
        """写一个学生的侧写正文（**先写盘、成功了才更新内存**，避免假成功）。"""
        sid = _clean_text(student_id)
        if not sid:
            raise ValueError("student_id 不能为空")
        rows = {key: dict(row) for key, row in self._cache.items() if isinstance(row, dict)}
        row = rows.get(sid) or {}
        row.update({
            "student_id": sid,
            "side_text": "" if text is None else str(text),
            "updated_at": _now(),
        })
        rows[sid] = row
        save_user_json(self._store, self._username, self.filename, {
            "table": self.table,
            "version": 1,
            "profiles": rows,
        })
        self._cache = rows

    # -- 内部 ---------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        payload = load_user_json(self._store, self._username, self.filename, None)
        if not isinstance(payload, dict):
            return {}
        stored = payload.get("profiles")
        if not isinstance(stored, dict):
            return {}
        return {str(sid): dict(row) for sid, row in stored.items() if isinstance(row, dict)}


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
