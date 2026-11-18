"""教务系统原始成绩表的导入适配。"""

import re
from dataclasses import dataclass, replace

import pandas as pd

from core.calculator import dedupe_courses
from core.parser import ParseResult, find_header_rows, normalize_header, parse_dataframe
from core.validator import PASS_SCORE, AnchorReport, check_credit_anchor, parse_and_validate


_EXPORT_COURSE = re.compile(
    r"(?P<name>.+)[,，]\s*(?P<category>必修|限选|选修|任选)\s*[,，]"
    r"\s*学分\s*(?P<credit>\d+(?:\.\d+)?)\s*[,，]"
    r"\s*学时\s*\d+(?:\.\d+)?\s*[,，]\s*(?P<year>\d{4}-\d{4})-(?P<half>[12])"
)
_CARD_TITLE = re.compile(
    r"(?P<year>\d{4}-\d{4})(?:-(?P<half>[12])\s*学期|\s*学年)\s*"
    r"(?P<class_name>[A-Za-z0-9][A-Za-z0-9-]*)班(?:班级)?成绩卡"
)


@dataclass(frozen=True)
class GradeCardScope:
    class_name: str = ""
    school_year: str = ""
    half: str = ""


def grade_card_scope(raw: pd.DataFrame, parsed: ParseResult) -> GradeCardScope:
    """从教务成绩卡标题及课程列核实班级、学年和覆盖范围。"""
    if not parsed.header_rows:
        return GradeCardScope()
    header = parsed.header_rows[0]
    title = next(
        (found for cell in raw.iloc[:header].to_numpy().flat
         if (found := _CARD_TITLE.search(normalize_header(cell))) is not None),
        None,
    )
    courses = [match for cell in raw.iloc[header]
               if (match := _EXPORT_COURSE.fullmatch(normalize_header(cell))) is not None]
    years = {match["year"] for match in courses}
    halves = {match["half"] for match in courses}
    if title is not None:
        years.add(title["year"])
        if title["half"]:
            halves.add(title["half"])
            if len(halves) > 1:
                raise ValueError("成绩卡标题与课程列标注的学期不一致。")
    if len(years) > 1:
        raise ValueError("同一份成绩表包含多个学年，请按学年分别导入。")
    half = {"1": "上", "2": "下"}.get(next(iter(halves)), "") if len(halves) == 1 else ""
    return GradeCardScope(
        title["class_name"] if title is not None else "",
        next(iter(years), ""),
        half,
    )


def parse_grade_frame(raw: pd.DataFrame) -> tuple[ParseResult, AnchorReport]:
    """保留原始课程名和重复列，并排除课程门数统计列。"""
    if raw.empty:
        return parse_and_validate(raw)
    header_rows, *_ = find_header_rows(raw)
    if not header_rows:
        return parse_and_validate(raw)
    header = header_rows[0]
    kept = [
        col for col in range(raw.shape[1])
        if not any("".join(normalize_header(raw.iat[row, col]).split()) == "课程门数"
                   for row in header_rows)
    ]
    frame = raw.iloc[:, kept].copy()
    names: dict[int, str] = {}
    for col in range(frame.shape[1]):
        match = _EXPORT_COURSE.fullmatch(normalize_header(frame.iat[header, col]))
        if match is not None:
            names[col] = match["name"].strip()
            # 让原解析器先读齐各列分数，后续计算器才能按学生选取同名课成绩。
            frame.iat[header, col] = f"教务课程列{col} ({match['credit']}学分) {match['category']}"

    if not names and len(kept) == raw.shape[1]:
        return parse_and_validate(raw)
    parsed = parse_dataframe(frame)
    if len(kept) != raw.shape[1]:
        parsed.warnings.append("已忽略课程门数统计列。")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for course in parsed.courses:
        course.name = names.get(course.col_index, course.name)
        if course.name in seen:
            duplicates.add(course.name)
        seen.add(course.name)
    parsed.warnings.extend(
        f"课程「{name}」有多列成绩，按同名取高规则计算，请核对类别与学分。"
        for name in sorted(duplicates)
    )

    anchor_rows = []
    for row in parsed.rows:
        pairs = list(zip(parsed.courses, row.score_list))
        if not duplicates:
            row.scores = {course.name: score for course, score in pairs}
            anchor_rows.append(row)
            continue
        winners = dedupe_courses(pairs).course_scores
        row.scores = {course.name: course.score for course in winners}
        winning_cols = {course.col_index for course in winners}
        # 校验锚点也应只计入同名课程的获选成绩。
        anchor_rows.append(replace(row, score_list=[
            score if course.col_index in winning_cols else None for course, score in pairs
        ]))
    # 教务成绩卡的「所得学分」只统计通过课程。该阈值仅用于核对原表，
    # 计算加权成绩仍按软件的原有规则纳入挂科与缺考。
    scope = grade_card_scope(raw, parsed)
    pass_score = 60.0 if names and scope.class_name else PASS_SCORE
    return parsed, check_credit_anchor(frame, replace(parsed, rows=anchor_rows), pass_score=pass_score)
