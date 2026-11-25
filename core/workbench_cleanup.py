"""盘点和删除当前账户的学生数据，包括迁移备份中的对应部分。"""

import re
from datetime import datetime
from pathlib import Path

from core.crypto_store import CryptoError, CryptoStore, app_salt_path
from core.database import GradeAuditLog, GradeHistoryDB, ProfileStore, StudentStore
from core.migrator import _backup_parents
from core.sensitive_vault import AUDIT_FILE
from core.user_store import ROSTER_WRITE_LOCK, save_user_json
from core.workbench_journal import JOURNAL, recover_import

STUDENT_FILES = {
    StudentStore.filename, GradeHistoryDB.filename, ProfileStore.filename, "archive.json.enc",
    "intentions.json.enc", "grades.enc", "tags.json.enc", "student_imports.json.enc", JOURNAL,
}
STUDENT_DIRS = {"semester_side", "vector", "sensitive"}
CLASS = re.compile(r"class_.+\.json\.enc\Z")


def account_directories(store: CryptoStore, uid: str) -> list[Path]:
    if not uid:
        raise CryptoError("找不到当前账户。")
    active = store.user_dir(uid)
    if active.is_symlink() or active.is_junction():
        raise CryptoError("当前账户目录存在重定向。")
    bases = [active]
    for parent in _backup_parents(store.root.resolve()):
        if not parent.is_dir():
            continue
        for backup in sorted(parent.glob("backup_v*")):
            base = backup / active.name
            if base.exists():
                if (base.is_symlink() or backup.is_symlink() or base.is_junction()
                        or backup.is_junction() or base.resolve() != parent.resolve() / backup.name / base.name):
                    raise CryptoError("迁移备份路径异常，请检查本机备份。")
                bases.append(base)
    return bases


def _files(base: Path, *, quotas: bool) -> list[Path]:
    if not base.is_dir():
        return []
    found = []
    for path in base.iterdir():
        name = path.name
        if name in STUDENT_FILES or CLASS.fullmatch(name):
            if quotas or not name.endswith("_quota.json.enc"):
                found.append(path)
        elif name in STUDENT_DIRS or name in {"cache", "audit"}:
            if path.is_symlink() or path.is_junction():
                raise CryptoError(f"数据目录有重定向：{name}。")
            if not path.is_dir():
                continue
            for item in path.rglob("*"):
                if item.is_symlink() or item.is_junction():
                    raise CryptoError(f"数据目录有重定向：{name}。")
                if not item.is_file():
                    continue
                relative = item.relative_to(base).as_posix()
                if name in STUDENT_DIRS or relative.startswith("cache/chat_history_") or relative in (
                    GradeAuditLog.filename, AUDIT_FILE,
                ):
                    found.append(item)
    for path in found:
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(base.resolve()):
            raise CryptoError("学生数据路径有重定向。")
    return sorted(found)


def clear_inventory(store, username: str, vault=None) -> dict:
    roster = StudentStore(store=store, username=username)
    with ROSTER_WRITE_LOCK:
        recover_import(store, username)
        bases = account_directories(store, roster.uid)
        counts = {
            "学生": roster.count(), "成绩": GradeHistoryDB(store=store, username=username).count(),
            "学业画像": ProfileStore(store=store, username=username).count(), "照片": 0,
            "联系方式": 0, "谈话": 0, "人工总结": 0, "照片特征": 0, "练习记录": 0,
            "AI 聊天": 0, "德育分表": 0, "班级快照": 0, "奖学金名额表": 0,
            "本机备份": sum(bool(_files(base, quotas=False)) for base in bases[1:]),
        }
        for path in _files(bases[0], quotas=True):
            if path.name.endswith("_moral.json.enc"):
                counts["德育分表"] += 1
            elif path.name.endswith("_quota.json.enc"):
                counts["奖学金名额表"] += 1
            elif path.name.startswith("class_"):
                counts["班级快照"] += 1
            elif path.name.startswith("chat_history_"):
                counts["AI 聊天"] += 1
        if vault is not None:
            for row in roster.rows():
                data = vault.student(row["student_id"])
                counts["照片"] += bool(data.get("photo"))
                counts["联系方式"] += len(data.get("contacts", {}))
                counts["谈话"] += len(data.get("interviews", []))
                counts["人工总结"] += bool(data.get("summary"))
                counts["照片特征"] += bool(data.get("face_vector"))
                counts["练习记录"] += int(data.get("practice", {}).get("attempts", 0))
        return counts


def clear_all_student_data(store, username: str, *, quotas: bool = False) -> dict:
    if not app_salt_path(store.root).is_file():
        raise CryptoError("本机加密盐缺失。")
    roster = StudentStore(store=store, username=username)
    with ROSTER_WRITE_LOCK:
        recover_import(store, username)
        bases = account_directories(store, roster.uid)
        counts = clear_inventory(store, username)
        targets = [(base, path) for base in bases for path in _files(base, quotas=quotas)]
        result = {"counts": counts, "removed": 0, "failed": []}
        for base, path in targets:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                scope = "当前数据" if base == bases[0] else f"备份 {base.parent.name}"
                result["failed"].append(f"{scope} / {path.relative_to(base).as_posix()}")
            else:
                result["removed"] += 1
        if not result["failed"]:
            save_user_json(store, username, "audit/data_cleared.json.enc", {
                "action": "DATA_CLEARED", "time": datetime.now().isoformat(timespec="seconds"),
                "operator": username, "files": result["removed"], "quotas": quotas, "counts": counts,
            })
        return result
