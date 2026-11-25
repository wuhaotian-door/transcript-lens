"""密码解锁的学生资料。每位学生一份密文，避免扫描或重写全班照片。"""

import base64
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.crypto_store import (
    CryptoError, CryptoStore, StoreCorruptError, WrongPasswordError, _atomic_write_bytes,
)
from core.user_store import ROSTER_WRITE_LOCK, load_user_json, save_user_json

LOCK_SECONDS = 600
AUDIT_FILE = "audit/sensitive_access.json.enc"
CONTACT_FIELDS = ("student_phone", "father_phone", "mother_phone", "guardian_phone")
CONTACT_LABELS = ("学生本人", "父亲", "母亲", "其他监护人")


def sensitive_filename(student_id: str) -> str:
    return "sensitive/student_" + hashlib.sha256(student_id.removeprefix("sid:").encode("utf-8")).hexdigest() + ".enc"


def _derive(key: bytes, context: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=context).derive(key)


def _seal(key: bytes, value: bytes, context: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, value, context)


def _open(key: bytes, blob: bytes, context: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], context)
    except (InvalidTag, ValueError) as error:
        raise StoreCorruptError("学生资料无法解密，请检查文件是否损坏。") from error


def mask_phone(number: str) -> str:
    if not number:
        return "未登记"
    return (number[:-8] if len(number) > 8 else "") + "****" + number[-4:]


class SensitiveVault:
    def __init__(self, store: CryptoStore, username: str):
        self.store = store
        self.username = username
        self._key: bytes | None = None
        self._uid = ""
        self._last_access = 0.0
        self._failures = 0
        self._retry_at = 0.0

    @property
    def unlocked(self) -> bool:
        if self._key is not None and time.monotonic() - self._last_access >= LOCK_SECONDS:
            self.lock()
        return self._key is not None

    def lock(self) -> None:
        with ROSTER_WRITE_LOCK:
            self._key = None

    def unlock(self, password: str) -> None:
        if time.monotonic() < self._retry_at:
            raise CryptoError("尝试次数过多，请等 30 秒后再试。")
        try:
            session = self.store.login(self.username, password)
        except WrongPasswordError:
            self._failures += 1
            self.audit("vault_unlock_failed")
            if self._failures >= 5:
                self._retry_at = time.monotonic() + 30
                self._failures = 0
            raise
        self._uid = session.uid
        self._key = _derive(base64.urlsafe_b64decode(session.key), b"transcript-lens/sensitive-vault/v1")
        self._last_access = time.monotonic()
        self._failures = 0
        self.audit("vault_unlock")

    def _require_key(self) -> bytes:
        if not self.unlocked or self._key is None:
            raise CryptoError("学生敏感资料已锁定，请重新输入登录密码。")
        self._last_access = time.monotonic()
        return self._key

    def _context(self, filename: str) -> bytes:
        return (self._uid + "/" + filename).encode("utf-8")

    def _encode(self, filename: str, data: dict) -> bytes:
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return _seal(self._require_key(), raw, self._context(filename))

    def _read(self, filename: str, base: Path | None = None) -> dict:
        key = self._require_key()
        path = (base or self.store.user_dir(self._uid)) / filename
        if not path.is_file():
            return {}
        value = json.loads(_open(key, path.read_bytes(), self._context(filename)))
        if not isinstance(value, dict):
            raise StoreCorruptError("学生资料格式损坏。")
        return value

    def student(self, sid: str) -> dict:
        with ROSTER_WRITE_LOCK:
            return self._read(sensitive_filename(sid))

    def encoded_student(self, sid: str, record: dict) -> tuple[str, bytes]:
        filename = sensitive_filename(sid)
        return filename, self._encode(filename, record)

    def update(self, sid: str, transform) -> dict:
        from core.database import StudentStore
        from core.workbench_journal import recover_import

        with ROSTER_WRITE_LOCK:
            recover_import(self.store, self.username)
            if not StudentStore(store=self.store, username=self.username).has(sid):
                raise CryptoError("学生已从名单中移除，请返回后刷新。")
            data = self._read(sensitive_filename(sid))
            transform(data)
            filename, blob = self.encoded_student(sid, data)
            _atomic_write_bytes(self.store.user_dir(self._uid) / filename, blob)
            return data

    def move_student(self, old_sid: str, new_sid: str) -> None:
        """重新绑定电话的字段密钥，并与主档变更记入同一提交。"""
        from core.crypto_store import app_fernet
        from core.database import StudentStore, _student_id_of
        from core.workbench_journal import recover_import, commit_import

        if _student_id_of(old_sid) == _student_id_of(new_sid):
            return
        roster = StudentStore(store=self.store, username=self.username)
        old_key, new_key = _student_id_of(old_sid), _student_id_of(new_sid)
        old_file = sensitive_filename(old_sid)
        new_file = sensitive_filename(new_sid)
        with ROSTER_WRITE_LOCK:
            recover_import(self.store, self.username)
            rows = roster._load(strict=True)
            if new_key in rows or (self.store.user_dir(roster.uid) / new_file).exists():
                raise CryptoError("目标学号已有学生资料，请检查后再修改。")
            data = self._read(old_file)
            for field, entry in data.get("contacts", {}).items():
                context = self._phone_context(old_sid, field)
                raw = _open(_derive(self._require_key(), context), base64.b64decode(entry["cipher"]), context)
                entry["cipher"] = self.seal_phone(new_sid, field, raw.decode("utf-8"))
            row = rows.pop(old_key)
            row["student_id"] = new_key
            row["updated_at"] = datetime.now().isoformat(timespec="seconds")
            rows[new_key] = row
            payload = {"table": roster.table, "version": 1, "rows": rows}
            commit_import(self.store, self.username, {
                new_file: self._encode(new_file, data),
                old_file: None,
                roster.filename: app_fernet(self.store.root).encrypt(json.dumps(payload).encode("utf-8")),
            })

    def options(self) -> dict:
        with ROSTER_WRITE_LOCK:
            return self._read("sensitive/options.enc")

    def save_options(self, data: dict) -> None:
        filename = "sensitive/options.enc"
        with ROSTER_WRITE_LOCK:
            _atomic_write_bytes(self.store.user_dir(self._uid) / filename, self._encode(filename, data))

    def _phone_context(self, sid: str, field: str) -> bytes:
        if field not in CONTACT_FIELDS:
            raise ValueError("不支持的联系方式")
        return "|".join(("phone", sid.removeprefix("sid:"), field)).encode("utf-8")

    def seal_phone(self, sid: str, field: str, number: str) -> str:
        context = self._phone_context(sid, field)
        return base64.b64encode(_seal(_derive(self._require_key(), context), number.encode("utf-8"), context)).decode("ascii")

    def phone(self, sid: str, field: str, *, action: str = "phone_reveal") -> str:
        context = self._phone_context(sid, field)
        with ROSTER_WRITE_LOCK:
            contact = self.student(sid).get("contacts", {}).get(field)
            if not contact:
                return ""
            key = _derive(self._require_key(), context)
            value = _open(key, base64.b64decode(contact["cipher"]), context).decode("utf-8")
            self.audit(action, sid, field)
            return value

    def audit(self, action: str, sid: str = "", field: str = "") -> None:
        with ROSTER_WRITE_LOCK:
            data = load_user_json(self.store, self.username, AUDIT_FILE, {}, strict=True)
            entries = data.get("events", [])
            entries.append({"at": datetime.now().isoformat(timespec="seconds"),
                            "action": action, "student_id": sid, "field": field})
            save_user_json(self.store, self.username, AUDIT_FILE, {"events": entries[-5000:]})
