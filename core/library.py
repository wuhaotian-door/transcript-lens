"""加密成绩库：把 :mod:`core.archival` 和 :mod:`core.crypto_store` 接起来。

三者的分工，别混：

* :mod:`core.archival` —— 纵向档案的**纯内存**结构。它连 ``open()`` 都没有，
  有测试盯着它不碰磁盘。
* :mod:`core.crypto_store` —— 只认字节和文件名，**完全不认识** ``Archive``。
* **本模块** —— 只干一件事：把 ``Archive`` 交给谁存、从谁那儿读回来。

落盘位置（``AppData/Local/SGMS/u_<uid 前 8 位>/``，全是密文）:

=========================  ====================================================
``grades.enc``             ``Archive.as_dict()`` —— 纵向成绩档案
``sessions.json.enc``      导入日志（``archival.session_log``）—— Spec 第 5 章
                           字面要求这个名字，**内容仍然是密文**
=========================  ====================================================

``tags.json.enc``（意愿标签）由画像页负责，不在本模块。

三条不許违反的规矩：

1. **没登录就没有成绩库。** 所有函数都要一个 ``UserSession``（含库密钥）。
   没登录时界面该做的是"这次只算不存"，而不是想办法绕过去。
2. **明文一个字节都不落盘。** 本模块只经 ``CryptoStore.save``，
   没有别的写文件路径。
3. **``archive.version`` 比本程序新 → 报错，不猜。** 硬读一个未来格式
   会悄悄丢掉自己看不懂的字段，那比读不出来危险得多。

本模块**不联网、不调大模型、不碰 UI**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.archival import ARCHIVE_VERSION, Archive, session_log
from core.crypto_store import (
    CryptoError,
    CryptoStore,
    FILE_GRADES,
    FILE_SESSIONS,
    UserSession,
)

__all__ = [
    "LibraryError",
    "LibraryInfo",
    "SaveReport",
    "library_info",
    "library_summary_dict",
    "load_library",
    "save_library",
]


class LibraryError(CryptoError):
    """成绩库这一层的问题（版本太新、结构不对等）。

    继承 :class:`~core.crypto_store.CryptoError`，这样界面只需要
    ``except CryptoError`` 一个兜底就能把两层的异常都接住。
    """


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveReport:
    """一次保存的结果，够界面原样报给用户看。"""

    students: int
    records: int
    log_entries: int
    grades_path: str
    sessions_path: str
    grades_bytes: int
    sessions_bytes: int

    def describe(self) -> str:
        return (
            f"已加密保存 {self.students} 名学生 · {self.records} 条学期记录 · "
            f"导入日志 {self.log_entries} 条\n"
            f"成绩档案 {self.grades_bytes / 1024:.1f} KB → {self.grades_path}\n"
            f"导入日志 {self.sessions_bytes / 1024:.1f} KB → {self.sessions_path}"
        )


@dataclass(frozen=True)
class LibraryInfo:
    """磁盘上这个用户的成绩库现在长什么样。"""

    present: bool = False
    students: int = 0
    records: int = 0
    terms: tuple[str, ...] = ()
    grades_bytes: int = 0
    sessions_bytes: int = 0
    modified: str = ""

    def describe(self) -> str:
        if not self.present:
            return "库里还没有东西（这次导入还只存在内存里）"
        terms = "、".join(self.terms) if self.terms else "未标注学期"
        return (
            f"{self.students} 名学生 · {self.records} 条学期记录 · "
            f"覆盖 {terms}\n"
            f"grades.enc {self.grades_bytes / 1024:.1f} KB + "
            f"sessions.json.enc {self.sessions_bytes / 1024:.1f} KB"
            + (f"　最后保存 {self.modified}" if self.modified else "")
        )


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def save_library(
    store: CryptoStore, crypto_session: UserSession, archive: Archive
) -> SaveReport:
    """把整份档案加密写进库。**覆盖**同名的两个文件（不做增量）。"""
    if not isinstance(archive, Archive):
        raise TypeError(f"archive 必须是 core.archival.Archive，收到 {type(archive).__name__}")

    log = session_log(archive)
    store.save(crypto_session, FILE_GRADES, archive.as_dict())
    store.save(crypto_session, FILE_SESSIONS, log)

    inventory = {item["filename"]: item for item in store.inventory(crypto_session.uid)}
    return SaveReport(
        students=len(archive),
        records=archive.total_records,
        log_entries=len(log.get("entries", [])),
        grades_path=str(store.payload_path(crypto_session.uid, FILE_GRADES)),
        sessions_path=str(store.payload_path(crypto_session.uid, FILE_SESSIONS)),
        grades_bytes=int(inventory.get(FILE_GRADES, {}).get("bytes", 0)),
        sessions_bytes=int(inventory.get(FILE_SESSIONS, {}).get("bytes", 0)),
    )


def load_library(store: CryptoStore, crypto_session: UserSession) -> Archive | None:
    """从库读回档案。**库里还没有东西时返回 ``None``**（不是空 Archive）。

    返回 ``None`` 和返回"一个空档案"是两件事：前者是"从没存过"，
    后者会让人以为"存过、但里面没人了"。界面据此说不同的话。

    Raises:
        LibraryError: 库里的 ``version`` 比本程序认识的还新。
        StoreCorruptError: 文件解不开 / 不是合法 JSON（在 crypto_store 里抛）。
    """
    payload = store.load(crypto_session, FILE_GRADES)
    if not payload:
        return None

    version = int(payload.get("version", ARCHIVE_VERSION))
    if version > ARCHIVE_VERSION:
        raise LibraryError(
            f"库里的成绩档案是 v{version}，本程序只认到 v{ARCHIVE_VERSION}。"
            "这份档案是更新的版本写的，硬读会丢掉看不懂的字段 —— "
            "请升级程序，不要用旧版打开它。"
        )
    return Archive.from_dict(payload)


def library_info(store: CryptoStore, crypto_session: UserSession) -> LibraryInfo:
    """只报"库里现在有什么"，不改任何东西。给界面刷新状态用。"""
    if not store.has(crypto_session.uid, FILE_GRADES):
        return LibraryInfo(present=False)

    inventory = {item["filename"]: item for item in store.inventory(crypto_session.uid)}
    archive = load_library(store, crypto_session)
    if archive is None:  # 理论上不可达：上面刚确认过文件存在
        return LibraryInfo(present=False)

    terms = sorted({term for student in archive.students.values() for term in student.terms})
    return LibraryInfo(
        present=True,
        students=len(archive),
        records=archive.total_records,
        terms=tuple(terms),
        grades_bytes=int(inventory.get(FILE_GRADES, {}).get("bytes", 0)),
        sessions_bytes=int(inventory.get(FILE_SESSIONS, {}).get("bytes", 0)),
        modified=str(inventory.get(FILE_GRADES, {}).get("modified", "")),
    )


def library_summary_dict(store: CryptoStore, crypto_session: UserSession) -> dict[str, Any]:
    """给自检/日志用的一份纯字典快照（**不含任何成绩内容**）。"""
    info = library_info(store, crypto_session)
    return {
        "present": info.present,
        "students": info.students,
        "records": info.records,
        "terms": list(info.terms),
        "grades_bytes": info.grades_bytes,
        "sessions_bytes": info.sessions_bytes,
    }
