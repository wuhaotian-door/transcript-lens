"""名单导入的可重放提交记录。记录本身和每个目标文件均为密文。"""

import base64
import re
from pathlib import Path

from core.crypto_store import CryptoError, _atomic_write_bytes, load_encrypted, save_encrypted
from core.user_store import ROSTER_WRITE_LOCK, user_file

JOURNAL = "roster_pending.enc"
_ROSTER = "students.json.enc"
_BATCHES = "student_imports.json.enc"
_VALID = re.compile(r"sensitive/student_[a-f0-9]{64}\.enc")


def _replay(base: Path, entries: dict) -> None:
    if not isinstance(entries, dict) or not entries.keys() & {_ROSTER, _BATCHES}:
        raise CryptoError("学生导入恢复记录格式错误。")
    targets = []
    for name, encoded in entries.items():
        if name not in (_ROSTER, _BATCHES) and not _VALID.fullmatch(name):
            raise CryptoError("学生导入恢复记录有无效目标。")
        target = base / name
        if target.is_symlink() or target.parent.resolve() != base.resolve() / Path(name).parent:
            raise CryptoError("学生导入目录存在重定向。")
        if encoded is None and not _VALID.fullmatch(name):
            raise CryptoError("学生导入恢复记录不能删除该目标。")
        targets.append((target, None if encoded is None else base64.b64decode(encoded, validate=True)))
    for target, blob in targets:
        if blob is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(target, blob)


def recover_import(store, username: str) -> bool:
    journal = user_file(store, username, JOURNAL)
    if journal is None or not journal.exists():
        return False
    with ROSTER_WRITE_LOCK:
        if not journal.exists():
            return False
        entries = load_encrypted(journal, root=store.root).get("entries")
        _replay(journal.parent, entries)
        journal.unlink()
        return True


def commit_import(store, username: str, entries: dict[str, bytes | None]) -> None:
    with ROSTER_WRITE_LOCK:
        journal = user_file(store, username, JOURNAL)
        if journal is None:
            raise CryptoError("当前账户不存在。")
        recover_import(store, username)
        save_encrypted({"entries": {name: None if blob is None else base64.b64encode(blob).decode("ascii")
                                    for name, blob in entries.items()}}, journal, root=store.root)
        recover_import(store, username)
