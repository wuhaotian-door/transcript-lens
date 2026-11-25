"""读取和预检学生 Excel 名单，按学号配照片，提交时只写密文。"""

import base64
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.crypto_store import app_fernet
from core.database import StudentStore, _bare_student_id, _student_id_of
from core.sensitive_vault import CONTACT_FIELDS, SensitiveVault, mask_phone
from core.user_store import ROSTER_WRITE_LOCK, load_user_json
from core.workbench_journal import commit_import

HEADERS = ("学号", "姓名", "班级", "本人电话", "父亲电话", "母亲电话", "其他监护人电话")
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PHONE = re.compile(r"[+]?\d[\d ()-]{4,22}")
VALID_ID = re.compile(r"[\w-]{1,40}")
BATCHES = "student_imports.json.enc"


@dataclass(frozen=True)
class StudentInput:
    row: int
    sid: str
    name: str
    class_name: str
    phones: tuple[str, ...]
    photo: Path | None


@dataclass
class ImportPlan:
    students: list[StudentInput]
    issues: list[tuple[int, str, str, str]]
    filename: str
    digest: str


def write_template(destination: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "学生资料"
    sheet.append(HEADERS)
    for column in range(1, len(HEADERS) + 1):
        sheet.cell(2, column).number_format = "@"
        sheet.column_dimensions[openpyxl.utils.get_column_letter(column)].width = 22
    sheet.freeze_panes = "D2"
    book.save(destination)


def _read_text(cell, *, identifier: bool = False) -> str:
    value = cell.value
    if value is None:
        return ""
    if cell.data_type == "f" or isinstance(value, (bool, float)):
        raise ValueError("含公式或小数，请改为文本。")
    if isinstance(value, int):
        digits = str(value)
        if value < 0 or len(digits) > 15:
            raise ValueError("学号或电话应为文本，核对是否丢失前导零。")
        if re.fullmatch(r"0+", cell.number_format or ""):
            return digits.zfill(len(cell.number_format))
        if not identifier and not re.fullmatch(r"1[3-9][0-9]{9}", digits):
            raise ValueError("座机和非标准手机号请按文本填写，保留前导零。")
    if not isinstance(value, (str, int)):
        raise ValueError("单元格应为文本。")
    return str(value).strip()


def _photo_map(directory: Path | None, ids: set[str], issues: list) -> dict[str, Path]:
    if directory is None:
        return {}
    found: dict[str, list[Path]] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in PHOTO_SUFFIXES:
            continue
        sid = path.stem
        if sid not in ids:
            candidates = {sid[:i] for i, char in enumerate(sid)
                          if char in "_-" and sid[:i] in ids
                          and re.fullmatch(r"(?:照(?:片)?|证件照|photo|[0-9]+)", sid[i + 1:], re.I)}
            if len(candidates) != 1:
                issues.append((0, "", "照片", "归属不明确或未找到学生：" + path.name))
                continue
            sid = candidates.pop()
        found.setdefault(sid, []).append(path)
    resolved = {}
    for sid, paths in found.items():
        exact = [path for path in paths if path.stem == sid]
        choices = exact or paths
        if len(choices) == 1:
            resolved[sid] = choices[0]
            if len(paths) > 1:
                issues.append((0, sid, "照片", "存在多个文件，已采用唯一的学号精确匹配。"))
        else:
            issues.append((0, sid, "照片", "同一学号有多张候选照片，请保留一张再补录。"))
    return resolved


def plan_import(path: Path, directory: Path | None) -> ImportPlan:
    import openpyxl

    if path.stat().st_size > 20_000_000:
        raise ValueError("学生资料工作簿超过 20 MB，请分批导入。")
    raw = path.read_bytes()
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    candidates: list[StudentInput] = []
    issues: list[tuple[int, str, str, str]] = []
    try:
        sheet = book.active
        rows = sheet.iter_rows(max_col=40)
        columns = {str(cell.value).strip(): i for i, cell in enumerate(next(rows, ())) if cell.value}
        if not set(HEADERS[:3]) <= columns.keys():
            raise ValueError("Excel 首行必须包含学号、姓名、班级。请使用导入模板。")
        if sheet.max_row and sheet.max_row > 10001:
            raise ValueError("请将学生资料分为每批最多 10000 人。")
        for number, cells in enumerate(rows, 2):
            if number > 10001:
                raise ValueError("请将学生资料分为每批最多 10000 人。")
            if not any(cell.value is not None for cell in cells):
                continue
            sid = ""
            try:
                sid = _read_text(cells[columns["学号"]], identifier=True)
                name = _read_text(cells[columns["姓名"]])
                class_name = _read_text(cells[columns["班级"]])
                if not VALID_ID.fullmatch(sid) or not name or not class_name or max(len(name), len(class_name)) > 80:
                    raise ValueError("学号、姓名或班级不符合模板要求。")
                phones = tuple(_read_text(cells[columns[header]]) if header in columns else ""
                               for header in HEADERS[3:])
                if any(number and not PHONE.fullmatch(number) for number in phones):
                    raise ValueError("联系电话格式不正确。")
            except ValueError as error:
                issues.append((number, sid, "拒绝", str(error)))
                continue
            if isinstance(cells[columns["学号"]].value, int):
                issues.append((number, sid, "学号", "原单元格为数字，请核对前导零。"))
            candidates.append(StudentInput(number, sid, name, class_name, phones, None))
    finally:
        book.close()
    counts = Counter(item.sid for item in candidates)
    photos = _photo_map(directory, set(counts), issues)
    students = []
    for item in candidates:
        if counts[item.sid] != 1:
            issues.append((item.row, item.sid, "拒绝", "学号在文件内重复。"))
            continue
        students.append(StudentInput(item.row, item.sid, item.name, item.class_name, item.phones, photos.get(item.sid)))
    return ImportPlan(students, issues, path.name, hashlib.sha256(raw).hexdigest())


def encode_photo(path: Path) -> str:
    from PIL import Image, ImageOps

    if path.stat().st_size > 20_000_000:
        raise ValueError("照片超过 20 MB。")
    with Image.open(path) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("照片像素过多。")
        corrected = ImageOps.exif_transpose(image).convert("RGB")
        corrected.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        corrected.save(buffer, format="JPEG", quality=86)
        return base64.b64encode(buffer.getvalue()).decode("ascii")


def apply_import(vault: SensitiveVault, plan: ImportPlan, *, replace_identity: bool = False,
                 progress=lambda done, total: None) -> tuple[int, list]:
    from core.workbench_journal import recover_import

    photos = {}
    issues = list(plan.issues)
    for position, item in enumerate(plan.students, 1):
        if item.photo:
            try:
                photos[item.sid] = encode_photo(item.photo)
            except (OSError, ValueError) as error:
                issues.append((item.row, item.sid, "照片", "照片无效：" + type(error).__name__))
        progress(position, len(plan.students))
    store, username = vault.store, vault.username
    roster = StudentStore(store=store, username=username)
    with ROSTER_WRITE_LOCK:
        recover_import(store, username)
        rows = roster._load(strict=True)
        captured_at = datetime.now().isoformat(timespec="seconds")
        entries = {}
        imported_ids = {_student_id_of(item.sid) for item in plan.students}
        issues.extend((0, _bare_student_id(sid) or sid, "未出现", "保留原学生。") for sid in rows.keys() - imported_ids)
        for item in plan.students:
            key = _student_id_of(item.sid)
            previous = rows.get(key, {})
            if not replace_identity and any(previous.get(field) and previous[field] != provided
                                            for field, provided in (("name", item.name), ("class_name", item.class_name))):
                issues.append((item.row, item.sid, "拒绝", "已有姓名或班级不同，需确认覆盖。"))
                continue
            data = vault.student(item.sid)
            contacts = data.setdefault("contacts", {})
            for field, number in zip(CONTACT_FIELDS, item.phones):
                if number:
                    contacts[field] = {"hint": mask_phone(number), "cipher": vault.seal_phone(item.sid, field, number)}
            if photos.get(item.sid):
                data["photo"] = photos[item.sid]
                data.pop("face_vector", None)
            if not data.get("photo"):
                issues.append((item.row, item.sid, "待补", "缺照片"))
            for index, field in enumerate(CONTACT_FIELDS):
                if index < 3 and not contacts.get(field):
                    issues.append((item.row, item.sid, "待补", f"缺{HEADERS[3 + index]}"))
            filename, blob = vault.encoded_student(item.sid, data)
            entries[filename] = blob
            rows[key] = {
                "note": "", "archived": False, "created_at": captured_at, "source": "roster_import",
                **previous, "student_id": key, "name": item.name, "class_name": item.class_name,
                "updated_at": captured_at,
            }
        count = len(entries)
        if count:
            batches = load_user_json(store, username, BATCHES, {}, strict=True).get("batches", [])
            batches.append({"at": captured_at, "file": plan.filename, "sha256": plan.digest,
                            "count": count, "issues": len(issues), "actor": username})
            cipher = app_fernet(store.root)
            for filename, data in (
                (roster.filename, {"table": roster.table, "version": 1, "rows": rows, "updated": captured_at}),
                (BATCHES, {"batches": batches[-100:]}),
            ):
                entries[filename] = cipher.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            commit_import(store, username, entries)
    return count, issues
