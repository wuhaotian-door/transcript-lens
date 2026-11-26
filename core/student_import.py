"""读取和预检学生 Excel 名单，按学号配照片，提交时只写密文。"""

import base64
import hashlib
import io
import json
import re
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from core.crypto_store import app_fernet
from core.database import StudentStore, _bare_student_id, _student_id_of
from core.sensitive_vault import CONTACT_FIELDS, SensitiveVault, mask_phone
from core.user_store import ROSTER_WRITE_LOCK, load_user_json, save_user_json
from core.workbench_journal import commit_import

HEADERS = ("学号", "姓名", "班级", "本人电话", "父亲电话", "母亲电话", "其他监护人电话")
CONTACT_HEADERS = (
    ("本人电话", "联系电话", "手机号", "手机号码", "电话", "学生电话", "本人手机", "联系方式",
     "本人联系方式", "联系方式(本人)", "联系电话(本人)", "学生联系方式"),
    ("父亲电话", "父亲手机号", "父亲手机", "父亲联系方式", "父亲联系方式(父亲)", "爸爸电话"),
    ("母亲电话", "母亲手机号", "母亲手机", "母亲联系方式", "母亲联系方式(母亲)", "妈妈电话"),
    ("其他监护人电话", "监护人电话", "监护人手机", "家长电话", "家长联系方式"),
)
IDENTITY_HEADERS = (("学号", "学生学号"), ("姓名", "学生姓名"), ("班级", "班级号", "班级名称"))
ROSTER_HEADERS = (*HEADERS[:3], "性别")
ROSTER_ALIASES = (*IDENTITY_HEADERS, ("性别", "学生性别"))
GENDER_OPTIONS = ("男", "女", "其他", "未说明")
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PHONE = re.compile(r"[+]?\d[\d ()-]{4,22}")
VALID_ID = re.compile(r"[\w-]{1,40}")
BATCHES = "student_imports.json.enc"
PHONE_TEMPLATE_FILE = "phone_template.json.enc"
DEFAULT_PHONE_TEMPLATE = tuple((header, header) for header in HEADERS)
DEFAULT_ROSTER_TEMPLATE = tuple((header, header) for header in ROSTER_HEADERS)
ROSTER_TEMPLATE_FILE = "roster_template.json.enc"


class MissingClassError(ValueError):
    """名册行缺少班级，需要用户提供一个默认值。"""


def normalize_gender(value: str) -> str:
    text = value.strip()
    aliases = {"男性": "男", "女性": "女", "m": "男", "f": "女", "male": "男", "female": "女",
               "未知": "未说明", "未填写": "未说明", "不详": "未说明"}
    return aliases.get(text.casefold(), text)


@dataclass(frozen=True)
class StudentInput:
    row: int
    sid: str
    name: str
    class_name: str
    phones: tuple[str, ...]
    photo: Path | None
    gender: str = ""


@dataclass(frozen=True)
class PhotoConflict:
    path: Path
    candidates: tuple[str, ...]
    reason: str


@dataclass
class ImportPlan:
    students: list[StudentInput]
    issues: list[tuple[int, str, str, str]]
    filename: str
    digest: str
    # 补录只允许写回预览时匹配到的已存在学生。
    matched_roster: dict[str, tuple[str, str, str]] | None = None
    photo_conflicts: tuple[PhotoConflict, ...] = ()
    photo_students: tuple[StudentInput, ...] = ()


def _validate_template(columns, allowed) -> tuple[tuple[str, str], ...]:
    if len(columns) > 20:
        raise ValueError("模板最多支持 20 列。")
    normalized = tuple((str(name).strip(), str(role).strip()) for name, role in columns)
    names = [_header(name) for name, _ in normalized]
    roles = [role for _, role in normalized if role]
    if any(not name or len(name) > 60 for name in names) or len(set(names)) != len(names):
        raise ValueError("每个列名须填写且不能重复，最长 60 个字符。")
    if any(role not in allowed for role in roles) or len(set(roles)) != len(roles):
        raise ValueError("每个字段只能对应一列。额外列可设为忽略。")
    return normalized


def validate_phone_template(columns) -> tuple[tuple[str, str], ...]:
    normalized = _validate_template(columns, HEADERS)
    roles = {role for _, role in normalized}
    if not roles & {"学号", "姓名"} or not roles & set(HEADERS[3:]):
        raise ValueError("模板至少需要学号或姓名，以及一个电话字段。")
    return normalized


def validate_roster_template(columns) -> tuple[tuple[str, str], ...]:
    normalized = _validate_template(columns, ROSTER_HEADERS)
    if not {"学号", "姓名"} <= {role for _, role in normalized}:
        raise ValueError("新建名册需要学号和姓名。班级和性别可按需添加。")
    return normalized


def import_template(store, username: str, mode: str) -> tuple[tuple[str, str], ...]:
    if mode == "roster":
        filename, default, validate = ROSTER_TEMPLATE_FILE, DEFAULT_ROSTER_TEMPLATE, validate_roster_template
    else:
        filename, default, validate = PHONE_TEMPLATE_FILE, DEFAULT_PHONE_TEMPLATE, validate_phone_template
    data = load_user_json(store, username, filename)
    try:
        return validate(data.get("columns", [])) if isinstance(data, dict) else default
    except (ValueError, TypeError):
        return default


def save_import_template(store, username: str, columns, mode: str) -> None:
    if mode == "roster":
        filename, validate = ROSTER_TEMPLATE_FILE, validate_roster_template
    else:
        filename, validate = PHONE_TEMPLATE_FILE, validate_phone_template
    save_user_json(store, username, filename, {"columns": validate(columns)})


def phone_template(store, username: str) -> tuple[tuple[str, str], ...]:
    return import_template(store, username, "phone")


def save_phone_template(store, username: str, columns) -> None:
    save_import_template(store, username, columns, "phone")


def write_template(destination: Path, *, supplement: bool = False, columns=None) -> None:
    import openpyxl

    validate, default = ((validate_phone_template, DEFAULT_PHONE_TEMPLATE) if supplement
                         else (validate_roster_template, DEFAULT_ROSTER_TEMPLATE))
    fields = default if columns is None else validate(columns)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "学生电话" if supplement else "学生名册"
    sheet.append([name for name, _ in fields])
    for column, cell in enumerate(sheet[1], 1):
        cell.data_type = "s"
        sheet.cell(2, column).number_format = "@"
        dimensions = sheet.column_dimensions[openpyxl.utils.get_column_letter(column)]
        dimensions.width = 22
        dimensions.number_format = "@"
    sheet.freeze_panes = "A2"
    guide = book.create_sheet("说明")
    guide.column_dimensions["A"].width = 88
    guide.column_dimensions["B"].width = 22
    if supplement:
        guide.append(("学号或姓名至少填写一项。两项均有时须对应同一学生。",))
        guide.append(("同名时填写学号，或填写能区分同名学生的班级。",))
        guide.append(("电话列按文本填写。留空表示保留原号码。",))
    else:
        guide.append(("学号和姓名必填。没有班级列时，可在导入界面指定班级。",))
        guide.append(("性别可填写男、女、其他、未说明，也可留空。",))
    guide.append(("列名", "对应字段"))
    for name, role in fields:
        guide.append((name, role or "忽略"))
        guide.cell(guide.max_row, 1).data_type = "s"
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


def _photo_map(directory: Path | None, roster: list[StudentInput], conflicts: list, issues: list) -> dict[str, Path]:
    if directory is None:
        return {}
    ids = {row.sid for row in roster}
    combined = defaultdict(set)
    names = defaultdict(set)
    for row in roster:
        names[row.name].add(row.sid)
        for separator in ("", "_", "-", " ", "－", "（", "("):
            end = {"（": "）", "(": ")"}.get(separator, "")
            combined[f"{row.sid}{separator}{row.name}{end}"].add(row.sid)
            combined[f"{row.name}{separator}{row.sid}{end}"].add(row.sid)
    found = defaultdict(list)
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in PHOTO_SUFFIXES:
            continue
        stem = path.stem.strip()
        # 明确的联合匹配优先于名字与旧版照片后缀。
        if stem in ids:
            candidates, priority = {stem}, 0
        elif stem in combined:
            candidates, priority = combined[stem], 1
        elif stem in names:
            candidates, priority = names[stem], 2
        else:
            candidates = {stem[:i] for i, char in enumerate(stem)
                          if char in "_-" and stem[:i] in ids
                          and re.fullmatch(r"(?:照(?:片)?|证件照|photo|[0-9]+)", stem[i + 1:], re.I)}
            priority = 3
        if len(candidates) != 1:
            reason = "同名或存在多个匹配，请在文件名加入学号" if candidates else "未匹配，请核对学号、姓名或班级"
            conflicts.append(PhotoConflict(path, tuple(sorted(candidates)), reason))
            continue
        found[next(iter(candidates))].append((priority, path))
    resolved = {}
    for sid, paths in found.items():
        priority = min(rank for rank, _ in paths)
        choices = [path for rank, path in paths if rank == priority]
        if len(choices) == 1:
            resolved[sid] = choices[0]
            if len(paths) > 1:
                issues.append((0, sid, "照片", f"已匹配 {choices[0].name}，忽略其他命名优先级较低的照片。"))
        else:
            conflicts.extend(PhotoConflict(path, (sid,), "存在多张同等优先的照片") for path in choices)
            for rank, path in paths:
                if rank != priority:
                    conflicts.append(PhotoConflict(path, (sid,), "还有更明确的同学号照片"))
    return resolved


@contextmanager
def _workbook(source: Path | bytes):
    import openpyxl

    if isinstance(source, Path):
        if source.stat().st_size > 20_000_000:
            raise ValueError("学生资料工作簿超过 20 MB，请分批导入。")
        raw = source.read_bytes()
    else:
        raw = source
    with ZipFile(io.BytesIO(raw)) as archive:
        if sum(part.file_size for part in archive.infolist()) > 128_000_000:
            raise ValueError("工作簿解压后过大，请分批导入。")
        if "xl/styles.xml" in archive.namelist():
            styles = archive.read("xl/styles.xml")
            repaired = re.sub(rb"<fill\s*/>|<fill>\s*</fill>", b"<fill><patternFill/></fill>", styles)
            if repaired != styles:
                buffer = io.BytesIO()
                with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as fixed:
                    for part in archive.namelist():
                        fixed.writestr(part, repaired if part == "xl/styles.xml" else archive.read(part))
                payload = buffer.getvalue()
            else:
                payload = raw
        else:
            payload = raw
    try:
        book = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=False)
    except (KeyError, TypeError, ValueError, OSError, BadZipFile) as error:
        raise ValueError("无法读取 Excel。请检查工作簿格式，或另存为标准 .xlsx 后重试。") from error
    try:
        yield book.worksheets, hashlib.sha256(raw).hexdigest()
    finally:
        book.close()


def _header(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")").casefold()


def _columns(headers, aliases, template=()) -> dict[str, int]:
    exact = {_header(name): role for name, role in template}
    output = {}
    for index, header in enumerate(headers):
        role = exact.get(header)
        if role:
            if role in output:
                raise ValueError(f"表中包含多列「{role}」，请保留一列。")
            output[role] = index
    for names in aliases:
        role = names[0]
        if role in output:
            continue
        accepted = {_header(name) for name in names}
        matches = [index for index, header in enumerate(headers) if header not in exact and header in accepted]
        if len(matches) > 1:
            raise ValueError(f"表中包含多列「{role}」，请保留一列。")
        if matches:
            output[role] = matches[0]
    return output


def _class_in_label(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{4,12})\s*班", value)
    return match[1] if match else ""


def _sheets(sheets, aliases, template, *, phone: bool):
    scanned = 0
    for sheet in sheets:
        values = enumerate(sheet.iter_rows(max_col=40), 1)
        first = []
        columns = {}
        start = 0
        column_error = ""
        for line, cells in values:
            headers = [_header(cell.value) for cell in cells]
            try:
                found = _columns(headers, aliases, template)
            except ValueError as error:
                if any(name in headers for name in ("学号", "姓名")):
                    column_error = str(error)
                    break
                found = {}
            identity = found.keys() & {"学号", "姓名"}
            ready = identity and found.keys() & set(HEADERS[3:]) if phone else len(identity) == 2
            if ready:
                columns, start = found, line
                break
            if identity and len(found) > len(columns):
                columns = found
            if line >= 16:
                break
            if cells and cells[0].value:
                first.append(str(cells[0].value))
        if column_error:
            yield sheet.title, {}, "", [], column_error
            continue
        if not start:
            if columns:
                missing = {"学号", "姓名"} - columns.keys()
                message = ("需要学号或姓名，以及至少一个电话列。" if phone else
                           f"缺少{'和'.join(sorted(missing))}列。请核对表头或设置模板。")
                yield sheet.title, {}, "", [], message
            continue
        class_hint = next((group for title in (*first, sheet.title) if (group := _class_in_label(title))), "")
        records = []
        for line, cells in values:
            if any(cell.value is not None for cell in cells):
                scanned += 1
                if scanned > 10_000:
                    raise ValueError("每个工作簿最多导入 10000 条，超过时请分批导入。")
                records.append((line, cells))
        yield sheet.title, columns, class_hint, records, ""


def _fix_reversed_identity(columns, records):
    if not {"学号", "姓名"} <= columns.keys():
        return False
    sid, name = columns["学号"], columns["姓名"]
    pairs = [(str(cells[sid].value or "").strip(), str(cells[name].value or "").strip())
             for _, cells in records if cells[sid].value is not None and cells[name].value is not None]
    if len(pairs) < 3 or not all(re.fullmatch(r"[\u3400-\u9fff·・\s]{2,40}", left)
                                  and re.fullmatch(r"[0-9]{5,20}", right) for left, right in pairs):
        return False
    columns["学号"], columns["姓名"] = name, sid
    return True


def _add_issues(target, entries, sheet_name, total):
    target.extend((line, sid, kind, f"工作表「{sheet_name}」· {detail}" if total > 1 else detail)
                  for line, sid, kind, detail in entries)


def _cell(cells, columns, header):
    return cells[columns[header]] if header in columns else None


def _check_phone(cells, columns, header: str) -> str:
    cell = _cell(cells, columns, header)
    try:
        number = _read_text(cell) if cell is not None else ""
    except ValueError as error:
        raise ValueError(f"{header}：{error}") from error
    if number and not PHONE.fullmatch(number):
        raise ValueError(f"{header}格式不正确。")
    return number


def plan_import(path: Path, directory: Path | None = None, *, columns=None, default_class: str = "",
                include_contacts: bool = True) -> ImportPlan:
    candidates: list[StudentInput] = []
    issues: list[tuple[int, str, str, str]] = []
    locations = {}
    seen_sheet = False
    missing_class = 0
    default_class = default_class.strip()
    aliases = ROSTER_ALIASES + (CONTACT_HEADERS if include_contacts else ())
    template = validate_roster_template(columns) if columns is not None else ()
    with _workbook(path) as (sheets, digest):
        for title, fields, class_hint, records, error in _sheets(sheets, aliases, template, phone=False):
            if error:
                _add_issues(issues, [(0, "", "名册", error)], title, len(sheets))
                continue
            seen_sheet = True
            sheet_issues = []
            if _fix_reversed_identity(fields, records):
                sheet_issues.append((0, "", "列校正", "学号与姓名列的内容和表头相反，已按实际内容识别。"))
            for number, cells in records:
                sid = ""
                try:
                    sid = _read_text(cells[fields["学号"]], identifier=True)
                    name = _read_text(cells[fields["姓名"]])
                    class_cell = _cell(cells, fields, "班级")
                    class_name = (_read_text(class_cell, identifier=True) if class_cell is not None else "") or class_hint or default_class
                    gender_cell = _cell(cells, fields, "性别")
                    gender = normalize_gender(_read_text(gender_cell)) if gender_cell is not None else ""
                    if not VALID_ID.fullmatch(sid) or not name or max(len(name), len(class_name)) > 80:
                        raise ValueError("学号或姓名为空或格式不正确。")
                    if gender and gender not in GENDER_OPTIONS:
                        sheet_issues.append((number, sid, "性别", "性别未识别。此字段留空，仍可导入该生。"))
                        gender = ""
                    if not class_name:
                        missing_class += 1
                        continue
                    phones = tuple(_check_phone(cells, fields, header) for header in HEADERS[3:])
                except ValueError as error:
                    sheet_issues.append((number, sid, "拒绝", str(error)))
                    continue
                if isinstance(cells[fields["学号"]].value, int) and len(sheets) == 1:
                    sheet_issues.append((number, sid, "学号", "原单元格为数字，请核对前导零。"))
                student = StudentInput(number, sid, name, class_name, phones, None, gender)
                locations[id(student)] = title
                candidates.append(student)
            _add_issues(issues, sheet_issues, title, len(sheets))
        sheet_count = len(sheets)
    if not seen_sheet:
        raise ValueError(issues[0][3] if issues else "未找到学号和姓名列。请核对表头或设置名册模板。")
    if missing_class:
        raise MissingClassError(f"有 {missing_class} 行缺少班级。请填写「默认班级」，再预览。"
                                "跨班名册请补齐班级列，或按班拆分。")
    counts = Counter(item.sid for item in candidates)
    available = []
    for item in candidates:
        if counts[item.sid] == 1:
            available.append(item)
        else:
            _add_issues(issues, [(item.row, item.sid, "拒绝", "学号在工作簿内重复。")],
                        locations[id(item)], sheet_count)
    conflicts = []
    photos = _photo_map(directory, available, conflicts, issues)
    return _include_photo_review(
        [replace(item, photo=photos.get(item.sid)) for item in available], issues, path.name, digest,
        None, conflicts, available,
    )


def _identity(row: dict) -> tuple[str, str, str]:
    return row.get("name", ""), row.get("class_name", ""), row.get("created_at", "")


def _same_class(left: str, right: str) -> bool:
    return left.strip().removesuffix("班").strip() == right.strip().removesuffix("班").strip()


def _contact_rows(source: Path | bytes, roster: dict[str, StudentInput], issues: list, columns_spec=None):
    names = defaultdict(list)
    for item in roster.values():
        names[item.name].append(item)
    accepted = {}
    duplicates = set()
    labels = {}
    seen_sheet = False
    template = validate_phone_template(columns_spec) if columns_spec is not None else ()
    with _workbook(source) as (sheets, digest):
        for title, columns, _class_hint, records, error in _sheets(sheets, IDENTITY_HEADERS + CONTACT_HEADERS, template, phone=True):
            if error:
                _add_issues(issues, [(0, "", "电话", error)], title, len(sheets))
                continue
            seen_sheet = True
            sheet_issues = []
            if _fix_reversed_identity(columns, records):
                sheet_issues.append((0, "", "列校正", "学号与姓名列内容和表头相反，已按实际内容识别。"))
            for number, cells in records:
                sid = ""
                try:
                    sid_cell = _cell(cells, columns, "学号")
                    sid = _read_text(sid_cell, identifier=True) if sid_cell is not None else ""
                    name_cell, class_cell = (_cell(cells, columns, name) for name in ("姓名", "班级"))
                    name = _read_text(name_cell) if name_cell is not None else ""
                    group = _read_text(class_cell, identifier=True) if class_cell is not None else ""
                    if sid:
                        student = roster.get(sid)
                        if student is None:
                            raise ValueError("学号未找到，请先导入名册或核对前导零。")
                        if (name and student.name != name) or (group and not _same_class(student.class_name, group)):
                            raise ValueError("学号与姓名或班级不一致。")
                    else:
                        group = group or _class_hint
                        matches = [item for item in names[name] if not group or _same_class(item.class_name, group)]
                        if not matches:
                            raise ValueError("姓名未找到，请核对班级或填写学号。")
                        if len(matches) != 1:
                            raise ValueError("存在同名学生，请补充学号或能区分的班级。")
                        student = matches[0]
                        sid = student.sid
                    phones = []
                    for header in HEADERS[3:]:
                        try:
                            phones.append(_check_phone(cells, columns, header))
                        except ValueError as error:
                            sheet_issues.append((number, sid, "电话", str(error)))
                            phones.append("")
                    if not any(phones) and not any(_cell(cells, columns, h) is not None and
                                                   _cell(cells, columns, h).value for h in HEADERS[3:]):
                        sheet_issues.append((number, sid, "电话", "未填电话号码。"))
                except ValueError as error:
                    sheet_issues.append((number, sid, "电话", str(error)))
                    continue
                if not any(phones):
                    continue
                if sid in accepted:
                    duplicates.add(sid)
                    sheet_issues.append((number, sid, "电话", "同一学生有多行电话，合并为一行后重试。"))
                else:
                    accepted[sid] = replace(student, row=number, phones=tuple(phones))
                    labels[sid] = title
            _add_issues(issues, sheet_issues, title, len(sheets))
        for sid in sorted(duplicates):
            original = accepted.pop(sid)
            _add_issues(issues, [(original.row, sid, "电话", "同一学生有多行电话，合并为一行后重试。")],
                        labels[sid], len(sheets))
    if not seen_sheet:
        raise ValueError(issues[0][3] if issues else "未找到学号或姓名，以及电话列。请核对表头或设置模板。")
    return accepted, digest


def grid_workbook(columns, rows) -> bytes:
    """用内存里的表格生成解析输入，保存操作仍统一走电话导入。"""
    from openpyxl import Workbook

    columns = validate_phone_template(columns)
    records = [row for row in rows if any(row)]
    if len(records) > 10_000:
        raise ValueError("每批最多录入 10000 人。")
    book = Workbook()
    sheet = book.active
    for row in ([name for name, _ in columns], *records):
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _include_photo_review(students, issues, filename, digest, snapshot, conflicts, available):
    issues.extend((0, "", "待核对照片", f"{item.reason}：{item.path.name}") for item in conflicts)
    return ImportPlan(students, issues, filename, digest, snapshot, tuple(conflicts), tuple(available))


def resolve_photos(plan: ImportPlan, decisions: dict[Path, str]) -> ImportPlan:
    """保留已匹配照片。人工确认只决定未定照片的学号。"""
    conflicts = {item.path: item for item in plan.photo_conflicts}
    roster = {item.sid: item for item in plan.photo_students}
    selected = {item.sid: item for item in plan.students}
    issues = [issue for issue in plan.issues if issue[2] != "待核对照片"]
    for path, sid in decisions.items():
        if path not in conflicts or sid not in roster:
            raise ValueError("待核对照片或学生不存在，请重新预览。")
        item = selected.get(sid, roster[sid])
        if item.photo is not None:
            raise ValueError("同一学生只能选择一张照片。请取消重复选择后重试。")
        selected[sid] = replace(item, photo=path)
    issues.extend((0, "", "跳过照片", f"{item.reason}：{item.path.name}")
                  for item in plan.photo_conflicts if item.path not in decisions)
    return replace(plan, students=list(selected.values()), issues=issues, photo_conflicts=())


def plan_supplement(store, username: str, path: Path | None, directory: Path | None, *,
                    photo_class: str = "", columns=None, grid: bytes | None = None) -> ImportPlan:
    """独立补录照片、电话或两者，只匹配当前账户的在册学生。"""
    if path is not None and grid is not None:
        raise ValueError("请选择一种电话来源。")
    source = path if path is not None else grid
    if source is None and directory is None:
        raise ValueError("请提供电话表或照片文件夹。")
    with ROSTER_WRITE_LOCK:
        entries = StudentStore(store=store, username=username).rows(include_archived=False)
    snapshot = {sid: _identity(row) for row in entries if (sid := _bare_student_id(row["student_id"]))}
    if not snapshot:
        raise ValueError("当前账户没有在册学生。请先在成长档案导入或新建学生。")
    roster = {sid: StudentInput(0, sid, name, class_name, ("",) * len(CONTACT_FIELDS), None)
              for sid, (name, class_name, _) in snapshot.items()}
    issues = []
    selected, digest = {}, ""
    if source is not None:
        try:
            selected, digest = _contact_rows(source, roster, issues, columns)
        except (ValueError, OSError, BadZipFile) as error:
            if directory is None:
                raise
            message = str(error) if isinstance(error, ValueError) else "电话表不可读，请检查文件。"
            issues.append((0, "", "电话", message))
    eligible = [row for row in roster.values() if not photo_class or row.class_name == photo_class]
    conflicts = []
    photos = _photo_map(directory, eligible, conflicts, issues)
    students = [replace(selected.get(sid, roster[sid]), photo=photos.get(sid))
                for sid in sorted(photos.keys() | selected.keys())]
    filename = path.name if path is not None else ("应用内录入" if grid is not None else directory.name)
    return _include_photo_review(students, issues, filename, digest, snapshot, conflicts, eligible)


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
                 replace_existing: bool = False, progress=lambda done, total: None) -> tuple[int, list]:
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
        supplement = plan.matched_roster is not None
        if not supplement:
            imported_ids = {_student_id_of(item.sid) for item in plan.students}
            issues.extend((0, _bare_student_id(sid) or sid, "未出现", "保留原学生。") for sid in rows.keys() - imported_ids)
        for item in plan.students:
            key = _student_id_of(item.sid)
            previous = rows.get(key, {})
            if supplement:
                if (not previous or previous.get("archived")
                        or _identity(previous) != plan.matched_roster.get(item.sid)):
                    issues.append((item.row, item.sid, "拒绝", "预览后学生身份或班级发生变化，请重新预览。"))
                    continue
            elif not replace_identity and any(previous.get(field) and previous[field] != provided
                                              for field, provided in (("name", item.name), ("class_name", item.class_name),
                                                                      ("gender", item.gender)) if provided):
                issues.append((item.row, item.sid, "拒绝", "已有姓名、班级或性别不同，需确认覆盖。"))
                continue
            data = vault.student(item.sid)
            contacts = data.setdefault("contacts", {})
            changed = False
            for field, header, number in zip(CONTACT_FIELDS, HEADERS[3:], item.phones):
                if not number:
                    continue
                if supplement and field in contacts and not replace_existing:
                    issues.append((item.row, item.sid, "已保留", f"已有{header}。"))
                    continue
                contacts[field] = {"hint": mask_phone(number), "cipher": vault.seal_phone(item.sid, field, number)}
                changed = True
            photo = photos.get(item.sid)
            if photo:
                if supplement and data.get("photo") and not replace_existing:
                    issues.append((item.row, item.sid, "已保留", "已有照片。"))
                elif photo != data.get("photo"):
                    data["photo"] = photo
                    data.pop("face_vector", None)
                    changed = True
            if supplement and not changed:
                issues.append((item.row, item.sid, "跳过", "本次没有可写入的补录项。"))
                continue
            if not supplement:
                if not data.get("photo"):
                    issues.append((item.row, item.sid, "待补", "缺照片"))
                for field, header in zip(CONTACT_FIELDS[:3], HEADERS[3:6]):
                    if not contacts.get(field):
                        issues.append((item.row, item.sid, "待补", f"缺{header}"))
                rows[key] = {
                    "note": "", "archived": False, "created_at": captured_at, "source": "roster_import",
                    **previous, "student_id": key, "name": item.name, "class_name": item.class_name,
                    "updated_at": captured_at,
                    "gender": item.gender or previous.get("gender", ""),
                }
            filename, blob = vault.encoded_student(item.sid, data)
            entries[filename] = blob
        count = len(entries)
        if count:
            batches = load_user_json(store, username, BATCHES, {}, strict=True).get("batches", [])
            batches.append({"at": captured_at, "file": plan.filename, "sha256": plan.digest,
                            "kind": "supplement" if supplement else "roster",
                            "count": count, "issues": len(issues), "actor": username})
            cipher = app_fernet(store.root)
            entries[BATCHES] = cipher.encrypt(json.dumps({"batches": batches[-100:]}, ensure_ascii=False).encode("utf-8"))
            if not supplement:
                payload = {"table": roster.table, "version": 1, "rows": rows, "updated": captured_at}
                entries[roster.filename] = cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            commit_import(store, username, entries)
    return count, issues
