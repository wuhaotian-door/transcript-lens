"""导出成绩结果（Spec 第 8 章第二批 · settings_page 的"导出（明文/脱敏两种 xlsx）"）。

两种导出**必须分开**：

* **明文版** —— 给辅导员自己留档，含姓名
* **脱敏版** —— 只含 ``S+序号``、分数、学分、意愿标签。**没有姓名、没有学号**
  （红线 6：传给任何外部接口的 payload 里禁止出现学生真名、真学号、身份证。

  严格讲红线 6 管的是"传 AI"，但导出文件同样可能被转手发出去，所以这里
  默认按同一标准做，并在文件里写明"此文件已脱敏"。）

本模块只写本地文件，**不联网**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

__all__ = [
    "ANONYMIZED_NOTICE",
    "PLAIN_NOTICE",
    "ExportResult",
    "anonymize_table",
    "export_anonymized_xlsx",
    "export_plain_xlsx",
    "to_dataframe",
]

PLAIN_NOTICE = "本文件含学生姓名，仅供辅导员本人留档，请勿外传。"
ANONYMIZED_NOTICE = "本文件已脱敏：不含姓名、不含学号，学生以 S 编号标识。"

#: 脱敏版允许出现的列（白名单）。多一列都不行 —— 宁可少导出，不可漏姓名。
ANONYMIZED_COLUMNS: tuple[str, ...] = (
    "S编号",
    "学期",
    "口径",
    "加权均分",
    "总学分",
    "挂科数",
    "缺考数",
    "意愿标签",
)


@dataclass
class ExportResult:
    """一次导出的结果。"""

    path: Path
    rows: int
    columns: list[str] = field(default_factory=list)
    anonymized: bool = False
    notice: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rows": self.rows,
            "columns": list(self.columns),
            "anonymized": self.anonymized,
            "notice": self.notice,
        }


def to_dataframe(records: Sequence[dict[str, Any]]) -> pd.DataFrame:
    """把导出记录转成 DataFrame；空列表时也保留列名。"""
    if not records:
        return pd.DataFrame(columns=list(ANONYMIZED_COLUMNS))
    return pd.DataFrame(list(records))


def anonymize_table(
    frame: pd.DataFrame,
    *,
    label_by_student: dict[str, str] | None = None,
) -> pd.DataFrame:
    """把明文表脱敏成只含白名单列的版本。

    Args:
        frame: 明文表（可能含 ``姓名``/``学号`` 等敏感列）。
        label_by_student: 姓名 → ``S编号`` 的映射；不传则按行序生成 ``S001`` 起。

    返回的 DataFrame **只有** :data:`ANONYMIZED_COLUMNS` 里的列，顺序也按它。
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(ANONYMIZED_COLUMNS))

    result = pd.DataFrame()
    name_column = "姓名" if "姓名" in frame.columns else None
    if name_column is not None:
        if label_by_student is None:
            labels = [f"S{index + 1:03d}" for index in range(len(frame))]
        else:
            labels = [
                label_by_student.get(str(value), f"S{index + 1:03d}")
                for index, value in enumerate(frame[name_column])
            ]
        result["S编号"] = labels

    for column in ANONYMIZED_COLUMNS:
        if column == "S编号" or column in result.columns:
            continue
        if column in frame.columns:
            result[column] = frame[column].to_numpy()

    # 补齐缺失列，保持列顺序稳定
    for column in ANONYMIZED_COLUMNS:
        if column not in result.columns:
            result[column] = ""
    return result[list(ANONYMIZED_COLUMNS)]


def export_plain_xlsx(
    records: Sequence[dict[str, Any]],
    path: str | Path,
) -> ExportResult:
    """导出明文版 xlsx（含姓名，仅供本人留档）。"""
    target = Path(path)
    frame = to_dataframe(records)
    target.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="成绩结果")
        _write_notice_sheet(writer, PLAIN_NOTICE)
    return ExportResult(
        path=target, rows=len(frame), columns=list(frame.columns),
        anonymized=False, notice=PLAIN_NOTICE,
    )


def export_anonymized_xlsx(
    records: Sequence[dict[str, Any]],
    path: str | Path,
) -> ExportResult:
    """导出脱敏版 xlsx：**不含姓名、不含学号**（红线 6 的同一标准）。"""
    target = Path(path)
    plain = to_dataframe(records)
    frame = anonymize_table(plain)
    target.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="脱敏结果")
        _write_notice_sheet(writer, ANONYMIZED_NOTICE)
    return ExportResult(
        path=target, rows=len(frame), columns=list(frame.columns),
        anonymized=True, notice=ANONYMIZED_NOTICE,
    )


def _write_notice_sheet(writer: pd.ExcelWriter, notice: str) -> None:
    """每个导出文件都附一张"说明"表，把"这是明文/已脱敏"写在文件里。"""
    info = pd.DataFrame(
        {
            "项目": ["文件性质", "生成工具", "数据来源"],
            "说明": [
                notice,
                "transcript-lens（本地离线运算，不经任何网络服务）",
                "由辅导员导入的成绩单，仅存在于本机",
            ],
        }
    )
    info.to_excel(writer, index=False, sheet_name="说明")
