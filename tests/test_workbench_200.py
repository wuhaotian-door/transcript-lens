"""2.0 数据合同回归。所有名单和查询均为合成数据。"""

from datetime import date, timedelta
from pathlib import Path

import openpyxl
from PIL import Image
import pytest

from core.crypto_store import CryptoError, CryptoStore, WrongPasswordError, load_encrypted
from core.database import StudentStore
from core.knowledge import KnowledgeLibrary
from core.migrator import backup_vault
from core.photo_matching import search_interviews, valid_consent
from core.sensitive_vault import SensitiveVault, sensitive_filename
from core.student_deletion import purge_student
from core.student_import import HEADERS, apply_import, plan_import
from core.user_store import save_user_json
from core.workbench_cleanup import clear_all_student_data
from core.workbench_journal import JOURNAL


@pytest.fixture
def accounts(tmp_path):
    vault = CryptoStore(tmp_path / "vault", iterations=2000)
    vault.register("teacher01", "synthetic-password-01")
    vault.register("teacher02", "synthetic-password-02")
    access = SensitiveVault(vault, "teacher01")
    access.unlock("synthetic-password-01")
    return vault, access


def sample_sheet(root: Path, size: int):
    photos = root / "photos"
    photos.mkdir()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(HEADERS)
    for index in range(size):
        sid = f"{index:05d}"
        sheet.append((sid, f"合成学生 {index}", "一班" if index % 2 else "二班",
                      "13000002222", "13100001111", "", ""))
        Image.new("RGB", (160, 240), (index % 255, 48, 100)).save(photos / f"{sid}.png")
    sheet.append(("DUP-1", "第一条重复", "一班"))
    sheet.append(("DUP-1", "第二条重复", "一班"))
    (photos / "orphan.jpg").write_bytes(b"invalid")
    (photos / "00001.png").write_bytes(b"broken photo")
    dest = root / "synthetic.xlsx"
    book.save(dest)
    return dest, photos


def test_210_roster_only_valid_rows_and_no_plaintext(tmp_path, accounts):
    store, access = accounts
    plan = plan_import(*sample_sheet(tmp_path, 210))
    count, issues = apply_import(access, plan)
    roster = StudentStore(store=store, username="teacher01")
    assert count == roster.count() == 210
    assert roster.get("00005")["name"] == "合成学生 5"
    assert not roster.has("DUP-1")
    assert any(sid == "00001" and "无效" in detail for _, sid, _, detail in issues)
    assert not access.student("00001").get("photo")
    assert access.student("00000")["photo"]
    assert access.student("00000")["contacts"]["father_phone"]["hint"] == "131****1111"
    assert access.phone("00000", "father_phone") == "13100001111"
    sensitive = store.user_dir(roster.uid) / sensitive_filename("00000")
    assert b"13100001111" not in sensitive.read_bytes()
    with pytest.raises(CryptoError):
        load_encrypted(sensitive, root=store.root)
    assert StudentStore(store=store, username="teacher02").count() == 0
    session = store.login("teacher01", "synthetic-password-01")
    store.change_password(session, "synthetic-password-01", "changed-password-01")
    access.lock()
    with pytest.raises(CryptoError, match="已锁定"):
        access.student("00000")
    with pytest.raises(WrongPasswordError):
        access.unlock("synthetic-password-01")
    access.unlock("changed-password-01")
    assert access.phone("00000", "father_phone") == "13100001111"


def test_interrupted_roster_commit_is_replayed(tmp_path, accounts, monkeypatch):
    from core import workbench_journal

    store, access = accounts
    plan = plan_import(*sample_sheet(tmp_path, 3))
    original = workbench_journal._atomic_write_bytes
    attempt = 0

    def interrupt(path, data):
        nonlocal attempt
        attempt += 1
        if attempt == 2:
            raise OSError("注入写入中断")
        original(path, data)

    monkeypatch.setattr(workbench_journal, "_atomic_write_bytes", interrupt)
    with pytest.raises(OSError, match="注入写入中断"):
        apply_import(access, plan)
    roster = StudentStore(store=store, username=access.username)
    journal = store.user_dir(roster.uid) / JOURNAL
    assert journal.is_file()
    monkeypatch.setattr(workbench_journal, "_atomic_write_bytes", original)
    assert roster.count() == 3
    assert not journal.exists()
    assert access.phone("00002", "student_phone") == "13000002222"


def test_search_and_removal_do_not_cross_students_or_accounts(accounts):
    store, access = accounts
    roster = StudentStore(store=store, username=access.username)
    for sid in ("001", "002"):
        roster.add(sid, f"测试 {sid}", "合成班")
    entry = {"id": "1", "date": "2026-05-04 15:00", "method": "面谈",
             "content": "竞赛训练与数学建模"}
    access.update("001", lambda data: data.update(interviews=[entry], summary="合成总结"))
    assert search_interviews(access, "001", "数学建模") == [entry]
    assert search_interviews(access, "002", "数学建模") == []
    assert purge_student(store, access.username, "001")
    assert not (store.user_dir(roster.uid) / sensitive_filename("001")).exists()
    assert roster.has("002")


def test_clear_removes_owned_backup_keeps_other_account_and_quota(accounts):
    store, access = accounts
    own = StudentStore(store=store, username=access.username)
    other = StudentStore(store=store, username="teacher02")
    own.add("001", "合成学生", "合成班")
    other.add("001", "其他账户", "模拟班")
    access.update("001", lambda data: data.update(summary="合成总结"))
    save_user_json(store, access.username, "class_demo_quota.json.enc", {"data": 1})
    save_user_json(store, access.username, "class_demo_moral.json.enc", {"data": 1})
    save_user_json(store, access.username, "knowledge/registry.json.enc", {"documents": {}, "scope": {}})
    backup = backup_vault(store.root, "1.9.9")
    assert not clear_all_student_data(store, access.username)["failed"]
    assert own.count() == 0 and other.count() == 1
    assert (store.user_dir(own.uid) / "audit/data_cleared.json.enc").exists()
    for parent in (store.root, backup):
        assert not (parent / store.user_dir(own.uid).name / own.filename).exists()
        assert not (parent / store.user_dir(own.uid).name / "class_demo_moral.json.enc").exists()
        assert (parent / store.user_dir(own.uid).name / "class_demo_quota.json.enc").exists()
        assert (parent / store.user_dir(own.uid).name / "knowledge/registry.json.enc").exists()
        assert (parent / store.user_dir(other.uid).name / own.filename).exists()


def test_knowledge_citations_scope_effective_version_and_deletion(tmp_path, accounts):
    store, access = accounts
    library = KnowledgeLibrary(store, access.username)
    path = tmp_path / "policy.txt"
    path.write_text("奖学金申请条件包含本学年通过必修课程。", encoding="utf-8")
    settings = {"title": "合成制度", "source": "合成学院", "school": "合成校", "year": "2025-2026"}
    old = library.add(path, settings)
    path.write_text("奖学金申请条件包含社区服务时数。", encoding="utf-8")
    future = library.add(path, {**settings, "start": (date.today() + timedelta(days=4)).isoformat()})
    assert future != old
    matches = library.search("奖学金申请条件")
    assert len(matches) == 1 and "必修课程" in matches[0]["text"] and "第 1 段" in matches[0]["source"]
    assert library.search("北极地区企鹅分布") == []
    library.set_scope("另一学校", "2025-2026")
    assert library.search("奖学金申请条件") == []
    library.set_scope("合成校", "2025-2026")
    library.delete(old)
    assert library.search("奖学金申请条件") == []


def test_expired_and_missing_photo_consent_are_never_eligible():
    assert not valid_consent({})
    assert not valid_consent({"reference": "test", "expires": (date.today() - timedelta(days=1)).isoformat()})
    assert valid_consent({"reference": "test", "expires": date.today().isoformat()})


def test_moving_student_rekeys_phone_and_confirmed_records(tmp_path, accounts):
    store, access = accounts
    plan = plan_import(*sample_sheet(tmp_path, 2))
    apply_import(access, plan)
    roster = StudentStore(store=store, username=access.username)
    access.update("00000", lambda data: data.update(summary="旧学号的总结"))
    access.move_student("00000", "00999")
    assert not roster.has("00000")
    assert roster.get("00999")["name"] == "合成学生 0"
    assert access.student("00999")["summary"] == "旧学号的总结"
    assert access.phone("00999", "father_phone") == "13100001111"
    assert not (store.user_dir(roster.uid) / sensitive_filename("00000")).exists()


def test_photo_candidates_require_consent_then_disappear_on_withdrawal(tmp_path, accounts, monkeypatch):
    from core import photo_matching as face

    store, access = accounts
    roster = StudentStore(store=store, username=access.username)
    for sid in ("001", "002"):
        roster.add(sid, f"学生 {sid}", "合成班")
        access.update(sid, lambda data: data.update(photo=sid))

    class SynthEngine:
        def __init__(self, _root):
            pass

        def encode(self, photo):
            return [1.0, 0.0] + [0.0] * 126 if photo == "001" else [0.0, 1.0] + [0.0] * 126

    monkeypatch.setattr(face, "FaceEngine", SynthEngine)
    monkeypatch.setattr(face, "encode_photo", lambda path: path.name)
    with pytest.raises(CryptoError, match="尚未启用"):
        face.match_photo(access, tmp_path / "001")
    face.configure_matching(access, "合成校准记录", .7, .1)
    for sid in ("001", "002"):
        face.set_consent(access, sid, "合成学生授权", date.today().isoformat())
        face.index_photo(access, sid)
    message, candidates = face.match_photo(access, tmp_path / "001")
    assert "人工" in message
    assert candidates[0][0]["student_id"] == "sid:001"
    backup = backup_vault(store.root, "2.0")
    face.revoke_consent(access, "001")
    assert face.match_photo(access, tmp_path / "001")[1] == []
    base = backup / store.user_dir(roster.uid).name
    assert "face_vector" not in access._read(sensitive_filename("001"), base)



def test_knowledge_edit_search_and_duplicate_confirmation(tmp_path, accounts):
    from core.knowledge import DocumentValidationError, DuplicateDocumentError

    store, access = accounts
    library = KnowledgeLibrary(store, access.username)
    other = KnowledgeLibrary(store, "teacher02")
    path = tmp_path / "政策.txt"
    path.write_text("经过审批的竞赛获奖证书可参与加分。", encoding="utf-8")
    fields = {"title": "竞赛奖励", "source": "教务处", "school": "合成学校", "year": "2025-2026"}
    for invalid, key in (({"source": "  "}, "source"), ({"year": "2025-2027"}, "year"),
                         ({"title": "字" * 121}, "title"), ({"start": "2026-02-31"}, "start"),
                         ({"start": "2026-02-02", "end": "2026-02-01"}, "end")):
        with pytest.raises(DocumentValidationError) as error:
            library.add(path, {**fields, **invalid})
        assert error.value.field == key
        assert not library.list()
    doc = library.add(path, {**fields, "end": "至今"})
    assert library.list("加分")[0]["id"] == doc
    assert not other.list("竞赛奖励")
    with pytest.raises(DuplicateDocumentError) as duplicate:
        library.add(path, {**fields, "title": "新标题"})
    assert duplicate.value.digest == doc and library.list()[0]["title"] == fields["title"]
    cached = library.excerpts(doc)
    library.edit(doc, {**fields, "title": "已编辑的奖励办法", "version": "第二版"})
    assert library.list("奖励办法 合成学校")[0]["version"] == "第二版"
    assert library.excerpts(doc) == cached
    assert library.add(path, {**fields, "title": "确认覆盖"}, overwrite_digest=doc) == doc
    assert len(library.list()) == 1 and library.list()[0]["title"] == "确认覆盖"
    path.write_text("新文件内容。", encoding="utf-8")
    with pytest.raises(ValueError, match="已变化"):
        library.add(path, fields, overwrite_digest=doc)
    library.delete(doc)
    assert not library.list("加分")


def test_aux_model_progress_and_group_uninstall(tmp_path, monkeypatch):
    import hashlib
    from core import workbench_models as models

    payloads = {"bge.onnx": b"test-bge", "tokenizer.json": b"test-tokens",
                "yunet.onnx": b"test-detector", "sface.onnx": b"test-face"}
    assets = {name: ("https://example.invalid/" + name, len(raw), hashlib.sha256(raw).hexdigest())
              for name, raw in payloads.items()}
    monkeypatch.setattr(models, "ASSETS", assets)

    def download(_url, target, *, progress_callback, expected_sha256, min_bytes, resume):
        raw = payloads[target.name]
        assert hashlib.sha256(raw).hexdigest() == expected_sha256 and len(raw) == min_bytes
        target.parent.mkdir(parents=True, exist_ok=True)
        progress_callback(1, len(raw))
        target.write_bytes(raw)

    monkeypatch.setattr(models, "download_exe", download)
    progress = []
    models.install_models(tmp_path, "semantic", progress=lambda *value: progress.append(value))
    assert models.installed(tmp_path, "semantic") and not models.installed(tmp_path, "face")
    assert progress[-1] == ("tokenizer.json", 19, 19)
    assert [entry[1] for entry in progress] == sorted(entry[1] for entry in progress)
    models.install_models(tmp_path, "face")
    models.uninstall_models(tmp_path, "semantic")
    assert not models.installed(tmp_path, "semantic") and models.installed(tmp_path, "face")
