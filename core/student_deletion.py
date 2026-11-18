"""从当前账户的加密数据中彻底删除一名学生。"""

from pathlib import Path
from threading import Lock
from typing import Any

from core import applog
from core.archival import archive_key
from core.crypto_store import CryptoError, CryptoStore, _atomic_write_bytes, app_salt_path, load_encrypted
from core.database import (
    ARCHIVE_PAYLOAD_FILE,
    CHAT_CACHE_DIR,
    CHAT_CACHE_PREFIX,
    OP_STUDENT_DELETE,
    GradeAuditLog,
    GradeHistoryDB,
    ProfileStore,
    StudentStore,
    _bare_student_id,
    _class_payload_names,
    _user_dir,
    safe_student_file,
)
from core.user_store import ROSTER_WRITE_LOCK, list_user_json, save_user_json


class StudentDeletionControl:
    """让界面能取消尚未写盘的删除，避免取消通知与开始写入互相抢跑。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled = False
        self._writing = False

    def cancel_before_write(self) -> bool:
        with self._lock:
            if self._writing:
                return False
            self._cancelled = True
            return True

    def check_cancelled(self) -> None:
        with self._lock:
            if self._cancelled:
                raise CryptoError("本次删除已取消，未写入数据。")

    def begin_write(self) -> None:
        with self._lock:
            if self._cancelled:
                raise CryptoError("本次删除已取消，未写入数据。")
            self._writing = True


def _remove_rows(table: Any, field: str, sid: str) -> Any:
    if isinstance(table, list):
        return [
            row for row in table
            if not isinstance(row, dict) or _bare_student_id(row.get(field)) != sid
        ]
    raise CryptoError("学生关联数据的表结构无法识别")


def _remove_keys(table: Any, keys: set[str]) -> dict:
    if isinstance(table, dict):
        return {key: value for key, value in table.items() if key not in keys}
    raise CryptoError("学生关联数据的索引结构无法识别")


def _archive_without_student(
    payload: dict, sid: str, name: str
) -> dict:
    students = payload.get("students", {})
    keys = {archive_key(sid, name)}
    if sid:
        keys.update((sid, f"sid:{sid}", archive_key(f"sid:{sid}")))
        if isinstance(students, dict):
            for key, student in students.items():
                if not isinstance(student, dict):
                    continue
                records = student.get("records") or ()
                if _bare_student_id(student.get("student_id")) == sid or any(
                    isinstance(row, dict) and _bare_student_id(row.get("student_id")) == sid
                    for row in records
                ):
                    keys.add(key)
    return {
        **payload,
        "students": _remove_keys(students, keys),
        "imported_terms": _remove_keys(payload.get("imported_terms", {}), keys),
    }


def purge_student(
    store: CryptoStore, username: str, student_id: str, name: str = "", *,
    control: StudentDeletionControl | None = None,
) -> bool:
    """先检查全部关联文件，再写盘。普通写入错误时回滚已修改的密文。"""
    if control is not None:
        control.check_cancelled()
    if not ROSTER_WRITE_LOCK.acquire(timeout=3):
        raise CryptoError("其他操作正在写入学生数据，本次未删除。请稍后重试。")
    try:
        return _purge_student(store, username, student_id, name, control)
    finally:
        ROSTER_WRITE_LOCK.release()


def _purge_student(
    store: CryptoStore, username: str, student_id: str, name: str,
    control: StudentDeletionControl | None,
) -> bool:
    if control is not None:
        control.check_cancelled()
    if not username or (base := _user_dir(store, username)) is None:
        raise CryptoError("当前账户的数据目录不可用")
    sid = _bare_student_id(student_id)
    if not sid and (student_id or not name):
        raise ValueError("缺少可用于删除的学生标识")

    # 路径、原始密文、更新后的载荷。None 表示删除整个文件。
    changes: list[tuple[Path, bytes, dict | None]] = []

    def stage(filename: str, transform) -> None:
        if control is not None:
            control.check_cancelled()
        path = base / filename
        if not path.exists():
            return
        if not app_salt_path(store.root).is_file():
            raise CryptoError("本地加密盐文件缺失，无法核对学生关联数据。")
        payload = load_encrypted(path, root=store.root)
        updated = transform(payload)
        if updated != payload:
            changes.append((path, path.read_bytes(), updated))

    stage(ARCHIVE_PAYLOAD_FILE, lambda payload: _archive_without_student(payload, sid, name))
    if sid:
        keys = {sid, f"sid:{sid}"}

        def stage_rows(filename: str, container: str) -> None:
            stage(filename, lambda p: {
                **p, container: _remove_rows(p.get(container), "student_id", sid),
            })

        def stage_keys(filename: str, container: str) -> None:
            stage(filename, lambda p: {**p, container: _remove_keys(p.get(container), keys)})

        stage_rows(GradeHistoryDB.filename, "rows")
        stage_keys(ProfileStore.filename, "profiles")
        stage_keys("intentions.json.enc", "intentions")
        for filename in _class_payload_names(store, username):
            stage_rows(filename, "students")
        for filename in list_user_json(store, username, "_moral.json.enc"):
            if filename.startswith("class_"):
                stage_keys(filename, "scores")

        safe_id = safe_student_file(sid)
        chat = base / CHAT_CACHE_DIR / f"{CHAT_CACHE_PREFIX}{safe_id}.json.enc"
        term_dir = base / "semester_side" / safe_id
        for path in (chat, *term_dir.glob("*.json.enc")):
            if path.is_file():
                changes.append((path, path.read_bytes(), None))

        def remove_roster_row(payload: dict) -> dict:
            rows = payload.get("rows")
            kept = (_remove_rows(rows, "student_id", sid) if isinstance(rows, list)
                    else _remove_keys(rows, keys))
            return {**payload, "rows": kept}

        # 主档最后写。即使操作中断，也应让用户还能从名单进入重试。
        stage(StudentStore.filename, remove_roster_row)

    return _commit_deletion(store, username, base, changes, control)


def _commit_deletion(store, username, base, changes, control) -> bool:
    if not changes:
        return False
    if control is not None:
        control.begin_write()

    applied: list[tuple[Path, bytes]] = []
    try:
        for path, before, payload in changes:
            if payload is None:
                path.unlink()
            else:
                save_user_json(store, username, path.relative_to(base).as_posix(), payload)
            applied.append((path, before))
    except Exception as error:
        rollback_error = None
        for path, before in reversed(applied):
            try:
                _atomic_write_bytes(path, before)
            except OSError as exc:
                rollback_error = exc
        if rollback_error is not None:
            raise CryptoError("删除未完成，部分文件未能回滚。请检查数据目录后重试。") from rollback_error
        raise CryptoError("删除未完成，已恢复本次改动。请检查数据目录后重试。") from error

    return True


def purge_class(
    store: CryptoStore, username: str, class_name: str, *,
    control: StudentDeletionControl | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """一次删除目标班的快照和独占学生数据，保留其他班仍使用的学生。"""
    name = class_name.strip()
    if not name:
        raise ValueError("请选择有班级号的班级。")
    if control is not None:
        control.check_cancelled()
    if not ROSTER_WRITE_LOCK.acquire(timeout=3):
        raise CryptoError("其他操作正在写入学生数据，请稍后重试。")
    try:
        return _purge_class(store, username, name, control)
    finally:
        ROSTER_WRITE_LOCK.release()


def _purge_class(store, username, name, control) -> tuple[bool, tuple[str, ...]]:
    if not username or (base := _user_dir(store, username)) is None:
        raise CryptoError("当前账户的数据目录不可用")
    if not app_salt_path(store.root).is_file():
        raise CryptoError("本地加密盐文件缺失，无法核对班级数据。")

    changes: list[tuple[Path, bytes, dict | None]] = []
    owned: set[str] = set()
    shared: set[str] = set()

    def read(path: Path) -> dict:
        if control is not None:
            control.check_cancelled()
        if path.is_symlink():
            raise CryptoError("班级数据目录中存在重定向文件，删除已取消。")
        return load_encrypted(path, root=store.root)

    def transform(filename, update):
        path = base / filename
        if path.exists():
            payload = read(path)
            changed = update(payload)
            if changed != payload:
                changes.append((path, path.read_bytes(), changed))

    def class_of(filename: str) -> str:
        # 班级号允许短横线，学年与用途后缀从第一个下划线起。
        return filename.removeprefix("class_").split(".", 1)[0].split("_", 1)[0]

    def collect(rows, class_name):
        if not isinstance(rows, list):
            raise CryptoError("班级名单格式无法识别。")
        (owned if class_name == name else shared).update(
            sid for row in rows if isinstance(row, dict)
            if (sid := _bare_student_id(row.get("student_id")))
        )

    roster_path = base / StudentStore.filename
    roster = read(roster_path) if roster_path.exists() else None
    if roster is not None:
        rows = roster.get("rows")
        if not isinstance(rows, (list, dict)):
            raise CryptoError("学生主档格式无法识别。")
        for row in rows.values() if isinstance(rows, dict) else rows:
            if isinstance(row, dict) and row.get("class_name"):
                collect([row], str(row["class_name"]).strip())

    for filename in _class_payload_names(store, username):
        path = base / filename
        payload = read(path)
        class_name = class_of(filename)
        collect(payload.get("students", []), class_name)
        if class_name == name:
            changes.append((path, path.read_bytes(), None))
    for filename in list_user_json(store, username):
        if not filename.startswith("class_") or class_of(filename) != name:
            continue
        if filename.endswith(("_moral.json.enc", "_quota.json.enc")):
            path = base / filename
            if path.is_symlink():
                raise CryptoError("班级数据目录中存在重定向文件，删除已取消。")
            changes.append((path, path.read_bytes(), None))

    purged = owned - shared
    retained_members = owned & shared
    keys = purged | {f"sid:{sid}" for sid in purged}
    archive_keys = {archive_key(key) for key in keys} | keys

    def without_affected_rows(payload):
        rows = payload.get("students", {})
        if not isinstance(rows, dict):
            raise CryptoError("学生档案格式无法识别。")
        for key, student in rows.items():
            if not isinstance(student, dict):
                continue
            ids = {_bare_student_id(student.get("student_id"))}
            ids.update(_bare_student_id(row.get("student_id"))
                       for row in (student.get("records") or ()) if isinstance(row, dict))
            if ids & purged:
                archive_keys.add(key)
        return {**payload, "students": _remove_keys(rows, archive_keys),
                "imported_terms": _remove_keys(payload.get("imported_terms", {}), archive_keys)}

    if purged:
        transform(ARCHIVE_PAYLOAD_FILE, without_affected_rows)
        def without_grades(payload):
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                raise CryptoError("班级关联成绩格式无法识别。")
            return {**payload, "rows": [
                row for row in rows if not isinstance(row, dict)
                or _bare_student_id(row.get("student_id")) not in purged
            ]}
        transform(GradeHistoryDB.filename, without_grades)
        for filename, field in ((ProfileStore.filename, "profiles"), ("intentions.json.enc", "intentions")):
            transform(filename, lambda p: {**p, field: _remove_keys(p.get(field, {}), keys)})
        for sid in purged:
            safe = safe_student_file(sid)
            cache = base / CHAT_CACHE_DIR / f"{CHAT_CACHE_PREFIX}{safe}.json.enc"
            terms = base / "semester_side" / safe
            if any(part.is_symlink() for part in (cache.parent, terms.parent, terms)):
                raise CryptoError("班级数据目录中存在重定向文件，删除已取消。")
            for path in (cache, *terms.glob("*.json.enc")):
                if path.is_symlink():
                    raise CryptoError("班级数据目录中存在重定向文件，删除已取消。")
                if path.is_file():
                    changes.append((path, path.read_bytes(), None))

    if roster is not None and owned:
        rows = roster["rows"]

        def kept(row):
            if not isinstance(row, dict):
                return row
            sid = _bare_student_id(row.get("student_id"))
            if sid in purged:
                return None
            if sid in retained_members and str(row.get("class_name") or "").strip() == name:
                return {**row, "class_name": ""}
            return row

        if isinstance(rows, dict):
            new_rows = {key: out for key, row in rows.items() if (out := kept(row)) is not None}
        else:
            new_rows = [out for row in rows if (out := kept(row)) is not None]
        if new_rows != rows:
            changes.append((roster_path, roster_path.read_bytes(), {**roster, "rows": new_rows}))

    deleted = _commit_deletion(store, username, base, changes, control)
    return deleted, tuple(sorted(purged))


def audit_student_deletion(store: CryptoStore, username: str, student_id: str) -> None:
    """数据写入之后补记审计。无法写日志时，删除结果仍然有效。"""
    sid = _bare_student_id(student_id)
    try:
        GradeAuditLog(store=store, username=username).record_student(
            OP_STUDENT_DELETE, student_id=f"sid:{sid}" if sid else "",
            detail="彻底删除学生及关联数据",
        )
    except Exception as exc:
        applog.write("student.delete", f"audit_failed: {type(exc).__name__}", root=store.root)
